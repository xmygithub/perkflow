"""有限的原生工具循环；仅完成一轮业务交互，不实现通用Harness。"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from agent.prompts import SYSTEM
from agent.schemas import ChatMessage, ConversationState, Progress
from agent.tools.feedback import purchase_feedback
from agent.tools.group_buys import GroupBuyTools, definitions
from backend.app.core.errors import ApiError


async def run_selection(
    model,
    database,
    settings,
    state: ConversationState,
    message: str,
    emit: Callable[[Progress], Awaitable[None]],
    purchase_reader=None,
    observe_tool: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[str, list[int]]:
    # 1. 本轮只接收用户文本，历史与条件从服务端读取；不接受前端伪造工具消息。
    tools = GroupBuyTools(
        database, state, emit, settings.mysql_query_timeout, message, purchase_reader
    )
    state.turn_number += 1
    purchases = await purchase_reader() if purchase_reader else []
    tools.purchases = purchases
    messages = [
        {
            "role": "system",
            "content": SYSTEM
            + "\n当前北京时间："
            + datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
            + "\n服务端当前状态："
            + json.dumps(
                {
                    "requirements": state.requirements.model_dump(mode="json"),
                    "goal": state.goal,
                    "phase": state.phase,
                    "preferences": state.preferences.model_dump(mode="json"),
                    "condition_sources": state.condition_sources,
                    "condition_details": {
                        k: v.model_dump() for k, v in state.condition_details.items()
                    },
                    "category_mapping": state.category_mapping.model_dump()
                    if state.category_mapping
                    else None,
                    "goal_revision": state.goal_revision,
                    "current_turn": state.turn_number,
                    "previous_intents": state.intents,
                    "previous_observations": [item.model_dump() for item in state.observations],
                    "previous_questions": state.questions,
                    "previous_cards": [card.model_dump(mode="json") for card in state.cards],
                    "previous_choices": [
                        {
                            "position": index + 1,
                            "voucher_id": card.voucher.voucher_id,
                            "title": card.voucher.title,
                            "shop_name": card.voucher.shop_name,
                        }
                        for index, card in enumerate(state.cards)
                    ],
                    "previous_focused_id": state.focused_id,
                    "purchases": purchases,
                    "purchase_action_boundary": purchase_feedback(purchases),
                    "previous_choice_sets": [
                        {
                            "turns_back": index + 1,
                            "choices": [
                                {"position": position + 1, **choice.model_dump()}
                                for position, choice in enumerate(choices)
                            ],
                        }
                        for index, choices in enumerate(tools.previous_sets)
                    ],
                },
                ensure_ascii=False,
            ),
        }
    ]
    messages.extend(item.model_dump() for item in state.messages)
    messages.append({"role": "user", "content": message})
    if purchases:
        # 已授权异步结果比历史报价更新；放在本轮附近，避免模型只复述旧的“尚未下单”。
        messages.append(
            {
                "role": "system",
                "content": (
                    "本轮最新购买事实如下。用户问进度时请直接围绕这些真实状态回答，"
                    "不要把succeeded解释为待确认，不得虚构扫码；可调用get_purchase_status更新。\n"
                    + json.dumps(purchases, ensure_ascii=False)
                ),
            }
        )
    # 先冻结用户看到的卡片编号，再清空本轮展示；旧卡片不得直接成为本轮事实。
    state.choice_history = tools.previous_sets[:2]
    state.plan, state.events, state.cards = [], [], []
    state.comparisons, state.focused_id = [], None
    state.purchase_proposal = None
    state.questions, state.candidate_ids = [], []
    state.intents = []
    labels = {
        "update_goal": "理解本轮意图与条件变化",
        "set_requirements": "整理消费条件",
        "set_goal": "理解目标与偏好",
        "discover_options": "查看已有品类与区域",
        "present_plan": "更新操作计划",
        "search_group_buys": "查询真实团购",
        "get_group_buy_detail": "读取团购详情",
        "evaluate_plan": "校验人数、日期和预算",
        "compare_group_buys": "比较方案与偏好取舍",
        "read_previous_choices": "读取你刚才提到的方案",
        "prepare_purchase": "准备商品与金额确认内容（尚未购买）",
        "get_purchase_status": "查询原购买请求进度",
        "finish": "整理本轮结果",
    }
    # 2. 工具请求先校验后执行，错误作为结构化反馈；只读失败不伪造成功。
    async with asyncio.timeout(settings.agent_timeout):
        for index in range(settings.agent_max_rounds):
            remaining = settings.agent_max_rounds - index
            if remaining <= 2:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"本轮剩余{remaining}次模型调用。优先用已有事实finish；"
                            "用display_ids选择可展示券，未知等级由Service判定；不要反复校验缺失资料；"
                            "必要时给出限制或合并提问，不编造可用结论。"
                        ),
                    }
                )
            if len(json.dumps(messages, ensure_ascii=False)) > 80000:
                raise ApiError(422, "AGENT_CONTEXT_LIMIT", "本次查询信息过多，请缩小范围或新建会话")
            # 未决状态动态收窄可用能力；结果核对为终态后下一轮可恢复报价工具。
            # 只限制交易安全，不为搜索/比较/追问规定固定先后顺序。
            options = tools.selection_options()
            reply = await model.complete(
                messages,
                definitions(
                    allow_purchase_preparation=purchase_feedback(tools.purchases)[
                        "can_prepare_new_purchase"
                    ],
                    displayable_ids=options["eligible_ids"] + options["candidate_ids"],
                    max_questions=2 if state.phase == "verify" else 1,
                ),
            )
            if not reply.tool_calls:
                # 强制经finish验证输出合同；纯文本只作为模型下一轮修正依据。
                messages.append({"role": "assistant", "content": reply.content or ""})
                messages.append(
                    {"role": "user", "content": "请用finish提交；可展示待确认候选，不必补齐表单。"}
                )
                continue
            calls = reply.tool_calls
            if len({call.id for call in calls}) != len(calls):
                raise ApiError(502, "AGENT_PROTOCOL_ERROR", "模型返回重复的工具调用编号")
            # usage属于观测元数据，不能作为供应商assistant消息协议字段回传。
            messages.append(reply.model_dump(exclude_none=True, exclude={"usage"}))
            for call in calls:
                name = call.function.name
                started = time.perf_counter()
                await emit(
                    Progress(kind="tool_start", tool=name, text=labels.get(name, "检查工具请求"))
                )
                try:
                    result = await tools.execute(name, call.function.arguments)
                except ValidationError as error:
                    # 仅返回字段路径/校验原因，不回显输入或异常上下文，帮助模型定点修正。
                    result = {
                        "error": "INVALID_ARGUMENTS",
                        "message": "参数未生效，请按problems修正错误字段，保留其余用户条件",
                        "problems": error.errors(
                            include_input=False, include_context=False, include_url=False
                        )[:4],
                    }
                except ApiError as error:
                    result = {"error": error.code, "message": error.message}
                # 同步可展示集合：选择有来源，分类由代码完成，错误可直接按实际ID修正。
                if tools.seen:
                    result = {**result, "selection_options": tools.selection_options()}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                if observe_tool is not None:
                    # 可选评测观察点只传公开工具协议，包含最后一次finish；不改变执行或授权。
                    try:
                        arguments = json.loads(call.function.arguments)
                    except ValueError:
                        arguments = {"malformed_json": True}
                    await observe_tool(
                        {"tool": name, "call_id": call.id, "arguments": arguments, "result": result}
                    )
                reused = (result.get("observation") or {}).get("snapshot_reused", False)
                if "error" in result:
                    summary = result.get("message", result["error"])
                elif result.get("answer_guard"):
                    summary = "服务端返回未决购买边界说明；本轮没有新报价或购买"
                elif reused:
                    summary = "复用本轮同条件结果，没有新增资料；最终返回和报价仍会核对"
                elif name == "search_group_buys" and "total" in result:
                    summary = f"查到{result['total']}个目录候选，本页{len(result['items'])}个"
                    if (result.get("feedback") or {}).get("code") in {"NO_MATCH", "EMPTY_PAGE"}:
                        summary += "；" + result["feedback"]["message"]
                    else:
                        summary += "，已返回本页条件核验，未知项不等于适用"
                elif name == "evaluate_plan" and "verdict" in result:
                    status = {
                        "eligible": "条件通过",
                        "unknown": "信息不足",
                        "ineligible": "条件不符",
                    }
                    detail = "；".join(result["reasons"] + result["unknowns"])
                    summary = status[result["verdict"]] + ("：" + detail if detail else "")
                else:
                    summary = result.get("message", "操作完成")
                code = result.get("error") or (result.get("feedback") or {}).get("code")
                if reused:
                    code = "REUSED_OBSERVATION"
                if result.get("answer_guard"):
                    code = result["answer_guard"]
                if code is None:
                    code = result.get("verdict") or ("FINISHED" if result.get("finished") else "OK")
                await emit(
                    Progress(
                        kind="tool_end",
                        tool=name,
                        text=summary,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        outcome_code=code,
                    )
                )
                if tools.answer is not None:
                    # 3. 只保存有限消息和业务结果，不持久化模型内部工具协议或思维链。
                    state.messages = (
                        state.messages
                        + [
                            ChatMessage(role="user", content=message),
                            ChatMessage(role="assistant", content=tools.answer),
                        ]
                    )[-12:]
                    return tools.answer, tools.recommended_ids
    raise ApiError(422, "AGENT_STEP_LIMIT", "本轮已达执行次数上限，请缩小条件后重试")
