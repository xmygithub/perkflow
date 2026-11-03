"""探索合同与真实业务反馈；替身只证明执行边界，真实规划另由学校模型验收。"""

import json
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from agent.schemas import ConversationState
from agent.tools.group_buys import GroupBuyTools, definitions
from agent.workflows.selection import run_selection
from backend.app.core.config import Settings
from backend.app.core.errors import ApiError
from backend.app.models import AgentConversation, Voucher
from backend.app.schemas.group_buys import Requirements, SearchInput
from backend.app.services import group_buys
from tests.backend.test_group_buy_assistant import NOW, ScriptedModel, call, item, needs
from tests.backend.test_voucher_orders import login
from tests.backend.test_voucher_orders import order_context as context_fixture

order_context = context_fixture


def tools_for(state=None, message=""):
    return GroupBuyTools(None, state or ConversationState(), AsyncMock(), user_message=message)


@pytest.mark.parametrize(
    "args",
    [
        {"answer": "最终购买仍需要你在页面确认。"},
        {"answer": "请点击确认卡片完成购买。"},
        {"answer": "已经准备好，请在页面点击确认。"},
        {"answer": "下一步由你决定。", "request_purchase_confirmation": True},
    ],
)
async def test_finish_cannot_point_to_purchase_confirmation_without_real_proposal(args):
    tools = tools_for(message="帮我购买")
    result = await tools.execute("finish", json.dumps(args, ensure_ascii=False))
    assert result["error"] == "PURCHASE_PROPOSAL_REQUIRED"
    assert tools.answer is None and tools.state.purchase_proposal is None


@pytest.mark.parametrize(
    "answer",
    [
        "还没有生成购买确认卡，请先说明想买哪张券。",
        "订单已创建，无需再次在页面确认。",
        "订单处理中，不需要你再次点击确认。",
        "先看看候选，使用日期尚未确认。",
    ],
)
async def test_missing_quote_explanation_and_existing_order_status_do_not_require_new_quote(answer):
    tools = tools_for()
    assert (await tools.execute("finish", json.dumps({"answer": answer}, ensure_ascii=False)))[
        "finished"
    ]


async def test_buy_after_failed_history_recovers_missing_prepare_before_finish(monkeypatch):
    async def read(self, identifier):
        self.seen[identifier] = item(voucher_id=identifier)
        return self.seen[identifier]

    monkeypatch.setattr(GroupBuyTools, "read", read)
    state = ConversationState()
    model = ScriptedModel(
        [
            call("get_group_buy_detail", {"voucher_id": 1}),
            call("finish", {"answer": "最终购买仍需要你在页面确认。", "candidate_ids": [1]}),
            call("prepare_purchase", {"voucher_id": 1, "evidence": "帮我购买"}),
            call(
                "finish",
                {
                    "answer": "已生成确认卡，请核对后确认。",
                    "request_purchase_confirmation": True,
                    "candidate_ids": [1],
                },
            ),
        ]
    )
    purchases = AsyncMock(
        return_value=[
            {
                "id": "old",
                "status": "failed",
                "error_code": "SECKILL_NOT_READY",
            }
        ]
    )
    await run_selection(
        model, None, Settings(_env_file=None), state, "帮我购买", AsyncMock(), purchases
    )
    assert state.purchase_proposal is not None
    assert state.purchase_proposal.evaluation.voucher.voucher_id == 1
    assert state.purchase_proposal.id != "old"
    assert "PURCHASE_PROPOSAL_REQUIRED" in json.dumps(model.inputs[2])
    assert len(model.inputs) == 4


