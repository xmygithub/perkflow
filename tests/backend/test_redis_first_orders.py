"""秒杀入口必须零 SQL；真实 Redis 原子受理，MQ 消费之后才首次写请求/订单。"""

import asyncio
import json
import os
import secrets
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import delete, event, func, select, update

from backend.app.core.config import Settings
from backend.app.core.rabbitmq import connect_broker
from backend.app.models import (
    OrderRequest,
    SeckillEffect,
    SeckillGate,
    SeckillVoucher,
    User,
    Voucher,
    VoucherOrder,
)
from backend.app.schemas.order_requests import RequestOut
from backend.app.services import redis_orders as fast_orders
from backend.app.services.order_requests import now_utc
from backend.app.workers import orders as worker
from tests.backend.test_voucher_orders import facts, login
from tests.backend.test_voucher_orders import order_context as base_context

order_context = base_context
pytestmark = [pytest.mark.mysql, pytest.mark.redis]


@pytest_asyncio.fixture(params=[0, 1], ids=["normal", "seckill"])
async def fast_context(order_context, request):
    app, client, base, phones = order_context
    app.state.settings.order_mode = "async"
    app.state.settings.order_admission_mode = "redis_first"
    # 同一组零 SQL、并发、MQ 故障、取消/重买用例同时覆盖两类券，防止只开放前端按钮。
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(update(Voucher).where(Voucher.id == base).values(type=request.param))
    headers = await login(client, app, phones[0])
    try:
        yield app, client, base, phones, headers
    finally:
        async with app.state.database.sessions() as session, session.begin():
            for model in [SeckillEffect, OrderRequest, SeckillGate]:
                await session.execute(delete(model).where(model.voucher_id.between(base, base + 7)))
        prefix = app.state.settings.seckill_key_prefix
        assert prefix.startswith("perkflow:test:")
        keys = [key async for key in app.state.cache_redis.scan_iter(match=prefix + "*")]
        if keys:
            await app.state.cache_redis.delete(*keys)


class SqlProbe:
    def __init__(self, app):
        self.engine, self.queries = app.state.database.engine.sync_engine, []

    def record(self, connection, cursor, statement, parameters, context, executemany):
        self.queries.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self.record)
        return self

    def __exit__(self, *args):
        event.remove(self.engine, "before_cursor_execute", self.record)


async def prepare(context):
    app, _, base, _, _ = context
    assert await fast_orders.warm(app.state.database, app.state.order_ingress, [base]) == 1


async def post(context, key=None):
    _, client, base, _, headers = context
    request_headers = {**headers, "Idempotency-Key": key or str(uuid4())}
    return await client.post(
        f"/api/vouchers/{base}/orders", headers=request_headers
    ), request_headers


async def count_requests(app, voucher_id):
    async with app.state.database.sessions() as session:
        return await session.scalar(
            select(func.count())
            .select_from(OrderRequest)
            .where(OrderRequest.voucher_id == voucher_id)
        )


async def capture_message(context):
    app = context[0]
    broker = AsyncMock()
    assert await fast_orders.dispatch_one(app.state.settings, broker, app.state.order_ingress)
    broker.publish_message.assert_awaited_once()
    return broker.publish_message.call_args.args[0]


async def consume(context, payload):
    app = context[0]
    message = AsyncMock()
    message.body = payload.model_dump_json(exclude_none=True).encode()
    await worker.consume_message(
        app.state.database,
        app.state.settings,
        message,
        app.state.seckill_store,
        app.state.order_ingress,
    )
    message.ack.assert_awaited_once()
    return message


async def available(context):
    app, _, base, _, _ = context
    return int(await app.state.cache_redis.hget(app.state.seckill_store.key(base), "available"))


async def test_unprepared_request_fails_closed_with_no_sql_or_request_row(fast_context):
    app, _, base, _, _ = fast_context
    with SqlProbe(app) as probe:
        response, _ = await post(fast_context)
    assert response.status_code == 503 and response.json()["error"]["code"] == "SECKILL_NOT_READY"
    assert probe.queries == []
    assert await count_requests(app, base) == 0


