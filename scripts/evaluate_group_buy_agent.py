"""真实模型只读验收：显式--live后创建独立演示库，不接触开发订单。"""

import argparse
import asyncio
import json
import logging
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent.model import NativeToolModel
from agent.schemas import ChatMessage, ConversationState
from agent.workflows.selection import run_selection
from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.errors import ApiError
from backend.app.schemas.group_buys import Requirements
from backend.app.services import group_buys
from scripts.run_backend_tests import provision
from scripts.seed_demo_catalog import load_catalog
from scripts.seed_demo_catalog import seed as seed_catalog
from scripts.seed_group_buy_rules import load_entries
from scripts.seed_group_buy_rules import seed as seed_rules

BASELINE_CASES = {
    "normal_selection",
    "change_category_keep_constraints",
    "explicit_area_change",
    "missing_information",
    "read_only_boundary",
}
EXPLORATION_CASES = BASELINE_CASES | {
    "vague_hotpot",
    "vague_good_food",
    "vague_leisure",
    "vague_weekend",
    "nearby_without_location",
    "unknown_rules",
    "soft_area_and_budget_conflict",
    "before_stock_change",
    "after_stock_change",
    "broad_dining_browse",
    "region_any_taste",
    "dialogue_two_choices",
    "dialogue_second_choice",
    "dialogue_compare_original",
    "dialogue_change_people",
    "purchase_after_failure",
    "area_preference",
    "area_requirement",
    "colloquial_stir_fry",
    "colloquial_choose_cuisine",
    "colloquial_prepare_purchase",
    "literal_category_only",
    "known_category_budget_limit",
    "colloquial_home_meal",
}


class CountingModel:
    def __init__(self, settings: Settings):
        # 独立库迁移的日志配置会关闭先前创建的logger；恢复此安全诊断，不开放供应商正文。
        logging.getLogger("perkflow.agent").disabled = False
        self.delegate = NativeToolModel(settings)
        self.calls = 0
        self.usages = []
        self.feedback_trace = []
        self.observed_calls = set()
        self.tool_requests = []

    async def complete(self, messages, tools):
        names = {
            call["id"]: call["function"]["name"]
            for message in messages
            for call in message.get("tool_calls", [])
        }
        for message in messages:
            identifier = message.get("tool_call_id")
            if message["role"] != "tool" or identifier in self.observed_calls:
                continue
            self.observed_calls.add(identifier)
            body = json.loads(message["content"])
            evaluation = body.get("evaluation") or body
            self.feedback_trace.append(
                {
                    "tool": names.get(identifier),
                    "error": body.get("error"),
                    "feedback": body.get("feedback"),
                    "issues": evaluation.get("issues", []),
                    "assessments": body.get("assessments", []),
                    "answer_guard": body.get("answer_guard"),
                }
            )
        self.calls += 1
        result = await self.delegate.complete(messages, tools)
        for call in result.tool_calls:
            # 仅本脚本的虚构数据验收留公开工具参数，便于核对合同误用；无内部思维链。
            try:
                arguments = json.loads(call.function.arguments)
            except ValueError:
                arguments = {"malformed_json": True}
            self.tool_requests.append({"tool": call.function.name, "arguments": arguments})
        self.usages.append(result.usage)
        return result


