"""确认接口不接受用户ID、券ID、金额或任意下单参数。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from agent.schemas import PurchaseProposal
from backend.app.schemas.group_buys import Contract
from backend.app.schemas.order_requests import RequestOut


class ConfirmPurchase(Contract):
    proposal_id: UUID
    expected_version: int = Field(ge=0, le=10000, strict=True)
    acknowledge_unknowns: bool = Field(default=False, strict=True)


class PurchaseOut(Contract):
    id: str
    conversation_id: str
    created_at: datetime
    request_id: str | None = None
    proposal: PurchaseProposal
    status: Literal["submitting", "pending", "succeeded", "failed"]
    request: RequestOut | None = None
    error_code: str | None = None
    message: str
    lookup_available: bool = True
    lookup_source: Literal["mysql", "redis", "saved", "not_found", "unavailable"] = "saved"
    checked_at: datetime | None = None
    needs_manual_review: bool = False
    recovery_message: str = ""


class PurchasePage(Contract):
    items: list[PurchaseOut]
    total: int
    page: int
    page_size: int
    unresolved_count: int
