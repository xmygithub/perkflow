"""只读助手的可序列化业务状态；不接收客户端伪造的工具结果。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from backend.app.schemas.group_buys import Contract, Evaluation, Requirements


class ChatMessage(Contract):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class Progress(Contract):
    kind: Literal["plan", "requirements", "tool_start", "tool_end", "answer", "error"]
    text: str = Field(max_length=8000)
    tool: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    outcome_code: str | None = None


class Preferences(Contract):
    area: str | None = Field(default=None, min_length=1, max_length=64)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    low_price: bool = False
    high_rating: bool = False
    avoid_reservation: bool = False
    notes: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=5
    )


class Observation(Contract):
    tool: str
    summary: str = Field(max_length=1000)
    voucher_ids: list[int] = Field(default_factory=list, max_length=20)


class ChoiceReference(Contract):
    voucher_id: int = Field(ge=1, strict=True)
    title: str
    shop_name: str


class PurchaseProposal(Contract):
    id: str
    evaluation: Evaluation
    requirements: Requirements
    quantity: Literal[1] = 1
    expires_at: datetime


class ConditionOrigin(Contract):
    strength: Literal["required", "preferred", "clear"]
    source_turn: int = Field(ge=0)
    source_text: str = Field(max_length=500)
    revision: int = Field(ge=0)


class CategoryMapping(Contract):
    expression: str
    categories: list[str]
    source_turn: int = Field(ge=0)
    city: str
    area: str | None
    budget_fen: int | None
    source: Literal["observed_catalog", "narrowed_mapping"] = "observed_catalog"
    certainty: Literal["related_only"] = "related_only"


class ConversationState(Contract):
    requirements: Requirements = Field(default_factory=Requirements)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=12)
    plan: list[str] = Field(default_factory=list, max_length=6)
    cards: list[Evaluation] = Field(default_factory=list, max_length=8)
    events: list[Progress] = Field(default_factory=list, max_length=40)
    goal: str = Field(default="", max_length=300)
    phase: Literal["explore", "compare", "verify"] = "explore"
    preferences: Preferences = Field(default_factory=Preferences)
    condition_sources: dict[str, str] = Field(default_factory=lambda: {"city": "平台当前城市"})
    observations: list[Observation] = Field(default_factory=list, max_length=12)
    questions: list[str] = Field(default_factory=list, max_length=2)
    candidate_ids: list[int] = Field(default_factory=list, max_length=3)
    comparisons: list[Evaluation] = Field(default_factory=list, max_length=3)
    focused_id: int | None = Field(default=None, ge=1, strict=True)
    choice_history: list[Annotated[list[ChoiceReference], Field(max_length=8)]] = Field(
        default_factory=list, max_length=2
    )
    purchase_proposal: PurchaseProposal | None = None
    # 新字段默认兼容旧会话；轮次从升级后的成功交互计数，不伪造早期完整历史。
    intents: list[str] = Field(default_factory=list, max_length=4)
    goal_revision: int = Field(default=0, ge=0)
    turn_number: int = Field(default=0, ge=0)
    condition_details: dict[str, ConditionOrigin] = Field(default_factory=dict)
    category_mapping: CategoryMapping | None = None


class ConversationOut(Contract):
    id: str
    version: int
    state: ConversationState


class TurnInput(Contract):
    message: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=0, le=10000, strict=True)


class TurnResult(Contract):
    conversation: ConversationOut
    answer: str
    recommended_ids: list[int] = Field(default_factory=list, max_length=3)
    candidate_ids: list[int] = Field(default_factory=list, max_length=3)


class ToolFunction(Contract):
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(max_length=10000)


class ToolCall(Contract):
    id: str = Field(min_length=1, max_length=100)
    type: Literal["function"] = "function"
    function: ToolFunction


class ModelUsage(Contract):
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


class ModelMessage(Contract):
    role: Literal["assistant"] = "assistant"
    content: str | None = Field(default=None, max_length=8000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=4)
    usage: ModelUsage | None = None