async def test_unambiguous_buy_does_not_finish_by_asking_permission_to_prepare(monkeypatch):
    async def read(self, identifier):
        self.seen[identifier] = item(voucher_id=identifier)
        return self.seen[identifier]

    monkeypatch.setattr(GroupBuyTools, "read", read)
    state = ConversationState(cards=[group_buys.evaluate(item(), Requirements())])
    model = ScriptedModel(
        [
            call("finish", {"answer": "我没有生成新的待确认报价。请告诉我是否继续。"}),
            call("prepare_purchase", {"voucher_id": 1, "evidence": "帮我购买"}),
            call("finish", {"answer": "已生成确认卡。", "request_purchase_confirmation": True}),
        ]
    )
    await run_selection(
        model,
        None,
        Settings(_env_file=None),
        state,
        "帮我购买",
        AsyncMock(),
        AsyncMock(return_value=[{"status": "failed"}]),
    )
    assert state.purchase_proposal is not None
    assert "PURCHASE_PROPOSAL_REQUIRED" in json.dumps(model.inputs[1])


@pytest.mark.parametrize("status", ["submitting", "pending", "succeeded"])
async def test_explicit_buy_with_existing_purchase_never_forces_a_new_proposal(status):
    state = ConversationState(cards=[group_buys.evaluate(item(), Requirements())])
    tools = tools_for(state, "帮我购买")
    tools.purchases = [{"status": status}]
    assert (await tools.execute("finish", '{"answer":"请先查看原购买结果。"}'))["finished"]


async def test_ambiguous_buy_can_ask_and_failed_preparation_can_explain():
    cards = [group_buys.evaluate(item(voucher_id=i), Requirements()) for i in [1, 2]]
    tools = tools_for(ConversationState(cards=cards), "帮我购买")
    assert (await tools.execute("finish", '{"answer":"想购买哪张？","questions":["选哪张？"]}'))[
        "finished"
    ]
    tools = tools_for(ConversationState(cards=cards[:1]), "帮我购买")
    tools.read = AsyncMock(side_effect=ApiError(503, "QUERY_FAILED", "商品暂不可查询"))
    with pytest.raises(ApiError):
        await tools.execute("prepare_purchase", '{"voucher_id":1,"evidence":"帮我购买"}')
    assert (await tools.execute("finish", '{"answer":"尚未生成确认卡，商品暂不可查询。"}'))[
        "finished"
    ]


async def test_soft_preferences_do_not_create_budget_or_hard_area_and_have_user_source():
    state = ConversationState()
    tools = tools_for(state, "南开区优先，别太贵")
    result = await tools.execute(
        "set_goal",
        json.dumps(
            {
                "goal": "找团购",
                "preferences": {"area": "南开区", "low_price": True},
                "evidence": "南开区优先，别太贵",
            },
            ensure_ascii=False,
        ),
    )
    assert "error" not in result
    assert state.requirements.area is None and state.requirements.budget_fen is None
    assert state.preferences.low_price and state.preferences.area == "南开区"
    assert state.condition_sources["city"] == "平台当前城市"
    invalid = await tools.execute("set_requirements", '{"budget_fen":30000,"evidence":"预算300"}')
    assert invalid["error"] == "USER_EVIDENCE_REQUIRED"
    assert state.requirements.budget_fen is None
    conflict = await tools.execute("set_requirements", '{"area":"南开区","evidence":"南开区优先"}')
    assert conflict["error"] == "SOFT_CONSTRAINT_CONFLICT" and state.requirements.area is None
    tools.user_message = "预算必须50以内，南开区优先"
    conflict = await tools.execute("set_requirements", '{"area":"南开区","evidence":"南开区优先"}')
    assert conflict["error"] == "SOFT_CONSTRAINT_CONFLICT"


async def test_partial_weekend_and_mid_conversation_change_keep_other_conditions():
    state = ConversationState(requirements=needs())
    tools = tools_for(state, "改成烤肉，周末去，其他不变")
    result = await tools.execute(
        "set_requirements",
        '{"category":"烤肉","use_date":null,"use_weekdays":[5,6],"evidence":"改成烤肉，周末去"}',
    )
    assert "error" not in result
    assert state.requirements.category == "烤肉"
    assert state.requirements.budget_fen == 20000 and state.requirements.area == "南开区"
    assert state.requirements.use_date is None and state.requirements.use_weekdays == [5, 6]
    weekdays = item(usage=item().usage.model_copy(update={"allowed_weekdays": [0, 1, 2, 3, 4]}))
    assert group_buys.evaluate(weekdays, state.requirements, NOW).verdict == "ineligible"


