"""账号找回、SQL事实优先和未知提交诊断；只使用随机隔离库与Redis前缀。"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, update

from backend.app.core.errors import ApiError
from backend.app.models import AgentPurchase, VoucherOrder
from backend.app.services import assistant
from backend.app.services.order_processing import process_order
from backend.app.services.redis_orders import materialize
from tests.backend.test_agent_purchases import confirmation, path, proposal_for
from tests.backend.test_agent_purchases import fast_context as fast_fixture
from tests.backend.test_agent_purchases import order_context as order_fixture
from tests.backend.test_agent_purchases import purchase_context as purchase_fixture
from tests.backend.test_group_buy_assistant import ScriptedModel, call
from tests.backend.test_redis_first_orders import available, capture_message
from tests.backend.test_voucher_orders import login

order_context = order_fixture
fast_context = fast_fixture
purchase_context = purchase_fixture
pytestmark = [pytest.mark.mysql, pytest.mark.redis]


async def submit_and_finish_without_projection(context, conversation):
    app, client, base, _, headers = context
    response = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    assert response.status_code == 202
    payload = await capture_message(context)
    await materialize(app.state.database, payload.purchase)
    assert (
        await process_order(app.state.database, int(payload.request_id), require_reservation=True)
        == "completed"
    )
    # 故意不执行Worker后续结果投影，保留真实Redis旧pending及未刷新授权。
    assert (
        await app.state.order_ingress.result(base, payload.request_id)
    ).request.status == "pending"
    return payload


async def add_history(context, conversation, status="failed", age=120):
    app, _, base, _, _ = context
    identifier = str(uuid4())
    proposal = conversation.state.purchase_proposal.model_dump(mode="json")
    proposal["id"] = identifier
    async with app.state.database.sessions() as session, session.begin():
        session.add(
            AgentPurchase(
                id=identifier,
                user_id=base,
                conversation_id=conversation.id,
                voucher_id=base,
                status=status,
                error_code="OUT_OF_STOCK" if status == "failed" else None,
                snapshot={"proposal": proposal, "acknowledge_unknowns": True},
                created_at=(datetime.now(UTC) - timedelta(seconds=age)).replace(tzinfo=None),
            )
        )
    return identifier


async def test_sql_terminal_beats_stale_redis_and_account_lookup_persists_recovery(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, base, _, _ = context
    submit = AsyncMock(wraps=app.state.order_ingress.submit)
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    payload = await submit_and_finish_without_projection(context, conversation)
    assert (
        await app.state.order_ingress.result(base, payload.request_id)
    ).request.status == "pending"
    before = await app.state.cache_redis.hgetall(app.state.seckill_store.key(base))
    response = await client.get("/api/assistant/purchases")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    result = response.json()
    receipt = result["items"][0]
    assert receipt["conversation_id"] == conversation.id and receipt["lookup_source"] == "mysql"
    assert (
        receipt["status"] == "succeeded" and receipt["request"]["order"]["id"] == payload.request_id
    )
    assert result["unresolved_count"] == 0
    assert before == await app.state.cache_redis.hgetall(app.state.seckill_store.key(base))
    assert submit.await_count == 1 and await available(context) == 2
    async with app.state.database.sessions() as session:
        assert (
            await session.get(AgentPurchase, conversation.state.purchase_proposal.id)
        ).status == "succeeded"


async def test_new_confirmation_reconciles_other_conversation_before_pending_guard(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    await submit_and_finish_without_projection(context, conversation)
    replacement = await proposal_for(app, base, base)
    submit = AsyncMock(side_effect=ApiError(409, "OUT_OF_STOCK", "库存不足"))
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    response = await client.post(
        path(replacement) + "/confirm", headers=headers, json=confirmation(replacement)
    )
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "OUT_OF_STOCK"  # 已越过旧pending检查。
    submit.assert_awaited_once()
    async with app.state.database.sessions() as session:
        old = await session.get(AgentPurchase, conversation.state.purchase_proposal.id)
        assert old.status == "succeeded"


async def test_uncertain_window_remains_blocking_with_original_manual_review_details(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    submit = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(AgentPurchase)
            .values(created_at=(datetime.now(UTC) - timedelta(seconds=120)).replace(tzinfo=None))
            .where(AgentPurchase.user_id == base)
        )
    receipt = (await client.get("/api/assistant/purchases?scope=unresolved")).json()["items"][0]
    assert receipt["status"] == "submitting" and receipt["request_id"] is None
    assert receipt["needs_manual_review"] and receipt["lookup_source"] == "not_found"
    assert (
        receipt["conversation_id"] == conversation.id and "不能证明" in receipt["recovery_message"]
    )
    replacement = await proposal_for(app, base, base)
    blocked = await client.post(
        path(replacement) + "/confirm", headers=headers, json=confirmation(replacement)
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "PURCHASE_PENDING"
    assert submit.await_count == 1 and await available(context) == 3


async def test_sql_result_is_available_even_when_redis_lookup_is_down(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, _, _, _ = context
    await submit_and_finish_without_projection(context, conversation)
    get = AsyncMock(side_effect=RedisConnectionError())
    monkeypatch.setattr(app.state.order_ingress.redis, "get", get)
    receipt = (await client.get("/api/assistant/purchases")).json()["items"][0]
    assert receipt["status"] == "succeeded" and receipt["lookup_available"]
    assert receipt["lookup_source"] == "mysql"
    get.assert_not_awaited()


async def test_partial_redis_index_loss_uses_original_embedded_payload_without_repair(
    purchase_context, monkeypatch
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    submit = AsyncMock(wraps=app.state.order_ingress.submit)
    monkeypatch.setattr(app.state.order_ingress, "submit", submit)
    response = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    identifier, request_id = response.json()["id"], response.json()["request_id"]
    ingress = app.state.order_ingress
    await ingress.redis.delete(
        ingress.intent_key(base, identifier), ingress.document_key(request_id)
    )
    async with app.state.database.sessions() as session, session.begin():
        row = await session.get(AgentPurchase, identifier)
        row.status, row.request_id = "submitting", None
        row.snapshot = {key: value for key, value in row.snapshot.items() if key != "result"}
    before = await ingress.redis.hgetall(ingress.store.key(base))
    receipt = (await client.get("/api/assistant/purchases")).json()["items"][0]
    assert receipt["status"] == "pending" and receipt["lookup_source"] == "redis"
    assert receipt["request_id"] == request_id
    assert before == await ingress.redis.hgetall(ingress.store.key(base))
    assert not await ingress.redis.exists(ingress.document_key(request_id))
    assert submit.await_count == 1 and await available(context) == 2


async def test_mismatched_redis_payload_is_not_exposed_or_accepted(purchase_context):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    response = await client.post(
        path(conversation) + "/confirm", headers=headers, json=confirmation(conversation)
    )
    request_id = response.json()["request_id"]
    ingress = app.state.order_ingress
    doc = await ingress.get_document(request_id)
    doc["purchase"]["request_key"] = str(uuid4())
    doc["result"]["title"] = "不应泄露的载荷"
    await ingress.redis.set(ingress.document_key(request_id), json.dumps(doc))
    result = await client.get("/api/assistant/purchases")
    receipt = result.json()["items"][0]
    assert receipt["status"] == "pending" and receipt["lookup_source"] == "unavailable"
    assert not receipt["lookup_available"] and "不应泄露" not in result.text


async def test_account_pagination_finds_old_unresolved_and_precise_old_purchase(purchase_context):
    context, conversation = purchase_context
    _, client, _, _, _ = context
    old = await add_history(context, conversation, "submitting", 600)
    for age in range(6):
        await add_history(context, conversation, age=age)
    first = (await client.get("/api/assistant/purchases")).json()
    assert first["total"] == 7 and len(first["items"]) == 5 and first["unresolved_count"] == 1
    second = (await client.get("/api/assistant/purchases?page=2")).json()
    assert len(second["items"]) == 2 and old in {row["id"] for row in second["items"]}
    unresolved = (await client.get("/api/assistant/purchases?scope=unresolved")).json()
    assert unresolved["total"] == 1 and unresolved["items"][0]["id"] == old
    older_terminal = second["items"][0]["id"]
    precise = (await client.get(path(conversation) + f"?purchase_id={older_terminal}")).json()
    assert older_terminal in {row["id"] for row in precise}


async def test_account_and_selected_purchase_are_owned_and_gets_need_no_csrf(purchase_context):
    context, conversation = purchase_context
    app, client, base, phones, _ = context
    identifier = await add_history(context, conversation)
    for query in ["page=0", "page_size=11", "scope=bad"]:
        assert (await client.get("/api/assistant/purchases?" + query)).status_code == 422
    assert (await client.get(path(conversation) + "?purchase_id=bad")).status_code == 422
    await login(client, app, phones[1])
    assert (await client.get("/api/assistant/purchases")).json()["total"] == 0
    assert (await client.get(path(conversation) + f"?purchase_id={identifier}")).status_code == 404
    foreign = await assistant.create(app.state.database, base + 1)
    assert (await client.get(path(foreign) + f"?purchase_id={identifier}")).status_code == 404
    client.cookies.clear()
    assert (await client.get("/api/assistant/purchases")).status_code == 401


async def test_new_conversation_progress_context_has_owned_source_without_new_purchase(
    purchase_context,
):
    context, conversation = purchase_context
    app, client, base, _, headers = context
    payload = await submit_and_finish_without_projection(context, conversation)
    other = await assistant.create(app.state.database, base)
    model = ScriptedModel([call("finish", {"answer": f"原订单已创建，编号{payload.request_id}。"})])
    app.state.agent_model = model
    response = await client.post(
        f"/api/assistant/conversations/{other.id}/turns",
        headers=headers,
        json={"message": "刚才购买怎么样了", "expected_version": 0},
    )
    assert response.status_code == 200 and '"type": "done"' in response.text
    context_message = json.loads(model.inputs[0][-1]["content"].split("\n", 1)[1])
    assert context_message[0]["conversation_id"] == conversation.id
    assert context_message[0]["in_current_conversation"] is False
    assert context_message[0]["status"] == "succeeded"
    async with app.state.database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(VoucherOrder).where(VoucherOrder.user_id == base)
            )
            == 1
        )
