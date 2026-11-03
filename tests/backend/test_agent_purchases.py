"""明确授权、未知结果回查及消费价格边界；只使用隔离MySQL与Redis。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update

from agent.schemas import ConversationState
from agent.tools.group_buys import GroupBuyTools, definitions
from backend.app.core.errors import ApiError
from backend.app.models import AgentConversation, AgentPurchase, Voucher, VoucherOrder, VoucherUsage
from backend.app.schemas.group_buys import Requirements
from backend.app.services import assistant
from tests.backend.test_group_buy_assistant import item
from tests.backend.test_redis_first_orders import available, capture_message, consume, prepare
from tests.backend.test_redis_first_orders import fast_context as fast_fixture
from tests.backend.test_voucher_orders import login
from tests.backend.test_voucher_orders import order_context as context_fixture

order_context = context_fixture
fast_context = fast_fixture
pytestmark = [pytest.mark.mysql, pytest.mark.redis]


async def proposal_for(app, user_id, voucher_id):
    conversation = await assistant.create(app.state.database, user_id)
    conversation.state = ConversationState(
        requirements=Requirements(
            people=2,
            budget_fen=10000,
            use_date=(datetime.now(UTC) + timedelta(days=1)).date(),
            use_time="18:00",
        )
    )
    tools = GroupBuyTools(
        app.state.database, conversation.state, AsyncMock(), user_message="买这张"
    )
    result = await tools.execute(
        "prepare_purchase", f'{{"voucher_id":{voucher_id},"evidence":"买这张"}}'
    )
    assert "proposal" in result
    assert (
        await tools.execute(
            "finish", f'{{"answer":"请核对后点击确认，尚未下单。","candidate_ids":[{voucher_id}]}}'
        )
    )["finished"]
    return await assistant.save(app.state.database, user_id, conversation)


@pytest_asyncio.fixture
async def purchase_context(fast_context):
    app, client, base, phones, headers = fast_context
    async with app.state.database.sessions() as session, session.begin():
        session.add(VoucherUsage(voucher_id=base, spec=item().usage.model_dump(mode="json")))
    await prepare(fast_context)
    conversation = await proposal_for(app, base, base)
    try:
        yield fast_context, conversation
    finally:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(
                delete(AgentPurchase).where(AgentPurchase.user_id.in_([base, base + 1]))
            )
            await session.execute(
                delete(AgentConversation).where(AgentConversation.user_id.in_([base, base + 1]))
            )
            await session.execute(delete(VoucherUsage).where(VoucherUsage.voucher_id == base))


def confirmation(conversation, **extra):
    return {
        "proposal_id": conversation.state.purchase_proposal.id,
        "expected_version": conversation.version,
        **extra,
    }


def path(conversation):
    return f"/api/assistant/conversations/{conversation.id}/purchases"


async def test_model_can_only_prepare_then_concurrent_confirm_submits_once(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    names = {row["function"]["name"] for row in definitions()}
    assert "prepare_purchase" in names and not names & {
        "confirm_purchase",
        "create_order",
        "submit",
    }
    assert await available(context) == 3
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentPurchase)) == 0
    submit = AsyncMock(wraps=app.state.order_ingress.submit)
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    responses = await asyncio.gather(
        *[
            client.post(
                path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
            )
            for _ in range(12)
        ]
    )
    assert all(row.status_code == 202 for row in responses), [row.text for row in responses]
    assert len({row.json()["request"]["id"] for row in responses}) == 1
    assert submit.await_count == 1 and await available(context) == 2
    payload = await capture_message(context)
    await consume(context, payload)
    await consume(context, payload)
    result = (await client.get(path(conversation))).json()[0]
    assert result["status"] == "succeeded" and result["request"]["order"]["pay_value"] == 5900
    repeated = await client.post(
        path(conversation) + "/confirm",
        headers=headers,
        json={**confirmation(conversation), "expected_version": 0},
    )
    assert repeated.json()["request"]["id"] == result["request"]["id"]
    assert submit.await_count == 1


async def test_acknowledged_redis_but_response_lost_recovers_original_without_retry(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, _, _, headers = context
    original = app.state.order_ingress.submit

    async def lost(*args):
        await original(*args)
        raise TimeoutError()

    submit = AsyncMock(side_effect=lost)
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    response = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    assert response.status_code == 202 and response.json()["status"] == "pending"
    second = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    assert second.json()["request"]["id"] == response.json()["request"]["id"]
    assert submit.await_count == 1 and await available(context) == 2


async def test_no_result_remains_uncertain_and_blocks_alternative_across_conversations(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    submit = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    for _ in range(2):
        response = await client.post(
            path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
        )
        assert response.status_code == 202 and response.json()["status"] == "submitting"
        assert response.json()["request"] is None
    replacement = await proposal_for(app, base, base)
    response = await client.post(
        path(replacement) + "/confirm", headers=headers, json=confirmation(replacement)
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "PURCHASE_PENDING"
    assert submit.await_count == 1 and await available(context) == 3


@pytest.mark.parametrize("change", ["price", "rules", "expired", "version"])
async def test_changed_terms_expiry_or_version_never_submit(purchase_context, monkeypatch, change):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    if change == "price":
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(update(Voucher).where(Voucher.id == base).values(pay_value=6100))
    elif change == "rules":
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(
                update(VoucherUsage).where(VoucherUsage.voucher_id == base).values(spec={})
            )
    elif change == "expired":
        conversation.state.purchase_proposal.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        conversation = await assistant.save(app.state.database, base, conversation)
    submit = AsyncMock()
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    body = confirmation(conversation)
    if change == "version":
        body["expected_version"] = 0
    response = await client.post(path(conversation) + "/confirm", headers=headers, json=body)
    assert response.status_code == 409, response.text
    submit.assert_not_awaited()
    assert await available(context) == 3


async def test_unknown_conditions_require_separate_user_acknowledgement(purchase_context):
    context, _ = purchase_context
    app, client, base, _, headers = context
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(VoucherUsage).where(VoucherUsage.voucher_id == base).values(spec={})
        )
    conversation = await proposal_for(app, base, base)
    response = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "PURCHASE_UNKNOWNS_REQUIRED"
    )
    response = await client.post(
        path(conversation) + "/confirm",
        headers=headers,
        json=confirmation(conversation, acknowledge_unknowns=True),
    )
    assert response.status_code == 202
    await consume(context, await capture_message(context))
    result = (await client.get(path(conversation))).json()[0]
    assert result["status"] == "succeeded"
    assert result["proposal"]["evaluation"]["verdict"] == "unknown"


async def test_related_category_quote_requires_ack_and_new_worker_contract_consumes_it(
    purchase_context,
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    tools = GroupBuyTools(
        app.state.database, conversation.state, AsyncMock(), user_message="想吃炒菜"
    )
    await tools.execute(
        "update_goal",
        json.dumps(
            {
                "changes": [
                    {
                        "field": "category",
                        "value": "炒菜",
                        "strength": "required",
                        "matching": "semantic",
                    },
                ]
            }
        ),
    )
    catalog = await tools.execute("discover_options", "{}")
    assert catalog["literal_catalog_match"] is False
    detail = await tools.read(base)
    result = await tools.execute(
        "search_group_buys", json.dumps({"category_matches": [detail.category]})
    )
    assert "error" not in result
    proposal = await tools.execute("prepare_purchase", json.dumps({"voucher_id": base}))
    assert any(
        i["code"] == "CATEGORY_RELATED_UNVERIFIED"
        for i in proposal["proposal"]["evaluation"]["issues"]
    )
    conversation = await assistant.save(app.state.database, base, conversation)
    rejected = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    assert (
        rejected.status_code == 409
        and rejected.json()["error"]["code"] == "PURCHASE_UNKNOWNS_REQUIRED"
    )
    assert await available(context) == 3
    accepted = await client.post(
        path(conversation) + "/confirm",
        headers=headers,
        json=confirmation(conversation, acknowledge_unknowns=True),
    )
    assert accepted.status_code == 202
    await consume(context, await capture_message(context))
    result = (await client.get(path(conversation))).json()[0]
    assert result["status"] == "succeeded"
    assert result["proposal"]["requirements"]["category"] == "炒菜"
    assert result["proposal"]["requirements"]["category_matches"] == [detail.category]


@pytest.mark.parametrize("change", ["price", "rules"])
async def test_consumer_rechecks_authorized_terms_and_original_compensation_restores_stock(
    purchase_context, change
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    assert (
        await client.post(
            path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
        )
    ).status_code == 202
    async with app.state.database.sessions() as session, session.begin():
        if change == "price":
            await session.execute(update(Voucher).where(Voucher.id == base).values(pay_value=6900))
        else:
            await session.execute(
                update(VoucherUsage).where(VoucherUsage.voucher_id == base).values(spec={})
            )
    await consume(context, await capture_message(context))
    result = (await client.get(path(conversation))).json()[0]
    assert result["status"] == "failed" and result["error_code"] == "PURCHASE_TERMS_CHANGED"
    assert await available(context) == 3
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(Voucher.stock).where(Voucher.id == base)) == 3
        assert (
            await session.scalar(
                select(func.count())
                .select_from(VoucherOrder)
                .where(VoucherOrder.voucher_id == base)
            )
            == 0
        )


async def test_explicit_failure_requires_new_confirmation_and_get_never_resubmits(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    submit = AsyncMock(side_effect=ApiError(409, "OUT_OF_STOCK", "已售罄"))
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    first = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    assert first.json()["status"] == "failed"
    await client.get(path(conversation))
    await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    assert submit.await_count == 1
    replacement = await proposal_for(app, base, base)
    assert replacement.state.purchase_proposal.id != conversation.state.purchase_proposal.id
    assert (
        await client.post(
            path(replacement) + "/confirm", headers=headers, json=confirmation(replacement)
        )
    ).json()["status"] == "failed"
    assert submit.await_count == 2


async def test_missing_ingress_is_explained_and_is_not_silently_rebuilt(purchase_context):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    await app.state.cache_redis.delete(app.state.seckill_store.key(base))
    response = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    result = response.json()
    assert result["status"] == "failed" and result["error_code"] == "SECKILL_NOT_READY"
    assert "未创建订单" in result["message"] and "重新" in result["message"]
    assert not await app.state.cache_redis.exists(app.state.seckill_store.key(base))
    repeated = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    assert repeated.json()["id"] == result["id"] and repeated.json()["status"] == "failed"


async def test_latest_purchase_result_is_supplied_after_historical_quote():
    from agent.schemas import ChatMessage
    from agent.workflows.selection import run_selection
    from backend.app.core.config import Settings
    from tests.backend.test_group_buy_assistant import ScriptedModel, call

    state = ConversationState(messages=[ChatMessage(role="assistant", content="尚未下单，请确认")])
    model = ScriptedModel([call("finish", {"answer": "订单已创建，编号123。"})])
    reader = AsyncMock(return_value=[{"status": "succeeded", "order_id": "123"}])
    await run_selection(
        model, None, Settings(_env_file=None), state, "订单怎么样了", AsyncMock(), reader
    )
    messages = model.inputs[0]
    assert messages[-1]["role"] == "system" and '"order_id": "123"' in messages[-1]["content"]
    assert "尚未下单" in messages[-3]["content"]
    reader.assert_awaited_once()


async def test_authentication_csrf_ownership_and_client_amount_tampering_rejected(purchase_context):
    context, conversation = purchase_context
    app, client, _, phones, headers = context
    assert (
        await client.post(path(conversation) + "/confirm", json=confirmation(conversation))
    ).status_code == 403
    tampered = await client.post(
        path(conversation) + "/confirm",
        headers=headers,
        json={**confirmation(conversation), "pay_value": 1, "quantity": 2},
    )
    assert tampered.status_code == 422
    other_headers = await login(client, app, phones[1])
    assert (await client.get(path(conversation))).status_code == 404
    assert (
        await client.post(
            path(conversation) + "/confirm", headers=other_headers, json=confirmation(conversation)
        )
    ).status_code == 404
    assert await available(context) == 3


async def test_order_cancel_is_displayed_as_history_without_repurchase(purchase_context):
    context, conversation = purchase_context
    _, client, _, _, headers = context
    await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    await consume(context, await capture_message(context))
    result = (await client.get(path(conversation))).json()[0]
    await client.post(f"/api/orders/{result['request']['order']['id']}/cancel", headers=headers)
    result = (await client.get(path(conversation))).json()[0]
    assert result["status"] == "succeeded" and result["request"]["order"]["status"] == "cancelled"
    assert await available(context) == 3


async def test_authorization_records_prevent_destructive_migration_downgrade(purchase_context):
    from alembic import command
    from alembic.config import Config

    from backend.app.core.schema_check import compare_schema
    from scripts.run_backend_tests import ROOT

    context, conversation = purchase_context
    app, client, _, _, headers = context
    await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )

    def downgrade(sync):
        config = Config(str(ROOT / "backend/alembic.ini"))
        config.attributes["connection"] = sync
        command.downgrade(config, "0006")

    with pytest.raises(RuntimeError, match="不能无损回退"):
        async with app.state.database.engine.begin() as connection:
            await connection.run_sync(downgrade)
    async with app.state.database.engine.connect() as connection:
        assert await connection.run_sync(compare_schema) == []