async def test_unknown_candidate_can_finish_without_filling_fields_but_cannot_be_confirmed():
    tools = tools_for()
    tools.seen[1] = item()
    tools.read = AsyncMock(return_value=item())
    result = await tools.execute(
        "finish", '{"answer":"先看看这个，日期人数待确认。","candidate_ids":[1]}'
    )
    assert result == {"finished": True}
    assert tools.state.cards[0].verdict == "unknown"
    assert tools.state.cards[0].stage == "candidate"
    assert tools.state.candidate_ids == [1] and tools.recommended_ids == []
    tools.answer = None
    result = await tools.execute("finish", '{"answer":"全通过","recommended_ids":[1]}')
    assert result["error"] == "UNVERIFIED_RECOMMENDATION" and tools.answer is None
    assert (await tools.execute("finish", '{"answer":"假的","candidate_ids":[2]}'))[
        "error"
    ] == "UNSEEN_CANDIDATE"


async def test_selection_contract_avoids_duplicate_lists_and_location_limit_is_platform_fact():
    tools = tools_for(message="附近有什么值得买的团购")
    tools.seen[1] = item()
    tools.read = AsyncMock(return_value=item())
    result = await tools.execute(
        "finish", '{"answer":"可以先看看这张。","selections":[{"voucher_id":1,"kind":"candidate"}]}'
    )
    assert result["finished"] and tools.state.candidate_ids == [1]
    assert "定位或距离" in tools.answer
    schema = next(d for d in definitions() if d["function"]["name"] == "finish")["function"][
        "parameters"
    ]
    assert "display_ids" in schema["properties"]
    assert not {"selections", "recommended_ids", "candidate_ids"} & schema["properties"].keys()


async def test_goal_update_after_search_keeps_facts_but_invalidates_old_verdicts():
    tools = tools_for(message="火锅团购便宜一点")
    tools.seen[1] = item()
    tools.evaluations[1] = group_buys.evaluate(item(), needs(), NOW)
    await tools.execute("set_goal", '{"preferences":{"low_price":true},"evidence":"便宜一点"}')
    assert 1 in tools.seen and not tools.evaluations
    tools.read = AsyncMock(return_value=item())
    result = await tools.execute(
        "finish", '{"answer":"先看看这张。","selections":[{"voucher_id":1,"kind":"candidate"}]}'
    )
    assert result["finished"] and tools.state.cards[0].verdict == "unknown"


async def test_browsing_question_budget_does_not_force_a_fixed_workflow():
    tools = tools_for(message="帮我核验方案")
    args = '{"answer":"有这些候选。","questions":["几个人？","预算多少？"]}'
    result = await tools.execute("finish", args)
    assert result["error"] == "TOO_MANY_QUESTIONS" and tools.answer is None
    await tools.execute("set_goal", '{"phase":"verify","evidence":"核验方案"}')
    assert (await tools.execute("finish", args))["finished"]
    assert tools.state.phase == "verify" and len(tools.state.questions) == 2


@pytest.mark.parametrize(
    "change", [{"stock_snapshot": 0, "sale_state": "sold_out"}, {"pay_value": 16800}]
)
async def test_final_recheck_observes_changed_stock_or_price_and_does_not_finish(change):
    tools = tools_for()
    tools.seen[1] = item()
    tools.read = AsyncMock(return_value=item(**change))
    result = await tools.execute("finish", '{"answer":"推荐","candidate_ids":[1]}')
    assert result["error"] == "SELECTION_CHANGED"
    assert tools.answer is None and result["evaluation"]["voucher"]["pay_value"] == change.get(
        "pay_value", 15800
    )


