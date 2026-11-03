"""团购使用规则与消费约束；销售类型和权益形态分别建模。"""

from datetime import date, time
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UsageRule(Contract):
    version: Literal[1] = 1
    benefit_kind: Literal["package", "cash", "unknown"] = "unknown"
    min_people: int | None = Field(default=None, ge=1, le=50, strict=True)
    max_people: int | None = Field(default=None, ge=1, le=50, strict=True)
    package_items: list[str] = Field(default_factory=list, max_length=15)
    covers_meal: bool | None = None
    allowed_weekdays: list[int] | None = Field(default=None, max_length=7)
    daily_start: time | None = None
    daily_end: time | None = None
    reservation_required: bool | None = None
    extra_cost_fen: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    holiday_policy: Literal["same_weekdays", "unknown"] = "unknown"
    stackable: bool | None = None
    note: str = Field(default="使用规则未补全", max_length=500)

    @model_validator(mode="after")
    def check_rules(self) -> Self:
        if self.min_people and self.max_people and self.min_people > self.max_people:
            raise ValueError("适用人数上下限不合法")
        if self.allowed_weekdays is not None and (
            not self.allowed_weekdays
            or any(type(day) is not int or day not in range(7) for day in self.allowed_weekdays)
            or len(set(self.allowed_weekdays)) != len(self.allowed_weekdays)
        ):
            raise ValueError("星期使用0至6的不重复整数，0代表周一")
        if (self.daily_start is None) != (self.daily_end is None):
            raise ValueError("使用时段必须同时提供起止")
        if self.daily_start is not None:
            if self.daily_start.tzinfo or self.daily_end.tzinfo:
                raise ValueError("使用时段按北京时间填写，不附时区")
            if self.daily_start >= self.daily_end:
                raise ValueError("本阶段只支持同一天内的使用时段")
        if any(not item or len(item) > 100 for item in self.package_items):
            raise ValueError("套餐项不能为空或超过100字")
        return self


class Requirements(Contract):
    city: str = Field(default="天津", min_length=1, max_length=32)
    area: str | None = Field(default=None, min_length=1, max_length=64)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    # 旧JSON默认字面检索；新口语目标可选择语义探索，相关目录不能等同菜品已核实。
    category_matching: Literal["legacy", "literal", "semantic"] = "legacy"
    category_matches: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=3
    )
    people: int | None = Field(default=None, ge=1, le=50, strict=True)
    budget_fen: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    use_date: date | None = None
    use_time: time | None = None
    allow_reservation: bool | None = None
    use_weekdays: list[Annotated[int, Field(strict=True, ge=0, le=6)]] | None = Field(
        default=None, max_length=7
    )

    @model_validator(mode="after")
    def check_weekdays(self) -> Self:
        if self.category_matches and (
            not self.category
            or self.category_matching != "semantic"
            or len(set(self.category_matches)) != len(self.category_matches)
        ):
            raise ValueError("相关类目只用于有原始品类的语义探索，不能重复或覆盖字面限制")
        if self.use_weekdays is not None and (
            not self.use_weekdays
            or any(type(day) is not int or day not in range(7) for day in self.use_weekdays)
            or len(set(self.use_weekdays)) != len(self.use_weekdays)
        ):
            raise ValueError("星期约束使用0至6的不重复整数，周末可保留为[5,6]")
        if self.use_weekdays and self.use_date and self.use_date.weekday() not in self.use_weekdays:
            raise ValueError("具体日期与星期范围冲突，请按用户新表达纠正或明确清除旧范围")
        return self


class RequirementPatch(Requirements):
    """只提交需要变化的字段；显式null清除条件，未提交字段保留。"""


class SearchInput(Contract):
    requirements: Requirements = Field(default_factory=Requirements)
    page: int = Field(default=1, ge=1, le=1000, strict=True)
    page_size: int = Field(default=8, ge=1, le=20, strict=True)
    keywords: list[str] = Field(default_factory=list, max_length=3)
    sort: Literal["price", "score"] = "price"
    preferred_area: str | None = Field(default=None, min_length=1, max_length=64)
    preferred_category: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def check_keywords(self) -> Self:
        if any(not word or len(word) > 40 for word in self.keywords):
            raise ValueError("探索关键词应为1至40字")
        return self


class EvaluateInput(Contract):
    voucher_id: int = Field(ge=1, le=2**64 - 1, strict=True)
    requirements: Requirements


class GroupBuy(Contract):
    voucher_id: int
    shop_id: int
    title: str
    shop_name: str
    city: str
    area: str
    category: str
    image: str
    pay_value: int
    face_value: int
    stock_snapshot: int
    sale_state: str
    validity_days: int
    rules_text: list[str]
    usage: UsageRule
    checked_at: str
    score: float | None = None


class RuleIssue(Contract):
    code: str
    message: str
    source: Literal["user", "merchant", "conflict", "verification"]
    fields: list[str] = Field(default_factory=list)


class Evaluation(Contract):
    voucher: GroupBuy
    verdict: Literal["eligible", "ineligible", "unknown"]
    reasons: list[str]
    unknowns: list[str]
    total_cost_fen: int | None
    stage: Literal["candidate", "checked"] = "checked"
    preference_notes: list[str] = Field(default_factory=list, max_length=5)
    issues: list[RuleIssue] = Field(default_factory=list)


class SearchFeedback(Contract):
    code: Literal["MATCHES", "EMPTY_PAGE", "NO_MATCH"]
    message: str
    # 仅空结果查询计算单独移除某过滤项的数量，诊断不改变原查询、更不返回越界商品。
    city_catalog_count: int | None = None
    counts_if_omitted: dict[str, int] = Field(default_factory=dict)
    requires_user_permission: list[str] = Field(default_factory=list)
    scope: str = "商品目录计数，不是使用资格、实时库存或最小冲突集合证明"


class SearchResult(Contract):
    items: list[GroupBuy]
    total: int
    page: int
    page_size: int
    feedback: SearchFeedback | None = None
    note: str = "按当前购买金额检索；使用规则需另行校验，库存为MySQL快照，不保证购买成功"
