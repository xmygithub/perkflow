from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.schemas.vouchers import OrderOut


class RedisPurchase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[1-9]\d{0,18}$")
    user_id: str = Field(pattern=r"^[1-9]\d{0,19}$")
    voucher_id: str = Field(pattern=r"^[1-9]\d{0,19}$")
    request_key: str = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    generation: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: datetime

    @model_validator(mode="after")
    def valid_identifiers(self):
        if int(self.request_id) >= 2**63 or max(int(self.user_id), int(self.voucher_id)) >= 2**64:
            raise ValueError("编号超出数据库范围")
        if self.created_at.tzinfo is None:
            raise ValueError("创建时间需要时区")
        return self


class OrderMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1, 2] = 1
    # 长整数按字符串跨语言传递；身份、价格等以数据库中的受理记录为准。
    request_id: str = Field(pattern=r"^[1-9]\d{0,18}$")
    purchase: RedisPurchase | None = None

    @model_validator(mode="after")
    def valid_version(self):
        if (self.version == 2) != (self.purchase is not None):
            raise ValueError("消息版本与载荷不匹配")
        if self.purchase and self.purchase.request_id != self.request_id:
            raise ValueError("消息编号不匹配")
        return self


class RequestOut(BaseModel):
    id: str
    voucher_id: int
    shop_name: str
    title: str
    status: Literal["pending", "succeeded", "failed"]
    order: OrderOut | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    # 曾确认准入，不代表此刻仍持有名额；前端只在 pending 时据此区分进度。
    reservation_acquired: bool = False


class RequestResult(BaseModel):
    request: RequestOut
    reused: bool


class RequestPage(BaseModel):
    items: list[RequestOut]
    total: int
    page: int
    page_size: int
