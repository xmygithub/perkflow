"""评测端确定性断言与汇总；不修改被测状态，不由裁判覆盖业务失败。"""

from datetime import date, time

from pydantic import TypeAdapter

from agent.schemas import Preferences
from backend.app.schemas.group_buys import Requirements
from scripts.agent_eval_dataset import Turn


def normalize(value):
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def field_value(state, path):
    group, field = path.split(".")
    return normalize(getattr(getattr(state, group), field))


def expected_value(path, value):
    group, field = path.split(".")
    contract = Requirements if group == "requirements" else Preferences
    return normalize(
        TypeAdapter(contract.model_fields[field].rebuild_annotation()).validate_python(value)
    )


def evaluate_checks(turn: Turn, before, after, recommended, trace):
    # 1. 所有检查只读取实际状态；preserved也使用真实上一轮，不能把gold回填给Agent。
    results = []

    def add(name, expected, actual, passed):
        results.append(
            {"check": name, "expected": expected, "actual": actual, "passed": bool(passed)}
        )

    for check in turn.state:
        actual = field_value(after, check.path)
        if check.op == "preserved":
            expected = field_value(before, check.path)
            passed = actual == expected
        elif check.op == "one_of":
            expected = [expected_value(check.path, item) for item in check.value]
            passed = actual in expected
        else:
            expected = expected_value(check.path, check.value)
            passed = actual == expected
        add("state:" + check.path + ":" + check.op, expected, actual, passed)

    # 2. 可选集合不包含解释用的淘汰卡片；核验等级仍取实际Service卡片。
    selected = set(recommended) | set(after.candidate_ids)
    verdicts = {card.voucher.voucher_id: card.verdict for card in after.cards}
    need = turn.selection
    add(
        "selection:count",
        [need.minimum_count, need.maximum_count],
        len(selected),
        need.minimum_count <= len(selected) <= need.maximum_count,
    )
    for identifier in need.include:
        add(f"selection:include:{identifier}", True, identifier in selected, identifier in selected)
    for identifier in need.exclude:
        add(
            f"selection:exclude:{identifier}",
            False,
            identifier in selected,
            identifier not in selected,
        )
    for identifier in need.not_eligible:
        actual = identifier in recommended or verdicts.get(identifier) == "eligible"
        add(f"selection:not_eligible:{identifier}", False, actual, not actual)
    for identifier in need.eligible:
        actual = identifier in selected and verdicts.get(identifier) == "eligible"
        add(f"selection:eligible:{identifier}", True, actual, actual)

    proposal = after.purchase_proposal
    if turn.proposal.mode != "optional":
        expected = turn.proposal.mode == "present"
        add("proposal:exists", expected, proposal is not None, (proposal is not None) == expected)
    if turn.proposal.mode == "present" and proposal is not None:
        actuals = {
            "voucher_id": proposal.evaluation.voucher.voucher_id,
            "price_fen": proposal.evaluation.voucher.pay_value,
            "verdict": proposal.evaluation.verdict,
        }
        for field, actual in actuals.items():
            expected = getattr(turn.proposal, field)
            if expected is not None:
                add("proposal:" + field, expected, actual, expected == actual)
        # 报价必须使用同一有效目标，不能回复改了人数却报价仍用旧人数。
        add(
            "proposal:requirements_match",
            True,
            proposal.requirements == after.requirements,
            proposal.requirements == after.requirements,
        )
    if turn.observe_any:
        successful = {item["tool"] for item in trace if "error" not in item["result"]}
        add(
            "tools:observe_any",
            turn.observe_any,
            sorted(successful),
            bool(set(turn.observe_any) & successful),
        )
    return results


def summarize(tasks):
    # 3. 任务级/轮次级和裁判结果分开统计，缺测保持null，不虚构统一加权总分。
    turns = [turn for task in tasks for turn in task.get("turns", [])]
    checks = [item for turn in turns for item in turn.get("checks", [])]
    state_checks = [item for item in checks if item["check"].startswith("state:")]
    judgments = [item for turn in turns for item in turn.get("judge", {}).get("reviews", [])]
    return {
        "tasks_recorded": len(tasks),
        "tasks_completed": sum(task.get("complete") is True for task in tasks),
        "automatic_tasks_passed": sum(task.get("automatic_pass") is True for task in tasks),
        "automatic_task_pass_rate": (
            sum(task.get("automatic_pass") is True for task in tasks) / len(tasks)
            if tasks
            else None
        ),
        "turns_executed": len(turns),
        "state_checks": len(state_checks),
        "state_checks_passed": sum(item["passed"] for item in state_checks),
        "runtime_errors": sum(turn.get("error") is not None for turn in turns),
        "model_calls": sum(turn.get("model_calls", 0) for turn in turns),
        "tool_requests": sum(len(turn.get("tool_requests", [])) for turn in turns),
        "tools_executed": sum(len(turn.get("tool_trace", [])) for turn in turns),
        "tool_errors": sum(
            "error" in item["result"] for turn in turns for item in turn.get("tool_trace", [])
        ),
        "invalid_arguments": sum(
            item["result"].get("error") == "INVALID_ARGUMENTS"
            for turn in turns
            for item in turn.get("tool_trace", [])
        ),
        "repeated_action_rejections": sum(
            item["result"].get("error") == "REPEATED_ACTION"
            for turn in turns
            for item in turn.get("tool_trace", [])
        ),
        "observations_reused": sum(
            bool(item["result"].get("observation", {}).get("snapshot_reused"))
            for turn in turns
            for item in turn.get("tool_trace", [])
        ),
        "judge": {
            status: sum(item["verdict"] == status for item in judgments)
            for status in ("pass", "fail", "uncertain")
        },
        "judge_errors": sum(turn.get("judge", {}).get("status") == "error" for turn in turns),
        "human_review": "pending",
        "safety_review": "requires_model_assisted_and_human_review; not inferred_from_zero_orders",
    }
