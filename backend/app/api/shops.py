from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from backend.app.core.config import SettingsDep
from backend.app.core.database import SessionDep
from backend.app.core.redis_connection import CacheRedisDep
from backend.app.core.session_refresh import SessionRefreshRoute
from backend.app.schemas.shops import ShopFilters, ShopOut, ShopPage, ShopTypeList
from backend.app.services import shops as service

# 公开查询不要求登录，但已登录用户浏览商户同样属于活跃访问。
router = APIRouter(prefix="/api", tags=["商户查询"], route_class=SessionRefreshRoute)


@router.get("/shop-types", response_model=ShopTypeList)
async def shop_types(
    session: SessionDep, cache: CacheRedisDep, settings: SettingsDep
) -> ShopTypeList:
    return await service.list_shop_types(session, cache, settings)


@router.get("/shops", response_model=ShopPage)
async def shops(session: SessionDep, filters: Annotated[ShopFilters, Query()]) -> ShopPage:
    return await service.list_shops(session, filters)


@router.get("/shops/{shop_id}", response_model=ShopOut)
async def shop_detail(
    session: SessionDep,
    shop_id: Annotated[int, Path(ge=1)],
    cache: CacheRedisDep,
    settings: SettingsDep,
) -> ShopOut:
    shop = await service.get_shop(session, shop_id, cache, settings)
    if shop is None:
        raise HTTPException(status_code=404, detail="商户不存在")
    return shop