async def test_http_acceptance_progress_and_idempotency_all_execute_zero_sql(fast_context):
    app, client, base, _, _ = fast_context
    await prepare(fast_context)
    with SqlProbe(app) as probe:
        response, headers = await post(fast_context)
        assert response.status_code == 202, response.text
        identifier = response.json()["request"]["id"]
        repeated = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        progress = await client.get(f"/api/order-requests/{identifier}")
        listed = await client.get("/api/order-requests")
    assert probe.queries == []
    assert repeated.status_code == 202 and repeated.json()["request"]["id"] == identifier
    assert progress.json()["reservation_acquired"] is True
    assert listed.json()["total"] == 1
    assert await count_requests(app, base) == 0
    assert await facts(app, base) == (3, 0) and await available(fast_context) == 2
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as other:
        await login(other, app, fast_context[3][1])
        with SqlProbe(app) as probe:
            hidden = await other.get(f"/api/order-requests/{identifier}")
        assert hidden.status_code == 404 and probe.queries == []


async def test_30_same_intent_http_requests_reserve_once_without_sql(fast_context):
    app = fast_context[0]
    await prepare(fast_context)
    key = str(uuid4())
    with SqlProbe(app) as probe:
        responses = await asyncio.gather(*[post(fast_context, key) for _ in range(30)])
    assert probe.queries == []
    assert all(response.status_code == 202 for response, _ in responses)
    assert len({response.json()["request"]["id"] for response, _ in responses}) == 1
    assert await available(fast_context) == 2
    assert await app.state.cache_redis.zcard(app.state.order_ingress.outbox) == 1


async def test_30_buyers_only_three_pass_http_and_only_these_reach_mysql(fast_context):
    app, _, base, _, _ = fast_context
    await prepare(fast_context)
    users = [base + 100 + i for i in range(30)]
    clients = []
    try:
        async with app.state.database.sessions() as session, session.begin():
            session.add_all(
                [
                    User(id=user, phone=f"135{user:08d}", nick_name="入口压力正确性测试")
                    for user in users
                ]
            )
        for user in users:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            )
            headers = await login(client, app, f"135{user:08d}")
            clients.append((client, {**headers, "Idempotency-Key": str(uuid4())}))
        with SqlProbe(app) as probe:
            responses = await asyncio.gather(
                *[
                    client.post(f"/api/vouchers/{base}/orders", headers=headers)
                    for client, headers in clients
                ]
            )
        assert probe.queries == []
        assert sum(response.status_code == 202 for response in responses) == 3
        assert sum(response.status_code == 409 for response in responses) == 27
        assert await count_requests(app, base) == 0 and await available(fast_context) == 0
        messages = []
        with SqlProbe(app) as probe:
            for _ in range(3):
                messages.append(await capture_message(fast_context))
        assert probe.queries == []  # Redis→MQ 投递也不先写 SQL。
        for payload in messages:
            assert payload.version == 2
            await consume(fast_context, payload)
        assert await count_requests(app, base) == 3 and await facts(app, base) == (0, 3)
    finally:
        for client, _ in clients:
            await client.aclose()
        async with app.state.database.sessions() as session, session.begin():
            for model in [SeckillEffect, OrderRequest, VoucherOrder, User]:
                condition = model.id.in_(users) if model is User else model.user_id.in_(users)
                await session.execute(delete(model).where(condition))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("available", "0", "OUT_OF_STOCK"),
        ("enabled", "0", "VOUCHER_OFF_SHELF"),
        ("end_ms", "1", "SECKILL_ENDED"),
        ("begin_ms", "9999999999999", "SECKILL_NOT_STARTED"),
    ],
)
async def test_lua_rejections_never_insert_mysql_intents(fast_context, field, value, code):
    app, _, base, _, _ = fast_context
    await prepare(fast_context)
    await app.state.cache_redis.hset(app.state.seckill_store.key(base), field, value)
    with SqlProbe(app) as probe:
        response, _ = await post(fast_context)
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert probe.queries == [] and await count_requests(app, base) == 0


