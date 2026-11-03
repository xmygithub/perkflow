"""异步订单事务与 HTTP；使用现有独立测试库/Redis 前缀，MQ 实测需显式开启。"""

import asyncio
import os
import runpy
import secrets
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import aio_pika
import httpx
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pamqp.commands import Basic
from sqlalchemy import delete, select, update

from backend.app.core.config import Settings
from backend.app.core.rabbitmq import OrderBroker, connect_broker
from backend.app.models import OrderRequest, SeckillVoucher, User, VoucherOrder
from backend.app.schemas.order_requests import OrderMessage
from backend.app.services import order_processing as processing
from backend.app.services.order_requests import now_utc
from backend.app.workers import orders as worker
from scripts.run_backend_tests import ROOT
from tests.backend.test_voucher_orders import facts
from tests.backend.test_voucher_orders import login as login_headers
from tests.backend.test_voucher_orders import order_context as sync_order_context

order_context = sync_order_context

pytestmark = [pytest.mark.mysql, pytest.mark.redis]


@pytest_asyncio.fixture
async def async_context(order_context):
    app, client, base, phones = order_context
    app.state.settings.order_mode = "async"
    try:
        yield app, client, base, phones
    finally:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(
                delete(OrderRequest).where(OrderRequest.voucher_id.between(base, base + 7))
            )


async def accepted(context, key=None, offset=0):
    app, client, base, phones = context
    current = (await client.get("/api/auth/session")).json()
    headers = (
        {"X-CSRF-Token": current["csrf_token"]}
        if current["user"]
        else await login_headers(client, app, phones[0])
    )
    headers["Idempotency-Key"] = key or str(uuid4())
    response = await client.post(f"/api/vouchers/{base + offset}/orders", headers=headers)
    assert response.status_code == 202, response.text
    return int(response.json()["request"]["id"]), headers


def delivery(identifier):
    message = AsyncMock()
    message.body = OrderMessage(request_id=str(identifier)).model_dump_json().encode()
    return message


async def task_row(app, identifier):
    async with app.state.database.sessions() as session:
        return await session.get(OrderRequest, identifier)


async def test_acceptance_has_no_order_or_stock_change_and_owner_only_lookup(async_context):
    app, client, base, _ = async_context
    identifier, headers = await accepted(async_context)
    assert await facts(app, base) == (3, 0)
    response = await client.get(f"/api/order-requests/{identifier}")
    assert response.status_code == 200 and response.json()["status"] == "pending"
    assert response.json()["order"] is None
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/order-requests")).json()["total"] == 1
    assert (
        await client.post(f"/api/orders/{identifier}/cancel", headers=headers)
    ).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as other:
        assert (await other.get(f"/api/order-requests/{identifier}")).status_code == 401
        await login_headers(other, app, async_context[3][1])
        assert (await other.get(f"/api/order-requests/{identifier}")).status_code == 404


