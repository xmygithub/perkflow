from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Shop, ShopType
from backend.app.schemas.shops import ShopFilters, ShopUpdate


async def list_shop_types(session: AsyncSession) -> list[ShopType]:
    rows = await session.scalars(select(ShopType).order_by(ShopType.sort, ShopType.id))
    return list(rows)


async def get_shop(session: AsyncSession, shop_id: int) -> Shop | None:
    return await session.get(Shop, shop_id)


async def get_shop_for_update(session: AsyncSession, shop_id: int) -> Shop | None:
    # 写事务中锁定商户行，维护操作按顺序读取当前值；普通查询不会获得此锁。
    return await session.scalar(select(Shop).where(Shop.id == shop_id).with_for_update())


async def update_shop(session: AsyncSession, shop: Shop, changes: ShopUpdate) -> list[str]:
    changed_fields = []
    # 仅应用白名单模型中明确传入的字段；不覆盖未传字段，不在 Repository 提交。
    for field, value in changes.model_dump(exclude_unset=True).items():
        if getattr(shop, field) != value:
            setattr(shop, field, value)
            changed_fields.append(field)
    await session.flush()
    return changed_fields


async def list_shops(session: AsyncSession, filters: ShopFilters) -> tuple[list[Shop], int]:
    conditions = []
    for column, value in (
        (Shop.type_id, filters.category_id),
        (Shop.city, filters.city),
        (Shop.area, filters.area),
        (Shop.kind, filters.kind),
    ):
        if value is not None:
            conditions.append(column == value)
    if filters.q is not None:
        # Bound parameters plus escaping: % and _ in user input are literal characters.
        conditions.append(Shop.name.contains(filters.q, autoescape=True))
    total = await session.scalar(select(func.count()).select_from(Shop).where(*conditions))
    order = {
        "id": [Shop.id],
        "score": [Shop.score.desc(), Shop.id],
        "price": [Shop.avg_price, Shop.id],
    }
    rows = await session.scalars(
        select(Shop)
        .where(*conditions)
        .order_by(*order[filters.sort])
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    return list(rows), int(total or 0)
