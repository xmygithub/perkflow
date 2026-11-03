"""执行器、确定性断言、证据裁判及独立SQL事件的对应测试。"""

import json
import sys
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent.schemas import ConversationState, ModelMessage, PurchaseProposal
from backend.app.core.config import Settings
from backend.app.core.errors import ApiError
from backend.app.schemas.group_buys import Requirements
from backend.app.services import group_buys
from scripts import evaluate_agent_dataset as runner
from scripts.agent_eval_checks import evaluate_checks, summarize
from scripts.agent_eval_dataset import Event, Task, Turn
from scripts.agent_eval_judge import judge_turn, validate_judgment
from scripts.evaluate_agent_dataset import (
    TracedModel,
    apply_events,
    business_facts,
    execute_task,
    execute_turn,
    isolated_database,
    safe_database_name,
    save_report,
    token_totals,
)
from tests.backend.test_group_buy_assistant import ScriptedModel, call, item


def turn(**extra):
    return Turn(message="双人", review=["state_alignment"], rationale="条件应落入状态", **extra)


def test_state_checks_use_actual_previous_values_and_normalize_times():
    before = ConversationState(requirements=Requirements(people=2, use_time="18:00"))
    after = before.model_copy(deep=True)
    after.requirements.people = 3
    spec = turn(
        state=[
            {"path": "requirements.people", "op": "preserved"},
            {"path": "requirements.use_time", "value": "18:00"},
        ]
    )
    results = evaluate_checks(spec, before, after, [], [])
    assert results[0]["expected"] == 2 and results[0]["actual"] == 3
    assert not results[0]["passed"] and results[1]["passed"]


def test_selection_does_not_count_rejected_cards_and_checks_verdict():
    state = ConversationState()
    good = group_buys.evaluate(
        item(),
        Requirements(people=2, budget_fen=20000, use_date="2026-10-24", use_time="18:00"),
        datetime(2026, 10, 19, tzinfo=UTC),
    )
    state.cards = [good]
    spec = turn(selection={"minimum_count": 1, "include": [1], "eligible": [1]})
    results = evaluate_checks(spec, state, state, [], [])
    assert not next(row for row in results if row["check"] == "selection:count")["passed"]
    results = evaluate_checks(spec, state, state, [1], [])
    assert all(row["passed"] for row in results)