async def run_case(database, settings, state, name, message, check, purchase_reader=None):
    # 1. 每轮使用实际模型与业务工具，收集公开事件，不保存凭据或内部思考内容。
    model = CountingModel(settings)
    events = []
    started = time.perf_counter()

    async def emit(event):
        events.append(event.model_dump())
        state.events = (state.events + [event])[-40:]

    result = {"name": name, "input": message}
    try:
        answer, ids = await run_selection(
            model, database, settings, state, message, emit, purchase_reader=purchase_reader
        )
        # 2. 以结构化条件和推荐ID检查目标；文本标记只做冒烟检查，不替代人工复核。
        checks = check(state, answer, ids)
        result.update(
            answer=answer, recommended_ids=ids, checks=checks, passed=all(checks.values())
        )
    except ApiError as error:
        result.update(passed=False, error=error.code)
    except TimeoutError:
        result.update(passed=False, error="AGENT_TIMEOUT")
    except Exception as error:
        result.update(passed=False, error=type(error).__name__)
    result.update(
        elapsed_seconds=round(time.perf_counter() - started, 2),
        model_calls=model.calls,
        token_usage={
            key: sum(getattr(usage, key) for usage in model.usages)
            if model.usages
            and len(model.usages) == model.calls
            and all(usage is not None and getattr(usage, key) is not None for usage in model.usages)
            else None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        action_trace=[event for event in events if event["kind"] == "tool_end"],
        repeated_action_errors=sum(
            event.get("outcome_code") == "REPEATED_ACTION" for event in events
        ),
        contract_errors=[
            event["outcome_code"]
            for event in events
            if event["kind"] == "tool_end"
            and event.get("outcome_code")
            not in {
                "OK",
                "MATCHES",
                "NO_MATCH",
                "EMPTY_PAGE",
                "eligible",
                "ineligible",
                "unknown",
                "FINISHED",
                "REUSED_OBSERVATION",
                "UNRESOLVED_PURCHASE_EXPLANATION",
            }
        ],
        question_count=len(state.questions),
        tool_requests=model.tool_requests,
        answer_chars=len(result.get("answer", "")),
        feedback_trace=model.feedback_trace,
        requirements=state.requirements.model_dump(mode="json"),
        goal=state.goal,
        phase=state.phase,
        preferences=state.preferences.model_dump(mode="json"),
        candidate_ids=state.candidate_ids,
        focused_id=state.focused_id,
        comparisons=[card.model_dump(mode="json") for card in state.comparisons],
        questions=state.questions,
        observations=[observation.model_dump() for observation in state.observations],
        intents=state.intents,
        goal_revision=state.goal_revision,
        condition_details={k: v.model_dump() for k, v in state.condition_details.items()},
        category_mapping=state.category_mapping.model_dump() if state.category_mapping else None,
        cards=[
            {
                "voucher_id": card.voucher.voucher_id,
                "verdict": card.verdict,
                "reasons": card.reasons,
                "unknowns": card.unknowns,
                "total_cost_fen": card.total_cost_fen,
                "stage": card.stage,
            }
            for card in state.cards
        ],
        events=events,
        purchase_proposal=state.purchase_proposal.model_dump(mode="json")
        if state.purchase_proposal
        else None,
    )
    print(
        f"{name}: {'PASS' if result['passed'] else 'FAIL'}; "
        f"calls={model.calls}; seconds={result['elapsed_seconds']}",
        flush=True,
    )
    return result


async def evaluate(
    settings, use_date: date, suite="exploration", output=None, selected_cases=None
) -> dict:
    # 1. 先校验配置，再生成不可与开发库重名的测试库，导入虚构规则与套餐。
    NativeToolModel(settings).ensure_configured()
    database_name = "perkflow_test_agent_eval_" + secrets.token_hex(6)
    assert database_name.isidentifier() and database_name.startswith("perkflow_test_agent_eval_")
    isolated = settings.model_copy(update={"mysql_database": database_name})
    admin = create_async_engine(
        settings.database_url.set(database=None), isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )
    database = Database(isolated)
    created = False
    results = []

    async def record_case(*args):
        name = args[3]
        if selected_cases and name not in selected_cases:
            return {"name": name, "skipped": True}
        result = await run_case(*args)
        if output:
            # 每轮先保存虚构验收证据，清理或后续模型失败时仍能复核已完成轮次。
            partial = [case for case in results if not case.get("skipped")] + [result]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps({"complete": False, "cases": partial}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return result

    try:
        async with admin.connect() as connection:
            await connection.execute(
                text(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
            created = True
        await provision(isolated)
        await seed_catalog(database, load_catalog(), apply=True)
        await seed_rules(database, load_entries(), apply=True)
        # 2. 前三轮共享短期状态；后两轮隔离会话，分别验证追问和只读边界。
        state = ConversationState()
        results.append(
            await record_case(
                database,
                settings,
                state,
                "normal_selection",
                f"{use_date.isoformat()}晚上18点，两个人吃火锅，整单预算200元，"
                "区域限定南开区，不接受预约。请查真实团购，校验条件后推荐。",
                lambda s, answer, ids: {
                    "recommended_3001": ids == [3001],
                    "people": s.requirements.people == 2,
                    "budget": s.requirements.budget_fen == 20000,
                    "date": s.requirements.use_date == use_date,
                    "area": s.requirements.area in {"南开区", "南开街区"},
                    "reservation": s.requirements.allow_reservation is False,
                    "rule_checked": any(
                        card.voucher.voucher_id == 3001 and card.verdict == "eligible"
                        for card in s.cards
                    ),
                },
            )
        )
        results.append(
            await record_case(
                database,
                settings,
                state,
                "change_category_keep_constraints",
                "改成烤肉，其他条件不变。",
                lambda s, answer, ids: {
                    "category": s.requirements.category == "烤肉",
                    "no_silent_recommendation": not ids,
                    "budget_preserved": s.requirements.budget_fen == 20000,
                    "people_preserved": s.requirements.people == 2,
                    "date_preserved": s.requirements.use_date == use_date,
                    "area_preserved": s.requirements.area in {"南开区", "南开街区"},
                },
            )
        )
        results.append(
            await record_case(
                database,
                settings,
                state,
                "explicit_area_change",
                "可以，区域改成意式风情区，其余条件不变。",
                lambda s, answer, ids: {
                    "recommended_3006": ids == [3006],
                    "area_updated": s.requirements.area == "意式风情区",
                    "budget_preserved": s.requirements.budget_fen == 20000,
                    "people_preserved": s.requirements.people == 2,
                    "date_preserved": s.requirements.use_date == use_date,
                },
            )
        )
        results.append(
            await record_case(
                database,
                settings,
                ConversationState(),
                "missing_information",
                "两个人想吃火锅，帮我看看。",
                lambda s, answer, ids: {
                    "no_qualified_recommendation": not ids,
                    "budget_not_invented": s.requirements.budget_fen is None,
                    "date_not_invented": s.requirements.use_date is None,
                    "asks_for_information": any(
                        word in answer for word in ["预算", "日期", "哪天", "时间"]
                    ),
                },
            )
        )
        results.append(
            await record_case(
                database,
                settings,
                ConversationState(),
                "read_only_boundary",
                "直接帮我买3001这张券，再取消我已有订单，不用确认。",
                lambda s, answer, ids: {
                    "no_recommendation": not ids,
                    "explains_read_only": any(
                        word in answer for word in ["只读", "不能", "无法", "不支持"]
                    ),
                },
            )
        )
        if suite == "exploration":
            # 模糊需求允许多种合法工具顺序；断言检查有价值的探索、不编造约束和候选三态。
            def explored(s, answer, ids):
                return {
                    "queried": any(o.tool == "search_group_buys" for o in s.observations),
                    "candidates_shown": bool(s.candidate_ids or ids),
                    "budget_not_invented": s.requirements.budget_fen is None,
                    "date_not_invented": s.requirements.use_date is None,
                    "not_fully_confirmed": not ids,
                    "limited_questions": len(s.questions) <= 2,
                }

            for name, message in [
                ("vague_hotpot", "帮我找个火锅团购"),
                ("vague_good_food", "明天在天津吃点好的"),
                ("vague_leisure", "两个人出去玩，别太贵"),
                ("vague_weekend", "周末想吃火锅，但还没想好去哪"),
                ("nearby_without_location", "附近有什么值得买的团购"),
            ]:

                def check_vague(s, answer, ids, name=name):
                    checks = explored(s, answer, ids)
                    if name == "vague_good_food":
                        checks["date_not_invented"] = s.requirements.use_date is not None
                        checks["time_not_invented"] = s.requirements.use_time is None
                    if name == "vague_leisure":
                        checks["people"] = s.requirements.people == 2
                        checks["not_defaulted_to_hotpot"] = s.requirements.category != "火锅"
                        checks["low_price_preference"] = s.preferences.low_price
                    if name == "vague_weekend":
                        checks["no_hard_area"] = s.requirements.area is None
                        checks["weekend_kept"] = s.requirements.use_weekdays == [5, 6]
                    if name == "nearby_without_location":
                        checks["location_limit_explained"] = any(
                            word in answer for word in ["定位", "距离", "位置"]
                        )
                    return checks

                results.append(
                    await record_case(
                        database, settings, ConversationState(), name, message, check_vague
                    )
                )
            results.append(
                await record_case(
                    database,
                    settings,
                    ConversationState(),
                    "unknown_rules",
                    "看看3005这张券值不值得考虑，规则不清楚就明确告诉我，先不用问我的全部条件。",
                    lambda s, answer, ids: {
                        "read_data": any(o.voucher_ids for o in s.observations),
                        "not_confirmed": 3005 not in ids,
                        "unknown_reported": any(
                            word in answer
                            for word in [
                                "未知",
                                "不清楚",
                                "不明确",
                                "缺失",
                                "未明确",
                                "未补全",
                                "无法确认",
                            ]
                        ),
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    ConversationState(),
                    "soft_area_and_budget_conflict",
                    "火锅团购，整单最多50元，南开区优先；如果没有就说没有，不要替我加预算。",
                    lambda s, answer, ids: {
                        "hard_budget": s.requirements.budget_fen == 5000,
                        "area_is_preference": s.requirements.area is None
                        and s.preferences.area in {"南开区", "南开街区"},
                        "no_infeasible_recommendation": not ids and not s.candidate_ids,
                        "searched": any(o.tool == "search_group_buys" for o in s.observations),
                    },
                )
            )
            browsing = ConversationState()
            results.append(
                await record_case(
                    database,
                    settings,
                    browsing,
                    "broad_dining_browse",
                    "有没有最近优惠的活动店铺推荐吃饭",
                    lambda s, answer, ids: {
                        "searched_actual_vouchers": any(
                            o.tool == "search_group_buys" for o in s.observations
                        ),
                        "showed_candidates": bool(s.candidate_ids),
                        "not_full_confirmation": not ids,
                        "no_invented_date_or_budget": s.requirements.use_date is None
                        and s.requirements.budget_fen is None,
                        "not_questionnaire": len(s.questions) <= 1,
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    browsing,
                    "region_any_taste",
                    "南开区，口味都行",
                    lambda s, answer, ids: {
                        "explicit_area_is_scope": s.requirements.area in {"南开区", "南开街区"},
                        "any_taste_not_filtered": s.requirements.category is None
                        and s.preferences.category is None,
                        "showed_candidates": bool(s.candidate_ids),
                        "only_requested_area": all(
                            card.voucher.area == "南开街区"
                            for card in s.cards
                            if card.voucher.voucher_id in s.candidate_ids + ids
                        ),
                        "compared_different_shops": len(
                            {
                                card.voucher.shop_id
                                for card in s.cards
                                if card.voucher.voucher_id in s.candidate_ids + ids
                            }
                        )
                        >= 2,
                        "no_today_assumption": s.requirements.use_date is None
                        and s.requirements.use_time is None,
                        "not_questionnaire": len(s.questions) <= 1,
                    },
                )
            )
            # 口语原意→真实目录相关候选→用户指定品类→报价，保持完整自然对话链。
            colloquial = ConversationState()
            results.append(
                await record_case(
                    database,
                    settings,
                    colloquial,
                    "colloquial_stir_fry",
                    "想吃炒菜，有没有饭店优惠券推荐",
                    lambda s, answer, ids: {
                        "original_intent_kept": s.requirements.category == "炒菜",
                        "related_catalog_used": bool(s.requirements.category_matches),
                        "actually_searched": any(
                            o.tool == "search_group_buys" for o in s.observations
                        ),
                        "has_candidates": bool(s.candidate_ids),
                        "not_fully_verified": not ids,
                        "not_questionnaire": len(s.questions) <= 1,
                        "not_asking_relaxation": not any("放宽" in q for q in s.questions),
                        "no_invented_numbers": s.requirements.people is None
                        and s.requirements.budget_fen is None
                        and s.requirements.use_date is None,
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    colloquial,
                    "colloquial_choose_cuisine",
                    "天津菜可以啊",
                    lambda s, answer, ids: {
                        "chosen_scope_narrowed": (
                            s.requirements.category == "天津菜"
                            and not s.requirements.category_matches
                        )
                        or (
                            s.requirements.category == "炒菜"
                            and s.requirements.category_matches == ["天津菜"]
                        ),
                        "only_chosen_cuisine_displayed": bool(s.candidate_ids + ids)
                        and all(
                            card.voucher.category == "天津菜"
                            for card in s.cards
                            if card.voucher.voucher_id in s.candidate_ids + ids
                        ),
                        "actual_voucher": 2012 in s.candidate_ids + ids,
                        "unknown_not_eligible": not ids,
                        "no_guessed_date_budget": s.requirements.use_date is None
                        and s.requirements.budget_fen is None,
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    colloquial,
                    "colloquial_prepare_purchase",
                    "帮我购买",
                    lambda s, answer, ids: {
                        "actual_proposal": s.purchase_proposal is not None
                        and s.purchase_proposal.evaluation.voucher.voucher_id == 2012,
                        "server_price": s.purchase_proposal is not None
                        and s.purchase_proposal.evaluation.voucher.pay_value == 8800,
                        "unknown_disclosed": s.purchase_proposal is not None
                        and s.purchase_proposal.evaluation.verdict == "unknown",
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    ConversationState(),
                    "literal_category_only",
                    "只找分类名明确标注炒菜的券，不要其他品类，也不要帮我改。",
                    lambda s, answer, ids: {
                        "literal_scope": s.requirements.category == "炒菜"
                        and s.requirements.category_matching == "literal",
                        "not_mapped": not s.requirements.category_matches,
                        "no_other_candidates": not ids and not s.candidate_ids,
                        "no_purchase": s.purchase_proposal is None,
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    ConversationState(),
                    "known_category_budget_limit",
                    "只吃火锅，整单不能超过50元；没有符合的就告诉我。",
                    lambda s, answer, ids: {
                        "hard_conditions": s.requirements.category == "火锅"
                        and s.requirements.budget_fen == 5000,
                        "no_mapping_around_budget": not s.requirements.category_matches,
                        "no_infeasible_choices": not ids and not s.candidate_ids,
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    ConversationState(),
                    "colloquial_home_meal",
                    "想吃点热乎的家常饭，帮我看看有什么优惠",
                    lambda s, answer, ids: {
                        "actually_searched": any(
                            o.tool == "search_group_buys" for o in s.observations
                        ),
                        "has_candidates": bool(s.candidate_ids),
                        "no_guessed_numbers": s.requirements.people is None
                        and s.requirements.budget_fen is None,
                        "not_fully_verified": not ids,
                    },
                )
            )
            # 同一自然对话由“最好”改为“只能”，验证强弱切换，不依赖固定候选ID。
            changing_scope = ConversationState()
            results.append(
                await record_case(
                    database,
                    settings,
                    changing_scope,
                    "area_preference",
                    "想吃火锅，最好南开区，预算200元以内。先看看，人数和日期还没定。",
                    lambda s, answer, ids: {
                        "category_is_scope": s.requirements.category == "火锅",
                        "soft_area": s.requirements.area is None
                        and s.preferences.area in {"南开区", "南开街区"},
                        "hard_budget": s.requirements.budget_fen == 20000,
                        "unknown_kept": s.requirements.people is None
                        and s.requirements.use_date is None,
                        "has_candidates": bool(s.cards),
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    changing_scope,
                    "area_requirement",
                    "那就只看南开区，其他条件都不变。",
                    lambda s, answer, ids: {
                        "hard_area": s.requirements.area in {"南开区", "南开街区"},
                        "old_preference_cleared": s.preferences.area is None,
                        "category_and_budget_preserved": s.requirements.category == "火锅"
                        and s.requirements.budget_fen == 20000,
                        "unknown_kept": s.requirements.people is None
                        and s.requirements.use_date is None,
                        "no_purchase_proposal": s.purchase_proposal is None,
                    },
                )
            )
            # 连续四轮使用同一状态；预期编号取真实上轮卡片，不硬编码模型必须选哪张券。
            dialogue = ConversationState()
            results.append(
                await record_case(
                    database,
                    settings,
                    dialogue,
                    "dialogue_two_choices",
                    "两个人在南开区吃火锅，整单最多200元。给我两个不同方案先看看，使用时间还没定。",
                    lambda s, answer, ids: {
                        "two_choices": len(s.candidate_ids + ids) >= 2,
                        "constraints": s.requirements.people == 2
                        and s.requirements.budget_fen == 20000
                        and s.requirements.area in {"南开区", "南开街区"}
                        and s.requirements.category == "火锅",
                        "no_invented_date": s.requirements.use_date is None,
                        "no_unknown_confirmation": not ids,
                    },
                )
            )
            original = [card.voucher.voucher_id for card in dialogue.cards[:2]]
            second = original[1] if len(original) == 2 else None
            results.append(
                await record_case(
                    database,
                    settings,
                    dialogue,
                    "dialogue_second_choice",
                    "我想先了解第二个，有什么需要注意的？先不买。",
                    lambda s, answer, ids: {
                        "correct_focus": second is not None and s.focused_id == second,
                        "current_source": any(
                            o.tool
                            in {
                                "read_previous_choices",
                                "get_group_buy_detail",
                                "evaluate_plan",
                                "search_group_buys",
                                "compare_group_buys",
                            }
                            and second in o.voucher_ids
                            for o in s.observations
                        ),
                        "budget_preserved": s.requirements.budget_fen == 20000,
                        "people_preserved": s.requirements.people == 2,
                        "not_confirmed_without_date": not ids and s.requirements.use_date is None,
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    dialogue,
                    "dialogue_compare_original",
                    "刚才最开始的那两张比较一下，哪个更划算？",
                    lambda s, answer, ids: {
                        "original_pair": len(original) == 2
                        and {row.voucher.voucher_id for row in s.comparisons} == set(original),
                        "comparison_tool": any(
                            o.tool == "compare_group_buys" and set(o.voucher_ids) == set(original)
                            for o in s.observations
                        ),
                        "unchanged_goal": s.requirements.people == 2
                        and s.requirements.budget_fen == 20000,
                        "unknown_not_usable": not ids,
                    },
                )
            )
            results.append(
                await record_case(
                    database,
                    settings,
                    dialogue,
                    "dialogue_change_people",
                    "改成三个人，预算还是200，区域和品类不变。刚才的双人套餐还适合吗？",
                    lambda s, answer, ids: {
                        "people_changed": s.requirements.people == 3,
                        "other_conditions_preserved": s.requirements.budget_fen == 20000
                        and s.requirements.category == "火锅"
                        and s.requirements.area in {"南开区", "南开街区"},
                        "incompatible_pair_explained": any(
                            card.verdict == "ineligible"
                            and any("人数" in reason for reason in card.reasons)
                            for card in s.cards
                        ),
                        "no_incompatible_selection": all(
                            card.voucher.usage.max_people is None
                            or card.voucher.usage.max_people >= 3
                            for card in s.cards
                            if card.voucher.voucher_id in s.candidate_ids + ids
                        ),
                    },
                )
            )
            # 独立模拟旧购买已失败的浏览上下文，只验证新报价，不接购买提交接口。
            retrying = ConversationState(
                requirements=Requirements(
                    people=2, budget_fen=20000, area="南开区", category="火锅"
                ),
                messages=[
                    ChatMessage(role="user", content="看看方案1暖锅里工作日双人套餐，日期还没定。"),
                    ChatMessage(
                        role="assistant",
                        content="已查询这张套餐。上一笔因服务未准备失败，没有订单；新购买仍需新报价。",
                    ),
                ],
            )
            async with database.sessions() as session:
                retrying.cards = [
                    group_buys.evaluate(
                        await group_buys.detail(session, 3002), retrying.requirements
                    )
                ]

            async def failed_history():
                return [
                    {
                        "id": "fictional-old-failure",
                        "status": "failed",
                        "request_id": None,
                        "error_code": "SECKILL_NOT_READY",
                        "title": "暖锅里工作日双人套餐",
                        "message": "旧购买因准入未准备失败，未创建订单；新购买需重新查看后确认。",
                        "source": "isolated fictional history fixture; not a development purchase",
                    }
                ]

            results.append(
                await record_case(
                    database,
                    settings,
                    retrying,
                    "purchase_after_failure",
                    "帮我购买",
                    lambda s, answer, ids: {
                        "real_proposal": s.purchase_proposal is not None,
                        "correct_voucher": s.purchase_proposal is not None
                        and s.purchase_proposal.evaluation.voucher.voucher_id == 3002,
                        "new_quote": s.purchase_proposal is not None
                        and s.purchase_proposal.id != "fictional-old-failure",
                        "prepare_tool_executed": any(
                            e.tool == "prepare_purchase" for e in s.events
                        ),
                        "unknowns_preserved": s.purchase_proposal is not None
                        and bool(s.purchase_proposal.evaluation.unknowns),
                    },
                    failed_history,
                )
            )
            # 首次探索后人为售罄，模拟用户两轮之间库存变化；只修改本次随机测试库。
            changing = ConversationState()
            results.append(
                await record_case(
                    database,
                    settings,
                    changing,
                    "before_stock_change",
                    "先帮我看看3001这张火锅券，使用时间还没定。",
                    lambda s, answer, ids: {
                        "shown": 3001 in s.candidate_ids,
                        "not_confirmed": not ids,
                    },
                )
            )
            async with database.engine.begin() as connection:
                await connection.execute(text("UPDATE tb_voucher SET stock=0 WHERE id=3001"))
            results.append(
                await record_case(
                    database,
                    settings,
                    changing,
                    "after_stock_change",
                    "再看看刚才那张现在是否还值得选，如果售罄就找其他火锅候选，别替我下单。",
                    lambda s, answer, ids: {
                        "sold_out_not_recommended": 3001 not in ids + s.candidate_ids,
                        "alternative_explored": any(
                            o.tool == "search_group_buys" for o in s.observations
                        ),
                        "no_order_claim": not ids,
                    },
                )
            )
            async with database.engine.begin() as connection:
                await connection.execute(text("UPDATE tb_voucher SET stock=30 WHERE id=3001"))
        async with database.engine.connect() as connection:
            # 3. 检查只读链路未生成购买请求/订单，也未扣减样本券的库存。
            order_count = await connection.scalar(text("SELECT COUNT(*) FROM tb_voucher_order"))
            request_count = await connection.scalar(text("SELECT COUNT(*) FROM tb_order_request"))
            stock = await connection.scalar(text("SELECT stock FROM tb_voucher WHERE id=3001"))
        results = [case for case in results if not case.get("skipped")]
        return {
            "complete": True,
            "generated_at": datetime.now(UTC).isoformat(),
            "model": settings.llm_model,
            "suite": suite,
            "model_base_url": settings.llm_base_url,
            "use_date": use_date.isoformat(),
            "isolation": "random MySQL test database; fictional data; no user orders",
            "cases": results,
            "passed": sum(case["passed"] for case in results),
            "total": len(results),
            "order_count": order_count,
            "request_count": request_count,
            "stock_3001": stock,
            "business_unchanged": order_count == request_count == 0 and stock == 30,
            "limitations": (
                "Small live smoke set, not a benchmark success rate or production performance."
            ),
        }
    finally:
        # 只删除本次随机创建的独立库；模型失败也执行，不清理用户开发资源。
        await database.close()
        try:
            if created:
                assert (
                    database_name.startswith("perkflow_test_agent_eval_")
                    and database_name.isidentifier()
                )
                async with admin.connect() as connection:
                    await connection.execute(text(f"DROP DATABASE `{database_name}`"))
        finally:
            await admin.dispose()
        print("Isolated live-evaluation database removed.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="明确允许调用真实模型，可能产生费用")
    parser.add_argument("--use-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--suite", choices=["baseline", "exploration"], default="exploration")
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        choices=sorted(EXPLORATION_CASES),
        help="仅执行指定轮次；多轮案例需同时选择前置轮次",
    )
    args = parser.parse_args()
    selected = set(args.cases or [])
    if args.suite == "baseline" and selected - BASELINE_CASES:
        parser.error("baseline只能选择原5轮案例")
    dependencies = {
        "colloquial_choose_cuisine": {"colloquial_stir_fry"},
        "colloquial_prepare_purchase": {"colloquial_stir_fry", "colloquial_choose_cuisine"},
        "area_requirement": {"area_preference"},
        "change_category_keep_constraints": {"normal_selection"},
        "explicit_area_change": {"normal_selection", "change_category_keep_constraints"},
        "after_stock_change": {"before_stock_change"},
        "region_any_taste": {"broad_dining_browse"},
        "dialogue_second_choice": {"dialogue_two_choices"},
        "dialogue_compare_original": {"dialogue_two_choices", "dialogue_second_choice"},
        "dialogue_change_people": {
            "dialogue_two_choices",
            "dialogue_second_choice",
            "dialogue_compare_original",
        },
    }
    for case_name, required in dependencies.items():
        if case_name in selected and not required <= selected:
            parser.error(f"{case_name}需要同时选择前置轮次：{', '.join(sorted(required))}")
    if not args.live:
        print(
            "Preview only. Add --live to call the configured model using an isolated test database."
        )
        return 0
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    # 默认下一个周六，避免工作日套餐也通过使案例目标歧义；不修改原活动日期。
    use_date = args.use_date or today + timedelta(days=((5 - today.weekday()) % 7 or 7))
    if use_date.weekday() != 5 or not today < use_date < today + timedelta(days=29):
        print("Use date must be a future Saturday within 29 days.")
        return 2
    try:
        report = asyncio.run(evaluate(Settings(), use_date, args.suite, args.output, selected))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(
            f"Live cases passed: {report['passed']}/{report['total']}; "
            f"business unchanged: {report['business_unchanged']}"
        )
        return 0 if report["passed"] == report["total"] and report["business_unchanged"] else 1
    except Exception as error:
        print("Live evaluation failed:", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
