"""第二阶段：真实 Redis Lua、独立 MySQL、恢复/补偿与真实 RabbitMQ Worker。"""

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, update
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.core.errors import ApiError
from backend.app.core.rabbitmq import connect_broker
from backend.app.models import (
    OrderRequest,
    SeckillEffect,
    SeckillGate,
    SeckillVoucher,
    User,
)
from backend.app.services import order_processing, seckill
from backend.app.services import vouchers as voucher_service
from backend.app.services.order_requests import now_utc
from backend.app.workers import orders as worker
from tests.backend.test_async_orders import delivery, task_row
from tests.backend.test_voucher_orders import facts, login
from tests.backend.test_voucher_orders import order_context as base_context

order_context = base_context
pytestmark = [pytest.mark.mysql, pytest.mark.redis]


@pytest_asyncio.fixture
async def reservation_context(order_context):
    app, client, base, phones = order_context
    app.state.settings.order_mode = "async"
    app.state.settings.order_admission_mode = "redis"
    try:
        yield app, client, base, phones
    finally:
        async with app.state.database.sessions() as session, session.begin():
            for model in [SeckillEffect, OrderRequest, SeckillGate]:
                await session.execute(delete(model).where(model.voucher_id.between(base, base + 7)))
        keys = [
            key
            async for key in app.state.cache_redis.scan_iter(
                match=app.state.settings.seckill_key_prefix + "*"
            )
        ]
        if keys:
            await app.state.cache_redis.delete(*keys)


async def purchase(context, key=None):
    app, client, base, phones = context
    current = (await client.get("/api/auth/session")).json()
    headers = (
        {"X-CSRF-Token": current["csrf_token"]}
        if current["user"]
        else await login(client, app, phones[0])
    )
    headers["Idempotency-Key"] = key or str(uuid4())
    response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert response.status_code == 202, response.text
    return int(response.json()["request"]["id"]), headers


async def state(context):
    app, _, base, _ = context
    return await app.state.cache_redis.hgetall(app.state.seckill_store.key(base))


async def consume(context, identifier):
    app = context[0]
    msg = delivery(identifier)
    await worker.consume_message(
        app.state.database, app.state.settings, msg, app.state.seckill_store
    )
    msg.ack.assert_awaited_once()
    return msg


async def make_effects_due(app):
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(SeckillEffect)
            .where(SeckillEffect.applied.is_(False))
            .values(next_attempt_at=now_utc() - timedelta(seconds=1))
        )


async def test_reserve_before_worker_and_duplicate_messages_do_not_double_deduct(
    reservation_context,
):
    app, client, base, _ = reservation_context
    identifier, headers = await purchase(reservation_context)
    before = await state(reservation_context)
    assert before["available"] == "2" and before[f"r:{identifier}"] == "held"
    assert before[f"u:{base}"] == str(identifier)
    assert await facts(app, base) == (3, 0)
    result = (await client.get(f"/api/order-requests/{identifier}")).json()
    assert result["reservation_acquired"] is True and result["order"] is None
    assert await app.state.cache_redis.ttl(app.state.seckill_store.key(base)) == -1
    repeated = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert repeated.json()["request"]["id"] == str(identifier)
    await asyncio.gather(*[consume(reservation_context, identifier) for _ in range(20)])
    after = await state(reservation_context)
    assert after["available"] == "2" and after[f"r:{identifier}"] == "committed"
    assert await facts(app, base) == (2, 1)


