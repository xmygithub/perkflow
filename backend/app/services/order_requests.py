"""异步请求受理与结果查询：先持久化待办，再由独立进程可靠投递。"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import ApiError
from backend.app.core.order_id import RedisOrderId
from backend.app.core.order_lock import RedisOrderLock
from backend.app.models import OrderRequest, Shop, Voucher
from backend.app.repositories import vouchers as repository
from backend.app.schemas.order_requests import RequestOut, RequestPage, RequestResult
from backend.app.schemas.vouchers import utc
from backend.app.services.vouchers import mysql_errno, order_out, require_orderable

FAILURES = {
    "PURCHASE_TERMS_CHANGED": "授权后的价格、规则或适用条件变化，本次未创建订单，请重新确认",
    "PURCHASE_AUTHORIZATION_MISMATCH": "购买授权与请求关联不符，本次未创建订单",
    "OUT_OF_STOCK": "优惠券已抢完，本次未创建订单",
    "ORDER_NOT_AVAILABLE": "库存或活动状态发生变化，本次未创建订单",
    "SECKILL_ENDED": "处理时活动已结束，本次未创建订单",
    "SECKILL_NOT_STARTED": "活动尚未开始",
    "VOUCHER_OFF_SHELF": "优惠券已下架",
    "VOUCHER_NOT_FOUND": "优惠券不存在",
    "SECKILL_NOT_CONFIGURED": "秒杀活动尚未配置",
    "NOT_SECKILL_VOUCHER": "该优惠券不支持秒杀下单",
    "ORDER_PROCESSING_FAILED": "多次处理失败，本次未创建订单，可稍后重新尝试",
    "ORDER_ID_CONFLICT": "购买编号冲突，请联系维护者检查编号服务",
    "PURCHASE_QUALIFICATION_BUSY": "已有购买名额或取消后的资格正在释放，请稍后重新尝试",
    "RESERVATION_RELEASED": "本次购买名额已释放，请重新发起购买",
}


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def request_out(session: AsyncSession, task: OrderRequest) -> RequestOut:
    voucher, shop = (
        await session.execute(
            select(Voucher, Shop)
            .join(Shop, Voucher.shop_id == Shop.id)
            .where(Voucher.id == task.voucher_id)
        )
    ).one()
    order = None
    if task.order_id is not None:
        # 结果查当前订单状态：旧请求可能成功后又被取消，不把旧成功当作重新购买。
        row = await repository.get_order(session, task.user_id, task.order_id)
        order = order_out(row) if row is not None else None
    return RequestOut(
        id=str(task.id),
        voucher_id=task.voucher_id,
        shop_name=shop.name,
        title=voucher.title,
        status=task.status,
        order=order,
        error_code=task.error_code,
        error_message=FAILURES.get(task.error_code) if task.error_code else None,
        created_at=utc(task.created_at),
        reservation_acquired=task.reservation_generation is not None,
    )


async def submit(
    session: AsyncSession,
    user_id: int,
    voucher_id: int,
    request_key: str,
    ids: RedisOrderId,
    lock: RedisOrderLock,
) -> RequestResult:
    # 1. 沿用用户＋券锁减少重复受理竞争；待处理唯一约束才是跨进程最终防线。
    token = await lock.try_acquire(user_id, voucher_id)
    if token is None:
        raise ApiError(409, "ORDER_IN_PROGRESS", "购买请求正在受理，请稍后查看我的订单")
    try:
        try:
            async with session.begin():
                # 2. 同一购买意图复用终态，必须先于活动校验；超时重试不重新扣库存。
                prior = await session.scalar(
                    select(OrderRequest).where(
                        OrderRequest.user_id == user_id,
                        OrderRequest.request_key == request_key,
                    )
                )
                if prior is not None:
                    if prior.voucher_id != voucher_id:
                        raise ApiError(
                            409, "IDEMPOTENCY_CONFLICT", "同一请求标识不能用于其他优惠券"
                        )
                    return RequestResult(request=await request_out(session, prior), reused=True)
                pending = await session.scalar(
                    select(OrderRequest.id).where(
                        OrderRequest.user_id == user_id,
                        OrderRequest.pending_voucher_id == voucher_id,
                    )
                )
                if pending is not None:
                    raise ApiError(409, "ORDER_IN_PROGRESS", "这张券有待处理请求，请到我的订单查看")
                existing = await repository.find_order(session, user_id, voucher_id)
                row = await repository.get_voucher(session, voucher_id)
                if row is None:
                    raise ApiError(404, "VOUCHER_NOT_FOUND", "优惠券不存在")
                if existing is None:
                    require_orderable(row, datetime.now(UTC))
                # 3. 本次编号以后直接作为新订单 ID；失败不回收，成功历史不自动删除。
                identifier = await ids.next_id()
                task = OrderRequest(
                    id=identifier,
                    user_id=user_id,
                    voucher_id=voucher_id,
                    request_key=request_key,
                    status="succeeded" if existing else "pending",
                    order_id=existing[0].id if existing else None,
                    next_delivery_at=now_utc(),
                )
                session.add(task)
                await session.flush()
                # MySQL 不支持 INSERT RETURNING；显式取回服务端时间和生成列，避免异步隐式加载。
                await session.refresh(task)
                result = RequestResult(
                    request=await request_out(session, task), reused=bool(existing)
                )
            # 4. 请求和待投递状态是一条记录，一起提交；此处不假报 broker 或落库成功。
            return result
        except IntegrityError as exc:
            if mysql_errno(exc) != 1062:
                raise
            # 锁到期或跨入口竞争仍可能撞约束；回滚后只回查，不重新分配并盲目提交。
            async with session.begin():
                prior = await session.scalar(
                    select(OrderRequest).where(
                        OrderRequest.user_id == user_id,
                        OrderRequest.request_key == request_key,
                    )
                )
                if prior is not None and prior.voucher_id == voucher_id:
                    return RequestResult(request=await request_out(session, prior), reused=True)
            raise ApiError(409, "ORDER_IN_PROGRESS", "购买请求发生竞争，请先查看我的订单") from exc
    finally:
        await lock.release(user_id, voucher_id, token)


async def get_request(session: AsyncSession, user_id: int, identifier: int) -> RequestOut:
    # 当前会话身份写入 SQL；其他用户与不存在的请求统一返回 404。
    task = await session.scalar(
        select(OrderRequest)
        .where(
            OrderRequest.id == identifier,
            OrderRequest.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise ApiError(404, "ORDER_REQUEST_NOT_FOUND", "购买请求不存在")
    return await request_out(session, task)


async def list_requests(
    session: AsyncSession, user_id: int, page: int, page_size: int
) -> RequestPage:
    # 仅列待处理/失败，成功结果在订单列表展示；刷新页面也可恢复待办，不依赖浏览器存储。
    condition = (OrderRequest.user_id == user_id) & OrderRequest.status.in_(["pending", "failed"])
    total = await session.scalar(select(func.count()).select_from(OrderRequest).where(condition))
    tasks = (
        await session.scalars(
            select(OrderRequest)
            .where(condition)
            .order_by(OrderRequest.created_at.desc(), OrderRequest.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return RequestPage(
        items=[await request_out(session, task) for task in tasks],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )
