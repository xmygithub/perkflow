"""反馈驱动的真实多轮验收；默认预览，--live仅在独立虚构SQL库调用学校模型。"""

import argparse
import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent.model import NativeToolModel
from agent.schemas import ConversationState
from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.schemas.group_buys import Requirements
from scripts.evaluate_group_buy_agent import run_case
from scripts.run_backend_tests import provision
from scripts.seed_demo_catalog import load_catalog
from scripts.seed_demo_catalog import seed as seed_catalog
from scripts.seed_group_buy_rules import load_entries
from scripts.seed_group_buy_rules import seed as seed_rules

CASES = {
    "vague_start",
    "budget_conflict",
    "user_changes_budget",
    "merchant_unknown",
    "failed_purchase_alternative",
    "pending_is_not_failure",
}


async def evaluate(output, selected_cases=None):
    local = Settings()
    NativeToolModel(local).ensure_configured()
    database_name = "perkflow_test_agent_adapt_" + secrets.token_hex(6)
    settings = local.model_copy(update={"mysql_database": database_name})
    db = Database(settings)
    admin = create_async_engine(
        local.database_url.set(database=None), isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )
    created = False
    report = {
        "complete": False,
        "model": local.llm_model,
        "cases": [],
        "isolation": "random fictional MySQL; no development orders; no Redis/MQ writes",
        "purchase_history_source": "fictional failed/pending fixtures; not actual MQ failures",
    }
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    state = ConversationState()

    async def case(name, message, check, current=state, reader=None):
        if selected_cases and name not in selected_cases:
            return
        result = await run_case(db, settings, current, name, message, check, reader)
        report["cases"].append(result)
        # 每轮留证据，后续模型失败不抹去前面结果；只保存虚构输入/公开工具观察。
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        async with admin.connect() as c:
            await c.execute(
                text(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
        created = True
        await provision(settings)
        await seed_catalog(db, load_catalog(), apply=True)
        await seed_rules(db, load_entries(), apply=True)
        await case(
            "vague_start",
            "帮我找个火锅团购，划算一点",
            lambda s, a, ids: {
                "actually_searched": any(e.tool == "search_group_buys" for e in s.events),
                "has_candidates": bool(s.cards),
                "no_invented_numbers": s.requirements.budget_fen is None
                and s.requirements.people is None,
                "limited_questions": len(s.questions) <= 1,
            },
        )
        await case(
            "budget_conflict",
            "就南开区，总预算最多50元，其他不变。没有就告诉我卡在哪，别调预算。",
            lambda s, a, ids: {
                "hard_conditions": s.requirements.area in {"南开区", "南开街区"}
                and s.requirements.budget_fen == 5000,
                "no_out_of_budget_selection": not ids and not s.candidate_ids,
                "observed_zero_result": any(e.outcome_code == "NO_MATCH" for e in s.events),
                "not_asking_irrelevant_details": not any(
                    word in q for q in s.questions for word in ("几个人", "人数", "几点", "预约")
                ),
            },
        )
        await case(
            "user_changes_budget",
            f"那预算改成200元，{saturday}晚上18点两个人去，不接受预约，区域和火锅不变。",
            lambda s, a, ids: {
                "preserved_area_category": s.requirements.area in {"南开区", "南开街区"}
                and s.requirements.category == "火锅",
                "updated_requirements": s.requirements.budget_fen == 20000
                and s.requirements.people == 2
                and s.requirements.allow_reservation is False,
                "weekend_eligible_choice": 3001 in ids + s.candidate_ids,
                "weekday_coupon_not_selected": 3002 not in ids + s.candidate_ids,
            },
        )
        await case(
            "merchant_unknown",
            "暖锅里99元待核对体验套餐能用吗？资料不全就看看别的，不要再问我已经说过的人数预算。",
            lambda s, a, ids: {
                "unknown_not_confirmed": 3005 not in ids,
                "missing_rules_explained": any(
                    word in a for word in ("未明确", "未知", "未补全", "资料", "待核对", "无法确认")
                ),
                "no_reasking_known_fields": not s.questions,
                "no_repeat_guard_hit": not any(
                    e.outcome_code == "REPEATED_ACTION" for e in s.events
                ),
            },
        )
        monday = saturday + timedelta(days=2)
        failed = ConversationState(
            requirements=Requirements(
                area="南开区",
                category="火锅",
                people=2,
                budget_fen=20000,
                use_date=monday,
                use_time="18:00",
                allow_reservation=False,
            )
        )
        # 仅隔离库模拟库存变动；购买失败来自明确标注的历史替身，不冒充真实MQ故障。
        async with db.engine.begin() as c:
            await c.execute(text("UPDATE tb_voucher SET stock=0 WHERE id=3001"))

        async def failure_reader():
            return [
                {
                    "id": "fictional-failed",
                    "status": "failed",
                    "error_code": "OUT_OF_STOCK",
                    "voucher_id": 3001,
                    "title": "暖锅里双人围炉套餐",
                    "in_current_conversation": True,
                }
            ]

        original_failure_needs = failed.requirements.model_copy(deep=True)
        await case(
            "failed_purchase_alternative",
            "原来的券明确买失败了，帮我看看其他符合条件的火锅券，先不要购买。",
            lambda s, a, ids: {
                "alternative_found": 3002 in ids + s.candidate_ids,
                "original_not_selected": 3001 not in ids + s.candidate_ids,
                "no_new_proposal": s.purchase_proposal is None,
                "constraints_kept": s.requirements == original_failure_needs,
            },
            failed,
            failure_reader,
        )
        async with db.engine.begin() as c:
            await c.execute(text("UPDATE tb_voucher SET stock=30 WHERE id=3001"))

        async def pending_reader():
            return [
                {
                    "id": "fictional-pending",
                    "status": "pending",
                    "voucher_id": 3001,
                    "title": "暖锅里双人围炉套餐",
                    "in_current_conversation": True,
                }
            ]

        await case(
            "pending_is_not_failure",
            "还在处理中？那就换一张帮我买吧。",
            lambda s, a, ids: {
                "no_replacement_proposal": s.purchase_proposal is None,
                "waiting_explained": any(
                    word in a for word in ("处理中", "未决", "等待", "结果", "确认")
                ),
                "no_new_search": not any(e.tool == "search_group_buys" for e in s.events),
                "no_replacement_preparation_attempt": not any(
                    e.tool == "prepare_purchase" for e in s.events
                ),
            },
            failed,
            pending_reader,
        )
        async with db.engine.connect() as c:
            report["order_count"] = await c.scalar(text("SELECT COUNT(*) FROM tb_voucher_order"))
            report["request_count"] = await c.scalar(text("SELECT COUNT(*) FROM tb_order_request"))
            report["authorization_count"] = await c.scalar(
                text("SELECT COUNT(*) FROM tb_agent_purchase")
            )
            report["stock_3001"] = await c.scalar(
                text("SELECT stock FROM tb_voucher WHERE id=3001")
            )
        report["business_unchanged"] = (
            report["order_count"] == report["request_count"] == report["authorization_count"] == 0
            and report["stock_3001"] == 30
        )
    except Exception as error:
        report["error"] = type(error).__name__  # 不打印包含本地配置的异常正文。
    finally:
        await db.close()
        if created:
            assert (
                database_name.startswith("perkflow_test_agent_adapt_")
                and database_name.isidentifier()
            )
            async with admin.connect() as c:
                await c.execute(text(f"DROP DATABASE `{database_name}`"))
        await admin.dispose()
        report.update(complete=True, cleaned=True, generated_at=datetime.now(UTC).isoformat())
        report["passed"] = sum(c.get("passed", False) for c in report["cases"])
        report["total"] = len(report["cases"])
        report["limitations"] = (
            "Small live smoke set; text checks need manual review; no fixed-workflow comparison, "
            "generalization/performance rate or production guarantee."
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="实际调用本地配置模型，可能产生费用")
    parser.add_argument("--case", action="append", dest="cases", choices=sorted(CASES))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/五、团购选购助手/验证资料/反馈驱动动态调整验收.json"),
    )
    args = parser.parse_args()
    selected = set(args.cases or [])
    sequence = ["vague_start", "budget_conflict", "user_changes_budget", "merchant_unknown"]
    for index, name in enumerate(sequence):
        if name in selected and not set(sequence[:index]) <= selected:
            parser.error(f"{name}需要同时选择前置轮次：{', '.join(sequence[:index])}")
    if not args.live:
        print(
            "Preview only: 6 fictional dialogue cases; --live calls the configured model, "
            "never confirms a purchase."
        )
        return 0
    try:
        report = asyncio.run(evaluate(args.output, selected))
        passed = (
            report["total"] == report["passed"] == (len(selected) if selected else 6)
            and report.get("business_unchanged")
            and not report.get("error")
        )
        print(
            "Live adaptation:",
            report["passed"],
            "/",
            report["total"],
            "cleaned:",
            report["cleaned"],
        )
        return 0 if passed else 1
    except Exception as error:
        print("Evaluation failed:", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
