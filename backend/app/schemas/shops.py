from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class ShopTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    icon: str
    sort: int


class ShopTypeList(BaseModel):
    items: list[ShopTypeOut]


class ShopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type_id: int
    name: str
    subtitle: str
    kind: str
    city: str
    area: str
    address: str
    images: list[str]
    tags: list[str]
    score: float
    avg_price: int = Field(description="人均金额，单位：分")
    open_hours: str
    description: str


class ShopUpdate(BaseModel):
    """本地维护允许的部分更新，未传字段保持原值；不允许清空必填字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None
    open_hours: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)] | None
    ) = None
    avg_price: Annotated[int, Field(ge=0, le=4294967295)] | None = None

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("至少提供一个需要维护的字段")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("维护字段不能显式设为 null")
        return self


class ShopUpdateResult(BaseModel):
    shop: ShopOut
    changed_fields: list[str]
    database_committed: bool
    cache_invalidated: bool


class ShopFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category_id: int | None = Field(default=None, ge=1)
    city: str | None = Field(default=None, min_length=1, max_length=32)
    area: str | None = Field(default=None, min_length=1, max_length=64)
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    q: str | None = Field(default=None, min_length=1, max_length=100)
    sort: Literal["id", "score", "price"] = "id"
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=10, ge=1, le=50)


class ShopPage(BaseModel):
    items: list[ShopOut]
    total: int
    page: int
    page_size: int
