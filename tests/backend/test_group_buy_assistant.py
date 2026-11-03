"""只读助手规则、原生工具循环与认证隔离；不调用收费模型或用户开发库。"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, update

from agent.model import NativeToolModel
from agent.schemas import ConversationState, ModelMessage
from agent.tools.group_buys import GroupBuyTools
from agent.workflows.selection import run_selection
from backend.app.core.config import Settings
from backend.app.core.errors import ApiError
from backend.app.models import AgentConversation, Voucher, VoucherUsage
from backend.app.schemas.group_buys import GroupBuy, Requirements, UsageRule
from backend.app.services import assistant, group_buys
from scripts.run_backend_tests import provision
from scripts.seed_demo_catalog import load_catalog
from scripts.seed_demo_catalog import seed as seed_catalog
from scripts.seed_group_buy_rules import load_entries
from scripts.seed_group_buy_rules import seed as seed_rules
from tests.backend.test_order_migration import migration_database as empty_database
from tests.backend.test_voucher_orders import login
from tests.backend.test_voucher_orders import order_context as base_context

order_context = base_context
migration_database = empty_database
NOW = datetime(2026, 10, 17, 2, 0, tzinfo=UTC)  # 周六，北京时间10点


def item(**updates):
    return GroupBuy(
        voucher_id=1,
        shop_id=1,
        title="双人火锅套餐",
        shop_name="虚构火锅店",
        city="天津",
        area="南开街区",
        category="火锅",
        image="public/assets/photos/01.jpg",
        pay_value=15800,
        face_value=19800,
        stock_snapshot=3,
        sale_state="active",
        validity_days=30,
        rules_text=["虚构"],
        checked_at=NOW.isoformat(),
        usage=UsageRule(
            benefit_kind="package",
            min_people=2,
            max_people=2,
            package_items=["双人套餐"],
            covers_meal=True,
            allowed_weekdays=list(range(7)),
            daily_start="11:00",
            daily_end="22:00",
            reservation_required=False,
            extra_cost_fen=0,
            holiday_policy="same_weekdays",
            stackable=False,
        ),
    ).model_copy(update=updates)


def needs(**updates):
    return Requirements(
        people=2,
        budget_fen=20000,
        use_date="2026-10-17",
        use_time="18:00",
        area="南开区",
        category="火锅",
        **updates,
    )


def call(name, args, identifier="test-call"):
    return ModelMessage(
        tool_calls=[
            {
                "id": identifier,
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        ]
    )


class ScriptedModel:
    """明确的测试替身，不作为运行时降级或假AI回答。"""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def ensure_configured(self):
        pass

    async def complete(self, messages, tools):
        self.inputs.append(json.loads(json.dumps(messages)))
        return next(self.replies)


def test_full_meal_budget_not_face_value_and_weekend_package():
    result = group_buys.evaluate(item(), needs(), NOW)
    assert result.verdict == "eligible" and result.total_cost_fen == 15800
    assert (
        group_buys.evaluate(item(), needs().model_copy(update={"budget_fen": 15000}), NOW).verdict
        == "ineligible"
    )
    cash = item(usage=UsageRule(benefit_kind="cash"))
    result = group_buys.evaluate(cash, needs(), NOW)
    assert result.verdict == "unknown" and result.total_cost_fen is None


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"max_people": 1, "min_people": 1}, "ineligible"),
        ({"allowed_weekdays": [0, 1, 2, 3, 4]}, "ineligible"),
        ({"extra_cost_fen": 5000}, "ineligible"),
        ({"extra_cost_fen": None}, "unknown"),
        ({"holiday_policy": "unknown"}, "unknown"),
        ({"daily_start": None, "daily_end": None}, "unknown"),
    ],
)
def test_rule_boundaries(updates, expected):
    sample = item()
    sample.usage = UsageRule.model_validate({**sample.usage.model_dump(), **updates})
    assert group_buys.evaluate(sample, needs(), NOW).verdict == expected


def test_dates_times_reservation_and_missing_data_are_not_inferred():
    sample = item()
    sample.usage.reservation_required = True
    for value, expected in [(False, "ineligible"), (None, "unknown"), (True, "eligible")]:
        request = needs().model_copy(update={"allow_reservation": value})
        assert group_buys.evaluate(sample, request, NOW).verdict == expected
    assert (
        group_buys.evaluate(item(), needs().model_copy(update={"use_time": None}), NOW).verdict
        == "unknown"
    )
    for changes in [{"use_date": NOW.date() - timedelta(days=1)}, {"use_time": "22:00"}]:
        request = Requirements.model_validate({**needs().model_dump(mode="json"), **changes})
        assert group_buys.evaluate(item(), request, NOW).verdict == "ineligible"
    assert group_buys.evaluate(item(usage=UsageRule()), needs(), NOW).verdict == "unknown"
    assert group_buys.evaluate(item(sale_state="sold_out"), needs(), NOW).verdict == "ineligible"


@pytest.mark.parametrize(
    "data",
    [
        {"budget_fen": True},
        {"budget_fen": -1},
        {"people": 0},
        {"user_id": 1},
    ],
)
def test_untrusted_constraints_validation(data):
    with pytest.raises(ValidationError):
        Requirements.model_validate(data)


async def test_patch_preserves_conditions_and_invalidates_previous_verdict():
    state = ConversationState(requirements=needs())
    tools = GroupBuyTools(None, state, AsyncMock())
    tools.evaluations[1] = group_buys.evaluate(item(), needs(), NOW)
    await tools.execute("set_requirements", '{"category":"烤肉"}')
    assert state.requirements.category == "烤肉"
    assert state.requirements.budget_fen == 20000 and state.requirements.people == 2
    assert not tools.evaluations
    result = await tools.execute("finish", '{"answer":"推荐1","recommended_ids":[1]}')
    assert result["error"] == "UNVERIFIED_RECOMMENDATION"
    assert (await tools.execute("create_order", "{}"))["error"] == "UNKNOWN_TOOL"


async def test_loop_observes_invalid_tool_and_asks_without_buying():
    model = ScriptedModel(
        [
            call("present_plan", {"steps": ["整理人数和预算", "查询并检查使用规则"]}),
            call("create_order", {"voucher_id": 1}),
            call("set_requirements", {"people": 2, "budget_fen": 20000, "evidence": "两人200元"}),
            call("finish", {"answer": "请补充使用日期和时段。", "recommended_ids": []}),
        ]
    )
    state, events = ConversationState(), []

    async def emit(event):
        events.append(event)

    answer, ids = await run_selection(
        model, None, Settings(_env_file=None), state, "两人200元", emit
    )
    assert "日期" in answer and ids == []
    assert state.requirements.people == 2 and len(state.messages) == 2
    assert any("UNKNOWN_TOOL" in m.get("content", "") for m in model.inputs[2])
    assert any(event.kind == "plan" for event in events)


async def test_loop_has_bounded_rounds_and_does_not_save_internal_reasoning():
    settings = Settings(_env_file=None, agent_max_rounds=2)
    model = ScriptedModel([ModelMessage(content="还在想"), ModelMessage(content="还在想")])
    with pytest.raises(ApiError) as error:
        await run_selection(model, None, settings, ConversationState(), "你好", AsyncMock())
    assert error.value.code == "AGENT_STEP_LIMIT" and len(model.inputs) == 2


async def test_native_transport_uses_configured_url_and_strips_reasoning(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "不得保存的内部推理",
                            "tool_calls": [
                                {
                                    "id": "one",
                                    "type": "function",
                                    "function": {
                                        "name": "finish",
                                        "arguments": '{"answer":"你好"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "agent.model.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(
        _env_file=None,
        llm_base_url="https://model.example/v1",
        llm_model="test-model",
        llm_api_key="test-key",
    )
    result = await NativeToolModel(settings).complete([{"role": "user", "content": "你好"}], [])
    assert str(seen[0].url) == "https://model.example/v1/chat/completions"
    assert "reasoning" not in result.model_dump_json()
    with pytest.raises(ApiError):
        NativeToolModel(Settings(_env_file=None)).ensure_configured()


@pytest.mark.parametrize(
    ("url", "allowed", "passes"),
    [
        ("http://campus.example/v1", [], False),
        ("http://campus.example/v1", ["http://campus.example"], True),
        ("http://campus.example.evil/v1", ["http://campus.example"], False),
        ("http://campus.example:8080/v1", ["http://campus.example"], False),
        ("http://user:password@campus.example/v1", ["http://campus.example"], False),
        ("https://campus.example/v1", [], True),
    ],
)
def test_remote_http_requires_exact_explicit_origin(url, allowed, passes):
    settings = Settings(
        _env_file=None,
        llm_base_url=url,
        llm_http_allowed_origins=allowed,
        llm_model="test-model",
        llm_api_key="synthetic-test-key",
    )
    model = NativeToolModel(settings)
    if passes:
        model.ensure_configured()
    else:
        with pytest.raises(ApiError):
            model.ensure_configured()


@pytest.mark.mysql
@pytest.mark.redis
async def test_real_query_tools_stream_and_conversation_ownership(order_context):
    app, client, base, phones = order_context
    headers = await login(client, app, phones[0])
    use_date = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    needs_now = {
        "people": 2,
        "budget_fen": 20000,
        "use_date": use_date,
        "use_time": "18:00",
        "category": None,
        "area": None,
    }
    rule = item().usage
    identifiers = []
    try:
        async with app.state.database.sessions() as session, session.begin():
            session.add(VoucherUsage(voucher_id=base, spec=rule.model_dump(mode="json")))
        response = await client.post(
            "/api/group-buys/evaluate",
            headers=headers,
            json={"voucher_id": base, "requirements": needs_now},
        )
        assert response.status_code == 200 and response.json()["verdict"] == "eligible"
        assert (
            await client.post(
                "/api/group-buys/search", headers=headers, json={"requirements": {"category": "%"}}
            )
        ).json()["total"] == 0
        created = await client.post("/api/assistant/conversations", headers=headers, json={})
        assert created.status_code == 201
        conversation = created.json()
        identifiers.append(conversation["id"])
        url = f"/api/assistant/conversations/{conversation['id']}"
        assert (
            await client.post(url + "/turns", json={"message": "你好", "expected_version": 0})
        ).status_code == 403
        assert (
            await client.post(
                url + "/turns", headers=headers, json={"message": "你好", "expected_version": 0}
            )
        ).status_code == 503
        model = ScriptedModel(
            [
                call("set_requirements", {**needs_now, "evidence": "找双人套餐"}),
                call("search_group_buys", {}),
                call("evaluate_plan", {"voucher_id": base}),
                call("finish", {"answer": "该演示套餐满足当前条件。", "recommended_ids": [base]}),
            ]
        )
        app.state.agent_model = model
        response = await client.post(
            url + "/turns", headers=headers, json={"message": "找双人套餐", "expected_version": 0}
        )
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]["type"] == "done", events[-1]
        assert events[-1]["result"]["recommended_ids"] == [base]
        assert any(e.get("event", {}).get("tool") == "search_group_buys" for e in events)
        saved = (await client.get(url)).json()
        assert saved["version"] == 1 and saved["state"]["requirements"]["people"] == 2
        assert (
            await client.post(
                url + "/turns", headers=headers, json={"message": "继续", "expected_version": 0}
            )
        ).status_code == 409
        await login(client, app, phones[1])
        assert (await client.get(url)).status_code == 404
        async with app.state.database.sessions() as session:
            assert await session.scalar(select(Voucher.stock).where(Voucher.id == base)) == 3
    finally:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(
                delete(AgentConversation).where(AgentConversation.id.in_(identifiers))
            )
            await session.execute(delete(VoucherUsage).where(VoucherUsage.voucher_id == base))


@pytest.mark.mysql
@pytest.mark.redis
async def test_cas_race_and_failed_turn_does_not_commit_partial_conditions(order_context):
    app, client, base, phones = order_context
    headers = await login(client, app, phones[0])
    created = await assistant.create(app.state.database, base)
    try:
        first = await assistant.get(app.state.database, base, created.id)
        second = await assistant.get(app.state.database, base, created.id)
        first.state.requirements.people = 2
        await assistant.save(app.state.database, base, first)
        second.state.requirements.people = 4
        with pytest.raises(ApiError) as error:
            await assistant.save(app.state.database, base, second)
        assert error.value.code == "AGENT_VERSION_CONFLICT"
        app.state.agent_model = ScriptedModel(
            [
                call("set_requirements", {"people": 5, "evidence": "五个人"}),
                call("finish", {"answer": "编造推荐", "recommended_ids": [999999]}),
            ]
        )
        app.state.settings.agent_max_rounds = 2
        result = await client.post(
            f"/api/assistant/conversations/{created.id}/turns",
            headers=headers,
            json={"message": "五个人", "expected_version": 1},
        )
        events = [json.loads(line) for line in result.text.splitlines()]
        assert events[-1]["type"] == "error" and events[-1]["code"] == "AGENT_STEP_LIMIT"
        saved = await assistant.get(app.state.database, base, created.id)
        assert saved.version == 1 and saved.state.requirements.people == 2
    finally:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(
                delete(AgentConversation).where(AgentConversation.id == created.id)
            )


@pytest.mark.mysql
async def test_demo_rules_insert_only_and_migration_downgrade_protects_data(
    migration_database, mysql_settings
):

    from backend.app.core.schema_check import compare_schema
    from scripts.run_backend_tests import ROOT

    database = migration_database
    await provision(
        mysql_settings.model_copy(update={"mysql_database": database.engine.url.database})
    )
    await seed_catalog(database, load_catalog(), apply=True)
    async with database.sessions() as session:
        baseline = (
            await session.execute(select(Voucher.id, Voucher.stock, Voucher.pay_value))
        ).all()
    entries = load_entries()
    assert (await seed_rules(database, entries))["new_vouchers"] == 6
    async with database.sessions() as session:
        assert await session.get(Voucher, 3001) is None
    result = await seed_rules(database, entries, apply=True)
    assert result["new_vouchers"] == result["new_rules"] == 6
    async with database.sessions() as session, session.begin():
        await session.execute(update(Voucher).where(Voucher.id == 3001).values(stock=19))
        modified = UsageRule(note="人工补充的规则，重跑不可覆盖")
        await session.execute(
            update(VoucherUsage)
            .where(VoucherUsage.voucher_id == 3001)
            .values(spec=modified.model_dump(mode="json"))
        )
    result = await seed_rules(database, entries, apply=True)
    assert result["new_vouchers"] == result["new_rules"] == 0
    async with database.sessions() as session:
        assert (await session.get(Voucher, 3001)).stock == 19
        assert (await session.get(VoucherUsage, 3001)).spec["note"].startswith("人工")
        rows = (
            await session.execute(
                select(Voucher.id, Voucher.stock, Voucher.pay_value).where(
                    Voucher.id.in_([row.id for row in baseline])
                )
            )
        ).all()
        assert sorted(rows) == sorted(baseline)
    conflict = [entry.model_copy(deep=True) for entry in entries]
    conflict[-1].voucher.title = "同ID不同券"
    with pytest.raises(ValueError, match="冲突"):
        await seed_rules(database, conflict, apply=True)

    def downgrade(sync):
        # 只核验冻结0006自身的非空保护；不跨0007空表DDL回退，MySQL不能回滚DDL。
        import runpy

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        revision = runpy.run_path(
            str(ROOT / "backend/alembic/versions/0006_group_buy_assistant.py")
        )
        revision["downgrade"].__globals__["op"] = Operations(MigrationContext.configure(sync))
        revision["downgrade"]()

    with pytest.raises(RuntimeError, match="不能无损回退"):
        async with database.engine.begin() as connection:
            await connection.run_sync(downgrade)
    async with database.engine.connect() as connection:
        assert await connection.run_sync(compare_schema) == []
