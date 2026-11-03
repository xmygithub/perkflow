import random
from asyncio import sleep
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import ApiError
from backend.app.core.order_id import RedisOrderId
from backend.app.core.order_lock import RedisOrderLock
from backend.app.models import VoucherOrder
from backend.app.repositories import vouchers as repository
from backend.app.schemas.vouchers import (
    CancelOrderResult,
    OrderFilters,
    OrderOut,
    OrderPage,
    OrderResult,
    VoucherFilters,
    VoucherOut,
    VoucherPage,
    utc,
)
from backend.app.services.seckill import queue_cancel

# 只对数据库明确报告的死锁做有限重试，不重试提交结果不确定的连接故障。
ORDER_TRANSACTION_ATTEMPTS = 3


def mysql_errno(error: DBAPIError) -> int | None:
    arguments = getattr(error.orig, "args", ())
    return arguments[0] if arguments and isinstance(arguments[0], int) else None


def voucher_out(row, now: datetime) -> VoucherOut:
    voucher, shop, seckill = row
    begin = utc(seckill.begin_time) if seckill else None
    end = utc(seckill.end_time) if seckill else None
    state = "active"
    if voucher.status != 1:
        state = "off_shelf"
    else:
        if not seckill:
            state = "unavailable"
        elif now < begin:
            state = "upcoming"
        elif now >= end:
            state = "ended"
    if state == "active" and voucher.stock == 0:
        state = "sold_out"
    return VoucherOut(
        id=voucher.id,
        shop_id=shop.id,
        shop_name=shop.name,
        shop_image=shop.images[0],
        title=voucher.title,
        sub_title=voucher.sub_title,
        rules=voucher.rules,
        pay_value=voucher.pay_value,
        actual_value=voucher.actual_value,
        type=voucher.type,
        status=voucher.status,
        stock=voucher.stock,
        total_stock=voucher.total_stock,
        validity_days=voucher.validity_days,
        begin_time=begin,
        end_time=end,
        state=state,
    )


def order_out(row) -> OrderOut:
    order, voucher, shop = row
    return OrderOut(
        id=str(order.id),
        voucher_id=order.voucher_id,
        shop_id=shop.id,
        shop_name=shop.name,
        shop_image=shop.images[0],
        title=voucher.title,
        pay_value=order.pay_value,
        status=order.status,
        created_at=utc(order.created_at),
        expires_at=utc(order.expires_at),
        cancelled_at=utc(order.cancelled_at) if order.cancelled_at else None,
    )


async def list_vouchers(session: AsyncSession, filters: VoucherFilters) -> VoucherPage:
    # 1. 分页查 MySQL，动态库存和时间状态不接入商户缓存。
    rows, total = await repository.list_vouchers(session, filters)
    now = datetime.now(UTC)
    # 2. 同一响应共用当前时间，返回金额分值及带 UTC 标记的活动时间。
    return VoucherPage(
        items=[voucher_out(row, now) for row in rows],
        total=total,
        page=filters.page,
        page_size=filters.page_size,
    )


async def get_voucher(session: AsyncSession, voucher_id: int) -> VoucherOut:
    row = await repository.get_voucher(session, voucher_id)
    if row is None:
        raise ApiError(404, "VOUCHER_NOT_FOUND", "优惠券不存在")
    return voucher_out(row, datetime.now(UTC))


async def list_orders(session: AsyncSession, user_id: int, filters: OrderFilters) -> OrderPage:
    rows, total = await repository.list_orders(session, user_id, filters)
    return OrderPage(
        items=[order_out(row) for row in rows],
        total=total,
        page=filters.page,
        page_size=filters.page_size,
    )


async def get_order(session: AsyncSession, user_id: int, order_id: int) -> OrderOut:
    row = await repository.get_order(session, user_id, order_id)
    if row is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "订单不存在")
    return order_out(row)