async def test_idempotency_key_pending_and_success_reuses_same_purchase(async_context):
    app, client, base, _ = async_context
    key = str(uuid4())
    identifier, headers = await accepted(async_context, key)
    again = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert again.status_code == 202 and again.json()["reused"] is True
    assert again.headers["location"] == f"/api/order-requests/{identifier}"
    assert again.json()["request"]["id"] == str(identifier)
    different = await client.post(f"/api/vouchers/{base + 7}/orders", headers=headers)
    assert (
        different.status_code == 409 and different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    headers.pop("Idempotency-Key")
    assert (await client.post(f"/api/vouchers/{base}/orders", headers=headers)).status_code == 422
    headers["Idempotency-Key"] = str(uuid4())
    assert (await client.post(f"/api/vouchers/{base}/orders", headers=headers)).status_code == 409
    await processing.process_order(app.state.database, identifier)
    headers["Idempotency-Key"] = key
    response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert response.status_code == 200 and response.json()["request"]["status"] == "succeeded"
    assert response.json()["request"]["order"]["id"] == str(identifier)
    assert await facts(app, base) == (2, 1)


async def test_30_concurrent_redeliveries_create_one_order_and_ack_after_commit(async_context):
    app, _, base, _ = async_context
    identifier, _ = await accepted(async_context)
    messages = [delivery(identifier) for _ in range(30)]
    await asyncio.gather(
        *[worker.consume_message(app.state.database, app.state.settings, msg) for msg in messages]
    )
    assert await facts(app, base) == (2, 1)
    assert (await task_row(app, identifier)).status == "succeeded"
    for message in messages:
        message.ack.assert_awaited_once()
        message.nack.assert_not_awaited()


async def test_end_before_consumption_is_terminal_and_does_not_resurrect(async_context):
    app, client, base, _ = async_context
    identifier, _ = await accepted(async_context)
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(SeckillVoucher)
            .where(SeckillVoucher.voucher_id == base)
            .values(end_time=now_utc() - timedelta(seconds=1))
        )
    await processing.process_order(app.state.database, identifier)
    result = (await client.get(f"/api/order-requests/{identifier}")).json()
    assert result["status"] == "failed" and result["error_code"] == "SECKILL_ENDED"
    assert await facts(app, base) == (3, 0)
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(SeckillVoucher)
            .where(SeckillVoucher.voucher_id == base)
            .values(end_time=now_utc() + timedelta(hours=1))
        )
    assert await processing.process_order(app.state.database, identifier) == "terminal"
    assert await facts(app, base) == (3, 0)


async def test_cancel_rebuy_and_old_message_or_old_key_cannot_recreate(async_context):
    app, client, base, _ = async_context
    identifier, headers = await accepted(async_context)
    await processing.process_order(app.state.database, identifier)
    cancel = await client.post(f"/api/orders/{identifier}/cancel", headers=headers)
    assert cancel.status_code == 200
    old = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert old.json()["request"]["order"]["status"] == "cancelled"
    assert await facts(app, base) == (3, 1)
    next_id, _ = await accepted(async_context)
    assert next_id != identifier
    await processing.process_order(app.state.database, next_id)
    await worker.consume_message(app.state.database, app.state.settings, delivery(identifier))
    assert await facts(app, base) == (2, 2)
    requests = (await client.get("/api/order-requests")).json()
    assert requests["total"] == 0
    orders = (await client.get("/api/orders")).json()["items"]
    assert {row["status"] for row in orders} == {"created", "cancelled"}


async def test_twenty_buyers_for_three_stock_have_exactly_three_orders(async_context):
    app, _, base, _ = async_context
    users = [base + 100 + i for i in range(20)]
    ids = [await app.state.order_ids.next_id() for _ in users]
    try:
        async with app.state.database.sessions() as session, session.begin():
            for user in users:
                session.add(
                    User(
                        id=user,
                        phone=f"137{secrets.randbelow(100_000_000):08d}",
                        nick_name="异步并发测试",
                    )
                )
            await session.flush()
            for user, identifier in zip(users, ids, strict=True):
                session.add(
                    OrderRequest(
                        id=identifier,
                        user_id=user,
                        voucher_id=base,
                        request_key=str(uuid4()),
                        status="pending",
                        next_delivery_at=now_utc(),
                    )
                )

        # 多 Worker 会竞争库存行；显式死锁交由重试记录恢复，再检查完整业务事实。
        async def process(identifier):
            await worker.consume_message(
                app.state.database, app.state.settings, delivery(identifier)
            )

        await asyncio.gather(*[process(identifier) for identifier in ids])
        for identifier in ids:
            if (await task_row(app, identifier)).status == "pending":
                await process(identifier)
        assert await facts(app, base) == (0, 3)
        async with app.state.database.sessions() as session:
            states = (
                await session.scalars(select(OrderRequest.status).where(OrderRequest.id.in_(ids)))
            ).all()
            assert states.count("succeeded") == 3 and states.count("failed") == 17
    finally:
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(delete(OrderRequest).where(OrderRequest.user_id.in_(users)))
            await session.execute(delete(VoucherOrder).where(VoucherOrder.user_id.in_(users)))
            await session.execute(delete(User).where(User.id.in_(users)))


