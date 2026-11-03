"""订单锁：验证 UUID 持有者、Lua 释放、过期不误删，以及事务/故障边界。"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import Settings
from backend.app.core.errors import ApiError
from backend.app.core.order_lock import RedisOrderLock
from backend.app.core.redis_connection import create_redis_client
from backend.app.models import VoucherOrder
from backend.app.repositories import vouchers as repository
from backend.app.services import vouchers as service
from tests.backend import test_voucher_orders as baseline

order_context = baseline.order_context


@pytest.mark.parametrize(
    "values",
    [
        {"order_lock_ttl": 0},
        {"order_lock_ttl": 301},
        {"order_lock_key_prefix": "bad*prefix:"},
        {"order_lock_key_prefix": "missing-colon"},
        {"order_lock_key_prefix": "perkflow:auth:"},
        {"order_lock_key_prefix": "perkflow:cache:"},
        {"order_lock_key_prefix": "perkflow:lock:"},
        {"order_lock_key_prefix": "perkflow:id:"},
    ],
)
def test_order_lock_rejects_invalid_lease_or_namespace(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


async def test_acquire_is_one_set_nx_ex_and_redis_failure_does_not_bypass_lock():
    redis = AsyncMock()
    lock = RedisOrderLock(redis, "perkflow:test:lock:", 30)
    redis.set.return_value = True
    token = await lock.try_acquire(12, 34)
    assert UUID(token).version == 4
    redis.set.assert_awaited_once_with("perkflow:test:lock:12:34", token, nx=True, ex=30)
    redis.set.side_effect = RedisConnectionError("private connection details")
    with pytest.raises(ApiError) as error:
        await lock.try_acquire(12, 34)
    assert error.value.code == "ORDER_LOCK_UNAVAILABLE"
    assert "private" not in error.value.message
    redis.delete.assert_not_awaited()
    redis.eval.assert_not_awaited()


async def test_busy_request_never_starts_database_or_releases_other_request_lock(monkeypatch):
    redis = AsyncMock()
    redis.set.return_value = None
    lock = RedisOrderLock(redis, "perkflow:test:lock:", 30)
    transaction = AsyncMock()
    monkeypatch.setattr(service, "_create_order_with_retry", transaction)
    with pytest.raises(ApiError) as error:
        await service.create_order(None, 12, 34, None, lock)
    assert error.value.code == "ORDER_IN_PROGRESS" and error.value.retry_after == 1
    transaction.assert_not_awaited()
    redis.delete.assert_not_awaited()
    redis.eval.assert_not_awaited()


async def test_release_redis_failure_preserves_committed_result_and_does_not_retry(monkeypatch):
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.side_effect = RedisConnectionError("private connection details")
    result = object()
    monkeypatch.setattr(service, "_create_order_with_retry", AsyncMock(return_value=result))
    assert (
        await service.create_order(
            None, 12, 34, None, RedisOrderLock(redis, "perkflow:test:lock:", 30)
        )
        is result
    )
    redis.eval.assert_awaited_once()
    assert redis.eval.await_args.args[1:] == (
        1,
        "perkflow:test:lock:12:34",
        redis.set.await_args.args[1],
    )
    redis.get.assert_not_awaited()
    redis.delete.assert_not_awaited()


@pytest.mark.redis
async def test_independent_clients_compete_only_same_user_and_voucher(redis_settings):
    first, second = create_redis_client(redis_settings), create_redis_client(redis_settings)
    a = RedisOrderLock(first, redis_settings.order_lock_key_prefix, 30)
    b = RedisOrderLock(second, redis_settings.order_lock_key_prefix, 30)
    keys = [a.key(1, 2), a.key(1, 3), a.key(4, 2)]
    try:
        outcomes = await asyncio.gather(*[(a if i % 2 else b).try_acquire(1, 2) for i in range(16)])
        tokens = [token for token in outcomes if token is not None]
        assert len(tokens) == 1
        assert await first.type(keys[0]) == "string" and await first.get(keys[0]) == tokens[0]
        assert 0 < await first.ttl(keys[0]) <= 30
        assert await b.try_acquire(1, 3) and await b.try_acquire(4, 2)
        assert await a.release(1, 2, tokens[0])
        assert await b.try_acquire(1, 2)
    finally:
        await first.delete(*keys)
        await first.aclose()
        await second.aclose()


@pytest.mark.redis
async def test_expiry_allows_reacquire_but_old_release_cannot_delete_new_lock(redis_settings):
    redis = create_redis_client(redis_settings)
    a = RedisOrderLock(redis, redis_settings.order_lock_key_prefix, 30)
    b = RedisOrderLock(redis, redis_settings.order_lock_key_prefix, 30)
    key = a.key(1, 2)
    try:
        token_a = await a.try_acquire(1, 2)
        assert token_a is not None
        # 用 Redis 自己的到期机制缩短等待，不把 DEL 模拟成到期。
        await redis.pexpire(key, 1)
        await asyncio.sleep(0.03)
        token_b = await b.try_acquire(1, 2)
        assert token_b is not None and token_b != token_a
        assert await a.release(1, 2, token_a) is False
        assert await redis.get(key) == token_b and 0 < await redis.ttl(key) <= 30
        assert await b.release(1, 2, token_b) is True
        assert await redis.exists(key) == 0
    finally:
        await redis.delete(key)
        await redis.aclose()


@pytest.mark.redis
async def test_another_python_process_observes_same_redis_lock(redis_settings):
    redis = create_redis_client(redis_settings)
    lock = RedisOrderLock(redis, redis_settings.order_lock_key_prefix, 30)
    env = os.environ.copy()
    for name, value in {
        "REDIS_HOST": redis_settings.redis_host,
        "REDIS_PORT": redis_settings.redis_port,
        "REDIS_DB": redis_settings.redis_db,
        "REDIS_PASSWORD": redis_settings.redis_password.get_secret_value(),
        "ORDER_LOCK_KEY_PREFIX": redis_settings.order_lock_key_prefix,
    }.items():
        env[name] = str(value)
    source = """
