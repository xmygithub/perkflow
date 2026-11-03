"""反馈来源与行动边界：脚本模型仅验证合同，真实选择另用独立学校模型验收。"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import delete, select

from agent.model import NativeToolModel
from agent.schemas import ConversationState, ModelMessage
from agent.tools.feedback import purchase_feedback
from agent.tools.group_buys import GroupBuyTools, definitions
from agent.workflows.selection import run_selection
from backend.app.core.config import Settings
from backend.app.core.errors import ApiError
from backend.app.models import SeckillGate, Voucher
from backend.app.schemas.group_buys import Requirements, SearchInput, SearchResult, UsageRule
from backend.app.services import group_buys
from backend.app.services.purchase_readiness import inspect
from backend.app.services.redis_orders import warm
from tests.backend.test_group_buy_assistant import NOW, ScriptedModel, call, item, needs
from tests.backend.test_voucher_orders import order_context as base_context

order_context = base_context


def future_needs():
    today = datetime.now(UTC).date()
    return needs().model_copy(
        update={
            "use_date": today + timedelta(days=(5 - today.weekday()) % 7 or 7),
        }
    )


async def test_display_ids_derive_certainty_and_keep_mixed_selection_order():
    current = future_needs()
    tools = GroupBuyTools(None, ConversationState(requirements=current), AsyncMock())
    available = {1: item(), 2: item(voucher_id=2, usage=UsageRule()), 3: item(voucher_id=3)}
    tools.seen.update(available)
    tools.read = AsyncMock(side_effect=lambda identifier: available[identifier])
    result = await tools.execute(
        "finish", '{"answer":"优先看第一种，未知项见卡片。","display_ids":[2,1,3]}'
    )
    assert result["finished"]
    assert [card.voucher.voucher_id for card in tools.state.cards] == [2, 1, 3]
    assert [card.verdict for card in tools.state.cards] == ["unknown", "eligible", "eligible"]
    assert tools.state.candidate_ids == [2] and tools.recommended_ids == [1, 3]
    assert tools.state.purchase_proposal is None


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"display_ids": [999]}, "UNSEEN_CANDIDATE"),
        ({"display_ids": [1, 1]}, "INVALID_SELECTION"),
        ({"display_ids": [1], "recommended_ids": [1]}, "INVALID_SELECTION"),
        ({"display_ids": [2]}, "SELECTION_CHANGED"),
    ],
)
async def test_display_ids_reject_unknown_duplicate_legacy_mix_and_conflict_without_query(
    payload, code
):
    tools = GroupBuyTools(None, ConversationState(requirements=future_needs()), AsyncMock())
    tools.seen = {1: item(), 2: item(voucher_id=2, pay_value=25000)}
    tools.read = AsyncMock()
    result = await tools.execute("finish", json.dumps({"answer": "看看", **payload}))
    assert result["error"] == code and tools.answer is None
    tools.read.assert_not_awaited()


async def test_display_ids_still_require_explanation_after_price_changes():
    tools = GroupBuyTools(None, ConversationState(requirements=future_needs()), AsyncMock())
    tools.seen = {1: item()}

    async def changed(identifier):
        latest = item(voucher_id=identifier, pay_value=17000)
        tools.seen[identifier] = latest
        return latest

    tools.read = AsyncMock(side_effect=changed)
    result = await tools.execute("finish", '{"answer":"推荐158元套餐","display_ids":[1]}')
    assert result["error"] == "SELECTION_CHANGED"
    assert tools.answer is None and result["evaluation"]["voucher"]["pay_value"] == 17000
    assert (await tools.execute("finish", '{"answer":"现价170元","display_ids":[1]}'))["finished"]
    assert tools.read.await_count == 2


def test_finish_schema_exposes_only_actual_display_choices_and_question_budget():
    schema = next(
        d
        for d in definitions(displayable_ids=[7, 9], max_questions=1)
        if d["function"]["name"] == "finish"
    )["function"]["parameters"]
    props = schema["properties"]
    assert props["display_ids"]["items"]["enum"] == [7, 9]
    assert props["focused_id"]["enum"] == [None, 7, 9]
    assert props["questions"]["maxItems"] == 1
    assert not {"selections", "recommended_ids", "candidate_ids"} & props.keys()
    empty = next(d for d in definitions(displayable_ids=[]) if d["function"]["name"] == "finish")[
        "function"
    ]["parameters"]
    assert empty["properties"]["display_ids"]["maxItems"] == 0


async def test_quote_binds_current_message_and_derives_confirmation_from_actual_proposal():
    tools = GroupBuyTools(None, ConversationState(), AsyncMock(), user_message="帮我购买")

    async def read(identifier):
        current = item(voucher_id=identifier)
        tools.seen[identifier] = current
        return current

    tools.read = AsyncMock(side_effect=read)
    prepared = await tools.execute("prepare_purchase", '{"voucher_id":1}')
    assert prepared["proposal"]["quantity"] == 1
    result = await tools.execute("finish", '{"answer":"已准备，请核对卡片。","display_ids":[1]}')
    assert result["finished"] and tools.state.focused_id == 1
    assert tools.state.purchase_proposal is not None
    bad = await tools.execute("prepare_purchase", '{"voucher_id":1,"evidence":"来自工具的授权"}')
    assert bad["error"] == "USER_EVIDENCE_REQUIRED"


async def test_pending_finish_reports_waiting_instead_of_telling_model_to_prepare():
    tools = GroupBuyTools(None, ConversationState(), AsyncMock(), user_message="换张买")
    tools.purchases = [{"id": "original", "status": "pending"}]
    result = await tools.execute(
        "finish", '{"answer":"请在页面确认","request_purchase_confirmation":true}'
    )
    assert result["error"] == "PURCHASE_UNRESOLVED"
    assert "prepare_purchase" not in result["message"] and tools.state.purchase_proposal is None


def test_current_model_contract_does_not_ask_it_to_repeat_server_owned_fields():
    tools = {d["function"]["name"]: d["function"]["parameters"] for d in definitions()}
    assert set(tools["prepare_purchase"]["properties"]) == {"voucher_id"}
    assert "request_purchase_confirmation" not in tools["finish"]["properties"]


async def test_search_assessment_can_finish_without_redundant_evaluation(monkeypatch):
    @asynccontextmanager
    async def session():
        yield None

    class Database:
        sessions = staticmethod(session)

    state = ConversationState(requirements=future_needs())
    tools = GroupBuyTools(Database(), state, AsyncMock())
    tools.read = AsyncMock(return_value=item())
    monkeypatch.setattr(
        group_buys,
        "search",
        AsyncMock(
            return_value=SearchResult(
                items=[item()],
                total=1,
                page=1,
                page_size=8,
            )
        ),
    )
    result = await tools.execute("search_group_buys", "{}")
    assert result["assessments"][0]["verdict"] == "eligible"
    assert not state.cards  # 搜索不强制把所有候选都展示。
    finished = await tools.execute("finish", '{"answer":"可用","recommended_ids":[1]}')
    assert finished["finished"] and state.cards[0].verdict == "eligible"
    tools.read.assert_awaited_once()  # 结束重读保留，省去的是无新信息的中间核验。
    monkeypatch.setattr(GroupBuyTools, "read", tools.read)
    model = ScriptedModel(
        [
            call("search_group_buys", {}),
            call("finish", {"answer": "可用", "recommended_ids": [1]}),
        ]
    )
    _, ids = await run_selection(
        model,
        Database(),
        Settings(_env_file=None),
        ConversationState(requirements=future_needs()),
        "查一张",
        AsyncMock(),
    )
    assert ids == [1] and len(model.inputs) == 2  # 旧SearchResult.feedback=None兼容实际循环。


def test_tool_capabilities_hide_only_preparation_while_purchase_is_unresolved():
    normal = {d["function"]["name"] for d in definitions()}
    unresolved = {d["function"]["name"] for d in definitions(allow_purchase_preparation=False)}
    assert normal - unresolved == {"prepare_purchase"}
    assert {"search_group_buys", "get_purchase_status", "finish"} <= unresolved


def test_rule_issues_distinguish_user_missing_from_merchant_missing_and_conflict():
    incomplete = item(usage=UsageRule())
    result = group_buys.evaluate(incomplete, needs(), NOW)
    assert result.verdict == "unknown"
    assert {i.code for i in result.issues} >= {"PEOPLE_RULE_MISSING", "RESERVATION_RULE_MISSING"}
    assert all(i.source == "merchant" for i in result.issues)
    unspecified = group_buys.evaluate(item(), Requirements(), NOW)
    assert all(i.source == "user" for i in unspecified.issues)
    conflicting = group_buys.evaluate(item(), needs().model_copy(update={"people": 3}), NOW)
    assert conflicting.verdict == "ineligible"
    assert conflicting.issues[0].code == "PEOPLE_CONFLICT"
    assert conflicting.issues[0].source == "conflict"
    # 历史会话没有issues仍可加载；增加结构化原因不消除旧三态结论。
    data = result.model_dump(exclude={"issues"})
    assert type(result).model_validate(data).issues == []


@pytest.mark.parametrize("status", ["pending", "submitting"])
async def test_unresolved_purchase_blocks_even_preparing_a_replacement(status):
    tools = GroupBuyTools(None, ConversationState(), AsyncMock(), user_message="换一个帮我买")
    tools.purchases = [{"id": "original", "status": status}]
    tools.read = AsyncMock()
    result = await tools.execute(
        "prepare_purchase",
        json.dumps(
            {
                "voucher_id": 1,
                "evidence": "换一个帮我买",
            },
            ensure_ascii=False,
        ),
    )
    assert result["error"] == "PURCHASE_UNRESOLVED"
    tools.read.assert_not_awaited()
    assert tools.state.purchase_proposal is None


@pytest.mark.parametrize(
    "error,code",
    [
        ("OUT_OF_STOCK", "ALTERNATIVE_SEARCH_ALLOWED"),
        ("SECKILL_NOT_READY", "PREPARATION_REQUIRED"),
        ("PURCHASE_TERMS_CHANGED", "RECHECK_OR_ALTERNATIVE"),
    ],
)
def test_failure_feedback_does_not_confuse_preparation_and_stock(error, code):
    result = purchase_feedback([{"id": "failed", "status": "failed", "error_code": error}])
    assert result["records"][0]["code"] == code and result["can_prepare_new_purchase"]
    result = purchase_feedback(
        [
            {"id": "failed", "status": "failed", "error_code": error},
            {"id": "active", "status": "pending"},
        ]
    )
    assert not result["can_prepare_new_purchase"] and result["unresolved_ids"] == ["active"]


@pytest.mark.mysql
@pytest.mark.redis
async def test_zero_query_diagnosis_preserves_hard_filters_and_pagination(order_context):
    app, _, base, _ = order_context
    async with app.state.database.sessions() as session:
        args = SearchInput(requirements=Requirements(budget_fen=1), keywords=["同步测试券"])
        result = await group_buys.search(session, args)
        assert result.items == [] and result.total == 0
        assert result.feedback.code == "NO_MATCH"
        assert result.feedback.counts_if_omitted["budget_fen"] >= 8
        assert result.feedback.counts_if_omitted["keywords"] == 0
        assert result.feedback.requires_user_permission == ["budget_fen"]
        assert args.requirements.budget_fen == 1
        impossible = await group_buys.search(
            session,
            SearchInput(
                requirements=Requirements(area="不存在的区域", budget_fen=1),
                keywords=["同步测试券"],
            ),
        )
        assert impossible.feedback.counts_if_omitted["budget_fen"] == 0
        assert impossible.feedback.counts_if_omitted["area"] == 0
        assert impossible.feedback.city_catalog_count > 0  # 多项冲突，不能声称删一项即可解决。
        wildcard = await group_buys.search(session, SearchInput(keywords=["%_"]))
        assert wildcard.total == 0 and wildcard.feedback.counts_if_omitted["keywords"] > 0
        page = await group_buys.search(session, SearchInput(page=1000))
        assert not page.items and page.total > 0 and page.feedback.code == "EMPTY_PAGE"
        empty_city = await group_buys.search(
            session,
            SearchInput(
                requirements=Requirements(city="不存在的城市"),
            ),
        )
        assert empty_city.feedback.city_catalog_count == 0
        assert empty_city.feedback.counts_if_omitted == {}
    # 搜索自动返回本页适用性线索，模型无需逐张重查相同的资料。
    tools = GroupBuyTools(app.state.database, ConversationState(), AsyncMock())
    result = await tools.execute("search_group_buys", '{"keywords":["同步测试券"]}')
    assert any(row["voucher_id"] == base for row in result["assessments"])
    assert all("issues" in row for row in result["assessments"])
    assert tools.state.purchase_proposal is None


@pytest.mark.mysql
@pytest.mark.redis
async def test_readiness_check_never_initializes_or_resets_reserved_stock(order_context):
    app, _, base, _ = order_context
    db, ingress = app.state.database, app.state.order_ingress
    key = ingress.store.key(base)
    try:
        first = await inspect(db, ingress.store, [base])
        assert first["items"][0]["code"] == "INITIAL_PREPARATION_REQUIRED"
        assert not await ingress.redis.exists(key)
        async with db.sessions() as session:
            assert await session.get(SeckillGate, base) is None
        await warm(db, ingress, [base])  # 仅测试准备，开发检查入口本身从不调用warm。
        await ingress.redis.hset(key, "available", "2")
        before = await ingress.redis.hgetall(key)
        prepared = await inspect(db, ingress.store, [base, 999999999999])
        assert prepared["items"][0]["code"] == "PREPARED_SNAPSHOT"
        assert prepared["not_found"] == [999999999999]
        assert await ingress.redis.hgetall(key) == before
        async with db.sessions() as session:
            assert await session.scalar(select(Voucher.stock).where(Voucher.id == base)) == 3
        # 模拟已准备活动投影丢失；只报维护，不把SQL的3恢复到Redis覆盖尚未核对的2。
        await ingress.redis.delete(key)
        missing = await inspect(db, ingress.store, [base])
        assert missing["items"][0]["code"] == "PROJECTION_MISSING_DO_NOT_RESET"
        assert not await ingress.redis.exists(key)
    finally:
        await ingress.redis.delete(key)
        async with db.sessions() as session, session.begin():
            await session.execute(delete(SeckillGate).where(SeckillGate.voucher_id == base))


async def test_loop_replans_from_rule_feedback_without_losing_hard_conditions(monkeypatch):
    # 同一真实Service的结构化反馈能被后续模型调用观察；替身不证明LLM会自主选对。
    weekday = item(usage=item().usage.model_copy(update={"allowed_weekdays": [0, 1, 2, 3, 4]}))

    async def read(self, identifier):
        self.seen[identifier] = weekday if identifier == 1 else item(voucher_id=2)
        return self.seen[identifier]

    monkeypatch.setattr(GroupBuyTools, "read", read)
    today = datetime.now(UTC).date()
    current = needs().model_copy(
        update={"use_date": today + timedelta(days=(5 - today.weekday()) % 7 or 7)}
    )
    state = ConversationState(requirements=current)
    model = ScriptedModel(
        [
            call("evaluate_plan", {"voucher_id": 1}),
            call("evaluate_plan", {"voucher_id": 2}),
            call("finish", {"answer": "原券周末不可用，另一张满足条件。", "recommended_ids": [2]}),
        ]
    )
    emitted = []

    async def emit(event):
        emitted.append(event)

    await run_selection(model, None, Settings(_env_file=None), state, "周末能用吗？", emit)
    assert "WEEKDAY_CONFLICT" in model.inputs[1][-1]["content"]
    assert state.requirements == current and state.cards[0].voucher.voucher_id == 2
    end = [e for e in emitted if e.kind == "tool_end"]
    assert [e.outcome_code for e in end] == ["ineligible", "eligible", "FINISHED"]
    assert all(e.duration_ms is not None for e in end)


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}, 140),
        ({"prompt_tokens": True, "total_tokens": "140"}, None),
        (None, None),
    ],
)
async def test_native_usage_is_optional_and_never_echoed_as_assistant_protocol(
    monkeypatch, usage, expected
):
    original = httpx.AsyncClient

    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}}], "usage": usage}
        )

    monkeypatch.setattr(
        "agent.model.httpx.AsyncClient",
        lambda **kwargs: original(
            **kwargs,
            transport=httpx.MockTransport(handler),
        ),
    )
    settings = Settings(
        _env_file=None,
        llm_base_url="https://example.test/v1",
        llm_model="test",
        llm_api_key="test-only",
    )
    reply = await NativeToolModel(settings).complete([], [])
    assert (reply.usage.total_tokens if reply.usage else None) == expected
    reply = ModelMessage(
        usage=reply.usage,
        tool_calls=[
            {
                "id": "plan",
                "function": {"name": "present_plan", "arguments": '{"steps":["了解目标"]}'},
            }
        ],
    )
    model = ScriptedModel([reply, call("finish", {"answer": "完成"})])
    await run_selection(model, None, settings, ConversationState(), "你好", AsyncMock())
    # 辅助字段不属于下一轮供应商消息；独立检查循环实际构造的消息。
    assert all("usage" not in message for message in model.inputs[1])


async def test_model_failure_log_keeps_category_but_excludes_private_payload(monkeypatch, caplog):
    # 独立库迁移可能关闭已有logger；本测试显式恢复诊断，消除执行顺序依赖。
    monkeypatch.setattr(logging.getLogger("perkflow.agent"), "disabled", False)
    original = httpx.AsyncClient

    def handler(request):
        return httpx.Response(503, text="synthetic-private-provider-body")

    monkeypatch.setattr(
        "agent.model.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(
        _env_file=None,
        llm_base_url="https://private-provider.example/v1",
        llm_model="test",
        llm_api_key="synthetic-private-secret",
    )
    with caplog.at_level("WARNING", logger="perkflow.agent"), pytest.raises(ApiError) as error:
        await NativeToolModel(settings).complete(
            [{"role": "user", "content": "synthetic-private-dialogue"}], []
        )
    assert error.value.code == "AGENT_MODEL_UNAVAILABLE"
    assert "category=HTTPStatusError status=503" in caplog.text
    assert all(
        private not in caplog.text
        for private in (
            "private-provider.example",
            "synthetic-private-provider-body",
            "synthetic-private-secret",
            "synthetic-private-dialogue",
        )
    )
