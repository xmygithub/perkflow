"""统一目标更新合同：模型解释语义，确定性代码原子合并条件及来源。"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from agent.schemas import ConditionOrigin, ConversationState, Preferences
from backend.app.schemas.group_buys import Contract, Requirements

TEXT_FIELDS = {"area", "category"}
PREFERENCE_FIELDS = {"low_price", "high_rating", "notes"}
DUAL_FIELDS = TEXT_FIELDS | {"allow_reservation", "avoid_reservation"}


class ConditionChange(Contract):
    field: Literal[
        "city",
        "area",
        "category",
        "people",
        "budget_fen",
        "use_date",
        "use_time",
        "use_weekdays",
        "allow_reservation",
        "low_price",
        "high_rating",
        "avoid_reservation",
        "notes",
    ]
    strength: Literal["required", "preferred", "clear"] = Field(
        description="直接指定要找的范围/条件用required；明确最好/优先用preferred；用户撤销用clear。"
    )
    value: (
        str
        | Annotated[int, Field(strict=True)]
        | bool
        | list[Annotated[int, Field(strict=True)]]
        | list[str]
        | None
    ) = Field(description="金额单位分；日期YYYY-MM-DD；星期0至6；clear时必须null。")
    matching: Literal["semantic", "literal"] | None = Field(
        default=None,
        description="仅品类使用；口语默认semantic，明确只找标注/分类名时literal，不能改原意。",
    )

    @model_validator(mode="after")
    def check_mode(self) -> Self:
        if self.matching is not None and self.field != "category":
            raise ValueError("matching只描述品类表达，不改变预算/区域匹配方式")
        if (self.strength == "clear") != (self.value is None):
            raise ValueError("仅明确撤销条件时用clear及null，其余需提供值")
        if self.field == "city" and self.strength != "required":
            raise ValueError("城市必须明确提供，不能清空或作为软偏好")
        if self.strength == "preferred" and self.field not in DUAL_FIELDS | PREFERENCE_FIELDS:
            raise ValueError("人数、数值预算和使用条件用required；模糊便宜用low_price偏好")
        if self.strength == "required" and self.field in PREFERENCE_FIELDS:
            raise ValueError("排序偏好使用preferred，不替代硬条件")
        if (
            self.field in {"allow_reservation", "avoid_reservation"}
            and self.strength != "clear"
            and type(self.value) is not bool
        ):
            raise ValueError("预约条件使用布尔值；不接受预约为allow_reservation=false")
        return self


class GoalUpdate(Contract):
    intents: (
        list[
            Literal[
                "find",
                "compare",
                "check_rules",
                "prepare_purchase",
                "check_purchase",
                "change_goal",
                "explain",
                "other",
            ]
        ]
        | None
    ) = Field(default=None, min_length=1, max_length=4)
    goal: str | None = Field(default=None, min_length=1, max_length=300)
    phase: Literal["explore", "compare", "verify"] | None = None
    changes: list[ConditionChange] = Field(default_factory=list, max_length=13)

    @model_validator(mode="after")
    def check_changes(self) -> Self:
        fields = [
            "allow_reservation" if change.field == "avoid_reservation" else change.field
            for change in self.changes
        ]
        if len(fields) != len(set(fields)):
            raise ValueError("同一字段只更新一次，不同时声明硬条件和软偏好")
        if not self.model_fields_set:
            raise ValueError("无需更新目标时直接选择有价值的查询或结束")
        if self.intents and len(self.intents) != len(set(self.intents)):
            raise ValueError("意图不重复")
        return self


def merge_goal(state: ConversationState, args: GoalUpdate, message: str):
    # 1. 在副本上同时修改硬条件与偏好；字段切换强弱时清除旧位置，未提及项保留。
    hard = state.requirements.model_dump(mode="json")
    soft = state.preferences.model_dump(mode="json")
    category_changed = False
    for change in args.changes:
        name, strength, value = change.field, change.strength, change.value
        if name == "avoid_reservation":
            # 兼容已有模型调用的同义字段；当前Schema只暴露allow_reservation，统一强弱语义。
            name, value = "allow_reservation", (not value if value is not None else None)
        if name == "allow_reservation":
            hard[name] = value if strength == "required" else None
            soft["avoid_reservation"] = not value if strength == "preferred" else False
        elif name in TEXT_FIELDS:
            hard[name] = value if strength == "required" else None
            soft[name] = value if strength == "preferred" else None
            if name == "category":
                mode = change.matching or "semantic"
                category_changed = (
                    hard[name] != state.requirements.category
                    or soft[name] != state.preferences.category
                    or mode != state.requirements.category_matching
                )
                if category_changed:
                    hard["category_matching"] = mode
                    hard["category_matches"] = []
        elif name in PREFERENCE_FIELDS:
            soft[name] = value if strength != "clear" else ([] if name == "notes" else False)
        else:
            hard[name] = value
    # 2. 所有类型/跨字段约束一起验证，任一失败都不修改会话、不失效旧报价。
    requirements = Requirements.model_validate(hard)
    preferences = Preferences.model_validate(soft)
    changed = requirements != state.requirements or preferences != state.preferences
    revision = state.goal_revision + int(changed)
    details, sources = dict(state.condition_details), dict(state.condition_sources)
    for change in args.changes:
        name = "allow_reservation" if change.field == "avoid_reservation" else change.field
        details[name] = ConditionOrigin(
            strength=change.strength,
            source_turn=state.turn_number,
            source_text=message[:500],
            revision=revision,
        )
        sources[name] = f"用户第{state.turn_number}轮：" + message[:160]
    # 3. 原话与轮次由服务端绑定；来源不等于语义正确，更不是购买授权。
    return {
        "requirements": requirements,
        "preferences": preferences,
        "condition_details": details,
        "condition_sources": sources,
        "goal_revision": revision,
        "intents": args.intents if args.intents is not None else state.intents,
        "goal": args.goal if args.goal is not None else state.goal,
        "phase": args.phase if args.phase is not None else state.phase,
        "category_mapping": None if category_changed else state.category_mapping,
    }, changed