import asyncio
from backend.app.core.config import Settings
from backend.app.core.order_lock import RedisOrderLock
from backend.app.core.redis_connection import create_redis_client
async def main():
    settings = Settings(_env_file=None)
    redis = create_redis_client(settings)
    try:
        lock = RedisOrderLock(redis, settings.order_lock_key_prefix, 30)
        print(int(await lock.try_acquire(1, 2) is not None))
    finally:
        await redis.aclose()
asyncio.run(main())
"""

    async def child_acquires():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            source,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 8)
            assert process.returncode == 0
            return stdout.strip() == b"1"
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    try:
        token = await lock.try_acquire(1, 2)
        assert token is not None
        assert not await child_acquires()
        await lock.release(1, 2, token)
        assert await child_acquires()
    finally:
        await redis.delete(lock.key(1, 2))
        await redis.aclose()


@pytest.mark.mysql
@pytest.mark.redis
async def test_http_busy_has_no_database_access_and_sequential_retry_reuses_order(
    order_context, monkeypatch
):
    app, client, base, phones = order_context
    headers = await baseline.login(client, app, phones[0])
    lock = app.state.order_lock
    token = await lock.try_acquire(base, base)
    assert token is not None
    original = repository.find_order
    find = AsyncMock(wraps=original)
    monkeypatch.setattr(repository, "find_order", find)
    try:
        response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ORDER_IN_PROGRESS"
        assert response.headers["Retry-After"] == "1"
        find.assert_not_awaited()
        assert await baseline.facts(app, base) == (3, 0)
        assert await app.state.cache_redis.get(lock.key(base, base)) == token
    finally:
        await lock.release(base, base, token)
    created = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert created.status_code == 201
    repeated = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert repeated.status_code == 200 and repeated.json()["reused"]
    assert repeated.json()["order"]["id"] == created.json()["order"]["id"]
    assert await baseline.facts(app, base) == (2, 1)
    assert await app.state.cache_redis.exists(lock.key(base, base)) == 0


@pytest.mark.mysql
@pytest.mark.redis
@pytest.mark.parametrize("failure", ["business", "database", "cancel"])
async def test_lock_released_after_transaction_end_on_failure(order_context, monkeypatch, failure):
    app, _, base, _ = order_context
    lock = app.state.order_lock
    original_release = lock.release
    observations = []
    async with app.state.database.sessions() as session:

        async def observe_release(user_id, voucher_id, token):
            assert not session.in_transaction()
            assert await app.state.cache_redis.get(lock.key(user_id, voucher_id)) == token
            observations.append("released")
            await original_release(user_id, voucher_id, token)

        original_insert = repository.insert_order

        async def injected_failure(current_session, order):
            await original_insert(current_session, order)
            if failure == "database":
                raise SQLAlchemyError("synthetic failure after INSERT")
            if failure == "cancel":
                raise asyncio.CancelledError()

        monkeypatch.setattr(lock, "release", observe_release)
        monkeypatch.setattr(repository, "insert_order", injected_failure)
        # 普通券现可购买；业务失败改用已结束活动，继续验证失败后事务结束再释放锁。
        voucher_id = base + 3 if failure == "business" else base
        expected = {
            "business": ApiError,
            "database": SQLAlchemyError,
            "cancel": asyncio.CancelledError,
        }[failure]
        with pytest.raises(expected):
            await service.create_order(session, base, voucher_id, app.state.order_ids, lock)
    assert observations == ["released"]
    assert await app.state.cache_redis.exists(lock.key(base, voucher_id)) == 0
    assert await baseline.facts(app, base) == (3, 0)


@pytest.mark.mysql
@pytest.mark.redis
async def test_success_commit_visible_before_lock_release(order_context, monkeypatch):
    app, _, base, _ = order_context
    lock = app.state.order_lock
    original_release = lock.release

    async def observe_release(user_id, voucher_id, token):
        async with app.state.database.sessions() as other_session:
            order = await other_session.scalar(
                select(VoucherOrder).where(
                    VoucherOrder.user_id == base, VoucherOrder.voucher_id == base
                )
            )
            assert order is not None
        assert await baseline.facts(app, base) == (2, 1)
        assert await app.state.cache_redis.get(lock.key(user_id, voucher_id)) == token
        await original_release(user_id, voucher_id, token)

    monkeypatch.setattr(lock, "release", observe_release)
    async with app.state.database.sessions() as session:
        result = await service.create_order(session, base, base, app.state.order_ids, lock)
    assert not result.reused
    assert await app.state.cache_redis.exists(lock.key(base, base)) == 0


@pytest.mark.mysql
@pytest.mark.redis
async def test_http_lock_acquire_failure_returns_specific_503_without_query(
    order_context, monkeypatch
):
    app, client, base, phones = order_context
    headers = await baseline.login(client, app, phones[0])
    original_set = app.state.cache_redis.set

    async def fail_only_order_lock(key, *args, **kwargs):
        if key.startswith(app.state.settings.order_lock_key_prefix):
            raise RedisConnectionError("synthetic lock failure")
        return await original_set(key, *args, **kwargs)

    query = AsyncMock()
    monkeypatch.setattr(app.state.cache_redis, "set", fail_only_order_lock)
    monkeypatch.setattr(repository, "find_order", query)
    response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ORDER_LOCK_UNAVAILABLE"
    query.assert_not_awaited()
    assert await baseline.facts(app, base) == (3, 0)


@pytest.mark.mysql
@pytest.mark.redis
async def test_http_release_failure_keeps_committed_order_and_lease(order_context, monkeypatch):
    app, client, base, phones = order_context
    headers = await baseline.login(client, app, phones[0])
    redis, lock = app.state.cache_redis, app.state.order_lock
    original_delete = redis.delete
    original_eval = redis.eval

    async def fail_only_order_lock(script, numkeys, *args):
        if numkeys == 1 and args[0] == lock.key(base, base):
            raise RedisConnectionError("synthetic release failure")
        return await original_eval(script, numkeys, *args)

    with monkeypatch.context() as patch:
        patch.setattr(redis, "eval", fail_only_order_lock)
        response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        assert response.status_code == 201
        assert await baseline.facts(app, base) == (2, 1)
        assert 0 < await redis.ttl(lock.key(base, base)) <= 30
    # 测试清理，不是在业务里绕过所属校验补发 DEL；业务释放失败依赖租约到期。
    await original_delete(lock.key(base, base))


@pytest.mark.mysql
@pytest.mark.redis
async def test_100_same_pair_requests_only_holder_enters_order_transaction(
    order_context, monkeypatch
):
    app, _, base, _ = order_context
    entered, finish = asyncio.Event(), asyncio.Event()
    original_insert = repository.insert_order
    inserts = 0

    async def hold_transaction(session, order):
        nonlocal inserts
        await original_insert(session, order)
        inserts += 1
        entered.set()
        await asyncio.wait_for(finish.wait(), 5)

    monkeypatch.setattr(repository, "insert_order", hold_transaction)

    async def buy():
        async with app.state.database.sessions() as session:
            return await service.create_order(
                session, base, base, app.state.order_ids, app.state.order_lock
            )

    holder = asyncio.create_task(buy())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        competitors = await asyncio.wait_for(
            asyncio.gather(*[buy() for _ in range(99)], return_exceptions=True), 3
        )
        assert all(
            isinstance(row, ApiError) and row.code == "ORDER_IN_PROGRESS" for row in competitors
        )
        assert inserts == 1
        finish.set()
        result = await asyncio.wait_for(holder, 3)
        assert not result.reused and await baseline.facts(app, base) == (2, 1)
        repeated = await buy()
        assert repeated.reused and repeated.order.id == result.order.id
    finally:
        finish.set()
        await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.redis
async def test_wrong_token_missing_key_and_repeat_release_are_harmless(redis_settings):
    redis = create_redis_client(redis_settings)
    lock = RedisOrderLock(redis, redis_settings.order_lock_key_prefix, 30)
    key = lock.key(1, 2)
    try:
        token = await lock.try_acquire(1, 2)
        assert token is not None
        before = await redis.pttl(key)
        assert await lock.release(1, 2, uuid4().hex) is False
        assert await redis.get(key) == token
        assert 0 < await redis.pttl(key) <= before
        assert await lock.release(1, 2, token) is True
        assert await lock.release(1, 2, token) is False
        next_token = await lock.try_acquire(1, 2)
        assert next_token is not None and next_token != token
        assert await lock.release(1, 2, token) is False
        assert await redis.get(key) == next_token
        assert await lock.release(1, 2, next_token) is True
    finally:
        await redis.delete(key)
        await redis.aclose()


@pytest.mark.redis
async def test_shared_lock_instance_keeps_token_local_to_each_coroutine(
    redis_settings, monkeypatch
):
    redis = create_redis_client(redis_settings)
    lock = RedisOrderLock(redis, redis_settings.order_lock_key_prefix, 30)
    arrived = asyncio.Event()
    tokens = {}

    async def overlap_transactions(session, user_id, voucher_id, ids):
        token = await redis.get(lock.key(user_id, voucher_id))
        assert UUID(token).version == 4
        tokens[voucher_id] = token
        if len(tokens) == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), 3)
        return voucher_id

    monkeypatch.setattr(service, "_create_order_with_retry", overlap_transactions)
    try:
        results = await asyncio.gather(
            service.create_order(None, 1, 2, None, lock),
            service.create_order(None, 1, 3, None, lock),
        )
        assert results == [2, 3] and len(set(tokens.values())) == 2
        assert await redis.exists(lock.key(1, 2), lock.key(1, 3)) == 0
    finally:
        await redis.delete(lock.key(1, 2), lock.key(1, 3))
        await redis.aclose()


@pytest.mark.mysql
@pytest.mark.redis
async def test_http_old_holder_finishes_without_deleting_successor_lock(order_context, monkeypatch):
    app, client, base, phones = order_context
    headers = await baseline.login(client, app, phones[0])
    redis, lock = app.state.cache_redis, app.state.order_lock
    original_release = lock.release
    successor = None

    async def replace_before_old_release(user_id, voucher_id, old_token):
        nonlocal successor
        assert await baseline.facts(app, base) == (2, 1)
        await redis.pexpire(lock.key(user_id, voucher_id), 1)
        await asyncio.sleep(0.03)
        successor = await lock.try_acquire(user_id, voucher_id)
        assert successor is not None and successor != old_token
        return await original_release(user_id, voucher_id, old_token)

    monkeypatch.setattr(lock, "release", replace_before_old_release)
    try:
        response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        assert response.status_code == 201
        assert await baseline.facts(app, base) == (2, 1)
        assert successor is not None and await redis.get(lock.key(base, base)) == successor
        assert await original_release(base, base, successor) is True
    finally:
        if successor is not None:
            await original_release(base, base, successor)


@pytest.mark.parametrize("error_type", [ApiError, SQLAlchemyError, asyncio.CancelledError])
async def test_release_failure_does_not_replace_original_transaction_error(monkeypatch, error_type):
    redis = AsyncMock()
    redis.set.return_value = True
    redis.eval.side_effect = RedisConnectionError("synthetic release failure")
    error = (
        ApiError(409, "OUT_OF_STOCK", "优惠券已抢完") if error_type is ApiError else error_type()
    )
    monkeypatch.setattr(service, "_create_order_with_retry", AsyncMock(side_effect=error))
    with pytest.raises(error_type) as captured:
        await service.create_order(
            None, 1, 2, None, RedisOrderLock(redis, "perkflow:test:lock:", 30)
        )
    assert captured.value is error
    redis.eval.assert_awaited_once()
