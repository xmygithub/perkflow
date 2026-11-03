from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utc(value: datetime) -> datetime:
    # MySQL DATETIME 没有时区标记；项目连接与存储约定为 UTC。
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class VoucherFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shop_id: int | None = Field(default=None, ge=1)
    type: int | None = Field(default=None, ge=0, le=1)
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=20, ge=1, le=50)


class VoucherOut(BaseModel):
    id: int
    shop_id: int
    shop_name: str
    shop_image: str
    title: str
    sub_title: str
    rules: list[str]
    pay_value: int
    actual_value: int
    type: Literal[0, 1]
    status: int
    stock: int
    total_stock: int
    validity_days: int
    begin_time: datetime | None
    end_time: datetime | None
    state: Literal["active", "upcoming", "ended", "sold_out", "off_shelf", "unavailable"]


class VoucherPage(BaseModel):
    items: list[VoucherOut]
    total: int
    page: int
    page_size: int


class OrderFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["created", "cancelled"] | None = None
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=20, ge=1, le=50)


class OrderOut(BaseModel):
    id: str
    voucher_id: int
    shop_id: int
    shop_name: str
    shop_image: str
    title: str
    pay_value: int
    status: Literal["created", "cancelled"]
    created_at: datetime
    expires_at: datetime
    cancelled_at: datetime | None = None


class OrderPage(BaseModel):
    items: list[OrderOut]
    total: int
    page: int
    page_size: int


class OrderResult(BaseModel):
    order: OrderOut
    reused: bool


class CancelOrderResult(BaseModel):
    order: OrderOut
    reused: bool