async def cancel_order(session: AsyncSession, user_id: int, order_id: int) -> CancelOrderResult:
    # 1. 取消靠 MySQL 订单行锁串行化，无须新增 Redis 锁或依赖它永不过期。
    for attempt in range(ORDER_TRANSACTION_ATTEMPTS):
        try:
            return await _cancel_order_once(session, user_id, order_id)
        except OperationalError as exc:
            code = mysql_errno(exc)
            # 2. 同下单策略：旧事务已整体回滚才重试明确死锁；不重试不确定的提交结果。
            if code not in {1205, 1213} or session.in_transaction():
                raise
            if code == 1205 or attempt == ORDER_TRANSACTION_ATTEMPTS - 1:
                raise ApiError(
                    503, "ORDER_BUSY", "订单正在处理，请先刷新订单，稍后重试", retry_after=1
                ) from exc
            await sleep(random.uniform(0.02, 0.06) * (attempt + 1))
    raise RuntimeError("订单事务尝试次数配置必须大于零")


async def _cancel_order_once(
    session: AsyncSession, user_id: int, order_id: int
) -> CancelOrderResult:
    async with session.begin():
        # 3. 在 SQL 中限制归属；其他用户与不存在的编号都返回 404，不泄漏订单存在性。
        order = await repository.lock_owned_order(session, user_id, order_id)
        if order is None:
            raise ApiError(404, "ORDER_NOT_FOUND", "订单不存在")
        reused = order.status == "cancelled"
        if not reused:
            # 4. 只有 created 能首次取消；状态变更、资格释放与返库存必须一起提交。
            if order.status != "created":
                raise ApiError(409, "ORDER_NOT_CANCELLABLE", "当前订单状态不允许取消")
            # 与取消/SQL 返库一起提交 Redis 归还待办；先与初始化协调，再修改真实库存。
            await queue_cancel(session, order)
            order.status = "cancelled"
            order.cancelled_at = datetime.now(UTC).replace(tzinfo=None)
            await session.flush()
            if not await repository.restore_stock(session, order.voucher_id):
                raise ApiError(503, "CANCEL_STOCK_CONFLICT", "库存状态异常，取消未完成，请稍后重试")
        # 5. 重复取消只返回原结果，不再加库存；旧订单 ID 永不重新激活或复用。
        result = CancelOrderResult(
            order=order_out(await repository.get_order(session, user_id, order_id)), reused=reused
        )
    return result


async def create_order(
    session: AsyncSession,
    user_id: int,
    voucher_id: int,
    ids: RedisOrderId,
    lock: RedisOrderLock,
) -> OrderResult:
    # 1. 在数据库事务开始前争抢同一用户/券的锁；失败者不查订单、不扣库存、不分配编号。
    token = await lock.try_acquire(user_id, voucher_id)
    if token is None:
        raise ApiError(
            409, "ORDER_IN_PROGRESS", "这张券的订单正在处理，请稍后查看我的订单", retry_after=1
        )
    try:
        # 2. 覆盖完整事务、有限死锁重试和重复回查；数据库防线不依赖租约一定有效。
        return await _create_order_with_retry(session, user_id, voucher_id, ids)
    finally:
        # 3. 内层事务已提交或回滚，再按本次 UUID 原子校验并释放；不删除后来者的锁。
        await lock.release(user_id, voucher_id, token)


async def _create_order_with_retry(
    session: AsyncSession,
    user_id: int,
    voucher_id: int,
    ids: RedisOrderId,
) -> OrderResult:
    # 1. 每次尝试执行完整事务；不在失败的旧事务里重新扣库存或插入。
    for attempt in range(ORDER_TRANSACTION_ATTEMPTS):
        try:
            return await _create_order_once(session, user_id, voucher_id, ids)
        except OperationalError as exc:
            code = mysql_errno(exc)
            # 2. 事务上下文已整体回滚。死锁 1213 可重试；锁等待超时 1205 不连续重试。
            if code not in {1205, 1213} or session.in_transaction():
                raise
            if code == 1205 or attempt == ORDER_TRANSACTION_ATTEMPTS - 1:
                raise ApiError(
                    503, "ORDER_BUSY", "下单竞争较多，请先查看我的订单，稍后重试", retry_after=1
                ) from exc
            # 3. 回滚后释放连接再让出事件循环，用短随机退避降低再次同时竞争的概率。
            #    下次重新查已有订单及活动；分配过的 Redis 编号仍不回收。
            await sleep(random.uniform(0.02, 0.06) * (attempt + 1))
    raise RuntimeError("订单事务尝试次数配置必须大于零")


