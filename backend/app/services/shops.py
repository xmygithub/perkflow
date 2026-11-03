import asyncio
import logging
import secrets
from random import randint

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.cache_lock import fill_cache_if_owner, release_cache_lock
from backend.app.core.config import Settings
from backend.app.core.errors import ApiError
from backend.app.repositories import shops as repository
from backend.app.schemas.shops import (
    ShopFilters,
    ShopOut,
    ShopPage,
    ShopTypeList,
    ShopTypeOut,
    ShopUpdate,
    ShopUpdateResult,
)

logger = logging.getLogger("uvicorn.error")


async def list_shop_types(session: AsyncSession, cache: Redis, settings: Settings) -> ShopTypeList:
    # 1. 全局分类共用一个 List，元素是分类 JSON，按数据库 sort、id 的顺序保存。
    key = f"{settings.cache_key_prefix}shop-types:all"
    try:
        values = await cache.lrange(key, 0, -1)
        if values:
            result = ShopTypeList(items=[ShopTypeOut.model_validate_json(v) for v in values])
            logger.info("店铺分类缓存 HIT")
            return result
    except (RedisError, ValidationError) as exc:
        # 缓存故障或格式损坏可以回源；只记错误类型，不打印缓存内容和连接凭据。
        logger.warning("店铺分类缓存读取失败，回源 MySQL：%s", type(exc).__name__)

    # 2. 未命中时查询 MySQL；数据库异常继续交给统一错误处理，不伪装成空列表。
    logger.info("店铺分类缓存 MISS，查询 MySQL")
    result = ShopTypeList(items=await repository.list_shop_types(session))

    # 3. Redis 不保留空 List；本阶段不缓存空分类，空清单下次仍会回源。
    if result.items:
        try:
            # 4. MULTI/EXEC 整体替换并设置 Key 级 TTL，避免重复追加和半份列表。
            # 此事务只协调 Redis 命令，不保证 MySQL/Redis 跨系统一致性。
            # 每次完整回填抽取一次随机秒数；整个 List 共用此期限，不给每个分类单独续期。
            ttl = settings.shop_type_cache_ttl + randint(0, settings.shop_type_cache_ttl_jitter)
            async with cache.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                pipe.rpush(key, *(item.model_dump_json() for item in result.items))
                pipe.expire(key, ttl)
                await pipe.execute()
        except RedisError as exc:
            logger.warning("店铺分类缓存回填失败，返回数据库结果：%s", type(exc).__name__)
    return result


async def _read_shop_cache(cache: Redis, key: str, shop_id: int) -> tuple[bool, ShopOut | None]:
    # 用独立命中标志区分空值命中和未命中，供初次读取、等待和持锁后二次检查共用。
    try:
        value = await cache.get(key)
        # 2. 空字符串是已确认不存在的标记；None 才是 Key 不存在，不能用 if value 混淆。
        # 先判断标记，再解析商户 JSON；命中只返回 None，由 Router 保持原有 404 响应。
        if value == "":
            logger.info("商户详情空值缓存 HIT shop_id=%s", shop_id)
            return True, None
        if value is not None:
            result = ShopOut.model_validate_json(value)
            if result.id == shop_id:
                logger.info("商户详情缓存 HIT shop_id=%s", shop_id)
                return True, result
            logger.warning("商户详情缓存 ID 不匹配，回源 MySQL shop_id=%s", shop_id)
    except ValidationError:
        logger.warning("商户详情缓存 JSON 损坏，进入重建 shop_id=%s", shop_id)
    except ResponseError as exc:
        if not str(exc).startswith("WRONGTYPE"):
            raise
        logger.warning("商户详情缓存类型损坏，进入重建 shop_id=%s", shop_id)
    return False, None


async def _wait_for_shop_rebuild(
    cache: Redis, key: str, lock_key: str, token: str, shop_id: int, settings: Settings
) -> tuple[bool, ShopOut | None]:
    # 等待预算覆盖 Redis 调用和异步休眠；不递归、不阻塞事件循环，也不超时后集体查库。
    try:
        async with asyncio.timeout(settings.shop_cache_lock_wait_ms / 1000):
            while True:
                acquired = await cache.set(
                    lock_key, token, nx=True, px=settings.shop_cache_lock_ttl_ms
                )
                if acquired:
                    return True, None
                hit, result = await _read_shop_cache(cache, key, shop_id)
                if hit:
                    return False, result
                await asyncio.sleep(settings.shop_cache_lock_retry_ms / 1000)
    except TimeoutError as exc:
        raise ApiError(
            503, "CACHE_REBUILD_BUSY", "商户缓存正在重建，请稍后重试", retry_after=1
        ) from exc