async def test_candidate_cannot_bypass_budget_and_search_cannot_override_constraints():
    tools = tools_for(ConversationState(requirements=Requirements(budget_fen=10000)))
    tools.seen[1] = item()
    tools.read = AsyncMock(return_value=item())
    result = await tools.execute("finish", '{"answer":"看看","candidate_ids":[1]}')
    assert result["error"] == "SELECTION_CHANGED"
    with pytest.raises(ValidationError):
        await tools.execute("search_group_buys", '{"budget_fen":20000,"area":null}')
    assert tools.state.requirements.budget_fen == 10000


async def test_repeated_missing_details_are_bounded_and_compare_keeps_unknowns():
    tools = tools_for()
    tools.read = AsyncMock(side_effect=lambda identifier: item(voucher_id=identifier))
    tools.seen = {1: item(), 2: item(voucher_id=2)}
    comparison = await tools.execute("compare_group_buys", '{"voucher_ids":[1,2]}')
    assert all(row["verdict"] == "unknown" for row in comparison["items"])
    for _ in range(2):
        assert "error" not in await tools.execute("get_group_buy_detail", '{"voucher_id":1}')
    assert (await tools.execute("get_group_buy_detail", '{"voucher_id":1}'))[
        "error"
    ] == "REPEATED_ACTION"
    assert len(tools.state.observations) == 3


async def test_previous_choice_numbers_survive_goal_change_and_never_reuse_old_facts():
    old = [
        group_buys.evaluate(item(voucher_id=17), needs(), NOW),
        group_buys.evaluate(item(voucher_id=29), needs(), NOW),
    ]
    state = ConversationState(requirements=needs(), cards=old)
    tools = tools_for(state, "三个人，第二个还合适吗")
    await tools.execute("set_requirements", '{"people":3,"evidence":"三个人"}')
    assert state.requirements.budget_fen == 20000 and state.requirements.area == "南开区"
    tools.read = AsyncMock(side_effect=lambda identifier: item(voucher_id=identifier))
    result = await tools.execute("read_previous_choices", '{"positions":[2]}')
    assert result["choices"][0]["evaluation"]["voucher"]["voucher_id"] == 29
    assert result["choices"][0]["evaluation"]["verdict"] == "ineligible"
    tools.read.assert_awaited_once_with(29)
    assert (await tools.execute("read_previous_choices", '{"positions":[3]}'))[
        "error"
    ] == "UNKNOWN_CHOICE"
    assert (await tools.execute("read_previous_choices", '{"positions":[1,1]}'))[
        "error"
    ] == "UNKNOWN_CHOICE"
    with pytest.raises(ValidationError):
        await tools.execute("read_previous_choices", '{"positions":[true]}')


async def test_comparison_finish_refreshes_rejected_choices_and_focus_requires_current_selection():
    future = needs().model_copy(update={"use_date": date.today() + timedelta(days=1)})
    tools = tools_for(ConversationState(requirements=future))
    tools.seen = {1: item(), 2: item(voucher_id=2, pay_value=25000)}

    async def read(identifier):
        row = item(voucher_id=identifier, pay_value=15800 if identifier == 1 else 25000)
        tools.seen[identifier] = row
        return row

    tools.read = AsyncMock(side_effect=read)
    await tools.execute("compare_group_buys", '{"voucher_ids":[1,2]}')
    assert (await tools.execute("finish", '{"answer":"看看","focused_id":2}'))[
        "error"
    ] == "INVALID_FOCUS"
    result = await tools.execute(
        "finish",
        '{"answer":"第二个超预算，保留第一个。","selections":[{"voucher_id":1,"kind":"confirmed"}],"focused_id":1}',
    )
    assert result["finished"] and tools.state.focused_id == 1
    assert [row.verdict for row in tools.state.comparisons] == ["eligible", "ineligible"]
    assert tools.read.await_count == 4  # 比较时读取两张，结束时再次复查两张。