async def test_mq_publish_failure_keeps_redis_outbox_and_retries_same_id(fast_context):
    app, client, base, _, _ = fast_context
    await prepare(fast_context)
    response, headers = await post(fast_context)
    identifier = response.json()["request"]["id"]
    broker = AsyncMock()
    broker.publish_message.side_effect = TimeoutError()
    with SqlProbe(app) as probe:
        assert await fast_orders.dispatch_one(app.state.settings, broker, app.state.order_ingress)
    assert probe.queries == [] and await count_requests(app, base) == 0
    assert await available(fast_context) == 2
    member = f"{base}:{identifier}"
    assert await app.state.cache_redis.zscore(app.state.order_ingress.outbox, member) is not None
    await app.state.cache_redis.zadd(app.state.order_ingress.outbox, {member: 0})
    payload = await capture_message(fast_context)
    assert payload.request_id == identifier
    await asyncio.gather(*[consume(fast_context, payload) for _ in range(15)])
    assert await facts(app, base) == (2, 1) and await count_requests(app, base) == 1
    with SqlProbe(app) as probe:
        repeated = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert repeated.status_code == 200 and repeated.json()["request"]["order"]["id"] == identifier
    assert probe.queries == []


async def test_lease_owner_recovery_cannot_be_overwritten_by_old_sender(fast_context):
    app = fast_context[0]
    await prepare(fast_context)
    await post(fast_context)
    ingress = app.state.order_ingress
    first = await ingress.claim(30)
    assert await ingress.claim(30) is None
    await ingress.redis.zadd(ingress.outbox, {first[0]: 0})
    second = await ingress.claim(30)
    await ingress.finish(first[0], first[1], delay=None)
    assert await ingress.redis.hget(ingress.owners, first[0]) == second[1]
    assert await ingress.redis.zscore(ingress.outbox, first[0]) is not None


async def test_cross_voucher_intent_and_duplicate_qualification_are_rejected_without_sql(
    fast_context,
):
    app, client, base, _, _ = fast_context
    await prepare(fast_context)
    _, headers = await post(fast_context)
    with SqlProbe(app) as probe:
        changed = await client.post(f"/api/vouchers/{base + 7}/orders", headers=headers)
        duplicate, _ = await post(fast_context)
    assert probe.queries == []
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert (
        duplicate.status_code == 409
        and duplicate.json()["error"]["code"] == "PURCHASE_QUALIFICATION_BUSY"
    )
    assert await count_requests(app, base) == 0 and await available(fast_context) == 2


async def test_legacy_request_alias_shows_cancelled_after_cache_projection(fast_context):
    app, client, base, _, _ = fast_context
    await prepare(fast_context)
    response, headers = await post(fast_context)
    identifier = int(response.json()["request"]["id"])
    await consume(fast_context, await capture_message(fast_context))
    alias = await app.state.order_ids.next_id()
    async with app.state.database.sessions() as session, session.begin():
        receipt = OrderRequest(
            id=alias,
            user_id=base,
            voucher_id=base,
            request_key=str(uuid4()),
            status="succeeded",
            order_id=identifier,
            next_delivery_at=now_utc(),
        )
        session.add(receipt)
        await session.flush()
        await session.refresh(receipt)
        await fast_orders.project_task(session, app.state.order_ingress, receipt)
    cancelled = await client.post(f"/api/orders/{identifier}/cancel", headers=headers)
    assert cancelled.status_code == 200
    with SqlProbe(app) as probe:
        result = await client.get(f"/api/order-requests/{alias}")
    assert probe.queries == [] and result.json()["order"]["status"] == "cancelled"