def test_stale_proposal_requirements_cannot_pass_even_if_price_matches():
    state = ConversationState(requirements=Requirements(people=2))
    evaluation = group_buys.evaluate(item(), state.requirements)
    state.purchase_proposal = PurchaseProposal(
        id="test",
        evaluation=evaluation,
        requirements=Requirements(people=1),
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    results = evaluate_checks(
        turn(proposal={"mode": "present", "voucher_id": 1, "price_fen": 15800}),
        state,
        state,
        [],
        [],
    )
    assert not next(row for row in results if row["check"] == "proposal:requirements_match")[
        "passed"
    ]


def test_tool_observation_requires_success_not_attempt():
    spec, state = turn(observe_any=["search_group_buys"]), ConversationState()
    failed = [{"tool": "search_group_buys", "result": {"error": "INVALID_ARGUMENTS"}}]
    assert not evaluate_checks(spec, state, state, [], failed)[-1]["passed"]


async def test_last_finish_result_is_captured_without_extra_model_call():
    state = ConversationState()
    spec = turn()
    model = ScriptedModel([call("finish", {"answer": "人数还没有给出。", "display_ids": []})])
    after, result = await execute_turn(None, Settings(_env_file=None), state, spec, {}, model)
    assert result["error"] is None and result["model_calls"] == 1
    assert result["tool_trace"][-1]["tool"] == "finish"
    assert result["tool_trace"][-1]["result"]["finished"]
    assert len(result["tool_requests"]) == 1
    assert state.messages == [] and len(after.messages) == 2


async def test_failed_turn_rolls_back_partial_state_and_does_not_use_gold():
    model = ScriptedModel(
        [
            call(
                "update_goal",
                {"changes": [{"field": "people", "value": 3, "strength": "required"}]},
            )
        ]
    )
    before = ConversationState(requirements=Requirements(people=2))
    after, result = await execute_turn(
        None,
        Settings(_env_file=None),
        before,
        turn(state=[{"path": "requirements.people", "value": 3}]),
        {},
        model,
    )
    assert after.requirements.people == before.requirements.people == 2
    assert result["error"] and not result["automatic_pass"]
    assert any(row["tool"] == "update_goal" for row in result["tool_trace"])


async def test_unknown_usage_is_not_reported_as_zero_or_complete_total():
    assert all(value is None for value in token_totals([]).values())
    assert token_totals([{"total_tokens": 20}, None])["total_tokens"] is None
    assert token_totals([{"total_tokens": 20}])["total_tokens"] == 20
    delegate = AsyncMock()
    delegate.complete.side_effect = ApiError(502, "AGENT_MODEL_UNAVAILABLE", "private")
    model = TracedModel(delegate)
    with pytest.raises(ApiError):
        await model.complete([], [])
    assert model.calls == 1 and model.usages == [None]


def judgment(verdict="pass", path="/requirements_after/people", quote="2"):
    return {
        "reviews": [
            {
                "criterion": "state_alignment",
                "verdict": verdict,
                "evidence_path": path,
                "evidence_quote": quote,
                "reason": "实际人数为2",
            }
        ]
    }


@pytest.mark.parametrize(
    "change",
    [
        {"evidence_quote": "不存在的原文"},
        {"evidence_path": "/missing"},
        {"evidence_path": None},
        {"criterion": "unknown"},
    ],
)
def test_judge_cannot_invent_or_omit_evidence(change):
    value = judgment()
    value["reviews"][0].update(change)
    with pytest.raises((ValueError, KeyError, ValidationError)):
        validate_judgment(value, ["state_alignment"], {"requirements_after": {"people": 2}})


def test_judge_must_return_each_criterion_once_and_uncertain_is_not_pass():
    value = judgment()
    value["reviews"] *= 2
    with pytest.raises(ValueError):
        validate_judgment(value, ["state_alignment"], {})
    parsed = validate_judgment(judgment("uncertain", None, None), ["state_alignment"], {})
    assert parsed.reviews[0].verdict == "uncertain"


def test_judge_supports_explicit_packet_root_but_not_rubric_evidence():
    packet = {"requirements_after": {"people": 2}}
    assert (
        validate_judgment(
            judgment(path="/packet/requirements_after/people"), ["state_alignment"], packet
        )
        .reviews[0]
        .verdict
        == "pass"
    )
    with pytest.raises(ValueError):
        validate_judgment(
            judgment(path="/criteria/state_alignment/question"), ["state_alignment"], packet
        )


async def test_judge_has_no_business_tools_and_validates_independent_response():
    model = ScriptedModel(
        [
            ModelMessage(
                tool_calls=[
                    {
                        "id": "judge",
                        "function": {"name": "submit_review", "arguments": json.dumps(judgment())},
                    }
                ]
            )
        ]
    )
    original = model.complete

    async def complete(messages, tools):
        assert [row["function"]["name"] for row in tools] == ["submit_review"]
        return await original(messages, tools)

    model.complete = complete
    result = await judge_turn(
        Settings(_env_file=None),
        {"requirements_after": {"people": 2}},
        ["state_alignment"],
        {"state_alignment": {"question": "人数是否落入状态"}},
        model,
    )
    assert result["status"] == "completed" and not result["human_verified"]


async def test_judge_protocol_error_is_separate_not_a_pass_or_agent_failure():
    model = ScriptedModel([ModelMessage(content="看起来很好，满分")])
    result = await judge_turn(Settings(_env_file=None), {}, ["grounding"], {"grounding": {}}, model)
    assert result["status"] == "error" and result["reviews"] == []


async def test_strict_json_judge_uses_same_evidence_checks():
    model = ScriptedModel([ModelMessage(content=json.dumps(judgment()))])
    result = await judge_turn(
        Settings(_env_file=None),
        {"requirements_after": {"people": 2}},
        ["state_alignment"],
        {"state_alignment": {}},
        model,
    )
    assert result["status"] == "completed" and result["protocol"] == "strict_text_json"


async def test_rejudge_reuses_agent_evidence_without_database_or_replacing_auto_fail(
    tmp_path, monkeypatch
):
    state = ConversationState().model_dump(mode="json")
    original = {
        "report_version": 1,
        "dataset_id": "perkflow-agent-eval-v1.1",
        "status": "completed",
        "metadata": {"agent_model": "original-model"},
        "isolation": "fresh random SQL database per task; no Redis/MQ or purchase submission",
        "tasks": [
            {
                "id": "DEV-009",
                "complete": True,
                "automatic_pass": False,
                "turns": [
                    {
                        "turn": 1,
                        "input": "两人",
                        "answer": "未知",
                        "error": None,
                        "automatic_pass": False,
                        "state_before": state,
                        "state_after": state,
                        "tool_trace": [],
                        "tool_requests": [],
                        "purchase_history": [],
                        "checks": [
                            {
                                "check": "state:requirements.people:eq",
                                "expected": 2,
                                "actual": None,
                                "passed": False,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    source = tmp_path / "original.json"
    source.write_text(json.dumps(original), encoding="utf-8")
    before = source.read_bytes()
    grader = AsyncMock(return_value={"status": "completed", "reviews": []})
    monkeypatch.setattr(runner, "judge_turn", grader)
    monkeypatch.setattr(runner, "isolated_database", lambda *_: pytest.fail("不能访问SQL"))
    monkeypatch.setattr(runner, "run_selection", lambda *_: pytest.fail("不能重跑Agent"))
    result = await runner.rejudge(
        source, tmp_path / "review.json", Settings(_env_file=None, llm_model="judge-model")
    )
    assert grader.await_count == 1 and result["summary"]["automatic_tasks_passed"] == 0
    assert result["metadata"]["agent_model"] == "original-model"
    assert not result["rejudge"]["agent_rerun"]
    assert source.read_bytes() == before


def test_model_review_cannot_override_failed_automatic_check(tmp_path):
    report = {
        "status": "completed",
        "tasks": [
            {
                "id": "DEV-008",
                "complete": True,
                "automatic_pass": False,
                "turns": [
                    {
                        "turn": 1,
                        "error": None,
                        "checks": [
                            {
                                "check": "state:requirements.people:eq",
                                "expected": 2,
                                "actual": None,
                                "passed": False,
                            }
                        ],
                        "judge": {
                            "status": "completed",
                            "reviews": [{"criterion": "state_alignment", "verdict": "pass"}],
                        },
                    }
                ],
            }
        ],
    }
    save_report(report, tmp_path / "report.json")
    result = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert result["summary"]["automatic_tasks_passed"] == 0
    assert result["summary"]["judge"]["pass"] == 1
    assert "失败" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert summarize([])["automatic_task_pass_rate"] is None


def test_live_rejudge_cannot_implicitly_enter_reserved_split(tmp_path, monkeypatch):
    source = tmp_path / "reserved.json"
    source.write_text(json.dumps({"tasks": [{"id": "RES-001"}]}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["eval", "--live", "--judge", "--judge-report", str(source)])
    monkeypatch.setattr(runner, "Settings", lambda: pytest.fail("分组检查必须先于配置或模型请求"))
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2


@pytest.mark.parametrize("name", ["perkflow", "perkflow_test_other", "bad-name;DROP", ""])
def test_cleanup_guard_rejects_development_or_unowned_names(name):
    with pytest.raises(ValueError):
        safe_database_name(name)


@pytest.mark.mysql
async def test_actual_sql_events_merge_rules_keep_zero_orders_and_fresh_next_task(mysql_settings):
    async with isolated_database(mysql_settings) as database:
        facts = await business_facts(database)
        expected, history = dict(facts["stocks"]), {}
        events = [
            Event(kind="set_stock", voucher_id=3001, value=0),
            Event(kind="set_price", voucher_id=3001, value=18800),
            Event(kind="patch_usage", voucher_id=3001, value={"reservation_required": True}),
            Event(
                kind="purchase_history",
                voucher_id=102,
                value={
                    "status": "succeeded",
                    "request_id": "fixture-alias",
                    "order_id": "alias",
                    "order_status": "created",
                },
            ),
        ]
        await apply_events(database, events, history, "DEV-TEST", expected)
        async with database.sessions() as session:
            latest = await group_buys.detail(session, 3001)
        assert latest.stock_snapshot == 0 and latest.pay_value == 18800
        assert latest.usage.reservation_required and latest.usage.min_people == 2
        assert next(iter(history.values()))["request_id"].isdigit()
        final = await business_facts(database)
        assert not any(final["counts"].values()) and final["stocks"] == expected
    async with isolated_database(mysql_settings) as database:
        fresh = await business_facts(database)
        assert fresh["stocks"]["3001"] == 30


@pytest.mark.mysql
async def test_two_turn_real_services_preserve_people_and_capture_quote(mysql_settings):
    task = Task(
        id="DEV-999",
        title="真实服务两轮测试",
        split="development",
        family="goal_update",
        origin="authored",
        source_note="测试替身，不作为模型成绩",
        turns=[
            turn(state=[{"path": "requirements.people", "value": 2}]).model_dump(),
            turn(
                proposal={
                    "mode": "present",
                    "voucher_id": 3001,
                    "price_fen": 15800,
                    "verdict": "unknown",
                }
            ).model_dump(),
        ],
    )
    replies = iter(
        [
            [
                call(
                    "update_goal",
                    {"changes": [{"field": "people", "value": 2, "strength": "required"}]},
                ),
                call("finish", {"answer": "已记录两人，先探索。", "display_ids": []}),
            ],
            [
                call("get_group_buy_detail", {"voucher_id": 3001}),
                call("prepare_purchase", {"voucher_id": 3001}),
                call(
                    "finish", {"answer": "已准备真实报价，未知条件请核对。", "display_ids": [3001]}
                ),
            ],
        ]
    )
    async with isolated_database(mysql_settings) as database:
        result = await execute_task(
            database, mysql_settings, task, date.today(), lambda: ScriptedModel(next(replies))
        )
    assert result["automatic_pass"] and result["business_invariants"]["zero_transactions"]
    assert result["turns"][1]["state_after"]["purchase_proposal"]["requirements"]["people"] == 2
    assert result["turns"][1]["tool_trace"][-1]["tool"] == "finish"