async def test_compare_previous_display_directly_refreshes_without_redundant_reads():
    cards = [group_buys.evaluate(item(voucher_id=key), Requirements(), NOW) for key in [7, 9]]
    state = ConversationState(cards=cards[:1])
    # 最近展示只有一张，更早那组的两张仍能恢复；保存/重载后只保留ID与名称，不存旧适用结论。
    from agent.schemas import ChoiceReference

    state.choice_history = [
        [ChoiceReference(voucher_id=key, title="双人券", shop_name="店") for key in [7, 9]]
    ]
    state = ConversationState.model_validate(state.model_dump(mode="json"))
    tools = tools_for(state)
    tools.read = AsyncMock(side_effect=lambda identifier: item(voucher_id=identifier))
    result = await tools.execute("read_previous_choices", '{"turns_back":2,"positions":[2]}')
    assert result["choices"][0]["evaluation"]["voucher"]["voucher_id"] == 9
    tools.seen.clear()
    assert (await tools.execute("compare_group_buys", '{"voucher_ids":[7,9]}'))["items"]
    assert (await tools.execute("compare_group_buys", '{"voucher_ids":[7,99]}'))[
        "error"
    ] == "UNSEEN_CANDIDATE"
    assert (await tools.execute("read_previous_choices", '{"turns_back":3,"positions":[1]}'))[
        "error"
    ] == "UNKNOWN_CHOICE"


async def test_unselected_comparison_stock_change_requires_new_explanation():
    tools = tools_for()
    tools.seen = {1: item(), 2: item(voucher_id=2)}

    async def read(identifier):
        row = item(voucher_id=identifier)
        tools.seen[identifier] = row
        return row

    tools.read = AsyncMock(side_effect=read)
    await tools.execute("compare_group_buys", '{"voucher_ids":[1,2]}')

    async def changed(identifier):
        row = item(
            voucher_id=identifier,
            stock_snapshot=0 if identifier == 2 else 3,
            sale_state="sold_out" if identifier == 2 else "active",
        )
        tools.seen[identifier] = row
        return row

    tools.read = AsyncMock(side_effect=changed)
    result = await tools.execute("finish", '{"answer":"第一张更合适。","candidate_ids":[1]}')
    assert result["error"] == "SELECTION_CHANGED" and tools.answer is None
    assert result["evaluation"]["voucher"]["voucher_id"] == 2


async def test_turn_freezes_previous_numbering_and_old_comparisons_are_not_silently_reused(
    monkeypatch,
):
    old = [group_buys.evaluate(item(voucher_id=19), needs(), NOW)]
    future = needs().model_copy(update={"use_date": date.today() + timedelta(days=1)})
    state = ConversationState(requirements=future, cards=old, comparisons=old, focused_id=19)
    monkeypatch.setattr(group_buys, "detail", AsyncMock(return_value=item(voucher_id=19)))
    # 真实数据库覆盖在HTTP用例；此处隔离原生工具循环的编号冻结时机。
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    @asynccontextmanager
    async def sessions():
        yield None

    model = ScriptedModel(
        [
            call("read_previous_choices", {"positions": [1]}),
            call(
                "finish",
                {
                    "answer": "这是刚才那张，尚未购买。",
                    "selections": [{"voucher_id": 19, "kind": "candidate"}],
                    "focused_id": 19,
                },
            ),
        ]
    )
    await run_selection(
        model,
        SimpleNamespace(sessions=sessions),
        Settings(_env_file=None),
        state,
        "第一个怎么样",
        AsyncMock(),
    )
    assert state.focused_id == 19 and not state.comparisons
    assert state.cards[0].voucher.voucher_id == 19
    assert '"position": 1' in model.inputs[0][0]["content"]


def test_old_saved_state_loads_with_new_defaults_and_invalid_exploration_rejected():
    state = ConversationState.model_validate({"requirements": {"city": "天津"}, "messages": []})
    assert not state.preferences.low_price and state.candidate_ids == []
    for data in [{"keywords": [""]}, {"keywords": ["a" * 41]}, {"sort": "distance"}]:
        with pytest.raises(ValidationError):
            SearchInput.model_validate(data)
    with pytest.raises(ValidationError):
        Requirements(use_weekdays=[5, True])
    with pytest.raises(ValidationError):
        Requirements(use_date="2026-10-19", use_weekdays=[5, 6])


