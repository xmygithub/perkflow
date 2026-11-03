from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import SeckillVoucher, Shop, Voucher, VoucherOrder
from backend.app.schemas.vouchers import OrderFilters, VoucherFilters


def voucher_query():
    return (
        select(Voucher, Shop, SeckillVoucher)
        .select_from(Voucher)
        .join(Shop, Voucher.shop_id == Shop.id)
        .outerjoin(SeckillVoucher, SeckillVoucher.voucher_id == Voucher.id)
    )


async def get_voucher(session: AsyncSession, voucher_id: int):
    return (await session.execute(voucher_query().where(Voucher.id == voucher_id))).first()


async def list_vouchers(session: AsyncSession, filters: VoucherFilters):
    conditions = []
    if filters.shop_id is not None:
        conditions.append(Voucher.shop_id == filters.shop_id)
    if filters.type is not None:
        conditions.append(Voucher.type == filters.type)
    total = await session.scalar(select(func.count()).select_from(Voucher).where(*conditions))
    rows = await session.execute(
        voucher_query()
        .where(*conditions)
        .order_by(Voucher.id)
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    return rows.all(), int(total or 0)


def order_query():
    return (
        select(VoucherOrder, Voucher, Shop)
        .select_from(VoucherOrder)
        .join(Voucher, VoucherOrder.voucher_id == Voucher.id)
        .join(Shop, Voucher.shop_id == Shop.id)
    )


async def find_order(session: AsyncSession, user_id: int, voucher_id: int):
    # 普通查询用于快速返回已有结果，不是互斥锁；两请求都未查到时靠唯一约束兜底。
    return (
        await session.execute(
            order_query().where(
                VoucherOrder.user_id == user_id,
                VoucherOrder.voucher_id == voucher_id,
                VoucherOrder.status == "created",
            )
        )
    ).first()


async def get_order(session: AsyncSession, user_id: int, order_id: int):
    # 始终把用户条件写入 SQL；猜中订单编号也不能读取其他用户的订单。
    return (
        await session.execute(
            order_query().where(VoucherOrder.user_id == user_id, VoucherOrder.id == order_id)
        )
    ).first()


async def list_orders(session: AsyncSession, user_id: int, filters: OrderFilters):
    condition = VoucherOrder.user_id == user_id
    if filters.status is not None:
        condition = condition & (VoucherOrder.status == filters.status)
    total = await session.scalar(select(func.count()).select_from(VoucherOrder).where(condition))
    rows = await session.execute(
        order_query()
        .where(condition)
        .order_by(VoucherOrder.created_at.desc(), VoucherOrder.id.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    return rows.all(), int(total or 0)


async def decrement_stock(session: AsyncSession, voucher_id: int) -> bool:
    # 扣库存语句再次校验活动窗口；资格以该语句开始时的数据库 UTC 时间为准。
    # 这不要求提交时活动仍未结束，也不能声称锁等待后的时间会不断重新计算。
    active_window = exists(
        select(SeckillVoucher.voucher_id).where(
            SeckillVoucher.voucher_id == Voucher.id,
            SeckillVoucher.begin_time <= func.utc_timestamp(6),
            SeckillVoucher.end_time > func.utc_timestamp(6),
        )
    )
    result = await session.execute(
        # 把校验与扣减放入一条 UPDATE，由 InnoDB 库存行锁协调，不拆成读后覆盖。
        update(Voucher)
        .where(
            Voucher.id == voucher_id,
            Voucher.type.in_([0, 1]),
            Voucher.status == 1,
            Voucher.stock > 0,
            active_window,
        )
        .values(stock=Voucher.stock - 1)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def insert_order(session: AsyncSession, order: VoucherOrder) -> None:
    # 只 flush 触发 SQL/约束；事务的提交与回滚由完整下单 Service 管理。
    session.add(order)
    await session.flush()


async def lock_owned_order(session: AsyncSession, user_id: int, order_id: int):
    # 只锁当前用户的订单行，不通过 JOIN 顺带锁整张券或商户；锁定读拿最新状态。
    return await session.scalar(
        select(VoucherOrder)
        .where(VoucherOrder.id == order_id, VoucherOrder.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def restore_stock(session: AsyncSession, voucher_id: int) -> bool:
    # 取消允许发生在下架/活动结束后；库存加一仍检查上限，异常时由外层整体回滚。
    result = await session.execute(
        update(Voucher)
        .where(Voucher.id == voucher_id, Voucher.stock < Voucher.total_stock)
        .values(stock=Voucher.stock + 1)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