async def test_database_first_comparison_has_sql_while_default_path_has_none(fast_context):
    app, client, base, _, headers = fast_context
    await fast_orders.warm(app.state.database, app.state.order_ingress, [base, base + 7])
    with SqlProbe(app) as before:
        app.state.settings.order_admission_mode = "redis"
        old = await client.post(
            f"/api/vouchers/{base + 7}/orders", headers={**headers, "Idempotency-Key": str(uuid4())}
        )
    app.state.settings.order_admission_mode = "redis_first"
    with SqlProbe(app) as after:
        new, _ = await post(fast_context)
    assert old.status_code == 202 and new.status_code == 202
    assert len(before.queries) > 0 and after.queries == []
    assert await count_requests(app, base + 7) == 1 and await count_requests(app, base) == 0
    print(f"Single POST SQL comparison: database-first={len(before.queries)}, redis-first=0")


async def test_lua_reply_loss_does_not_return_stock_or_create_sql_row(fast_context, monkeypatch):
    app, client, base, _, headers = fast_context
    await prepare(fast_context)
    ingress = app.state.order_ingress
    original = ingress.admit_script
    key = str(uuid4())

    async def lost_reply(*args, **kwargs):
        await original(*args, **kwargs)
        raise RedisTimeoutError("simulated lost response")

    monkeypatch.setattr(ingress, "admit_script", lost_reply)
    with SqlProbe(app) as probe:
        response, _ = await post(fast_context, key)
    assert response.status_code == 503 and probe.queries == []
    assert await available(fast_context) == 2 and await count_requests(app, base) == 0
    monkeypatch.setattr(ingress, "admit_script", original)
    with SqlProbe(app) as probe:
        response = await client.post(
            f"/api/vouchers/{base}/orders", headers={**headers, "Idempotency-Key": key}
        )
    assert response.status_code == 202 and response.json()["reused"] is True and probe.queries == []
    assert await available(fast_context) == 2


async def test_payload_repairs_missing_indexes_without_new_reservation(fast_context):
    app, client, base, _, _ = fast_context
    await prepare(fast_context)
    response, headers = await post(fast_context)
    identifier = response.json()["request"]["id"]
    ingress = app.state.order_ingress
    await ingress.redis.delete(
        ingress.document_key(identifier),
        ingress.outbox,
        ingress.intent_key(base, headers["Idempotency-Key"]),
        ingress.user_key(base),
    )
    with SqlProbe(app) as probe:
        repeated = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert probe.queries == [] and repeated.status_code == 202
    assert repeated.json()["request"]["id"] == identifier and await available(fast_context) == 2
    assert await ingress.redis.zcard(ingress.outbox) == 1


async def test_cancel_rebuy_cache_and_stale_pending_projection_do_not_regress(fast_context):
    app, client, base, _, _ = fast_context
    await prepare(fast_context)
    response, headers = await post(fast_context)
    identifier = response.json()["request"]["id"]
    ingress = app.state.order_ingress
    old_pending = await ingress.get_document(identifier)
    payload = await capture_message(fast_context)
    await consume(fast_context, payload)
    old_created = await ingress.get_document(identifier)
    await ingress.project(old_pending, RequestOut.model_validate(old_pending["result"]))
    await ingress.repair(old_pending)
    assert (await ingress.get_document(identifier))["result"]["status"] == "succeeded"
    cancelled = await client.post(f"/api/orders/{identifier}/cancel", headers=headers)
    assert cancelled.status_code == 200 and await available(fast_context) == 3
    await ingress.project(old_created, RequestOut.model_validate(old_created["result"]))
    with SqlProbe(app) as probe:
        old = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        response, _ = await post(fast_context)
    assert probe.queries == []
    assert old.json()["request"]["order"]["status"] == "cancelled"
    assert response.status_code == 202 and response.json()["request"]["id"] != identifier
    await consume(fast_context, payload)
    assert await available(fast_context) == 2  # 旧消息不释放新购买的资格。


async def test_activity_ends_in_queue_compensation_updates_cached_failure(fast_context):
    app, client, base, _, _ = fast_context
    await prepare(fast_context)
    response, _ = await post(fast_context)
    identifier = response.json()["request"]["id"]
    payload = await capture_message(fast_context)
    async with app.state.database.sessions() as session, session.begin():
        await session.execute(
            update(SeckillVoucher)
            .where(SeckillVoucher.voucher_id == base)
            .values(end_time=now_utc() - timedelta(seconds=1))
        )
    await consume(fast_context, payload)
    with SqlProbe(app) as probe:
        result = await client.get(f"/api/order-requests/{identifier}")
    assert probe.queries == [] and result.json()["error_code"] == "SECKILL_ENDED"
    assert result.json()["status"] == "failed" and await available(fast_context) == 3
    assert await facts(app, base) == (3, 0)


