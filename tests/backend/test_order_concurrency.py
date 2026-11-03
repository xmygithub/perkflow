"""验证数据库防线；Service 用例直接覆盖内部事务，HTTP 用例经过 Redis 基础下单锁。"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from pymysql.err import OperationalError as MySQLOperationalError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.core.errors import ApiError
from backend.app.models import SeckillVoucher, User, Voucher, VoucherOrder
from backend.app.repositories import vouchers as repository
from backend.app.services import vouchers as service
from backend.app.services.auth import public_user
from tests.backend import test_voucher_orders as baseline
from tests.backend.test_voucher_orders import facts, login

# 两组测试复用同一隔离建数/清理 fixture，不复制另一套数据库初始化规则。
order_context = baseline.order_context

pytestmark = [pytest.mark.mysql, pytest.mark.redis]


@pytest_asyncio.fixture
async def race_context(order_context):
    app, client, base, phones = order_context
    db = app.state.database
    # 为每个买家建立真实用户，不用同一个用户反复请求来冒充多用户抢库存。
    async with db.sessions() as session, session.begin():
        session.add_all(
            [
                User(id=base + i, phone=f"139{base + i:08d}", nick_name=f"并发测试{i}")
                for i in range(2, 100)
            ]
        )
    try:
        yield app, client, base, phones
    finally:
        async with db.sessions() as session, session.begin():
            await session.execute(
                delete(VoucherOrder).where(VoucherOrder.user_id.between(base + 2, base + 99))
            )
            await session.execute(delete(User).where(User.id.between(base + 2, base + 99)))


async def buy(app, user_id, voucher_id):
    # 每个任务独立 Session；直接验证数据库兜底，避免入口锁遮住唯一约束竞争。
    async with app.state.database.sessions() as session:
        try:
            return await service._create_order_with_retry(
                session, user_id, voucher_id, app.state.order_ids
            )
        except ApiError as error:
            return error


async def set_stock(app, voucher_id, stock):
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(Voucher)
            .where(Voucher.id == voucher_id)
            .values(stock=stock, total_stock=max(stock, 3))
        )


@pytest.mark.parametrize("stock", [0, 1, 10])
async def test_100_distinct_buyers_cannot_oversell_and_facts_balance(race_context, stock):
    app, _, base, _ = race_context
    await set_stock(app, base, stock)
    start = asyncio.Event()

    async def compete(i):
        await start.wait()
        return await buy(app, base + i, base)

    tasks = [asyncio.create_task(compete(i)) for i in range(100)]
    start.set()
    results = await asyncio.gather(*tasks)
    successful = [row for row in results if not isinstance(row, ApiError)]
    assert len(successful) == stock and all(not row.reused for row in successful)
    assert len({row.order.id for row in successful}) == stock
    assert all(row.code == "OUT_OF_STOCK" for row in results if isinstance(row, ApiError))
    remaining, count = await facts(app, base)
    assert remaining == 0 and count == stock and remaining + count == stock
    async with app.state.database.sessions() as session:
        duplicates = await session.execute(
            select(VoucherOrder.user_id, func.count())
            .where(VoucherOrder.voucher_id == base)
            .group_by(VoucherOrder.user_id)
            .having(func.count() > 1)
        )
        assert duplicates.all() == []


async def test_100_same_user_requests_return_one_id_and_only_decrement_once(order_context):
    app, _, base, _ = order_context
    await set_stock(app, base, 10)
    results = await asyncio.gather(*[buy(app, base, base) for _ in range(100)])
    assert all(not isinstance(row, ApiError) for row in results)
    assert len({row.order.id for row in results}) == 1
    assert sum(not row.reused for row in results) == 1
    assert await facts(app, base) == (9, 1)


async def test_all_read_no_order_then_unique_constraint_rolls_back_losers(
    order_context, monkeypatch
):
    app, _, base, _ = order_context
    await set_stock(app, base, 10)
    original_find = repository.find_order
    original_insert = repository.insert_order
    all_read = asyncio.Event()
    initial_reads = 0
    unique_conflicts = []

    async def first_reads_together(session, user_id, voucher_id):
        nonlocal initial_reads
        result = await original_find(session, user_id, voucher_id)
        initial_reads += 1
        position = initial_reads
        if position <= 8:
            assert result is None
            if position == 8:
                all_read.set()
            # 有限等待防止连接池或代码回归导致测试无限挂起。
            await asyncio.wait_for(all_read.wait(), timeout=5)
        return result

    async def record_constraint_failure(session, order):
        try:
            await original_insert(session, order)
        except IntegrityError as error:
            unique_conflicts.append(service.mysql_errno(error))
            raise

    monkeypatch.setattr(repository, "find_order", first_reads_together)
    monkeypatch.setattr(repository, "insert_order", record_constraint_failure)
    results = await asyncio.gather(*[buy(app, base, base) for _ in range(8)])
    assert all(not isinstance(row, ApiError) for row in results)
    assert len({row.order.id for row in results}) == 1
    assert unique_conflicts and set(unique_conflicts) == {1062}
    assert await facts(app, base) == (9, 1)


async def test_independent_sessions_of_same_account_cannot_spoof_another_user(order_context):
    app, _, base, _ = order_context
    await set_stock(app, base, 10)
    async with app.state.database.sessions() as session:
        user = public_user(await session.get(User, base))
    sessions = []
    for _ in range(8):
        anonymous = await app.state.session_store.create_anonymous()
        sessions.append(await app.state.session_store.rotate(anonymous, user))

    async def submit(browser_session):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={app.state.settings.session_cookie_name: browser_session.sid},
        ) as client:
            # 客户端试图指定另一个用户；路由仍使用当前服务器会话的身份。
            response = await client.post(
                f"/api/vouchers/{base}/orders",
                json={"user_id": base + 1, "pay_value": 1},
                headers={"X-CSRF-Token": browser_session.data.csrf_token},
            )
            return response

    responses = await asyncio.gather(*[submit(session) for session in sessions])
    successful = [row for row in responses if row.status_code in {200, 201}]
    busy = [row for row in responses if row.status_code == 409]
    assert sum(row.status_code == 201 for row in successful) == 1
    assert len(successful) + len(busy) == 8
    assert all(row.json()["error"]["code"] == "ORDER_IN_PROGRESS" for row in busy)
    assert len({row.json()["order"]["id"] for row in successful}) == 1
    assert all(row.json()["order"]["pay_value"] == 5900 for row in successful)
    # 基础锁采用立即失败，竞争者稍后重试仍回到同一个已提交订单。
    repeated = await submit(sessions[0])
    assert repeated.status_code == 200 and repeated.json()["reused"]
    assert repeated.json()["order"]["id"] == successful[0].json()["order"]["id"]
    assert await facts(app, base) == (9, 1)
    async with app.state.database.sessions() as session:
        owner = await session.scalar(
            select(VoucherOrder.user_id).where(VoucherOrder.voucher_id == base)
        )
        assert owner == base


async def test_same_user_can_buy_distinct_coupons_while_one_stock_row_is_locked(
    order_context, monkeypatch
):
    app, _, base, _ = order_context
    db = app.state.database
    entered_update = asyncio.Event()
    original_update = repository.decrement_stock

    async def observe_update(session, voucher_id):
        if voucher_id == base:
            entered_update.set()
        return await original_update(session, voucher_id)

    monkeypatch.setattr(repository, "decrement_stock", observe_update)
    pending = None
    try:
        async with db.sessions() as locker, locker.begin():
            await locker.execute(select(Voucher.id).where(Voucher.id == base).with_for_update())
            pending = asyncio.create_task(buy(app, base, base))
            await asyncio.wait_for(entered_update.wait(), timeout=5)
            # 第二张券不等待第一张券的库存行锁，也不被“按用户全局锁”限制。
            other = await asyncio.wait_for(buy(app, base, base + 7), timeout=3)
            assert not isinstance(other, ApiError) and not pending.done()
        first = await asyncio.wait_for(pending, timeout=5)
        assert not isinstance(first, ApiError) and first.order.id != other.order.id
        assert await facts(app, base) == (2, 1)
        assert await facts(app, base + 7) == (2, 1)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_same_user_retries_after_sold_out_still_return_original_order(order_context):
    app, _, base, _ = order_context
    await set_stock(app, base, 1)
    results = await asyncio.gather(*[buy(app, base, base) for _ in range(12)])
    assert all(not isinstance(row, ApiError) for row in results)
    assert len({row.order.id for row in results}) == 1
    assert sum(not row.reused for row in results) == 1
    assert await facts(app, base) == (0, 1)


async def test_deadlock_retry_restarts_full_transaction_and_rechecks_stock(
    order_context, monkeypatch
):
    app, _, base, _ = order_context
    original_insert, original_find = repository.insert_order, repository.find_order
    attempts = 0
    clean_retry_snapshots = []

    async def record_retry_read(session, user_id, voucher_id):
        clean_retry_snapshots.append(
            await session.scalar(select(Voucher.stock).where(Voucher.id == base))
        )
        return await original_find(session, user_id, voucher_id)

    async def fail_twice_after_real_insert(session, order):
        nonlocal attempts
        attempts += 1
        await original_insert(session, order)
        if attempts <= 2:
            # 服务端故障注入产生真实驱动 1213；不是构造实际锁环或生产死锁压测。
            await session.execute(
                text("SIGNAL SQLSTATE '40001' SET MYSQL_ERRNO=1213, MESSAGE_TEXT='test deadlock'")
            )

    monkeypatch.setattr(repository, "find_order", record_retry_read)
    monkeypatch.setattr(repository, "insert_order", fail_twice_after_real_insert)
    result = await buy(app, base, base)
    assert not isinstance(result, ApiError) and not result.reused and attempts == 3
    assert clean_retry_snapshots == [3, 3, 3]
    assert int(result.order.id) & (2**32 - 1) == 3
    assert await facts(app, base) == (2, 1)


async def test_deadlock_exhaustion_has_bounded_attempts_and_retry_header(
    order_context, monkeypatch
):
    app, client, base, phones = order_context

    headers = await login(client, app, phones[0])
    original_insert = repository.insert_order
    attempts = 0

    async def always_deadlock(session, order):
        nonlocal attempts
        attempts += 1
        await original_insert(session, order)
        await session.execute(
            text("SIGNAL SQLSTATE '40001' SET MYSQL_ERRNO=1213, MESSAGE_TEXT='test deadlock'")
        )

    monkeypatch.setattr(repository, "insert_order", always_deadlock)
    response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ORDER_BUSY"
    assert response.headers["retry-after"] == "1" and attempts == 3
    assert await facts(app, base) == (3, 0)


@pytest.mark.parametrize("change", ["activity_ended", "other_request_won"])
async def test_deadlock_retry_rechecks_changed_rules_or_completed_order(
    order_context,
    monkeypatch,
    change,
):
    app, _, base, _ = order_context
    original_insert = repository.insert_order
    attempts = 0
    winner = []

    async def fail_first_insert(session, order):
        nonlocal attempts
        attempts += 1
        await original_insert(session, order)
        if attempts == 1:
            await session.execute(
                text("SIGNAL SQLSTATE '40001' SET MYSQL_ERRNO=1213, MESSAGE_TEXT='test deadlock'")
            )

    async def change_after_rollback(_seconds):
        if change == "activity_ended":
            async with app.state.database.sessions() as session, session.begin():
                await session.execute(
                    update(SeckillVoucher)
                    .where(SeckillVoucher.voucher_id == base)
                    .values(end_time=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1))
                )
        else:
            winner.append(await buy(app, base, base))

    monkeypatch.setattr(repository, "insert_order", fail_first_insert)
    monkeypatch.setattr(service, "sleep", change_after_rollback)
    result = await buy(app, base, base)
    if change == "activity_ended":
        assert isinstance(result, ApiError) and result.code == "SECKILL_ENDED"
        assert attempts == 1 and await facts(app, base) == (3, 0)
    else:
        assert not isinstance(result, ApiError) and result.reused
        assert result.order.id == winner[0].order.id
        assert attempts == 2 and await facts(app, base) == (2, 1)


@pytest.mark.parametrize("errno", [2006, 2013])
async def test_connection_fault_is_not_blindly_retried(order_context, monkeypatch, errno):
    app, _, base, _ = order_context
    original_insert = repository.insert_order
    calls = 0

    async def uncertain_error(session, order):
        nonlocal calls
        calls += 1
        await original_insert(session, order)
        raise OperationalError(None, None, MySQLOperationalError(errno, "test connection fault"))

    monkeypatch.setattr(repository, "insert_order", uncertain_error)
    async with app.state.database.sessions() as session:
        with pytest.raises(OperationalError):
            await service._create_order_with_retry(session, base, base, app.state.order_ids)
    assert calls == 1 and await facts(app, base) == (3, 0)


async def test_real_mysql_lock_wait_timeout_rolls_back_and_is_not_retried(order_context):
    app, _, base, _ = order_context
    db = app.state.database
    async with db.sessions() as locker, locker.begin():
        await locker.execute(select(Voucher.id).where(Voucher.id == base).with_for_update())
        # 绑定同一连接，确保测试结束恢复本连接的会话变量，不污染连接池。
        async with db.engine.connect() as connection:
            previous = await connection.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
            await connection.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
            await connection.commit()
            try:
                async with db.sessions(bind=connection) as session:
                    result = None
                    with pytest.raises(ApiError) as error:
                        result = await asyncio.wait_for(
                            service._create_order_with_retry(
                                session, base, base, app.state.order_ids
                            ),
                            timeout=5,
                        )
                    assert error.value.code == "ORDER_BUSY" and result is None
            finally:
                await connection.execute(
                    text(f"SET SESSION innodb_lock_wait_timeout={int(previous)}")
                )
                await connection.commit()
    assert await facts(app, base) == (3, 0)