def require_orderable(row, now: datetime) -> None:
    # 上下架/时间检查改善业务提示；是否能扣最后一份库存仍以条件 UPDATE 为准。
    errors = {
        "off_shelf": ("VOUCHER_OFF_SHELF", "优惠券已下架"),
        "unavailable": ("SECKILL_NOT_CONFIGURED", "优惠券购买活动尚未配置"),
        "upcoming": ("SECKILL_NOT_STARTED", "活动尚未开始"),
        "ended": ("SECKILL_ENDED", "活动已经结束"),
        "sold_out": ("OUT_OF_STOCK", "优惠券已抢完"),
    }
    state = voucher_out(row, now).state
    if state in errors:
        code, message = errors[state]
        raise ApiError(409, code, message)


async def _create_order_once(
    session: AsyncSession,
    user_id: int,
    voucher_id: int,
    ids: RedisOrderId,
) -> OrderResult:
    try:
        # 1. 一个事务覆盖规则读取、库存扣减和订单写入；未提交前不能报告成功。
        async with session.begin():
            existing = await repository.find_order(session, user_id, voucher_id)
            if existing is not None:
                return OrderResult(order=order_out(existing), reused=True)
            row = await repository.get_voucher(session, voucher_id)
            if row is None:
                raise ApiError(404, "VOUCHER_NOT_FOUND", "优惠券不存在")
            voucher, shop, _ = row
            require_orderable(row, datetime.now(UTC))
            # 2. 分配编号后不回退；Redis 不可用时停止下单，不用自增 ID 临时兜底。
            order_id = await ids.next_id()
            # 3. 条件扣减由 MySQL 协调并发；最后一份只能被一条更新扣掉。
            #    不使用 SET stock=之前读到的值-1，也不要求 stock=旧值而误拒绝正常竞争。
            if not await repository.decrement_stock(session, voucher_id):
                raise ApiError(409, "ORDER_NOT_AVAILABLE", "库存或活动状态已变化，请刷新后重试")
            now = datetime.now(UTC).replace(tzinfo=None)
            order = VoucherOrder(
                id=order_id,
                user_id=user_id,
                voucher_id=voucher_id,
                pay_value=voucher.pay_value,
                status="created",
                created_at=now,
                expires_at=now + timedelta(days=voucher.validity_days),
            )
            # 4. 先查订单不能防止两个请求都查到“没有”。UNIQUE(user_id,active_voucher_id)
            #    是最终防线；重复插入或其他写入失败，事务同时撤销该次库存扣减。
            await repository.insert_order(session, order)
            result = OrderResult(order=order_out((order, voucher, shop)), reused=False)
        # 5. 离开事务上下文并确认提交后，才把成功结果交给路由。
        return result
    except (IntegrityError, ApiError) as exc:
        # 6. 并发重复请求可能撞唯一约束或最后一份库存；回滚后开启新事务回查。
        duplicate = isinstance(exc, IntegrityError) and mysql_errno(exc) == 1062
        changed = isinstance(exc, ApiError) and exc.code in {"OUT_OF_STOCK", "ORDER_NOT_AVAILABLE"}
        if duplicate or changed:
            async with session.begin():
                existing = await repository.find_order(session, user_id, voucher_id)
                if existing is not None:
                    return OrderResult(order=order_out(existing), reused=True)
                if changed:
                    # 新事务读取最新状态，区分“抢完”和活动结束；已有订单优先返回。
                    current = await repository.get_voucher(session, voucher_id)
                    if current is None:
                        raise ApiError(404, "VOUCHER_NOT_FOUND", "优惠券不存在")
                    require_orderable(current, datetime.now(UTC))
            if duplicate:
                # 没有本用户的已有订单，可能是编号冲突；不把它误报为购买成功。
                raise ApiError(
                    503, "ORDER_ID_CONFLICT", "订单编号冲突，请检查编号服务后重试"
                ) from exc
        raise