async def test_lua_time_qualification_and_stock_checks_use_server_state(reservation_context):
    app, _, base, _ = reservation_context
    store = app.state.seckill_store
    generation = await seckill.ensure_gate(app.state.database, store, base)
    key = store.key(base)
    now = now_utc().replace(tzinfo=UTC).timestamp() * 1000
    await store.redis.hset(key, "begin_ms", int(now + 3600000))
    assert await store.reserve(base, generation, base, 1001) == 3
    await store.redis.hset(key, mapping={"begin_ms": int(now - 3600000), "end_ms": int(now - 1000)})
    assert await store.reserve(base, generation, base, 1001) == 4
    await store.redis.hset(key, mapping={"end_ms": int(now + 3600000), "enabled": 0})
    assert await store.reserve(base, generation, base, 1001) == 5
    await store.redis.hset(key, "enabled", 1)
    assert await store.reserve(base, generation, base, 1001) == 0
    assert await store.reserve(base, generation, base, 1002) == 2
    assert await store.reserve(base, generation, base, 1001) == 0
    assert (await state(reservation_context))["available"] == "2"


async def test_40_distinct_durable_intents_only_three_reservations(reservation_context):
    app, _, base, _ = reservation_context
    await seckill.ensure_gate(app.state.database, app.state.seckill_store, base)
    users = [base + 100 + i for i in range(40)]
    identifiers = [await app.state.order_ids.next_id() for _ in users]
    try:
        async with app.state.database.sessions() as session, session.begin():
            session.add_all(
                [User(id=user, phone=f"136{user:08d}", nick_name="准入测试") for user in users]
            )
            await session.flush()
            session.add_all(
                [
                    OrderRequest(
                        id=identifier,
                        user_id=user,
                        voucher_id=base,
                        request_key=str(uuid4()),
                        next_delivery_at=now_utc(),
                    )
                    for user, identifier in zip(users, identifiers, strict=True)
                ]
            )
        results = await asyncio.gather(
            *[seckill.admit(app.state.database, app.state.seckill_store, i) for i in identifiers]
        )
        assert results.count("admitted") == 3 and results.count("rejected") == 37
        assert (await state(reservation_context))["available"] == "0"
        assert await facts(app, base) == (3, 0)
        for identifier, result in zip(identifiers, results, strict=True):
            if result == "admitted":
                await consume(reservation_context, identifier)
        assert await facts(app, base) == (0, 3)
    finally:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(delete(SeckillEffect).where(SeckillEffect.user_id.in_(users)))
            await session.execute(delete(OrderRequest).where(OrderRequest.user_id.in_(users)))
            # 订单需要在其 FK 请求之后删，最后才删除临时用户。
            from backend.app.models import VoucherOrder

            await session.execute(delete(VoucherOrder).where(VoucherOrder.user_id.in_(users)))
            await session.execute(delete(User).where(User.id.in_(users)))


async def test_reserve_reply_unknown_keeps_intent_and_recovers_same_slot(
    reservation_context, monkeypatch
):
    app, client, base, _ = reservation_context
    store = app.state.seckill_store
    original = store.reserve

    async def unknown(*args):
        await original(*args)
        raise store.unavailable()

    monkeypatch.setattr(store, "reserve", unknown)
    identifier, headers = await purchase(reservation_context)
    assert (await task_row(app, identifier)).reservation_generation is None
    assert (await state(reservation_context))["available"] == "2"
    monkeypatch.setattr(store, "reserve", original)
    result = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert result.status_code == 202 and result.json()["request"]["reservation_acquired"]
    await consume(reservation_context, identifier)
    assert await facts(app, base) == (2, 1)
    assert (await state(reservation_context))["available"] == "2"


