"""版本化Agent数据集执行器；默认预览，--live隔离运行，--judge启用模型辅助复核。"""

import argparse
import asyncio
import json
import logging
import platform
import secrets
import sys
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from agent.model import NativeToolModel
from agent.schemas import ConversationState
from agent.workflows.selection import run_selection
from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.errors import ApiError
from backend.app.models import Voucher, VoucherUsage
from backend.app.schemas.group_buys import UsageRule
from scripts.agent_eval_checks import evaluate_checks, summarize
from scripts.agent_eval_dataset import DEFAULT_DATASET, ROOT, Task, digest, load_dataset, render
from scripts.agent_eval_judge import judge_turn
from scripts.run_backend_tests import provision
from scripts.seed_demo_catalog import load_catalog
from scripts.seed_demo_catalog import seed as seed_catalog
from scripts.seed_group_buy_rules import load_entries
from scripts.seed_group_buy_rules import seed as seed_rules

TIANJIN = ZoneInfo("Asia/Shanghai")
REPORT_DIR = ROOT / "docs/五、团购选购助手/验证资料"


class TracedModel:
    def __init__(self, delegate):
        self.delegate, self.calls, self.usages, self.requests = delegate, 0, [], []

    async def complete(self, messages, tools):
        self.calls += 1
        self.usages.append(None)
        reply = await self.delegate.complete(messages, tools)
        self.usages[-1] = reply.usage.model_dump() if reply.usage else None
        self.requests.extend(
            {"tool": call.function.name, "call_id": call.id} for call in reply.tool_calls
        )
        return reply


