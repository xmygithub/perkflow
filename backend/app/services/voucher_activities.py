"""后台补齐普通券首次活动；沿用历史活动表，不在购买请求中创建或续展。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.dialects.mysql import insert

from backend.app.models import OrderRequest, SeckillGate, SeckillVoucher, Voucher, VoucherOrder

NORMAL_ACTIVITY_DAYS = 30


async def prepare_normal_activities(session, voucher_ids: list[int] | None = None) -> int:
    # 1. 只补从未投入使用且缺配置的普通券；已预热/已购买后丢配置必须人工处理，不能重新开放。
    query = (
        select(Voucher.id)
        .outerjoin(SeckillVoucher, SeckillVoucher.voucher_id == Voucher.id)
        .outerjoin(SeckillGate, SeckillGate.voucher_id == Voucher.id)
        .where(
            Voucher.type == 0,
            SeckillVoucher.voucher_id.is_(None),
            or_(SeckillGate.voucher_id.is_(None), SeckillGate.ready == 0),
            ~exists(select(OrderRequest.id).where(OrderRequest.voucher_id == Voucher.id)),
            ~exists(select(VoucherOrder.id).where(VoucherOrder.voucher_id == Voucher.id)),
        )
    )
    if voucher_ids is not None:
        query = query.where(Voucher.id.in_(voucher_ids))
    missing = list(await session.scalars(query))
    if not missing:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    values = [
        {
            "voucher_id": identifier,
            "begin_time": now,
            "end_time": now + timedelta(days=NORMAL_ACTIVITY_DAYS),
        }
        for identifier in missing
    ]
    # 2. 并发准备靠活动主键协调；已存在时不改起止时间，不因再次启动或导入而延期。
    statement = insert(SeckillVoucher).values(values)
    await session.execute(statement.on_duplicate_key_update(voucher_id=SeckillVoucher.voucher_id))
    return len(missing)