async def test_sql_rollback_after_lua_has_durable_intent_no_orphan(
    reservation_context, monkeypatch
):
    app, client, base, phones = reservation_context
    headers = await login(client, app, phones[0])
    headers["Idempotency-Key"] = str(uuid4())
    store = app.state.seckill_store
    original = store.reserve
    fail_commit = False

    async def reserve(*args):
        nonlocal fail_commit
        result = await original(*args)
        fail_commit = True
        return result

    def before_commit(session):
        nonlocal fail_commit
        if fail_commit:
            fail_commit = False
            raise RuntimeError("simulated SQL rollback after Lua")

    monkeypatch.setattr(store, "reserve", reserve)
    event.listen(Session, "before_commit", before_commit)
    try:
        with pytest.raises(RuntimeError, match="SQL rollback"):
            await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    finally:
        event.remove(Session, "before_commit", before_commit)
        monkeypatch.setattr(store, "reserve", original)
    async with app.state.database.sessions() as session:
        task = await session.scalar(select(OrderRequest).where(OrderRequest.voucher_id == base))
    assert task.status == "pending" and task.reservation_generation is None
    assert (await state(reservation_context))["available"] == "2"
    await consume(reservation_context, task.id)
    assert await facts(app, base) == (2, 1)
    assert (await state(reservation_context))["available"] == "2"


async def test_activity_ends_after_reserve_persists_failure_and_releases_once(reservation_context):
    app, _, base, _ = reservation_context
    identifier, _ = await purchase(reservation_context)
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(SeckillVoucher)
            .where(SeckillVoucher.voucher_id == base)
            .values(end_time=now_utc() - timedelta(seconds=1))
        )
    await consume(reservation_context, identifier)
    assert (await task_row(app, identifier)).error_code == "SECKILL_ENDED"
    assert await facts(app, base) == (3, 0)
    assert (await state(reservation_context))["available"] == "3"
    await consume(reservation_context, identifier)
    assert (await state(reservation_context))["available"] == "3"


async def test_retry_exhaustion_releases_reservation(reservation_context, monkeypatch):
    app, _, base, _ = reservation_context
    identifier, _ = await purchase(reservation_context)
    monkeypatch.setattr(worker, "process_order", AsyncMock(side_effect=ConnectionError()))
    for _ in range(app.state.settings.order_retry_limit):
        await consume(reservation_context, identifier)
    assert (await task_row(app, identifier)).status == "failed"
    assert (await state(reservation_context))["available"] == "3"
    assert await facts(app, base) == (3, 0)


async def test_compensation_reply_unknown_retries_without_extra_stock(
    reservation_context, monkeypatch
):
    app, _, base, _ = reservation_context
    identifier, _ = await purchase(reservation_context)
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(SeckillVoucher)
            .where(SeckillVoucher.voucher_id == base)
            .values(end_time=now_utc() - timedelta(seconds=1))
        )
    await order_processing.process_order(app.state.database, identifier, require_reservation=True)
    store = app.state.seckill_store
    original = store.effect

    async def lost_reply(*args):
        await original(*args)
        raise store.unavailable()

    monkeypatch.setattr(store, "effect", lost_reply)
    assert await seckill.sync_effects(app.state.database, store) == 0
    assert (await state(reservation_context))["available"] == "3"
    monkeypatch.setattr(store, "effect", original)
    await make_effects_due(app)
    assert await seckill.sync_effects(app.state.database, store) == 1
    assert (await state(reservation_context))["available"] == "3"