async def test_processing_failures_are_bounded_persisted_then_acked(async_context, monkeypatch):
    app, _, base, _ = async_context
    identifier, _ = await accepted(async_context)
    monkeypatch.setattr(
        worker, "process_order", AsyncMock(side_effect=RuntimeError("do not print body"))
    )
    for _ in range(app.state.settings.order_retry_limit):
        message = delivery(identifier)
        await worker.consume_message(app.state.database, app.state.settings, message)
        message.ack.assert_awaited_once()
    task = await task_row(app, identifier)
    assert task.status == "failed" and task.attempts == 5
    assert await facts(app, base) == (3, 0)
    # 真实事务入口不再处理失败终态；新购买才使用新的编号。
    assert await processing.process_order(app.state.database, identifier) == "terminal"


async def test_commit_reply_loss_does_not_mark_completed_request_failed(async_context, monkeypatch):
    app, _, base, _ = async_context
    identifier, _ = await accepted(async_context)

    async def lost_reply(database, identifier):
        await processing.process_order(database, identifier)
        raise ConnectionError("uncertain commit reply")

    monkeypatch.setattr(worker, "process_order", lost_reply)
    message = delivery(identifier)
    await worker.consume_message(app.state.database, app.state.settings, message)
    message.ack.assert_awaited_once()
    assert (await task_row(app, identifier)).status == "succeeded"
    assert await facts(app, base) == (2, 1)


async def test_missing_failure_record_is_not_acked_and_message_requeued(async_context, monkeypatch):
    app, _, _, _ = async_context
    identifier, _ = await accepted(async_context)
    monkeypatch.setattr(worker, "process_order", AsyncMock(side_effect=ConnectionError()))
    monkeypatch.setattr(worker, "record_failure", AsyncMock(side_effect=ConnectionError()))
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())
    message = delivery(identifier)
    await worker.consume_message(app.state.database, app.state.settings, message)
    message.ack.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)


async def test_dispatch_crash_lease_recovers_and_stale_sender_cannot_overwrite(async_context):
    app, _, _, _ = async_context
    identifier, _ = await accepted(async_context)
    db, settings = app.state.database, app.state.settings
    first = await processing.claim_delivery(db, settings)
    assert first[0] == identifier
    assert await processing.claim_delivery(db, settings) is None
    async with db.sessions() as session, session.begin():
        await session.execute(
            update(OrderRequest)
            .where(OrderRequest.id == identifier)
            .values(lease_until=now_utc() - timedelta(seconds=1))
        )
    second = await processing.claim_delivery(db, settings)
    assert second[1] != first[1]
    await processing.finish_delivery(db, settings, *first, success=True)
    assert (await task_row(app, identifier)).delivery_token == second[1]
    await processing.finish_delivery(db, settings, *second, success=True)
    assert (await task_row(app, identifier)).delivery_token is None


async def test_publish_unknown_keeps_recoverable_request(async_context):
    app, _, base, _ = async_context
    identifier, _ = await accepted(async_context)
    broker = AsyncMock()
    broker.publish.side_effect = TimeoutError("private connection detail")
    assert await worker.dispatch_one(app.state.database, app.state.settings, broker)
    task = await task_row(app, identifier)
    assert task.status == "pending" and task.delivery_token is None and task.deliveries == 1
    assert task.next_delivery_at > now_utc()
    assert await facts(app, base) == (3, 0)


async def test_existing_request_prevents_destructive_schema_downgrade(async_context):
    app = async_context[0]
    identifier, _ = await accepted(async_context)

    def downgrade(connection):
        # 只验证0003自身的拒绝条件；跨版本回退会先删0006空表，DDL无法靠回滚恢复。
        path = ROOT / "backend/alembic/versions/0003_async_order_requests.py"
        with Operations.context(MigrationContext.configure(connection)):
            runpy.run_path(str(path))["downgrade"]()

    with pytest.raises(RuntimeError, match="不能无损回退"):
        async with app.state.database.engine.begin() as connection:
            await connection.run_sync(downgrade)
    assert (await task_row(app, identifier)).status == "pending"