@pytest.mark.mysql
@pytest.mark.redis
async def test_exploration_http_real_queries_soft_area_and_saved_candidates(order_context):
    app, client, base, phones = order_context
    headers = await login(client, app, phones[0])
    database = app.state.database
    async with database.sessions() as session:
        # 不存在的软区域仍可返回候选；同样的硬区域则返回空，关键词不能扩大预算。
        soft = await group_buys.search(session, SearchInput(preferred_area="不存在的区域"))
        assert soft.total > 0
        hard = await group_buys.search(
            session, SearchInput(requirements=Requirements(area="不存在的区域"))
        )
        assert hard.total == 0
        empty = await group_buys.search(session, SearchInput(keywords=["%_"], sort="score"))
        assert empty.total == 0
        limited = await group_buys.search(
            session,
            SearchInput(requirements=Requirements(budget_fen=6000), keywords=["同步测试券"]),
        )
        assert limited.items and all(row.pay_value <= 6000 for row in limited.items)
        discovered = await group_buys.discover(session, Requirements(budget_fen=6000))
        assert discovered["options"] and "无定位能力" in discovered["note"]
        assert all(row["voucher_count"] >= row["shop_count"] >= 1 for row in discovered["options"])
        assert any(row["voucher_count"] > row["shop_count"] for row in discovered["options"])
    created = (await client.post("/api/assistant/conversations", headers=headers, json={})).json()
    path = f"/api/assistant/conversations/{created['id']}"
    try:
        app.state.agent_model = ScriptedModel(
            [
                call("set_goal", {"preferences": {"low_price": True}, "evidence": "便宜点"}),
                call("search_group_buys", {"keywords": ["同步测试券"]}),
                call(
                    "finish", {"answer": "先看看这张，使用条件仍需确认。", "candidate_ids": [base]}
                ),
            ]
        )
        response = await client.post(
            path + "/turns",
            headers=headers,
            json={"message": "帮我找团购，便宜点", "expected_version": 0},
        )
        final = json.loads(response.text.splitlines()[-1])
        assert final["type"] == "done", final
        assert final["result"]["candidate_ids"] == [base]
        saved = (await client.get(path)).json()["state"]
        assert saved["requirements"]["budget_fen"] is None and saved["preferences"]["low_price"]
        assert saved["cards"][0]["stage"] == "candidate" and saved["observations"]
        # 在同一真实会话继续指代、比较并聚焦；刷新后恢复相同结构化结果。
        app.state.agent_model = ScriptedModel(
            [
                call("read_previous_choices", {"positions": [1]}),
                call("search_group_buys", {"keywords": ["同步测试券"]}),
                call("compare_group_buys", {"voucher_ids": [base, base + 7]}),
                call(
                    "finish",
                    {
                        "answer": "先了解第一张，两张规则都未补全。",
                        "candidate_ids": [base, base + 7],
                        "focused_id": base,
                    },
                ),
            ]
        )
        response = await client.post(
            path + "/turns",
            headers=headers,
            json={"message": "刚才那个和另一张比较一下，先了解原来那张", "expected_version": 1},
        )
        final = json.loads(response.text.splitlines()[-1])
        assert final["type"] == "done", final
        saved = (await client.get(path)).json()["state"]
        assert saved["focused_id"] == base
        assert [row["voucher"]["voucher_id"] for row in saved["comparisons"]] == [base, base + 7]
        assert all(row["verdict"] == "unknown" for row in saved["comparisons"])
        async with database.sessions() as session:
            assert await session.scalar(select(Voucher.stock).where(Voucher.id == base)) == 3
    finally:
        async with database.sessions() as session, session.begin():
            await session.execute(
                delete(AgentConversation).where(AgentConversation.id == created["id"])
            )
