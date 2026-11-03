"""活动时间是持久配置，不能因重启、重复预热或丢失配置自动延长。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, update

from backend.app.models import SeckillGate, SeckillVoucher
from backend.app.services import redis_orders
from backend.app.services.order_requests import now_utc
from backend.app.services.voucher_activities import prepare_normal_activities
from tests.backend.test_voucher_orders import order_context as base_context

order_context = base_context

pytestmark = [pytest.mark.mysql, pytest.mark.redis]


async def activity(app, identifier):
    async with app.state.database.sessions() as session:
        row = await session.get(SeckillVoucher, identifier)
        return (row.begin_time, row.end_time) if row else None


async def test_new_normal_window_created_once_and_expired_window_never_extended(order_context):
    app, client, base, _ = order_context
    identifier = base + 1
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(delete(SeckillVoucher).where(SeckillVoucher.voucher_id == identifier))
        assert await prepare_normal_activities(session, [identifier]) == 1
    first = await activity(app, identifier)
    assert first[1] - first[0] == timedelta(days=30)
    async with app.state.database.sessions() as session, session.begin():
        assert await prepare_normal_activities(session, [identifier]) == 0
    assert await activity(app, identifier) == first
    expired = (now_utc() - timedelta(days=31), now_utc() - timedelta(days=1))
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(SeckillVoucher)
            .where(SeckillVoucher.voucher_id == identifier)
            .values(begin_time=expired[0], end_time=expired[1])
        )
        assert await prepare_normal_activities(session, [identifier]) == 0
    assert await activity(app, identifier) == expired
    result = (await client.get(f"/api/vouchers/{identifier}")).json()
    assert result["type"] == 0 and result["state"] == "ended"


async def test_used_normal_activity_missing_is_not_automatically_reopened(order_context):
    app, client, base, _ = order_context
    identifier = base + 1
    app.state.settings.order_mode = "async"
    app.state.settings.order_admission_mode = "redis_first"
    try:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(
                delete(SeckillVoucher).where(SeckillVoucher.voucher_id == identifier)
            )
            session.add(SeckillGate(voucher_id=identifier, generation=uuid4().hex, ready=1))
            await session.flush()
            assert await prepare_normal_activities(session, [identifier]) == 0
        assert (
            await redis_orders.warm(app.state.database, app.state.order_ingress, [identifier]) == 0
        )
        assert await activity(app, identifier) is None
        assert (await client.get(f"/api/vouchers/{identifier}")).json()["state"] == "unavailable"
    finally:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(delete(SeckillGate).where(SeckillGate.voucher_id == identifier))


async def test_normal_preparation_never_changes_seckill_window(order_context):
    app, _, base, _ = order_context
    before = await activity(app, base)
    async with app.state.database.sessions() as session, session.begin():
        assert await prepare_normal_activities(session, [base]) == 0
    assert await activity(app, base) == before