async def test_publisher_nack_cannot_be_mistaken_for_ack():
    broker = OrderBroker(Settings(_env_file=None), None)
    broker.exchange = AsyncMock()
    broker.exchange.publish.return_value = Basic.Nack()
    with pytest.raises(RuntimeError, match="not confirmed"):
        await broker.publish(123)


@pytest_asyncio.fixture
async def real_broker(async_context):
    if os.getenv("PERKFLOW_TEST_RABBITMQ") != "1":
        pytest.skip("Use --with-rabbitmq to opt into isolated broker integration tests")
    app = async_context[0]
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
    assert settings.rabbitmq_prefix.startswith("perkflow.test.mq.")
    try:
        async with connect_broker(settings) as broker:
            yield broker, settings
    finally:
        # 即使测试断开了原连接，也只通过新连接清理本次随机拓扑，不触碰业务队列。
        async with connect_broker(settings) as cleanup:
            await cleanup.queue.delete(if_unused=False, if_empty=False)
            await cleanup.dead_queue.delete(if_unused=False, if_empty=False)
            await cleanup.exchange.delete(if_unused=False)
            dead = await cleanup.consumer.get_exchange(f"{settings.rabbitmq_prefix}.dead")
            await dead.delete(if_unused=False)


@pytest.mark.rabbitmq
async def test_real_amqp_confirm_dispatch_consume_and_unroutable(async_context, real_broker):
    app, _, base, _ = async_context
    broker, settings = real_broker
    identifier, _ = await accepted(async_context)
    assert await worker.dispatch_one(app.state.database, settings, broker)
    message = await broker.queue.get(timeout=5)
    assert message.message_id == str(identifier) and message.delivery_mode == 2
    await worker.consume_message(app.state.database, settings, message)
    assert await facts(app, base) == (2, 1)
    with pytest.raises(aio_pika.exceptions.DeliveryError):
        await broker.exchange.publish(
            aio_pika.Message(b"test"), routing_key="not.bound", mandatory=True
        )


@pytest.mark.rabbitmq
async def test_real_connection_loss_after_commit_redelivers_without_duplicate(
    async_context, real_broker
):
    app, _, base, _ = async_context
    broker, settings = real_broker
    identifier, _ = await accepted(async_context)
    await broker.publish(identifier)
    message = await broker.queue.get(timeout=5)
    assert not message.redelivered
    await processing.process_order(app.state.database, identifier)
    # 实际关闭连接，故意没有 ACK；新连接应收到重复交付，不能再次扣库存。
    await broker.connection.close()
    async with connect_broker(settings) as recovered:
        again = await recovered.queue.get(timeout=5)
        assert again.redelivered
        await worker.consume_message(app.state.database, settings, again)
    assert await facts(app, base) == (2, 1)


@pytest.mark.rabbitmq
async def test_real_invalid_message_is_quarantined(async_context, real_broker):
    app = async_context[0]
    broker, settings = real_broker
    await broker.exchange.publish(
        aio_pika.Message(b'{"version":99,"request_id":"1"}'),
        routing_key="order.create",
        mandatory=True,
    )
    message = await broker.queue.get(timeout=5)
    await worker.consume_message(app.state.database, settings, message)
    # basic.get 是一次立即查询，timeout 不是“等待出现消息”；DLX 转发完成前可暂时为空。
    dead = None
    for _ in range(40):
        dead = await broker.dead_queue.get(timeout=5, fail=False)
        if dead is not None:
            break
        await asyncio.sleep(0.05)
    assert dead is not None
    assert dead.body == message.body
    await dead.ack()


@pytest.mark.rabbitmq
async def test_real_worker_entry_dispatches_consumes_and_shuts_down(async_context, real_broker):
    app, _, base, _ = async_context
    _, settings = real_broker
    identifier, _ = await accepted(async_context)
    running = asyncio.create_task(worker.run(settings))
    try:
        for _ in range(100):
            if (await task_row(app, identifier)).status == "succeeded":
                break
            await asyncio.sleep(0.05)
        assert (await task_row(app, identifier)).status == "succeeded"
        assert await facts(app, base) == (2, 1)
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