async def _query_shop_and_fill(
    session: AsyncSession,
    shop_id: int,
    cache: Redis,
    settings: Settings,
    key: str,
    owner: tuple[str, str] | None = None,
) -> ShopOut | None:

    # 数据库故障向上传递，不伪装成不存在；健康 Redis 路径由外层持有重建租约。
    logger.info("商户详情缓存 MISS，查询 MySQL shop_id=%s", shop_id)
    shop = await repository.get_shop(session, shop_id)
    result = ShopOut.model_validate(shop) if shop is not None else None
    # 保留穿透与雪崩处理：空值固定短 TTL，正常 JSON 每次回填计算随机偏移。
    value = result.model_dump_json() if result is not None else ""
    ttl = (
        settings.shop_cache_ttl + randint(0, settings.shop_cache_ttl_jitter)
        if result is not None
        else settings.shop_null_cache_ttl
    )
    try:
        if owner is None:
            # Redis 故障降级沿用原回源规则，尚无限流；不声称此分支具备互斥保证。
            await cache.set(key, value, ex=ttl)
        elif not await fill_cache_if_owner(cache, *owner, key, value, ttl):
            # 查询超过租约时仍可返回本次数据库结果，但旧持有者不能覆盖其他重建者。
            logger.warning("商户重建租约已失效，跳过回填 shop_id=%s", shop_id)
    except RedisError as exc:
        logger.warning("商户详情缓存回填未确认，返回数据库结果：%s", type(exc).__name__)
    return result


async def get_shop(
    session: AsyncSession, shop_id: int, cache: Redis, settings: Settings
) -> ShopOut | None:
    # 1. 先读公开缓存，正常 JSON 或空值命中直接返回，完全不申请锁、不刷新 TTL。
    key = f"{settings.cache_key_prefix}shop:{shop_id}"
    try:
        hit, result = await _read_shop_cache(cache, key, shop_id)
        if hit:
            return result
    except RedisError as exc:
        logger.warning("商户缓存读取失败，降级回源：%s", type(exc).__name__)
        return await _query_shop_and_fill(session, shop_id, cache, settings, key)

    # 2. 按商户主键协调，同一家店共用锁，不让不同商户互相等待。
    # 随机标识代表这一次获取锁的请求，不使用所有请求相同的常量或用户会话编号。
    lock_key = f"{settings.cache_lock_key_prefix}shop:{shop_id}"
    token = secrets.token_urlsafe(32)
    acquired = False
    try:
        try:
            acquired, result = await _wait_for_shop_rebuild(
                cache, key, lock_key, token, shop_id, settings
            )
            if not acquired:
                return result
            # 3. 拿到锁后再查缓存，避免其他请求已回填而本请求仍重复查询数据库。
            hit, result = await _read_shop_cache(cache, key, shop_id)
            if hit:
                return result
        except RedisError as exc:
            logger.warning("商户重建锁/二次读取失败，降级回源：%s", type(exc).__name__)
        # 4. 有租约时用 Lua 检查所属并原子回填；无租约的 Redis 故障分支沿用旧规则。
        owner = (lock_key, token) if acquired else None
        return await _query_shop_and_fill(session, shop_id, cache, settings, key, owner)
    finally:
        # 5. 成功、查询异常或取消都尝试释放自己的锁；未获得锁的请求不能释放他人锁。
        if acquired:
            await release_cache_lock(cache, lock_key, token)


async def list_shops(session: AsyncSession, filters: ShopFilters) -> ShopPage:
    shops, total = await repository.list_shops(session, filters)
    return ShopPage(items=shops, total=total, page=filters.page, page_size=filters.page_size)


async def invalidate_shop_cache(cache: Redis, settings: Settings, shop_id: int) -> bool:
    if type(shop_id) is not int or shop_id < 1:
        raise ValueError("商户 ID 必须为正整数")
    # DEL 返回 0 也代表目标缓存已不存在，重复执行安全，不把 TTL 重设或写旧快照。
    key = f"{settings.cache_key_prefix}shop:{shop_id}"
    try:
        await cache.delete(key)
        return True
    except RedisError as exc:
        # 网络异常可能发生在命令已执行后，因此只能说删除未确认，不能断言未删除。
        logger.warning("商户缓存删除未确认 shop_id=%s：%s", shop_id, type(exc).__name__)
        return False


async def update_shop(
    session: AsyncSession, shop_id: int, changes: ShopUpdate, cache: Redis, settings: Settings
) -> ShopUpdateResult:
    # 1. Service 持有写事务边界，调用方提供独立且未开启事务的 Session。
    if type(shop_id) is not int or shop_id < 1:
        raise ValueError("商户 ID 必须为正整数")
    if session.in_transaction():
        raise ValueError("维护操作需要独立、未开启事务的 Session")
    async with asyncio.timeout(settings.mysql_query_timeout):
        async with session.begin():
            # 2. 从 MySQL 锁定当前记录，不以可能陈旧的 Redis 内容作为修改依据。
            shop = await repository.get_shop_for_update(session, shop_id)
            if shop is None:
                raise ApiError(404, "SHOP_NOT_FOUND", "商户不存在")
            changed_fields = await repository.update_shop(session, shop, changes)
            snapshot = ShopOut.model_validate(shop)
        # 离开 begin 上下文并确认成功，才代表数据库事务已经提交。

    # 3. 提交后再删缓存，数据库异常/回滚会跳过这里；Redis 不参与 MySQL 事务。
    # 即使值未变化也删缓存，允许修复已有旧快照。更新不改分类表，不删分类 List。
    invalidated = await invalidate_shop_cache(cache, settings, shop_id)
    # 4. 返回两部分的真实结果，删除未确认也不能说数据库已经回滚或要求重做修改。
    return ShopUpdateResult(
        shop=snapshot,
        changed_fields=changed_fields,
        database_committed=True,
        cache_invalidated=invalidated,
    )
