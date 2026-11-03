"""真实 MySQL：订单取消、资格释放、并发幂等及回滚，只使用隔离库和测试数据。"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pymysql.err import OperationalError as MySQLOperationalError
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from backend.app.core.errors import ApiError
from backend.app.models import SeckillVoucher, Voucher, VoucherOrder
from backend.app.repositories import vouchers as repository
from backend.app.services import vouchers as service
from tests.backend import test_voucher_orders as baseline

order_context = baseline.order_context
pytestmark = [pytest.mark.mysql, pytest.mark.redis]


async def create(app, base, user_id=None):
    async with app.state.database.sessions() as session:
        return await service.create_order(
            session, user_id or base, base, app.state.order_ids, app.state.order_lock
        )


async def cancel(app, base, order_id):
    async with app.state.database.sessions() as session:
        return await service.cancel_order(session, base, int(order_id))


async def facts(app, voucher_id):
    async with app.state.database.sessions() as session:
        stock = await session.scalar(select(Voucher.stock).where(Voucher.id == voucher_id))
        counts = dict(
            (
                await session.execute(
                    select(VoucherOrder.status, func.count())
                    .where(VoucherOrder.voucher_id == voucher_id)
                    .group_by(VoucherOrder.status)
                )
            ).all()
        )
        return stock, counts.get("created", 0), counts.get("cancelled", 0)


async def test_cancel_repeat_repurchase_keeps_history_and_old_retry_cannot_cancel_new_order(
    order_context,
):
    app, client, base, phones = order_context
    headers = await baseline.login(client, app, phones[0])
    first = await create(app, base)
    path = f"/api/orders/{first.order.id}/cancel"
    response = await client.post(path, headers=headers)
    assert response.status_code == 200 and not response.json()["reused"]
    order = response.json()["order"]
    assert order["status"] == "cancelled" and order["cancelled_at"].endswith("Z")
    assert order["pay_value"] == first.order.pay_value and order["id"] == first.order.id
    assert response.headers["cache-control"] == "no-store"
    assert await facts(app, base) == (3, 0, 1)
    repeated = await client.post(path, headers=headers)
    assert (
        repeated.json()["reused"]
        and repeated.json()["order"]["cancelled_at"] == order["cancelled_at"]
    )
    second = await create(app, base)
    assert second.order.id != first.order.id and not second.reused
    assert await facts(app, base) == (2, 1, 1)
    assert (await client.post(path, headers=headers)).json()["reused"]
    assert await facts(app, base) == (2, 1, 1)
    all_orders = (await client.get("/api/orders")).json()
    assert all_orders["total"] == 2
    active = (await client.get("/api/orders?status=created")).json()
    cancelled = (await client.get("/api/orders?status=cancelled")).json()
    assert active["total"] == cancelled["total"] == 1
    assert active["items"][0]["id"] == second.order.id
    assert (await client.get("/api/orders?status=paid")).status_code == 422


async def test_cancel_requires_login_csrf_origin_and_owner_before_change(order_context):
    app, client, base, phones = order_context
    first = await create(app, base)
    path = f"/api/orders/{first.order.id}/cancel"
    assert (await client.post(path)).status_code == 401
    headers = await baseline.login(client, app, phones[0])
    assert (await client.post(path)).status_code == 403
    assert (await client.post(path, headers={"X-CSRF-Token": "forged"})).status_code == 403
    assert (
        await client.post(path, headers={**headers, "Origin": "https://untrusted.invalid"})
    ).status_code == 403
    other_headers = await baseline.login(client, app, phones[1])
    assert (
        await client.post(path, headers=other_headers, json={"user_id": base})
    ).status_code == 404
    assert (
        await client.post("/api/orders/9223372036854775807/cancel", headers=other_headers)
    ).status_code == 404
    assert (
        await client.post("/api/orders/9223372036854775808/cancel", headers=other_headers)
    ).status_code == 422
    assert await facts(app, base) == (2, 1, 0)


async def test_concurrent_cancellation_only_restores_one_unit(order_context):
    app, _, base, _ = order_context
    first = await create(app, base)
    results = await asyncio.gather(*[cancel(app, base, first.order.id) for _ in range(32)])
    assert sum(not result.reused for result in results) == 1
    assert len({row.order.cancelled_at for row in results}) == 1
    assert await facts(app, base) == (3, 0, 1)


async def test_generated_unique_constraint_protects_concurrent_repurchase_with_history(
    order_context, monkeypatch
):
    app, _, base, _ = order_context
    original = await create(app, base)
    await cancel(app, base, original.order.id)
    original_find = repository.find_order
    arrived = asyncio.Event()
    reads = 0

    async def all_see_no_active_order(session, user_id, voucher_id):
        nonlocal reads
        row = await original_find(session, user_id, voucher_id)
        reads += 1
        if reads <= 8:
            assert row is None
            if reads == 8:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), 4)
        return row

    monkeypatch.setattr(repository, "find_order", all_see_no_active_order)

    async def buy_without_entry_lock():
        async with app.state.database.sessions() as session:
            return await service._create_order_with_retry(session, base, base, app.state.order_ids)

    results = await asyncio.gather(*[buy_without_entry_lock() for _ in range(8)])
    assert sum(not row.reused for row in results) == 1
    assert len({row.order.id for row in results}) == 1
    assert results[0].order.id != original.order.id
    assert await facts(app, base) == (2, 1, 1)
    async with app.state.database.sessions() as session:
        rows = (
            await session.execute(
                select(VoucherOrder.status, VoucherOrder.active_voucher_id).where(
                    VoucherOrder.user_id == base
                )
            )
        ).all()
        assert set(rows) == {("created", base), ("cancelled", None)}


async def test_multiple_cancelled_records_are_allowed_but_only_one_active_order(order_context):
    app, _, base, _ = order_context
    ids = []
    for _ in range(3):
        order = await create(app, base)
        ids.append(order.order.id)
        await cancel(app, base, order.order.id)
    assert len(set(ids)) == 3 and await facts(app, base) == (3, 0, 3)
    active = await create(app, base)
    assert active.order.id not in ids and await facts(app, base) == (2, 1, 3)


@pytest.mark.parametrize("failure", ["after_restore", "before_commit"])
async def test_cancel_failure_rolls_back_state_qualification_and_stock(
    order_context, monkeypatch, failure
):
    app, _, base, _ = order_context
    first = await create(app, base)
    original = repository.restore_stock

    async def fail_after_stock_update(session, voucher_id):
        assert await original(session, voucher_id)
        raise SQLAlchemyError("synthetic failure after stock restore")

    def fail_commit(_session):
        raise SQLAlchemyError("synthetic failure before COMMIT")

    async with app.state.database.sessions() as session:
        if failure == "after_restore":
            monkeypatch.setattr(repository, "restore_stock", fail_after_stock_update)
        else:
            event.listen(session.sync_session, "before_commit", fail_commit)
        try:
            with pytest.raises(SQLAlchemyError):
                await service.cancel_order(session, base, int(first.order.id))
        finally:
            if failure == "before_commit":
                event.remove(session.sync_session, "before_commit", fail_commit)
    assert await facts(app, base) == (2, 1, 0)
    async with app.state.database.sessions() as session:
        row = await session.get(VoucherOrder, int(first.order.id))
        assert (
            row.status == "created" and row.cancelled_at is None and row.active_voucher_id == base
        )


async def test_inconsistent_stock_prevents_cancellation_without_half_state(order_context):
    app, _, base, _ = order_context
    first = await create(app, base)
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(update(Voucher).where(Voucher.id == base).values(stock=3))
    with pytest.raises(ApiError) as error:
        await cancel(app, base, first.order.id)
    assert error.value.code == "CANCEL_STOCK_CONFLICT"
    assert await facts(app, base) == (3, 1, 0)


@pytest.mark.parametrize("state", ["off_shelf", "ended", "expired_voucher"])
async def test_cancel_allowed_after_sale_or_voucher_expiry_without_reopening_sale(
    order_context, state
):
    app, _, base, _ = order_context
    first = await create(app, base)
    async with app.state.database.sessions() as session, session.begin():
        if state == "off_shelf":
            await session.execute(update(Voucher).where(Voucher.id == base).values(status=0))
        elif state == "ended":
            await session.execute(
                update(SeckillVoucher)
                .where(SeckillVoucher.voucher_id == base)
                .values(end_time=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1))
            )
        else:
            now = datetime.now(UTC).replace(tzinfo=None)
            await session.execute(
                update(VoucherOrder)
                .where(VoucherOrder.id == int(first.order.id))
                .values(created_at=now - timedelta(days=2), expires_at=now - timedelta(days=1))
            )
    assert not (await cancel(app, base, first.order.id)).reused
    assert await facts(app, base) == (3, 0, 1)
    if state != "expired_voucher":
        with pytest.raises(ApiError):
            await create(app, base)


async def test_cancel_uses_latest_order_state_and_new_purchase_can_follow_commit(
    order_context, monkeypatch
):
    app, _, base, _ = order_context
    first = await create(app, base)
    restored, finish = asyncio.Event(), asyncio.Event()
    original = repository.restore_stock

    async def pause_before_commit(session, voucher_id):
        result = await original(session, voucher_id)
        restored.set()
        await asyncio.wait_for(finish.wait(), 4)
        return result

    monkeypatch.setattr(repository, "restore_stock", pause_before_commit)
    task = asyncio.create_task(cancel(app, base, first.order.id))
    try:
        await asyncio.wait_for(restored.wait(), 3)
        # 取消尚未提交时的并发购买可以回查旧订单，不应提前释放已提交资格。
        during = await create(app, base)
        assert during.reused and during.order.id == first.order.id
        finish.set()
        await task
        later = await create(app, base)
        assert not later.reused and later.order.id != first.order.id
        assert await facts(app, base) == (2, 1, 1)
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("code,expected_attempts", [(1213, 3), (1205, 1), (2006, 1)])
async def test_cancel_database_retry_is_bounded_and_rolls_back_every_attempt(
    order_context, monkeypatch, code, expected_attempts
):
    app, _, base, _ = order_context
    first = await create(app, base)
    original = repository.restore_stock
    attempts = 0

    async def fail_restore(session, voucher_id):
        nonlocal attempts
        attempts += 1
        assert await original(session, voucher_id)
        raise OperationalError(
            "synthetic database competition", {}, MySQLOperationalError(code, "test")
        )

    monkeypatch.setattr(repository, "restore_stock", fail_restore)
    monkeypatch.setattr(service, "sleep", AsyncMock())
    with pytest.raises(ApiError if code in {1213, 1205} else OperationalError) as error:
        await cancel(app, base, first.order.id)
    if code in {1213, 1205}:
        assert error.value.code == "ORDER_BUSY"
    assert attempts == expected_attempts and await facts(app, base) == (2, 1, 0)


@pytest.mark.parametrize(
    "values",
    [
        {"status": "cancelled"},
        {"status": "created", "cancelled_at": datetime(2026, 10, 15)},
        {"status": "paid"},
    ],
)
async def test_database_rejects_inconsistent_cancellation_state(order_context, values):
    app, _, base, _ = order_context
    first = await create(app, base)
    with pytest.raises(OperationalError) as error:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(
                update(VoucherOrder).where(VoucherOrder.id == int(first.order.id)).values(**values)
            )
    assert service.mysql_errno(error.value) == 3819
    assert await facts(app, base) == (2, 1, 0)
