"""目标状态、观察复用与安全边界；脚本模型验证执行合同，不冒充真实语义理解。"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent.schemas import ConversationState, Preferences
from agent.tools.feedback import decision_feedback
from agent.tools.group_buys import GroupBuyTools, definitions
from agent.workflows.selection import run_selection
from backend.app.core.config import Settings
from backend.app.schemas.group_buys import Requirements, SearchFeedback, SearchResult, UsageRule
from backend.app.services import group_buys
from tests.backend.test_group_buy_assistant import NOW, ScriptedModel, call, item


def tools_for(state=None, message="找个火锅团购，划算一点"):
    return GroupBuyTools(None, state or ConversationState(), AsyncMock(), user_message=message)


async def update(tools, changes, **extra):
    return await tools.execute("update_goal", json.dumps({"changes": changes, **extra}))


def change(field, value, strength="required"):
    return {"field": field, "value": value, "strength": strength}


async def test_unified_category_and_incremental_budget_keep_original_range_and_source():
    state = ConversationState(turn_number=1)
    first = tools_for(state)
    await update(
        first,
        [change("category", "火锅"), change("low_price", True, "preferred")],
        intents=["find"],
    )
    second = tools_for(state, "南开区，最多200元，其他不变")
    state.turn_number = 2
    await update(
        second,
        [change("area", "南开区"), change("budget_fen", 20000)],
        intents=["find", "change_goal"],
    )
    assert state.requirements.category == "火锅" and state.preferences.category is None
    assert state.requirements.area == "南开区" and state.requirements.budget_fen == 20000
    assert state.preferences.low_price and state.goal_revision == 2
    assert state.condition_details["category"].source_turn == 1
    assert state.condition_details["area"].source_turn == 2
    assert state.condition_details["area"].source_text == second.user_message
    assert state.condition_details["category"].strength == "required"


async def test_area_strength_switch_and_explicit_clear_are_atomic_and_keep_other_fields():
    state = ConversationState(requirements=Requirements(category="火锅", budget_fen=20000))
    tools = tools_for(state, "最好南开区")
    await update(tools, [change("area", "南开区", "preferred")])
    assert state.preferences.area == "南开区" and state.requirements.area is None
    tools.user_message = "只能南开区"
    await update(tools, [change("area", "南开区")])
    assert state.requirements.area == "南开区" and state.preferences.area is None
    tools.user_message = "区域不限了，预算也不限了"
    await update(tools, [change("area", None, "clear"), change("budget_fen", None, "clear")])
    assert state.requirements.area is None and state.preferences.area is None
    assert state.requirements.budget_fen is None and state.requirements.category == "火锅"
    assert state.condition_details["budget_fen"].strength == "clear"


@pytest.mark.parametrize(
    "changes",
    [
        [change("area", "南开区"), change("budget_fen", True)],
        [change("budget_fen", 20000, "preferred")],
        [change("area", None)],
        [change("city", None, "clear")],
        [change("area", "南开区"), change("area", "五大道", "preferred")],
        [change("people", "两个人")],
        [change("use_weekdays", [5, 6]), change("use_date", "2026-10-19")],
    ],
)
async def test_invalid_multi_field_update_preserves_complete_state(changes):
    state = ConversationState(requirements=Requirements(area="五大道", budget_fen=10000))
    tools = tools_for(state)
    before = state.model_dump(mode="json")
    with pytest.raises(ValidationError):
        await update(tools, changes)
    assert state.model_dump(mode="json") == before
    tools.emit.assert_not_awaited()


async def test_same_value_phase_or_intent_update_does_not_erase_observations_or_quote():
    tools = tools_for()

    async def read(identifier):
        tools.seen[identifier] = item(voucher_id=identifier)
        return tools.seen[identifier]

    tools.read = AsyncMock(side_effect=read)
    await update(tools, [change("category", "火锅")])
    await tools.execute("prepare_purchase", '{"voucher_id":1}')
    quote = tools.state.purchase_proposal
    tools.repetitions["same-query"] = 2
    result = await update(
        tools,
        [change("category", "火锅")],
        phase="verify",
        intents=["check_rules", "prepare_purchase"],
    )
    assert not result["conditions_changed"] and tools.state.goal_revision == 1
    assert tools.state.purchase_proposal == quote and tools.repetitions["same-query"] == 2
    assert tools.state.cards
    # 同一调用里真正更改人数时，旧报价必须失效，不因保留资料绕过重新授权。
    await update(tools, [change("people", 3)])
    assert tools.state.purchase_proposal is None and tools.state.cards == []


def test_current_schema_only_exposes_one_goal_entry_and_old_json_remains_readable():
    names = {definition["function"]["name"] for definition in definitions()}
    assert "update_goal" in names and not {"set_goal", "set_requirements"} & names
    old = ConversationState.model_validate({"requirements": {"category": "火锅"}})
    assert old.turn_number == old.goal_revision == 0 and old.condition_details == {}


async def test_reused_observation_avoids_query_but_refresh_and_final_finish_recheck():
    tools = tools_for()

    async def read(identifier):
        result = item(voucher_id=identifier, pay_value=15800 + tools.read.await_count * 100)
        tools.seen[identifier] = result
        return result

    tools.read = AsyncMock(side_effect=read)
    first = await tools.execute("evaluate_plan", '{"voucher_id":1}')
    reused = await tools.execute("evaluate_plan", '{"voucher_id":1}')
    assert reused["observation"]["new_information"] is False and tools.read.await_count == 1
    assert reused["voucher"] == first["voucher"]
    final = await tools.execute("finish", '{"answer":"看看这张","display_ids":[1]}')
    assert final["error"] == "SELECTION_CHANGED" and tools.read.await_count == 2
    tools2 = tools_for()
    tools2.read = AsyncMock(return_value=item())
    await tools2.execute("evaluate_plan", '{"voucher_id":1}')
    fresh = await tools2.execute("evaluate_plan", '{"voucher_id":1,"refresh":true}')
    assert not fresh["observation"]["snapshot_reused"] and tools2.read.await_count == 2


async def test_real_condition_change_invalidates_query_reuse_but_noop_cannot_reset_budget():
    tools = tools_for()

    async def read(identifier):
        tools.seen[identifier] = item(voucher_id=identifier)
        return tools.seen[identifier]

    tools.read = AsyncMock(side_effect=read)
    await tools.execute("evaluate_plan", '{"voucher_id":1}')
    await tools.execute("evaluate_plan", '{"voucher_id":1}')
    await update(tools, [], goal="换一个目标描述", phase="compare")
    result = await tools.execute("evaluate_plan", '{"voucher_id":1}')
    assert result["error"] == "REPEATED_ACTION" and tools.read.await_count == 1
    await update(tools, [change("people", 3)])
    result = await tools.execute("evaluate_plan", '{"voucher_id":1}')
    assert result["verdict"] == "ineligible" and tools.read.await_count == 2


def test_feedback_separates_missing_merchant_data_from_user_unknowns_and_search_conflicts():
    merchant_unknown = group_buys.evaluate(item(usage=UsageRule()), Requirements(people=2), NOW)
    feedback = decision_feedback([merchant_unknown])
    fields = feedback["remaining_fields_by_source"]
    assert fields["merchant"] and fields["user"] and feedback["can_finish_with_candidates"]
    assert any("商家缺资料" in option for option in feedback["options"])
    no_match = decision_feedback([], SearchFeedback(code="NO_MATCH", message="预算冲突"))
    assert not no_match["can_finish_with_candidates"]
    assert any("用户明确修改" in option for option in no_match["options"])


async def test_loop_updates_goal_then_uses_search_feedback_without_forced_extra_steps(monkeypatch):
    @asynccontextmanager
    async def session():
        yield None

    class Database:
        sessions = staticmethod(session)

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

    async def read(self, identifier):
        self.seen[identifier] = item(voucher_id=identifier)
        return self.seen[identifier]

    monkeypatch.setattr(GroupBuyTools, "read", read)
    model = ScriptedModel(
        [
            call("update_goal", {"changes": [change("category", "火锅")], "intents": ["find"]}),
            call("search_group_buys", {}),
            call("finish", {"answer": "先看这张，使用条件见卡片。", "display_ids": [1]}),
        ]
    )
    state = ConversationState(preferences=Preferences(low_price=True))
    await run_selection(
        model, Database(), Settings(_env_file=None), state, "找个火锅团购", AsyncMock()
    )
    observation = json.loads(model.inputs[2][-1]["content"])
    assert observation["decision_feedback"]["can_finish_with_candidates"]
    assert state.requirements.category == "火锅" and state.turn_number == 1
    assert state.condition_details["category"].source_text == "找个火锅团购"
    assert [entry.role for entry in state.messages] == ["user", "assistant"]
    assert group_buys.search.await_count == 1


async def test_purchase_intent_never_bypasses_unresolved_purchase_guard():
    tools = tools_for(message="能用就买，但原订单还在处理")
    tools.purchases = [{"id": "original", "status": "pending"}]
    await update(tools, [], intents=["check_rules", "prepare_purchase"])
    tools.read = AsyncMock()
    result = await tools.execute("prepare_purchase", '{"voucher_id":1}')
    assert result["error"] == "PURCHASE_UNRESOLVED"
    tools.read.assert_not_awaited()
    assert tools.state.purchase_proposal is None


async def test_reservation_strength_has_one_public_field_and_hard_alias_never_becomes_preference():
    tools = tools_for(message="不接受预约")
    await update(tools, [change("avoid_reservation", True)])
    assert tools.state.requirements.allow_reservation is False
    assert not tools.state.preferences.avoid_reservation
    assert "allow_reservation" in tools.state.condition_details
    tools.user_message = "可以预约，只是尽量不预约"
    await update(tools, [change("allow_reservation", False, "preferred")])
    assert tools.state.requirements.allow_reservation is None
    assert tools.state.preferences.avoid_reservation
    before = tools.state.model_dump()
    with pytest.raises(ValidationError):
        await update(tools, [change("allow_reservation", True), change("avoid_reservation", True)])
    assert tools.state.model_dump() == before
    schema = next(d for d in definitions() if d["function"]["name"] == "update_goal")
    fields = schema["function"]["parameters"]["$defs"]["ConditionChange"]["properties"]["field"][
        "enum"
    ]
    assert "avoid_reservation" not in fields and "allow_reservation" in fields


async def test_loop_validation_feedback_identifies_field_without_echoing_private_input():
    model = ScriptedModel(
        [
            call("update_goal", {"changes": [change("budget_fen", "synthetic-private-input")]}),
            call("update_goal", {"changes": [change("budget_fen", 20000)]}),
            call("finish", {"answer": "已更新预算。"}),
        ]
    )
    state = ConversationState(requirements=Requirements(category="火锅"))
    await run_selection(
        model, None, Settings(_env_file=None), state, "预算200元，其他不变", AsyncMock()
    )
    feedback = json.loads(model.inputs[1][-1]["content"])
    assert feedback["error"] == "INVALID_ARGUMENTS"
    assert list(feedback["problems"][0]["loc"]) == ["budget_fen"]
    assert "synthetic-private-input" not in json.dumps(feedback)
    assert all("input" not in problem and "ctx" not in problem for problem in feedback["problems"])
    assert state.requirements.budget_fen == 20000 and state.requirements.category == "火锅"


async def test_pending_textual_confirmation_uses_current_facts_without_retry_or_new_action():
    tools = tools_for(message="还在处理中？换一张买吧")
    tools.purchases = [{"id": "original", "status": "pending"}]
    tools.read = AsyncMock()
    result = await tools.execute(
        "finish",
        json.dumps(
            {
                "answer": "原购买还在处理，若失败后换券，仍需要你在页面确认。",
                "display_ids": [999],
            },
            ensure_ascii=False,
        ),
    )
    assert result["finished"] and result["answer_guard"] == "UNRESOLVED_PURCHASE_EXPLANATION"
    assert "最近读取" in tools.answer and "本轮没有生成新报价" in tools.answer
    assert not tools.state.cards and not tools.state.purchase_proposal
    tools.read.assert_not_awaited()
    # 这是确定性的边界回复，不把结构化准备/确认行为静默变成成功。
    explicit = await tools.execute(
        "finish", '{"answer":"现在确认","request_purchase_confirmation":true}'
    )
    assert explicit["error"] == "PURCHASE_UNRESOLVED"
    events = []

    async def emit(event):
        events.append(event)

    model = ScriptedModel(
        [
            call(
                "finish",
                {
                    "answer": "原单还在处理中，之后新购买仍需要你在页面确认。",
                },
            )
        ]
    )
    answer, ids = await run_selection(
        model,
        None,
        Settings(_env_file=None),
        ConversationState(),
        "换张买吧",
        emit,
        purchase_reader=AsyncMock(return_value=[{"id": "original", "status": "pending"}]),
    )
    assert "最近读取" in answer and ids == []
    assert events[-1].outcome_code == "UNRESOLVED_PURCHASE_EXPLANATION"
    assert len(model.inputs) == 1