def token_totals(usages):
    # 缺测不是0；只有每次成功响应都有该项时才给出完整总量。
    return {
        key: (
            sum(row[key] for row in usages)
            if usages and all(row and type(row.get(key)) is int for row in usages)
            else None
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def safe_error(error):
    return error.code if isinstance(error, ApiError) else type(error).__name__


def safe_database_name(name):
    if not name.startswith("perkflow_test_agent_dataset_") or not name.isidentifier():
        raise ValueError("只允许本执行器随机生成的测试库")
    return name


@asynccontextmanager
async def isolated_database(local):
    # 1. 库名不接受命令行/数据集传入；创建失败、种子失败或模型错误都走清理。
    name = safe_database_name("perkflow_test_agent_dataset_" + secrets.token_hex(6))
    isolated = local.model_copy(update={"mysql_database": name})
    admin = create_async_engine(
        local.database_url.set(database=None),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        connect_args={"connect_timeout": local.mysql_connect_timeout},
    )
    database, created = Database(isolated), False
    try:
        async with admin.connect() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            )
            created = True
        await provision(isolated)
        await seed_catalog(database, load_catalog(), apply=True)
        await seed_rules(database, load_entries(), apply=True)
        yield database
    finally:
        try:
            await database.close()
        finally:
            try:
                if created:
                    safe_database_name(name)
                    async with admin.connect() as connection:
                        await connection.execute(text(f"DROP DATABASE `{name}`"))
            finally:
                await admin.dispose()


async def business_facts(database):
    async with database.engine.connect() as connection:
        counts = {
            table: await connection.scalar(text(f"SELECT COUNT(*) FROM {table}"))
            for table in ("tb_voucher_order", "tb_order_request", "tb_agent_purchase")
        }
        stocks = dict((await connection.execute(select(Voucher.id, Voucher.stock))).all())
    return {"counts": counts, "stocks": {str(k): v for k, v in stocks.items()}}


async def apply_events(database, events, history, task_id, expected_stocks):
    # 2. 仅在隔离SQL执行白名单事件。库存只在tb_voucher；活动表存时间，不伪造活动库存列。
    async with database.sessions() as session, session.begin():
        for event in events:
            voucher = await session.get(Voucher, event.voucher_id)
            if voucher is None:
                raise ValueError("事件商品不存在")
            if event.kind == "set_stock":
                if event.value > voucher.total_stock:
                    raise ValueError("评测库存超过种子总库存")
                voucher.stock = event.value
                expected_stocks[str(voucher.id)] = event.value
            elif event.kind == "set_price":
                if event.value > voucher.actual_value:
                    raise ValueError("评测售价不能超过面额，不能用非法夹具冒充Agent失败")
                voucher.pay_value = event.value
            elif event.kind == "patch_usage":
                row = await session.get(VoucherUsage, voucher.id)
                original = row.spec if row else {}
                spec = UsageRule.model_validate({**original, **event.value}).model_dump(mode="json")
                if row:
                    row.spec = spec
                else:
                    session.add(VoucherUsage(voucher_id=voucher.id, spec=spec))
            else:
                # 明确的合成事实，与生产purchase_context使用相同的摘要字段，不写SQL订单。
                value = event.value
                alias = value.get("request_id") or f"unknown-{voucher.id}"
                identifier = str(uuid5(NAMESPACE_URL, f"eval:{task_id}:{alias}"))
                numeric_id = str(10**15 + int(uuid5(NAMESPACE_URL, alias).hex[:12], 16))
                lookup = value.get("lookup") != "not_found"
                history[identifier] = {
                    "id": identifier,
                    "conversation_id": "fictional-" + task_id,
                    "in_current_conversation": True,
                    "status": value["status"],
                    "request_id": numeric_id if lookup else None,
                    "order_id": numeric_id if value.get("order_id") else None,
                    "order_status": value.get("order_status"),
                    "voucher_id": voucher.id,
                    "title": voucher.title,
                    "pay_value": voucher.pay_value,
                    "error_code": value.get("error_code"),
                    "lookup_available": lookup,
                    "lookup_source": "synthetic_fixture",
                    "needs_manual_review": not lookup,
                    "recovery_message": "原请求未找到，需人工核对；不能判失败或重买"
                    if not lookup
                    else None,
                }


async def execute_turn(database, settings, state, turn, history, delegate):
    # 3. 在副本执行，失败不提交部分条件，保持与API仅保存成功轮次的语义一致。
    before, working = state.model_copy(deep=True), state.model_copy(deep=True)
    trace, events, model = [], [], TracedModel(delegate)

    async def emit(event):
        value = event.model_dump(mode="json")
        events.append(value)
        working.events = (working.events + [event])[-40:]

    async def observe(value):
        trace.append(deepcopy(value))

    async def purchases():
        return deepcopy(list(history.values()))

    started = time.perf_counter()
    error, answer, recommended = None, None, []
    try:
        answer, recommended = await run_selection(
            model,
            database,
            settings,
            working,
            turn.message,
            emit,
            purchase_reader=purchases,
            observe_tool=observe,
        )
        after = working
    except Exception as failure:
        error, after = safe_error(failure), before
    checks = evaluate_checks(turn, before, after, recommended, trace)
    result = {
        "input": turn.message,
        "error": error,
        "answer": answer,
        "automatic_pass": error is None and all(item["passed"] for item in checks),
        "checks": checks,
        "recommended_ids": recommended,
        "candidate_ids": after.candidate_ids,
        "model_calls": model.calls,
        "token_usage": token_totals(model.usages),
        "tool_requests": model.requests,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "tool_trace": trace,
        "events": events,
        "question_diagnostic": {
            "count": len(after.questions),
            "reference_max": turn.max_questions,
            "exceeded": len(after.questions) > turn.max_questions,
        },
        "state_before": before.model_dump(mode="json"),
        "state_after": after.model_dump(mode="json"),
        "purchase_history": deepcopy(list(history.values())),
        "judge": {"status": "disabled", "reviews": []},
        "human_review": {key: {"verdict": None, "evidence": None} for key in turn.review},
    }
    return after, result


def judge_packet(turn_result):
    before, after = turn_result["state_before"], turn_result["state_after"]
    return {
        "history_before": before["messages"],
        "input": turn_result["input"],
        "requirements_before": before["requirements"],
        "requirements_after": after["requirements"],
        "preferences_after": after["preferences"],
        "condition_details": after["condition_details"],
        "answer": turn_result["answer"],
        "cards": after["cards"],
        "proposal": after["purchase_proposal"],
        "tool_requests": turn_result["tool_requests"],
        "tool_trace": turn_result["tool_trace"],
        "purchase_history": turn_result["purchase_history"],
    }


async def execute_task(
    database,
    settings,
    task,
    anchor,
    model_factory,
    judge_settings=None,
    checkpoint=None,
    judge_model=None,
):
    task = Task.model_validate(render(task.model_dump(), anchor))
    state = ConversationState.model_validate(task.initial_state)
    initial = await business_facts(database)
    if any(initial["counts"].values()):
        raise ValueError("种子库不是无交易基线")
    expected_stocks, history = dict(initial["stocks"]), {}
    result = {
        "id": task.id,
        "title": task.title,
        "family": task.family,
        "automatic_pass": False,
        "complete": False,
        "turns": [],
    }
    for index, turn in enumerate(task.turns):
        await apply_events(database, turn.events_before, history, task.id, expected_stocks)
        state, observation = await execute_turn(
            database, settings, state, turn, history, model_factory()
        )
        observation["turn"] = index + 1
        if judge_settings is not None and observation["error"] is None:
            rubrics = json.loads((DEFAULT_DATASET / "rubrics.json").read_text(encoding="utf-8"))
            observation["judge"] = await judge_turn(
                judge_settings, judge_packet(observation), turn.review, rubrics, judge_model
            )
        elif judge_settings is not None:
            observation["judge"] = {"status": "not_applicable_runtime_error", "reviews": []}
        result["turns"].append(observation)
        if checkpoint:
            checkpoint(result)
    final = await business_facts(database)
    result["business_invariants"] = {
        "zero_transactions": not any(final["counts"].values()),
        "stock_matches_fixture_events": final["stocks"] == expected_stocks,
        "counts": final["counts"],
    }
    result["automatic_pass"] = (
        all(t["automatic_pass"] for t in result["turns"])
        and result["business_invariants"]["zero_transactions"]
        and result["business_invariants"]["stock_matches_fixture_events"]
    )
    result["complete"] = True
    return result


def source_metadata(settings, judge_settings):
    files = [p for folder in ("agent", "backend/app") for p in (ROOT / folder).rglob("*.py")]
    files += [
        ROOT / "scripts" / name
        for name in (
            "evaluate_agent_dataset.py",
            "agent_eval_dataset.py",
            "agent_eval_checks.py",
            "agent_eval_judge.py",
            "run_backend_tests.py",
            "seed_demo_catalog.py",
            "seed_group_buy_rules.py",
        )
    ]
    return {
        "python": platform.python_version(),
        "agent_model": settings.llm_model,
        "agent_limits": {"rounds": settings.agent_max_rounds, "timeout": settings.agent_timeout},
        "model_parameters": {
            k: settings.llm_extra_body[k]
            for k in ("temperature", "top_p", "seed", "max_tokens")
            if k in settings.llm_extra_body
        },
        "source_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p) for p in files},
        "manifest_sha256": digest(DEFAULT_DATASET / "manifest.json"),
        "judge_model": judge_settings.llm_model if judge_settings else None,
        "judge_same_model_as_agent": judge_settings.llm_model == settings.llm_model
        if judge_settings
        else None,
        "judge_limits": {
            "http_timeout": judge_settings.llm_timeout,
            "max_tokens": judge_settings.llm_extra_body.get("max_tokens", 1600),
        }
        if judge_settings
        else None,
    }


def save_report(report, path):
    # 4. 每轮保存JSON和可读摘要；未完/失败也留证据，不只保留最好结果。
    report["summary"] = summarize(report["tasks"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    lines = [
        "# Agent数据集评测报告",
        "",
        f"状态：{report['status']}；人工复核待完成。",
        "",
        "| 任务 | 自动检查 | 轮次 | 裁判通过/失败/不确定 |",
        "|---|---|---|---|",
    ]
    for task in report["tasks"]:
        scores = summarize([task])["judge"]
        status = (
            "通过" if task.get("automatic_pass") else ("失败" if task.get("complete") else "未完成")
        )
        lines.append(
            f"| {task['id']} | {status} | {len(task['turns'])} | "
            f"{scores['pass']}/{scores['fail']}/{scores['uncertain']} |"
        )
    lines += ["", "确定性检查失败：", ""]
    for task in report["tasks"]:
        for turn in task["turns"]:
            if turn["error"]:
                lines.append(f"- {task['id']} 第{turn['turn']}轮：{turn['error']}")
            for check in turn["checks"]:
                if not check["passed"]:
                    lines.append(
                        f"- {task['id']} 第{turn['turn']}轮：{check['check']}；"
                        f"预期 {check['expected']}；实际 {check['actual']}"
                    )
    lines += [
        "",
        "完整事实、公开工具轨迹、Token及逐项裁判意见见同名JSON。",
        "模型意见未经人工确认，不能覆盖业务断言；零订单不代表自然语言没有越权引导。",
        "本次仅运行列出的任务，不代表全部数据集、生产泛化率或性能压测。",
        "",
    ]
    lines += ["## 逐轮结果与模型辅助意见", ""]
    for task in report["tasks"]:
        for turn in task["turns"]:
            lines += [
                f"### {task['id']} 第{turn['turn']}轮",
                "",
                "用户：" + turn.get("input", ""),
                "",
                "回答：" + (turn.get("answer") or "本轮未完成"),
                "",
                f"模型调用{turn.get('model_calls', 0)}次；"
                f"耗时{turn.get('elapsed_seconds')}秒；人工复核待完成。",
                "",
            ]
            judge = turn.get("judge", {})
            if judge.get("status") == "error":
                lines.append("- 裁判未完成：" + judge["error"])
            for item in judge.get("reviews", []):
                lines.append(f"- {item['criterion']}：{item['verdict']}；{item.get('reason', '')}")
                if item.get("evidence_path"):
                    lines.append(
                        f"  证据 {item['evidence_path']}：{item.get('evidence_quote', '')}"
                    )
            lines.append("")
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


async def evaluate(tasks, output, settings, judge_settings=None):
    started = datetime.now(UTC)
    anchor = started.astimezone(TIANJIN).date()
    manifest = json.loads((DEFAULT_DATASET / "manifest.json").read_text(encoding="utf-8"))
    report = {
        "report_version": 1,
        "dataset_id": manifest["dataset_id"],
        "status": "running",
        "started_at": started.isoformat(),
        "date_anchor": anchor.isoformat(),
        "selected_task_ids": [task.id for task in tasks],
        "tasks": [],
        "isolation": "fresh random SQL database per task; no Redis/MQ or purchase submission",
        "history_source": "synthetic fixtures; not real transactions or MQ faults",
        "metadata": source_metadata(settings, judge_settings),
    }
    save_report(report, output)
    try:
        for task in tasks:
            if datetime.now(TIANJIN).date() != anchor:
                raise ValueError("运行跨北京时间午夜，日期案例不可直接比较")

            def checkpoint(current):
                rows = [row for row in report["tasks"] if row["id"] != current["id"]]
                report["tasks"] = rows + [current]
                save_report(report, output)

            async with isolated_database(settings) as database:
                logging.getLogger("perkflow.agent").disabled = False
                result = await execute_task(
                    database,
                    settings,
                    task,
                    anchor,
                    lambda: NativeToolModel(settings),
                    judge_settings,
                    checkpoint,
                )
            checkpoint(result)
            if datetime.now(TIANJIN).date() != anchor:
                raise ValueError("运行跨北京时间午夜，日期案例不可直接比较")
            print(
                f"{task.id}: {'PASS' if result['automatic_pass'] else 'FAIL'}; "
                f"turns={len(result['turns'])}",
                flush=True,
            )
        report["status"] = "completed"
    except asyncio.CancelledError:
        report["status"] = "interrupted"
        raise
    except Exception as error:
        report.update(status="infrastructure_error", error=safe_error(error))
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        save_report(report, output)
    return report


async def rejudge(source, output, settings, task_ids=None):
    # 5. 只复核本执行器完整虚构报告，不重跑Agent、不触碰数据库，也不改原报告。
    report = json.loads(source.read_text(encoding="utf-8"))
    manifest, tasks = load_dataset()
    if (
        report.get("report_version") != 1
        or report.get("dataset_id") != manifest["dataset_id"]
        or report.get("status") != "completed"
        or report.get("isolation")
        != "fresh random SQL database per task; no Redis/MQ or purchase submission"
    ):
        raise ValueError("只支持本版本完整虚构评测报告")
    lookup = {task.id: task for task in tasks}
    selected = set(task_ids or [task["id"] for task in report["tasks"]])
    if not selected <= {task["id"] for task in report["tasks"]}:
        raise ValueError("所选任务不在原报告内")
    report["tasks"] = [task for task in report["tasks"] if task["id"] in selected]
    report.update(status="rejudging", selected_task_ids=[task["id"] for task in report["tasks"]])
    report["rejudge"] = {
        "source_report_sha256": digest(source),
        "started_at": datetime.now(UTC).isoformat(),
        "judge_model": settings.llm_model,
        "judge_same_model_as_agent": settings.llm_model == report["metadata"]["agent_model"],
        "judge_max_tokens": settings.llm_extra_body.get("max_tokens"),
        "judge_http_timeout": settings.llm_timeout,
        "judge_source_sha256": digest(ROOT / "scripts/agent_eval_judge.py"),
        "agent_rerun": False,
    }
    rubrics = json.loads((DEFAULT_DATASET / "rubrics.json").read_text(encoding="utf-8"))
    save_report(report, output)
    for task in report["tasks"]:
        specification = lookup[task["id"]]
        if not task.get("complete") or len(task["turns"]) != len(specification.turns):
            raise ValueError("任务轮次不完整")
        for index, turn in enumerate(task["turns"]):
            if turn["error"] is None:
                turn["judge"] = await judge_turn(
                    settings, judge_packet(turn), specification.turns[index].review, rubrics
                )
            save_report(report, output)
            print(f"{task['id']} turn={index + 1}: judge={turn['judge']['status']}", flush=True)
    report["status"] = "completed"
    report["rejudge"]["finished_at"] = datetime.now(UTC).isoformat()
    save_report(report, output)
    return report


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--split", choices=["development", "reserved"], default="development")
    parser.add_argument("--task", action="append", dest="task_ids")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--judge-model", help="复用本地同一服务凭据的裁判模型名；不传则同模型辅助")
    parser.add_argument("--judge-max-tokens", type=int, default=6144)
    parser.add_argument("--judge-timeout", type=int, default=60)
    parser.add_argument("--judge-report", type=Path, help="只重评已有完整报告，不重跑Agent")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest, all_tasks = load_dataset()
    tasks = [task for task in all_tasks if task.split == args.split]
    if args.task_ids:
        unknown = set(args.task_ids) - {task.id for task in tasks}
        if unknown:
            parser.error("任务ID不属于当前split")
        tasks = [task for task in tasks if task.id in args.task_ids]
    if args.judge_model and not args.judge:
        parser.error("--judge-model需与--judge一起使用")
    if args.judge_report and not args.judge:
        parser.error("--judge-report需与--judge一起使用")
    if not 512 <= args.judge_max_tokens <= 8192 or not 1 <= args.judge_timeout <= 60:
        parser.error("裁判Token范围512～8192，HTTP超时1～60秒")
    rejudge_ids = None
    if args.judge_report:
        try:
            saved = json.loads(args.judge_report.read_text(encoding="utf-8"))
            source_ids = {task["id"] for task in saved["tasks"]}
            rejudge_ids = args.task_ids or sorted(source_ids)
        except (OSError, ValueError, KeyError, TypeError):
            parser.error("原报告格式无法读取")
        if not set(rejudge_ids) <= {task.id for task in tasks}:
            parser.error("原报告任务不属于所选split")
        if not set(rejudge_ids) <= source_ids:
            parser.error("所选任务不在原报告内")
    if not args.live:
        if args.judge_report:
            print(
                json.dumps(
                    {
                        "status": "preview_not_executed",
                        "mode": "rejudge_only",
                        "tasks": rejudge_ids,
                        "agent_rerun": False,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "status": "preview_not_executed",
                    "dataset": manifest["dataset_id"],
                    "tasks": [task.id for task in tasks],
                    "turns": sum(len(task.turns) for task in tasks),
                    "judge": args.judge,
                },
                ensure_ascii=False,
            )
        )
        return 0
    output = args.output or REPORT_DIR / (
        "数据集评测-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(3)
        + ".json"
    )
    if output.suffix != ".json" or output.exists() or output.with_suffix(".md").exists():
        parser.error("输出需为新的.json路径，不覆盖既有报告")
    try:
        settings = Settings()
        NativeToolModel(settings).ensure_configured()
        judge_settings = (
            settings.model_copy(
                update={
                    "llm_model": args.judge_model or settings.llm_model,
                    "llm_timeout": args.judge_timeout,
                    "agent_timeout": args.judge_timeout + 5,
                    "llm_extra_body": {
                        **settings.llm_extra_body,
                        "max_tokens": args.judge_max_tokens,
                    },
                }
            )
            if args.judge
            else None
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.touch(exist_ok=False)
        output.write_text('{"status":"initializing"}', encoding="utf-8")
        report = asyncio.run(
            rejudge(args.judge_report, output, judge_settings, rejudge_ids)
            if args.judge_report
            else evaluate(tasks, output, settings, judge_settings)
        )
        evaluated_count = len(report["tasks"])
        print(
            f"Report: {output}; status={report['status']}; "
            f"automatic={report['summary']['automatic_tasks_passed']}/{evaluated_count}",
            flush=True,
        )
        if report["status"] != "completed":
            return 2
        if report["summary"]["automatic_tasks_passed"] != evaluated_count:
            return 1
        if (
            report["summary"]["judge_errors"]
            or report["summary"]["judge"]["fail"]
            or report["summary"]["judge"]["uncertain"]
        ):
            return 1
        return 0
    except KeyboardInterrupt:
        print("Evaluation interrupted; completed observations remain in the report.")
        return 130
    except Exception as error:
        print("Evaluation unavailable:", safe_error(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
