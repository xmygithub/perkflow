"""团购探索工具：约束由后端执行，模型选择查询、比较、追问与结束。"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import Field, ValidationError

from agent.schemas import (
    CategoryMapping,
    ChoiceReference,
    ConversationState,
    Observation,
    Preferences,
    Progress,
    PurchaseProposal,
)
from agent.tools.feedback import assessment, decision_feedback, purchase_feedback
from agent.tools.goals import GoalUpdate, merge_goal
from backend.app.core.database import Database
from backend.app.schemas.group_buys import Contract, RequirementPatch, Requirements, SearchInput
from backend.app.services import group_buys
from backend.app.services.agent_purchase_policy import terms


class PlanInput(Contract):
    steps: list[str] = Field(min_length=1, max_length=6)


class ConstraintInput(RequirementPatch):
    area: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "用户直接指定的本次搜索范围也写此处，如南开区；不要求必须带只能。"
            "带优先/最好才放preferences.area，勿同时填两处"
        ),
    )
    evidence: str = Field(default="", max_length=500, description="用户本轮原话中的连续片段")


class GoalInput(Contract):
    goal: str | None = Field(default=None, min_length=1, max_length=300)
    preferences: Preferences | None = None
    phase: Literal["explore", "compare", "verify"] | None = None
    evidence: str = Field(default="", max_length=500)


class ExploreInput(Contract):
    page: int = Field(default=1, ge=1, le=1000, strict=True)
    keywords: list[str] = Field(default_factory=list, max_length=3)
    sort: Literal["price", "score"] | None = None
    refresh: bool = Field(
        default=False, description="本轮相同条件资料已足够时不重查；确需新快照才true"
    )
    category_matches: list[Annotated[str, Field(min_length=1, max_length=40)]] | None = Field(
        default=None,
        min_length=1,
        max_length=3,
        description="口语原品类无字面匹配时，从本轮discover实际类目选择相关候选；不是修改原意。",
    )


class VoucherInput(Contract):
    voucher_id: int = Field(ge=1, le=2**64 - 1, strict=True)


class ReadVoucherInput(VoucherInput):
    refresh: bool = Field(default=False, description="已有本轮结果优先复用；明确核对变化才true")


class CompareInput(Contract):
    voucher_ids: list[Annotated[int, Field(ge=1, le=2**64 - 1, strict=True)]] = Field(
        min_length=2, max_length=3
    )


class PreviousChoicesInput(Contract):
    turns_back: int = Field(
        default=1,
        ge=1,
        le=3,
        strict=True,
        description="1是最近展示，2是再前一组，3是再前两组；根据服务端历史定位，不确定则澄清。",
    )
    positions: list[Annotated[int, Field(ge=1, le=8, strict=True)]] = Field(
        min_length=1,
        max_length=3,
        description="上一轮页面方案编号，从1开始；编号不是券ID。不确定指哪张时先澄清。",
    )


class EmptyInput(Contract):
    pass


class PurchaseInput(VoucherInput):
    quantity: Literal[1] = 1
    evidence: str = Field(
        default="", max_length=500, description="兼容旧调用；当前调用由服务端绑定本轮原话"
    )


class SelectionInput(Contract):
    voucher_id: int = Field(ge=1, le=2**64 - 1, strict=True)
    kind: Literal["candidate", "confirmed"]


class FinishInput(Contract):
    answer: str = Field(
        min_length=1,
        max_length=6000,
        description="通常1至3句结论/取舍/必要限制；卡片负责价格和详情，不重列所有券或重复questions。",
    )
    display_ids: list[Annotated[int, Field(ge=1, le=2**64 - 1, strict=True)]] = Field(
        default_factory=list,
        max_length=3,
        description="按展示顺序选本轮已读取且无已知冲突的券ID；可空。是否条件通过由Service判定。",
    )
    request_purchase_confirmation: bool = Field(
        default=False,
        description="本轮引导用户点击购买确认时为true，必须先prepare_purchase取得真实报价；仅解释流程为false",
    )
    selections: list[SelectionInput] = Field(default_factory=list, max_length=3)
    # 兼容旧工具调用；当前模型只选display_ids，不再自行分类candidate/confirmed。
    recommended_ids: list[int] = Field(default_factory=list, max_length=3)
    candidate_ids: list[int] = Field(default_factory=list, max_length=3)
    questions: list[str] = Field(default_factory=list, max_length=2)
    focused_id: int | None = Field(
        default=None,
        ge=1,
        le=2**64 - 1,
        strict=True,
        description="用户本轮正在了解/选中的一张券，须在展示ID中；单张展示可省略，不代表购买授权。",
    )


SPECS = {
    "update_goal": (
        GoalUpdate,
        "统一增量理解消费目标/多意图与条件；仅在需要更新时调用，不要求先填表。"
        "changes同一字段只写一次：直接指定找火锅/南开区用required；明确最好/优先才preferred；"
        "用户撤销才clear+null。未提及项保留，转换强弱由服务器清除旧位置。"
        "别太贵只写low_price偏好，不猜预算；周末可用use_weekdays=[5,6]不猜日期。"
        "不接受预约用allow_reservation=false/required，尽量不预约用false/preferred。"
        "炒菜/聚餐等口语保留原意category并用semantic；明确只找分类名用literal。"
        "notes仅非结构化软偏好，用preferred+字符串数组；不重复已有硬条件。"
        "意图可组合，不能将准备购买等同实际授权；服务器绑定本轮原话和轮次。",
    ),
    "set_requirements": (
        ConstraintInput,
        "仅增量记录用户明确的硬条件/事实，未提及保留，null清除；evidence引用本轮原话。"
        "金额单位分。别太贵不能填数值预算，优先区域写软偏好；周末可用use_weekdays=[5,6]，"
        "不要猜具体日期、时间或人数。用户明确修改才改变已有硬约束。",
    ),
    "set_goal": (
        GoalInput,
        "记录消费目标及软偏好，preferences只传变化字段，evidence引用本轮原话。"
        "low_price便宜优先、high_rating评分优先、area/category优先但可比较其他候选、"
        "avoid_reservation尽量不预约；notes保存尚不能量化的偏好。不要将出去玩默认成吃饭。"
        "phase按用户任务选explore/compare/verify，仅核验具体适用方案才选verify，不为多问问题改变阶段。",
    ),
    "present_plan": (PlanInput, "必要时展示简短下一步计划；简单需求跳过，不输出内部思考。"),
    "discover_options": (
        EmptyInput,
        "查看当前城市/硬区域/预算内品类和区域，区分voucher_count券数量与shop_count不同门店数；"
        "适合尚未确定吃什么/玩什么，"
        "也可发现口语与类目差异；返回原词全目录有无字面匹配。数量不代表可用。",
    ),
    "search_group_buys": (
        ExploreInput,
        "不必补齐人数/预算/日期即可搜索。硬条件由后端自动追加、不能覆盖；keywords为"
        "额外探索词（最多3个，或关系），可空数组撤去无效探索词；sort可按price/score。"
        "软区域/品类优先排序，不过滤其他备选。feedback说明零结果单项诊断/分页边界；"
        "assessments为本页确定性校验，issues.source区分用户未知与商家缺资料，不代表全库最优。"
        "口语原词无字面匹配时，先discover再用category_matches选实际相关类目，无需问是否放宽；"
        "用户选定某相关类目后用category_matches收窄至所选类目，已有映射的子集不必重复discover；"
        "明确更换原品类才update_goal。原词保留，相关只作待核验候选。"
        "已有字面范围或literal要求不可借映射改变。",
    ),
    "get_group_buy_detail": (
        ReadVoucherInput,
        "读取实际券详情；本轮已有结果默认复用，需要核对新变化才refresh=true。规则缺失不靠重复查询补全。",
    ),
    "evaluate_plan": (
        ReadVoucherInput,
        "按当前硬条件重新读取券并确定性校验；unknown仍可作为待确认候选，"
        "ineligible不可宣称满足目标。无需先补全全部条件。已有相同条件核验默认复用，核对变化才refresh。",
    ),
    "compare_group_buys": (
        CompareInput,
        "比较2至3张本轮已查询或最近三组已展示的券：直接重读当前价格/评分/规则和软偏好取舍，"
        "无需为了比较而重复逐张查询。不证明全网最优。",
    ),
    "read_previous_choices": (
        PreviousChoicesInput,
        "理解第二个/刚才两个等指代时，按上一轮页面方案编号映射并重新读取当前规则与校验。"
        "编号固定取本轮开始时的卡片，不因更新条件而移动；历史快照不作当前证据。"
        "比较上一轮方案可先用此工具再compare；用户含糊指代多张同名方案时不要猜。",
    ),
    "finish": (
        FinishInput,
        "结束并展示0至3张券，用display_ids选择当前可展示ID；不要把历史编号当新事实。"
        "Service重读并决定通过/未知。宽泛浏览有跨店候选时优先展示不同商家，避免同店多券挤满首屏。"
        "正文简短说明取舍，不复述卡片价格/详情，用户指定详细说明再展开。"
        "questions仅放真正需要用户回答的新问题，默认留空；不要在正文再重复提问。"
        "报价是否已准备由服务端判定，无报价不能虚构确认入口。",
    ),
    "prepare_purchase": (
        PurchaseInput,
        "用户明确表示想买且商品唯一时，用voucher_id生成待确认快照，不提交购买。"
        "数量固定1、本轮原话由服务端绑定。known冲突不可买，unknown需用户页面知悉。"
        "不能把浏览、聚焦或工具文案当购买授权；价格/数量由服务器控制。用户必须点击确认。",
    ),
    "get_purchase_status": (
        EmptyInput,
        "查询当前账号本会话与账号最近购买的真实授权/异步状态，按conversation_id区分来源。"
        "pending/submitting都不是成功或失败，"
        "只回查，不重新下单。开始时purchases已预读，足以回答就不用重复查询；需再次核对才调用。"
        "只有明确failed才可探索替代，新购买仍需页面确认。",
    ),
}


def definitions(
    *,
    allow_purchase_preparation: bool = True,
    displayable_ids: list[int] | None = None,
    max_questions: int = 2,
) -> list[dict]:
    result = []
    for name, (schema, description) in SPECS.items():
        if name in {"set_goal", "set_requirements"}:
            # 旧执行合同供已有调用兼容；当前模型仅见统一目标工具，避免同一条件写进两处。
            continue
        if name == "prepare_purchase" and not allow_purchase_preparation:
            continue
        parameters = schema.model_json_schema()
        if name == "update_goal":
            parameters["$defs"]["ConditionChange"]["properties"]["field"]["enum"].remove(
                "avoid_reservation"
            )
        if name == "finish":
            for legacy in (
                "selections",
                "candidate_ids",
                "recommended_ids",
                "request_purchase_confirmation",
            ):
                parameters["properties"].pop(legacy)
            parameters.get("$defs", {}).pop("SelectionInput", None)
            parameters["properties"]["questions"]["maxItems"] = max_questions
            if displayable_ids is not None:
                # 把当前能引用的ID交给模型，降低从历史文本猜ID的概率；执行层校验仍保留。
                ids = sorted(set(displayable_ids))
                parameters["properties"]["focused_id"]["enum"] = [None, *ids]
                if ids:
                    parameters["properties"]["display_ids"]["items"]["enum"] = ids
                else:
                    parameters["properties"]["display_ids"]["maxItems"] = 0
        if name == "prepare_purchase":
            for bound_field in ("evidence", "quantity"):
                parameters["properties"].pop(bound_field)
        result.append(
            {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            }
        )
    return result


class GroupBuyTools:
    def __init__(
        self,
        database: Database,
        state: ConversationState,
        emit: Callable[[Progress], Awaitable[None]],
        query_timeout: int = 10,
        user_message: str = "",
        purchase_reader=None,
    ):
        self.database, self.state, self.emit = database, state, emit
        self.query_timeout, self.user_message = query_timeout, user_message
        self.purchase_reader = purchase_reader
        self.evaluations, self.seen, self.repetitions = {}, {}, {}
        self.query_results = {}
        self.catalog_observation = None
        self.goal_update_pending = False
        # 1. 冻结上轮页面顺序；后续更新条件会清空旧结论，但不改变用户口中的“第二个”。
        latest = [
            ChoiceReference(
                voucher_id=card.voucher.voucher_id,
                title=card.voucher.title,
                shop_name=card.voucher.shop_name,
            )
            for card in state.cards
        ]
        self.previous_sets = ([latest] if latest else []) + state.choice_history
        self.previous_sets = self.previous_sets[:3]
        self.comparison_ids = []
        self.answer = None
        self.recommended_ids = []
        self.purchases = []
        self.purchase_preparation_attempted = False
        # 明确的短购买指令已有行动含义；这里只约束唯一候选的报价准备，不执行交易。
        command = re.sub(r"[，。！!？?\s]", "", user_message)
        self.explicit_purchase_command = (
            re.fullmatch(
                r"(?:请)?(?:帮我|替我|给我|我想|我要|就|直接)?(?:重新|再)?(?:购买|买)"
                r"(?:这个|这张|那个|那张|它|一张|第[一二三\d]+[个张])?(?:吧|一下)?",
                command,
            )
            is not None
        )

    def evidence_error(self, evidence):
        # 原话校验可阻止引用工具文案/历史回答修改目标；语义解释仍需模型评测，不能靠引用证明。
        if self.user_message and (not evidence or evidence not in self.user_message):
            return {
                "error": "USER_EVIDENCE_REQUIRED",
                "message": "请引用本轮用户原话，不能自行修改目标",
            }
        return None

    def observe(self, tool, summary, ids=()):
        self.state.observations = (
            self.state.observations
            + [Observation(tool=tool, summary=summary[:1000], voucher_ids=list(ids)[:20])]
        )[-12:]

    def preference_notes(self, item):
        prefs, notes = self.state.preferences, []
        if prefs.area:
            area = {"南开区": "南开街区"}.get(prefs.area, prefs.area)
            notes.append("符合优先区域" if item.area == area else "其他区域备选，并非优先区域")
        if prefs.category:
            notes.append(
                "符合优先品类" if prefs.category in item.category else "其他品类备选，并非优先品类"
            )
        if prefs.avoid_reservation:
            notes.append(
                {
                    True: "需要预约，与软偏好有取舍",
                    False: "无需预约，符合偏好",
                    None: "预约规则未知",
                }[item.usage.reservation_required]
            )
        if prefs.high_rating and item.score is not None:
            notes.append(f"店铺评分{item.score:g}，不代表套餐适用性已确认")
        return notes

    async def read(self, identifier):
        async with asyncio.timeout(self.query_timeout), self.database.sessions() as session:
            item = await group_buys.detail(session, identifier)
        self.seen[item.voucher_id] = item
        return item

    def check(self, item):
        result = group_buys.evaluate(item, self.state.requirements).model_copy(
            update={"preference_notes": self.preference_notes(item)}
        )
        self.evaluations[item.voucher_id] = result
        self.state.cards = list(self.evaluations.values())[-8:]
        return result

    def selection_options(self):
        # 仅用本轮真实读取的资料做纯函数判定，不查库、不改卡片；结束时还会重读。
        result = {"eligible_ids": [], "candidate_ids": [], "rejected_ids": []}
        groups = {
            "eligible": "eligible_ids",
            "unknown": "candidate_ids",
            "ineligible": "rejected_ids",
        }
        for identifier, item in self.seen.items():
            result[groups[group_buys.evaluate(item, self.state.requirements).verdict]].append(
                identifier
            )
        return result

    async def finish(self, args):
        # 1. 确认入口必须对应服务端报价，不能用一句“去页面确认”替代工具执行。
        # 结构化动作是主要合同；常见引导措辞只作补充拦截，不宣称覆盖全部自然语言。
        confirmation_text = re.sub(
            r"(?:不再需要|不需要|无需|不用|不要)[^。！？；\n]{0,25}"
            r"(?:页面[^。！？；\n]{0,12}确认|点击[^。！？；\n]{0,8}确认|确认卡(?:片)?)",
            "",
            args.answer,
        )
        points_to_confirmation = re.search(
            r"(?:请|仍需|需要|必须|须|记得|可以|等待|等你|只有|已生成|已准备)"
            r"[^。！？；\n]{0,35}(?:页面[^。！？；\n]{0,12}确认|点击[^。！？；\n]{0,8}确认|确认卡(?:片)?)",
            confirmation_text,
        )
        explains_missing = re.search(
            r"(?:尚未|还未|没有|未能|无法|不能)[^。！？；\n]{0,10}"
            r"(?:生成|提供|准备)[^。！？；\n]{0,8}(?:确认|报价)",
            args.answer,
        )
        single_choice = len(self.previous_sets[0]) == 1 if self.previous_sets else False
        must_attempt_preparation = (
            self.explicit_purchase_command
            and single_choice
            and not self.purchase_preparation_attempted
            and not any(
                p.get("status") in {"submitting", "pending"}
                or (p.get("status") == "succeeded" and p.get("in_current_conversation", True))
                for p in self.purchases
            )
        )
        needs_proposal = (
            args.request_purchase_confirmation
            or (points_to_confirmation and not explains_missing)
            or must_attempt_preparation
        )
        answer_guard = None
        if needs_proposal and not purchase_feedback(self.purchases)["can_prepare_new_purchase"]:
            self.state.purchase_proposal = None
            if args.request_purchase_confirmation:
                return {
                    "error": "PURCHASE_UNRESOLVED",
                    "message": "原购买仍未决，本轮不能引导替代购买或新报价；只回查原状态",
                }
            # 自由文字触发守护时直接落到已知业务事实，避免模型反复解释将来确认被误拦。
            # 不猜测句子意图、不授予新权限；结构化确认动作仍在上方明确拒绝。
            args = args.model_copy(
                update={
                    "answer": (
                        "最近读取的购买状态仍在处理中，尚未确认成功或失败。"
                        "请先回查原购买结果；本轮没有生成新报价或提交新购买。"
                    ),
                    "display_ids": [],
                    "selections": [],
                    "recommended_ids": [],
                    "candidate_ids": [],
                    "focused_id": None,
                    "questions": [],
                }
            )
            self.evaluations.clear()
            self.comparison_ids = []
            needs_proposal = False
            answer_guard = "UNRESOLVED_PURCHASE_EXPLANATION"
        if self.state.purchase_proposal is None and needs_proposal:
            return {
                "error": "PURCHASE_PROPOSAL_REQUIRED",
                "message": (
                    "本轮没有购买确认卡，不能引导点击。浏览/比较只展示候选，不必生成报价；"
                    "用户明确要买且商品唯一才prepare_purchase，勿再追问是否准备。"
                    "商品歧义或准备失败则说明原因，不虚构入口。"
                ),
            }
        # 2. 当前合同只让模型选择展示对象，适用等级由Service计算；旧合同仍严格校验。
        if args.display_ids:
            if args.selections or args.recommended_ids or args.candidate_ids:
                return {"error": "INVALID_SELECTION", "message": "display_ids不与旧分类字段混用"}
            if len(set(args.display_ids)) != len(args.display_ids):
                return {"error": "INVALID_SELECTION", "message": "展示ID不能重复"}
            if any(identifier not in self.seen for identifier in args.display_ids):
                return {
                    "error": "UNSEEN_CANDIDATE",
                    "message": "先读取历史或查询候选再展示",
                    "selection_options": self.selection_options(),
                }
            checked = {
                identifier: group_buys.evaluate(self.seen[identifier], self.state.requirements)
                for identifier in args.display_ids
            }
            conflict = next((row for row in checked.values() if row.verdict == "ineligible"), None)
            if conflict:
                return {
                    "error": "SELECTION_CHANGED",
                    "message": "该券与当前条件冲突，请调整展示选择",
                    "evaluation": conflict.model_dump(mode="json"),
                    "selection_options": self.selection_options(),
                }
            self.evaluations.update(checked)
            args = args.model_copy(
                update={
                    "recommended_ids": [
                        key for key in args.display_ids if checked[key].verdict == "eligible"
                    ],
                    "candidate_ids": [
                        key for key in args.display_ids if checked[key].verdict == "unknown"
                    ],
                }
            )
        # 旧字段仅用于兼容；其confirmed声明仍须已有eligible证据，不静默降级。
        if args.selections:
            if args.recommended_ids or args.candidate_ids:
                return {
                    "error": "INVALID_SELECTION",
                    "message": "只使用selections，不同时传历史ID数组",
                }
            args = args.model_copy(
                update={
                    "candidate_ids": [
                        item.voucher_id for item in args.selections if item.kind == "candidate"
                    ],
                    "recommended_ids": [
                        item.voucher_id for item in args.selections if item.kind == "confirmed"
                    ],
                }
            )
        ids = args.display_ids or (args.recommended_ids + args.candidate_ids)
        if len(ids) > 3 or len(set(ids)) != len(ids):
            return {"error": "INVALID_SELECTION", "message": "候选和确认方案合计最多3项且不能重复"}
        if args.focused_id is not None and args.focused_id not in ids:
            return {"error": "INVALID_FOCUS", "message": "正在了解的方案必须属于本轮展示的选择"}
        if any(
            identifier not in self.evaluations or self.evaluations[identifier].verdict != "eligible"
            for identifier in args.recommended_ids
        ):
            return {
                "error": "UNVERIFIED_RECOMMENDATION",
                "message": (
                    "确认方案须先按当前条件校验通过；unknown用kind=candidate，不要反复校验缺失字段"
                ),
                "eligible_ids": [
                    key for key, value in self.evaluations.items() if value.verdict == "eligible"
                ],
            }
        if any(identifier not in self.seen for identifier in args.candidate_ids):
            return {"error": "UNSEEN_CANDIDATE", "message": "候选必须来自本轮实际查询或详情"}
        if any(not question or len(question) > 300 for question in args.questions):
            return {"error": "INVALID_QUESTION", "message": "最多2个必要问题，每个不超过300字"}
        question_budget = 2 if self.state.phase == "verify" else 1
        if len(args.questions) > question_budget:
            return {
                "error": "TOO_MANY_QUESTIONS",
                "message": (
                    "浏览/比较阶段只保留一个最影响下一步的问题；"
                    "已有候选不要求补全人数、预算、预约清单"
                ),
                "max_questions": question_budget,
            }
        # 3. 返回前重新查库，价格、库存或规则变动使旧推荐失效时反馈模型重规划。
        selected = []
        proposal = self.state.purchase_proposal
        proposal_ids = [proposal.evaluation.voucher.voucher_id] if proposal else []
        for identifier in dict.fromkeys(ids + self.comparison_ids + proposal_ids):
            previous = self.seen.get(identifier)
            latest = await self.read(identifier)
            result = self.check(latest)
            if (
                proposal
                and identifier in proposal_ids
                and (
                    terms(latest) != terms(proposal.evaluation.voucher)
                    or result.verdict == "ineligible"
                )
            ):
                self.state.purchase_proposal = None
                return {
                    "error": "PURCHASE_TERMS_CHANGED",
                    "message": "确认商品资料已变化，请重新准备",
                    "evaluation": result.model_dump(mode="json"),
                }
            changed = previous is not None and any(
                getattr(previous, field) != getattr(latest, field)
                for field in ("pay_value", "stock_snapshot", "sale_state", "usage", "score")
            )
            if (
                changed
                or (identifier in ids and result.verdict == "ineligible")
                or (identifier in args.recommended_ids and result.verdict != "eligible")
            ):
                return {
                    "error": "SELECTION_CHANGED",
                    "message": "最新数据与目标冲突或不再能确认，请根据原因调整选择",
                    "evaluation": result.model_dump(mode="json"),
                }
            if identifier in ids:
                selected.append(
                    result.model_copy(
                        update={
                            "stage": "candidate" if identifier in args.candidate_ids else "checked"
                        }
                    )
                )
        # 4. 卡片顺序与“第几个”一致；失败候选仅作为校验记录，不能混成确认推荐。
        other = [card for key, card in self.evaluations.items() if key not in ids]
        self.state.cards = (selected + other)[:8]
        self.state.questions, self.state.candidate_ids = args.questions, args.candidate_ids
        self.state.comparisons = [self.evaluations[key] for key in self.comparison_ids]
        # 单张展示天然是当前正在了解的对象，无需模型重复填写UI标记；该标记不授予购买权限。
        self.state.focused_id = args.focused_id or (
            ids[0] if args.display_ids and len(ids) == 1 else None
        )
        self.answer, self.recommended_ids = args.answer, args.recommended_ids
        if "附近" in self.user_message and not any(
            word in self.answer for word in ("定位", "距离", "位置")
        ):
            # 能力边界是平台事实，不能依赖模型是否记得解释；这不改变模型选择的探索路径。
            self.answer += (
                "\n\n目前没有定位或距离查询能力，以上按平台城市探索，不是按离你远近排序。"
            )
        return {"finished": True, **({"answer_guard": answer_guard} if answer_guard else {})}

    async def execute(self, name: str, raw: str) -> dict:
        # 1. 工具白名单与参数合同限制能力；无购买、取消、任意SQL或网络工具。
        if name not in SPECS:
            return {"error": "UNKNOWN_TOOL", "message": "本阶段只支持团购只读工具"}
        try:
            args = SPECS[name][0].model_validate_json(raw)
        except ValidationError:
            if name == "update_goal":
                self.goal_update_pending = True
            raise
        if self.goal_update_pending and name not in {"update_goal", "get_purchase_status"}:
            return {
                "error": "GOAL_UPDATE_REQUIRED",
                "message": (
                    "本轮条件更新尚未通过，不能按旧条件查询或结束；"
                    "先修正update_goal报错字段，其余条件保留"
                ),
            }
        if name == "search_group_buys" and args.category_matches is not None:
            if error := self.apply_category_matches(args.category_matches):
                return error
        reusable = name in {
            "search_group_buys",
            "discover_options",
            "evaluate_plan",
            "get_group_buy_detail",
        }
        if not reusable:
            try:
                result = await self._execute(name, args)
            except ValidationError:
                if name == "update_goal":
                    self.goal_update_pending = True
                raise
            if name == "update_goal" and "error" not in result:
                self.goal_update_pending = False
            return result
        # 2. 缓存仅存在本轮请求内；签名含完整有效条件/偏好，目标改变后不会误用旧结论。
        signature = json.dumps(
            {
                "tool": name,
                "goal_revision": self.state.goal_revision,
                "args": args.model_dump(mode="json", exclude={"refresh"}),
                "requirements": self.state.requirements.model_dump(mode="json"),
                "preferences": self.state.preferences.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        count = self.repetitions.get(signature, 0) + 1
        self.repetitions[signature] = count
        if count > 2:
            return {
                "error": "REPEATED_ACTION",
                "message": (
                    "相同条件已提供两次观察，请使用已有结果、换策略或必要澄清；最终校验仍会重读"
                ),
            }
        if not getattr(args, "refresh", False) and signature in self.query_results:
            result = deepcopy(self.query_results[signature])
            result["observation"] = {
                "snapshot_reused": True,
                "new_information": False,
                "message": (
                    "本轮同条件结果已读取，本次无新增资料；确需核对变化可refresh，缺失规则仍未知"
                ),
            }
            self.observe(name, "复用本轮同条件观察，没有新增资料")
            if name == "discover_options":
                self.remember_catalog(result)
            return result
        result = await self._execute(name, args)
        if "error" not in result:
            result = {
                **result,
                "observation": {
                    "snapshot_reused": False,
                    "new_information": None,
                    "message": "本次已查询；是否有新事实由返回资料判断，不能因重读就视为规则已补齐",
                },
            }
            self.query_results[signature] = deepcopy(result)
        return result

    def catalog_scope(self):
        need = self.state.requirements
        return (need.city, need.area, need.budget_fen, need.category)

    def remember_catalog(self, result):
        self.catalog_observation = {
            "scope": self.catalog_scope(),
            "result": deepcopy(result),
            "categories": {row["category"] for row in result["options"]},
        }

    def apply_category_matches(self, categories):
        # 只解释口语品类与已观察目录的关系；不更改原词、预算、区域、日期或购买权限。
        need, catalog = self.state.requirements, self.catalog_observation
        if not need.category or need.category_matching != "semantic":
            return {
                "error": "CATEGORY_MAPPING_FORBIDDEN",
                "message": "没有口语品类或要求字面匹配，不可扩展类目",
            }
        previous = self.state.category_mapping
        narrowed = (
            previous is not None
            and previous.expression == need.category
            and set(previous.categories) == set(need.category_matches)
            and set(categories) <= set(previous.categories)
        )
        if len(set(categories)) != len(categories):
            return {"error": "UNSEEN_CATEGORY", "message": "相关类目不能重复"}
        if not narrowed and (catalog is None or catalog["scope"] != self.catalog_scope()):
            return {
                "error": "CATALOG_REQUIRED",
                "message": "先discover当前范围的实际目录，再选相关类目",
            }
        if not narrowed and not set(categories) <= catalog["categories"]:
            return {
                "error": "UNSEEN_CATEGORY",
                "message": "只能选择本轮当前范围目录实际返回的不重复类目",
            }
        if not narrowed and catalog["result"].get("literal_catalog_match") is not False:
            return {
                "error": "LITERAL_CATEGORY_EXISTS",
                "message": "原词已有字面匹配，按原条件查询；不能用映射绕开预算/区域或换品类",
            }
        if need.category_matches != categories:
            self.state.requirements = Requirements.model_validate(
                {
                    **need.model_dump(mode="json"),
                    "category_matches": categories,
                }
            )
            self.state.category_mapping = CategoryMapping(
                expression=need.category,
                categories=categories,
                source_turn=self.state.turn_number,
                city=need.city,
                area=need.area,
                budget_fen=need.budget_fen,
                source="narrowed_mapping" if narrowed else "observed_catalog",
            )
            self.state.goal_revision += 1
            self.invalidate_selection()
            self.observe(
                "search_group_buys", "保留原消费意图，按已观察相关类目探索；菜品是否符合仍未知"
            )
        return None

    def invalidate_selection(self):
        # 条件变化使结论/报价失效，原始资料仍供按新条件计算，历史编号保持冻结。
        self.evaluations.clear()
        self.state.cards, self.state.candidate_ids, self.state.questions = [], [], []
        self.state.comparisons, self.comparison_ids = [], []
        self.state.focused_id, self.state.purchase_proposal = None, None

    async def _execute(self, name, args):
        if name == "update_goal":
            # 1. 全部验证成功后才写回；意图/阶段/无变化的复述不清空有效资料或重试预算。
            updates, changed = merge_goal(self.state, args, self.user_message)
            for field, value in updates.items():
                setattr(self.state, field, value)
            if changed:
                self.invalidate_selection()
            self.observe(
                name, "有效条件已更新，旧结论失效" if changed else "目标信息已记录，条件未变化"
            )
            await self.emit(Progress(kind="requirements", text="已理解本轮意图并增量更新目标"))
            return {
                "intents": self.state.intents,
                "goal": self.state.goal,
                "phase": self.state.phase,
                "requirements": self.state.requirements.model_dump(mode="json"),
                "preferences": self.state.preferences.model_dump(mode="json"),
                "condition_details": {
                    k: v.model_dump() for k, v in self.state.condition_details.items()
                },
                "goal_revision": self.state.goal_revision,
                "conditions_changed": changed,
                "message": "未提及条件保留；意图不授予购买权限，模型仍按工具反馈选择下一步",
            }
        if name in {"set_requirements", "set_goal"}:
            if error := self.evidence_error(args.evidence):
                return error
            if name == "set_requirements":
                if {"category_matches", "category_matching"} & args.model_fields_set:
                    return {"error": "INVALID_ARGUMENTS", "message": "旧接口不能写类目解释字段"}
                if args.area and self.user_message:
                    # 拒绝明显的“区域优先”与硬区域字段冲突，不静默改目标。
                    # 这只是局部防护，其他措辞仍依赖语义评测。
                    aliases = {args.area}
                    if args.area == "南开街区":
                        aliases.add("南开区")
                    soft_area = any(
                        re.search(re.escape(area) + r"(?:为)?(?:优先|首选)", self.user_message)
                        or re.search(
                            r"(?:优先|最好|尽量)(?:去|在)?" + re.escape(area), self.user_message
                        )
                        for area in aliases
                    )
                    hard_area = any(
                        re.search(
                            r"(?:只能|必须|限定|仅限)(?:在|去)?" + re.escape(area),
                            self.user_message,
                        )
                        for area in aliases
                    )
                    if soft_area and not hard_area:
                        return {
                            "error": "SOFT_CONSTRAINT_CONFLICT",
                            "message": (
                                "用户区域措辞是优先偏好，"
                                "请仅写set_goal.preferences.area，不设置硬区域"
                            ),
                        }
                patch = args.model_dump(mode="json", exclude_unset=True, exclude={"evidence"})
                if "category" in patch:
                    patch.update(category_matches=[], category_matching="legacy")
                self.state.requirements = Requirements.model_validate(
                    {**self.state.requirements.model_dump(mode="json"), **patch}
                )
                if "category" in patch:
                    self.state.category_mapping = None
                for key in patch:
                    self.state.condition_sources[key] = "用户原话：" + args.evidence
            else:
                if args.goal is not None:
                    self.state.goal = args.goal
                if args.phase is not None:
                    self.state.phase = args.phase
                if args.preferences is not None:
                    patch = args.preferences.model_dump(mode="json", exclude_unset=True)
                    self.state.preferences = Preferences.model_validate(
                        {**self.state.preferences.model_dump(mode="json"), **patch}
                    )
                self.state.condition_sources["goal/preferences"] = "用户原话：" + args.evidence
            self.invalidate_selection()
            self.repetitions.clear()
            self.observe(name, "目标已更新，旧卡片与校验失效；历史观察仅供解释")
            await self.emit(Progress(kind="requirements", text="已更新目标、硬条件与软偏好"))
            return {
                "goal": self.state.goal,
                "phase": self.state.phase,
                "requirements": self.state.requirements.model_dump(mode="json"),
                "preferences": self.state.preferences.model_dump(mode="json"),
            }
        if name == "present_plan":
            if any(not step or len(step) > 120 for step in args.steps):
                return {"error": "INVALID_PLAN", "message": "计划每步应为1至120字"}
            self.state.plan = args.steps
            await self.emit(Progress(kind="plan", text="；".join(args.steps)))
            return {"steps": args.steps}
        if name == "finish":
            if "request_purchase_confirmation" not in args.model_fields_set:
                args = args.model_copy(
                    update={
                        "request_purchase_confirmation": self.state.purchase_proposal is not None,
                    }
                )
            return await self.finish(args)
        if name == "get_purchase_status":
            if self.purchase_reader is None:
                return {"items": [], "message": "本次运行未接交易结果查询，不能宣称订单已创建"}
            result = await self.purchase_reader()
            self.purchases = result
            self.observe(name, "已读取真实购买状态；未决请求不重新购买")
            return {"items": result, "feedback": purchase_feedback(result)}
        if name == "prepare_purchase":
            if "evidence" not in args.model_fields_set and self.user_message:
                # 当前发言是HTTP已收到的事实，无需模型复述；旧调用显式传错原话仍会拒绝。
                args = args.model_copy(update={"evidence": self.user_message[:500]})
            if error := self.evidence_error(args.evidence):
                return error
            self.purchase_preparation_attempted = True
            feedback = purchase_feedback(self.purchases)
            if not feedback["can_prepare_new_purchase"]:
                return {
                    "error": "PURCHASE_UNRESOLVED",
                    "message": "账号仍有未决购买，只回查原编号，不生成替代购买报价",
                    "feedback": feedback,
                }
            item = await self.read(args.voucher_id)
            evaluation = self.check(item)
            if evaluation.verdict == "ineligible":
                return {
                    "error": "PURCHASE_INELIGIBLE",
                    "evaluation": evaluation.model_dump(mode="json"),
                    "message": "已知条件不符，按issues选择重查/替代/澄清，不能放宽硬条件",
                }
            # 仅准备五分钟内有效的单券报价。模型无确认/submit工具，ID不是购买许可。
            self.state.purchase_proposal = PurchaseProposal(
                id=str(uuid5(NAMESPACE_URL, "perkflow:purchase:" + uuid4().hex)),
                evaluation=evaluation,
                requirements=self.state.requirements.model_copy(deep=True),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            return {
                "proposal": self.state.purchase_proposal.model_dump(mode="json"),
                "message": "待用户在页面明确确认，尚未提交购买；未知项须知悉",
            }
        # 2. 相同目标下同一查询最多执行两次，继续重复时反馈而非无限查同一份缺失资料。
        if name in {"read_previous_choices", "compare_group_buys"}:
            signature = name + args.model_dump_json()
            self.repetitions[signature] = self.repetitions.get(signature, 0) + 1
            if self.repetitions[signature] > 2:
                return {
                    "error": "REPEATED_ACTION",
                    "message": "同一动作已执行两次，请使用已有结果或必要澄清",
                }
        if name == "read_previous_choices":
            # 2. 不从历史回答猜券ID；仅允许引用本账号已保存卡片的有效位置，再查库获取新事实。
            choices = (
                self.previous_sets[args.turns_back - 1]
                if args.turns_back <= len(self.previous_sets)
                else []
            )
            if len(set(args.positions)) != len(args.positions) or any(
                position > len(choices) for position in args.positions
            ):
                return {
                    "error": "UNKNOWN_CHOICE",
                    "message": "该上轮方案编号不存在，请确认指的是哪张券",
                }
            results = []
            for position in args.positions:
                identifier = choices[position - 1].voucher_id
                result = self.check(await self.read(identifier))
                results.append({"position": position, "evaluation": result.model_dump(mode="json")})
            self.observe(
                name,
                "按上轮展示编号重新读取，适用结论使用本轮条件",
                [row["evaluation"]["voucher"]["voucher_id"] for row in results],
            )
            return {
                "turns_back": args.turns_back,
                "choices": results,
                "decision_feedback": decision_feedback(
                    [
                        self.evaluations[row["evaluation"]["voucher"]["voucher_id"]]
                        for row in results
                    ]
                ),
            }
        if name in {"search_group_buys", "discover_options"}:
            async with asyncio.timeout(self.query_timeout), self.database.sessions() as session:
                if name == "discover_options":
                    result = await group_buys.discover(session, self.state.requirements)
                    self.remember_catalog(result)
                    self.observe(name, json.dumps(result, ensure_ascii=False))
                    return result
                prefs = self.state.preferences
                search = SearchInput(
                    requirements=self.state.requirements,
                    page=args.page,
                    keywords=args.keywords,
                    sort=args.sort or ("score" if prefs.high_rating else "price"),
                    preferred_area=prefs.area,
                    preferred_category=prefs.category,
                )
                result = await group_buys.search(session, search)
            assessments = []
            for item in result.items:
                self.seen[item.voucher_id] = item
                evaluation = group_buys.evaluate(item, self.state.requirements).model_copy(
                    update={"preference_notes": self.preference_notes(item)}
                )
                # 搜索已在本轮完成确定性校验，可供finish引用；结束前仍重读，目标变更仍失效。
                # 不把所有搜索结果强行展示成卡片，展示选择仍交给模型。
                self.evaluations[item.voucher_id] = evaluation
                assessments.append(assessment(evaluation))
            summary = (
                f"策略{args.model_dump_json()}：候选共{result.total}项，本页{len(result.items)}项"
            )
            if result.feedback:
                summary += "；" + result.feedback.model_dump_json()
            self.observe(name, summary, [item.voucher_id for item in result.items])
            return {
                **result.model_dump(mode="json"),
                "strategy": summary,
                "assessments": assessments,
                "category_interpretation": self.state.category_mapping.model_dump()
                if self.state.category_mapping
                else None,
                "decision_feedback": decision_feedback(
                    [self.evaluations[item.voucher_id] for item in result.items], result.feedback
                ),
                "assessment_scope": "仅本页，缺失规则不会通过重读自行补全；不修改目标或提交购买",
            }
        if name == "compare_group_buys":
            known = set(self.seen) | {row.voucher_id for rows in self.previous_sets for row in rows}
            if len(set(args.voucher_ids)) != len(args.voucher_ids) or any(
                identifier not in known for identifier in args.voucher_ids
            ):
                return {
                    "error": "UNSEEN_CANDIDATE",
                    "message": "只能比较本轮已查询或最近已展示的不同券",
                }
            results = [self.check(await self.read(identifier)) for identifier in args.voucher_ids]
            self.comparison_ids = list(args.voucher_ids)
            self.observe(name, "已重读价格、评分及规则并列出偏好取舍", args.voucher_ids)
            return {
                "items": [result.model_dump(mode="json") for result in results],
                "decision_feedback": decision_feedback(results),
            }
        item = await self.read(args.voucher_id)
        self.observe(
            name,
            "已读取最新详情" if name == "get_group_buy_detail" else "已校验当前条件",
            [item.voucher_id],
        )
        if name == "get_group_buy_detail":
            return item.model_dump(mode="json")
        result = self.check(item)
        return {**result.model_dump(mode="json"), "decision_feedback": decision_feedback([result])}