async def test_cancel_redis_down_then_rebuy_old_events_do_not_release_new_slot(
    reservation_context, monkeypatch
):
    app, client, base, _ = reservation_context
    identifier, headers = await purchase(reservation_context)
    await consume(reservation_context, identifier)
    store = app.state.seckill_store
    original = store.effect
    monkeypatch.setattr(store, "effect", AsyncMock(side_effect=store.unavailable()))
    cancelled = await client.post(f"/api/orders/{identifier}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["order"]["status"] == "cancelled"
    assert await facts(app, base) == (3, 1)
    assert (await state(reservation_context))["available"] == "2"
    # 未完成资格释放前立即重买会失败；SQL 取消成功不会被 Redis 同步失败撤销。
    retry_headers = {**headers, "Idempotency-Key": str(uuid4())}
    retry = await client.post(f"/api/vouchers/{base}/orders", headers=retry_headers)
    assert retry.status_code == 200
    assert retry.json()["request"]["error_code"] == "PURCHASE_QUALIFICATION_BUSY"
    monkeypatch.setattr(store, "effect", original)
    await make_effects_due(app)
    await seckill.sync_effects(app.state.database, store)
    next_id, _ = await purchase(reservation_context)
    generation = (await state(reservation_context))["generation"]
    for action in ["cancel", "release", "confirm"]:
        await store.effect(base, generation, base, identifier, action)
    assert (await state(reservation_context))[f"u:{base}"] == str(next_id)
    assert (await state(reservation_context))["available"] == "2"
    await consume(reservation_context, next_id)
    assert await facts(app, base) == (2, 2)


async def test_key_loss_fails_closed_then_offline_rebuild_accounts_pending(reservation_context):
    app, _, base, _ = reservation_context
    identifier, _ = await purchase(reservation_context)
    store = app.state.seckill_store
    old_generation = (await state(reservation_context))["generation"]
    await store.redis.delete(store.key(base))
    assert await seckill.ensure_gate(app.state.database, store, base) == old_generation
    assert await store.redis.exists(store.key(base)) == 0
    broker = AsyncMock()
    assert await worker.dispatch_one(app.state.database, app.state.settings, broker, store) is False
    broker.publish.assert_not_awaited()
    await consume(reservation_context, identifier)
    assert await facts(app, base) == (3, 0)
    assert (await task_row(app, identifier)).attempts == 0
    result = await seckill.rebuild(app.state.database, store, base)
    assert result["available"] == 2 and result["pending_reserved"] == 1
    assert (await state(reservation_context))["generation"] != old_generation
    with pytest.raises(ApiError):
        await store.effect(base, old_generation, base, identifier, "release")
    await consume(reservation_context, identifier)
    assert await facts(app, base) == (2, 1)


async def test_missing_fields_and_accidental_ttl_reject_without_stock_mutation(reservation_context):
    app, _, base, _ = reservation_context
    identifier, _ = await purchase(reservation_context)
    store = app.state.seckill_store
    generation = (await state(reservation_context))["generation"]
    await store.redis.expire(store.key(base), 100)
    with pytest.raises(ApiError):
        await store.reserve(base, generation, base + 1, identifier + 1)
    await store.redis.persist(store.key(base))
    await store.redis.hdel(store.key(base), "begin_ms")
    with pytest.raises(ApiError):
        await store.reserve(base, generation, base + 1, identifier + 1)
    assert (await state(reservation_context))["available"] == "2"


async def test_existing_orders_seed_qualification_and_cancel_can_release(reservation_context):
    app, client, base, phones = reservation_context
    headers = await login(client, app, phones[0])
    # 模拟升级前已存在的同步订单；只在首次建立 Redis 投影之前使用旧入口。
    async with app.state.database.sessions() as session:
        old = await voucher_service.create_order(
            session, base, base, app.state.order_ids, app.state.order_lock
        )
    generation = await seckill.ensure_gate(app.state.database, app.state.seckill_store, base)
    snapshot = await state(reservation_context)
    assert snapshot["available"] == "2" and snapshot[f"u:{base}"] == old.order.id
    assert snapshot[f"r:{old.order.id}"] == "committed"
    response = await client.post(f"/api/orders/{old.order.id}/cancel", headers=headers)
    assert response.status_code == 200
    assert await facts(app, base) == (3, 1)
    assert (await state(reservation_context))["available"] == "3"
    identifier, _ = await purchase(reservation_context)
    assert identifier != int(old.order.id)
    await app.state.seckill_store.effect(base, generation, base, int(old.order.id), "cancel")
    assert (await state(reservation_context))["available"] == "2"


async def test_worker_recovers_compensation_while_mq_connection_unavailable(
    reservation_context, monkeypatch
):
    app, _, base, _ = reservation_context
    identifier, _ = await purchase(reservation_context)
    await consume(reservation_context, identifier)
    async with app.state.database.sessions() as session:
        await voucher_service.cancel_order(session, base, identifier)
    assert (await state(reservation_context))["available"] == "2"

    @asynccontextmanager
    async def unavailable(settings):
        raise ConnectionError("simulated broker failure")
        yield  # pragma: no cover; 定义异步上下文管理器，不会真的获得连接。

    monkeypatch.setattr(worker, "connect_broker", unavailable)
    running = asyncio.create_task(worker.run(app.state.settings))
    try:
        async with asyncio.timeout(5):
            while (await state(reservation_context))["available"] != "3":
                await asyncio.sleep(0.05)
        assert await facts(app, base) == (3, 1)
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running


@pytest.mark.parametrize("entry", ["dispatch", "consume"])
async def test_admission_timeout_keeps_recoverable_slot_without_spending_order_retries(
    reservation_context, monkeypatch, entry
):
    app, _, base, _ = reservation_context
    identifier, _ = await purchase(reservation_context)
    monkeypatch.setattr(seckill, "admit", AsyncMock(side_effect=TimeoutError()))
    if entry == "dispatch":
        broker = AsyncMock()
        assert (
            await worker.dispatch_one(
                app.state.database, app.state.settings, broker, app.state.seckill_store
            )
            is False
        )
        broker.publish.assert_not_awaited()
    else:
        await consume(reservation_context, identifier)
    task = await task_row(app, identifier)
    assert task.status == "pending" and task.attempts == 0
    assert (await state(reservation_context))["available"] == "2"
    assert await facts(app, base) == (3, 0)


async def test_used_projection_rejects_destructive_migration_downgrade(reservation_context):
    import runpy

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from scripts.run_backend_tests import ROOT

    app = reservation_context[0]
    identifier, _ = await purchase(reservation_context)

    def downgrade(connection):
        # 只执行0004的真实保护，不让多版本DDL污染后续测试共享的独立测试库。
        path = ROOT / "backend/alembic/versions/0004_seckill_reservations.py"
        with Operations.context(MigrationContext.configure(connection)):
            runpy.run_path(str(path))["downgrade"]()

    with pytest.raises(RuntimeError, match="不能无损回退"):
        async with app.state.database.engine.begin() as connection:
            await connection.run_sync(downgrade)
    assert (await task_row(app, identifier)).reservation_generation is not None
    assert (await state(reservation_context))["available"] == "2"


@pytest.mark.rabbitmq
async def test_real_phase_two_worker_publishes_consumes_and_confirms(reservation_context):
    if os.getenv("PERKFLOW_TEST_RABBITMQ") != "1":
        pytest.skip("Use --with-rabbitmq for isolated AMQP validation")
    app, _, base, _ = reservation_context
    local = Settings()
    settings = app.state.settings.model_copy(
        update={
            **{
                name: getattr(local, name)
                for name in [
                    "rabbitmq_host",
                    "rabbitmq_port",
                    "rabbitmq_user",
                    "rabbitmq_password",
                    "rabbitmq_vhost",
                ]
            },
            "rabbitmq_prefix": "perkflow.test.mq." + secrets.token_hex(8),
        }
    )
    identifier, _ = await purchase(reservation_context)
    running = asyncio.create_task(worker.run(settings))
    try:
        async with asyncio.timeout(15):
            while (await task_row(app, identifier)).status != "succeeded":
                await asyncio.sleep(0.1)
            while (await state(reservation_context))[f"r:{identifier}"] != "committed":
                await asyncio.sleep(0.1)
        assert await facts(app, base) == (2, 1)
        assert (await state(reservation_context))["available"] == "2"
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        async with connect_broker(settings) as cleanup:
            await cleanup.queue.delete(if_unused=False, if_empty=False)
            await cleanup.dead_queue.delete(if_unused=False, if_empty=False)
            await cleanup.exchange.delete(if_unused=False)
            dead = await cleanup.consumer.get_exchange(f"{settings.rabbitmq_prefix}.dead")
            await dead.delete(if_unused=False)