async def test_offline_rebuild_includes_redis_only_intent_before_any_sql_materialization(
    fast_context,
):
    app, _, base, _, _ = fast_context
    await prepare(fast_context)
    response, _ = await post(fast_context)
    identifier = response.json()["request"]["id"]
    assert await count_requests(app, base) == 0
    await app.state.cache_redis.delete(app.state.seckill_store.key(base))
    result = await fast_orders.rebuild(app.state.database, app.state.order_ingress, base)
    assert result["pending_reserved"] == 1 and result["available"] == 2
    payload = await capture_message(fast_context)
    assert payload.request_id == identifier
    await consume(fast_context, payload)
    assert await facts(app, base) == (2, 1)


@pytest_asyncio.fixture
async def fast_broker(fast_context):
    if os.getenv("PERKFLOW_TEST_RABBITMQ") != "1":
        pytest.skip("Use --with-rabbitmq for isolated AMQP validation")
    app = fast_context[0]
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
    try:
        async with connect_broker(settings) as broker:
            yield broker, settings
    finally:
        async with connect_broker(settings) as broker:
            await broker.queue.delete(if_unused=False, if_empty=False)
            await broker.dead_queue.delete(if_unused=False, if_empty=False)
            await broker.exchange.delete(if_unused=False)
            dead = await broker.consumer.get_exchange(settings.rabbitmq_prefix + ".dead")
            await dead.delete(if_unused=False)


@pytest.mark.rabbitmq
async def test_real_mq_v2_message_and_commit_before_ack_loss_are_idempotent(
    fast_context, fast_broker
):
    app, _, base, _, _ = fast_context
    broker, settings = fast_broker
    await prepare(fast_context)
    response, _ = await post(fast_context)
    identifier = int(response.json()["request"]["id"])
    with SqlProbe(app) as probe:
        await fast_orders.dispatch_one(settings, broker, app.state.order_ingress)
    assert probe.queries == [] and await count_requests(app, base) == 0
    message = await broker.queue.get(timeout=5)
    body = json.loads(message.body)
    assert body["version"] == 2 and body["purchase"]["request_id"] == str(identifier)
    from backend.app.schemas.order_requests import RedisPurchase
    from backend.app.services.order_processing import process_order

    await fast_orders.materialize(
        app.state.database, RedisPurchase.model_validate(body["purchase"])
    )
    await process_order(app.state.database, identifier, require_reservation=True)
    await broker.connection.close()  # SQL 已提交但未 ACK；真实连接断开触发重新交付。
    async with connect_broker(settings) as again:
        redelivered = await again.queue.get(timeout=5)
        assert redelivered.redelivered
        await worker.consume_message(
            app.state.database,
            settings,
            redelivered,
            app.state.seckill_store,
            app.state.order_ingress,
        )
    assert await facts(app, base) == (2, 1) and await available(fast_context) == 2


@pytest.mark.rabbitmq
async def test_real_worker_warms_and_processes_default_redis_first_path(fast_context, fast_broker):
    app, client, base, _, _ = fast_context
    _, settings = fast_broker
    running = asyncio.create_task(worker.run(settings))
    try:
        async with asyncio.timeout(15):
            while not await app.state.cache_redis.hget(
                app.state.seckill_store.key(base), "ingress_version"
            ):
                await asyncio.sleep(0.05)
            response, _ = await post(fast_context)
            assert response.status_code == 202
            identifier = response.json()["request"]["id"]
            while (await client.get(f"/api/order-requests/{identifier}")).json()[
                "status"
            ] == "pending":
                await asyncio.sleep(0.1)
        assert await facts(app, base) == (2, 1)
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
