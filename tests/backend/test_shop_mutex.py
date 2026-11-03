"""真实 Redis 与 API：验证同一商户重建协调、所属检查和等待边界。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from tests.backend.test_shop_cache import SHOP, key_for
from tests.backend.test_shop_cache import cache_client as cache_client

pytestmark = pytest.mark.redis
PATH = "/api/shops/1"


def lock_for(app, shop_id=1):
    return f"{app.state.settings.cache_lock_key_prefix}shop:{shop_id}"


@pytest.mark.parametrize("exists", [True, False])
async def test_expired_shop_burst_uses_one_query_and_releases_lock(
    cache_client, monkeypatch, exists
):
    client, app, detail, _ = cache_client
    cache = app.state.cache_redis
    key, lock_key = key_for(app, PATH), lock_for(app)
    await cache.set(key, SHOP.model_dump_json(), ex=60)
    await cache.pexpire(key, 1)
    await asyncio.sleep(0.02)
    attempts, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    count = 12
    arrived = set()
    original_set = cache.set

    async def observe_lock(*args, **kwargs):
        if args[0] == lock_key and kwargs.get("nx"):
            arrived.add(args[1])
            if len(arrived) == count:
                attempts.set()
        return await original_set(*args, **kwargs)

    async def slow_query(*_args):
        entered.set()
        await asyncio.wait_for(release.wait(), 3)
        return SHOP if exists else None

    detail.side_effect = slow_query
    monkeypatch.setattr(cache, "set", observe_lock)
    tasks = [asyncio.create_task(client.get(PATH)) for _ in range(count)]
    try:
        await asyncio.wait_for(attempts.wait(), 2)
        await asyncio.wait_for(entered.wait(), 2)
        assert detail.await_count == 1
        assert 0 < await cache.pttl(lock_key) <= app.state.settings.shop_cache_lock_ttl_ms
    finally:
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
    assert all(r.status_code == (200 if exists else 404) for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)
    assert detail.await_count == 1
    assert await cache.exists(lock_key) == 0
    assert await cache.get(key) == (SHOP.model_dump_json() if exists else "")
    ttl = await cache.ttl(key)
    assert 1799 <= ttl <= 2100 if exists else 119 <= ttl <= 120


@pytest.mark.parametrize("exists", [True, False])
async def test_holder_rechecks_after_acquiring_lock_and_skips_query(
    cache_client, monkeypatch, exists
):
    client, app, detail, _ = cache_client
    cache = app.state.cache_redis
    key, lock_key = key_for(app, PATH), lock_for(app)
    original_set = cache.set

    async def fill_between_initial_read_and_lock(*args, **kwargs):
        result = await original_set(*args, **kwargs)
        if args[0] == lock_key and result:
            await original_set(key, SHOP.model_dump_json() if exists else "", ex=60)
        return result

    monkeypatch.setattr(cache, "set", fill_between_initial_read_and_lock)
    response = await client.get(PATH)
    assert response.status_code == (200 if exists else 404)
    assert detail.await_count == 0
    assert await cache.exists(lock_key) == 0


async def test_different_shop_ids_can_rebuild_concurrently(cache_client):
    client, app, detail, _ = cache_client
    entered = set()
    together = asyncio.Event()

    async def query(_session, shop_id):
        entered.add(shop_id)
        if len(entered) == 2:
            together.set()
        await asyncio.wait_for(together.wait(), 2)
        return SHOP.model_copy(update={"id": shop_id})

    detail.side_effect = query
    responses = await asyncio.gather(client.get(PATH), client.get("/api/shops/2"))
    assert [r.status_code for r in responses] == [200, 200]
    assert detail.await_count == 2
    assert await app.state.cache_redis.exists(lock_for(app), lock_for(app, 2)) == 0


async def test_wait_budget_exhausted_is_503_and_never_queries_database(cache_client):
    client, app, detail, _ = cache_client
    cache = app.state.cache_redis
    app.state.settings.shop_cache_lock_wait_ms = 60
    app.state.settings.shop_cache_lock_retry_ms = 10
    await cache.set(lock_for(app), "other-test-request", px=5000)
    before = await cache.pttl(lock_for(app))
    response = await asyncio.wait_for(client.get(PATH), 1)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CACHE_REBUILD_BUSY"
    assert response.headers["retry-after"] == "1"
    assert detail.await_count == 0
    assert await cache.get(lock_for(app)) == "other-test-request"
    assert 0 < await cache.pttl(lock_for(app)) <= before
    assert await cache.exists(key_for(app, PATH)) == 0


@pytest.mark.parametrize("exists", [True, False])
async def test_expired_holder_cannot_overwrite_or_delete_successor(cache_client, exists):
    client, app, detail, _ = cache_client
    cache = app.state.cache_redis
    entered, release = asyncio.Event(), asyncio.Event()
    key, lock_key = key_for(app, PATH), lock_for(app)

    async def query(*_args):
        entered.set()
        await asyncio.wait_for(release.wait(), 3)
        return SHOP if exists else None

    detail.side_effect = query
    task = asyncio.create_task(client.get(PATH))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await cache.pexpire(lock_key, 1)
        await asyncio.sleep(0.02)
        assert await cache.exists(lock_key) == 0
        assert await cache.set(lock_key, "successor-test-request", nx=True, px=5000)
        successor = SHOP.model_copy(update={"name": "后续请求已经回填的新结果"})
        await cache.set(key, successor.model_dump_json(), ex=60)
    finally:
        release.set()
        response = await asyncio.wait_for(task, 3)
    assert response.status_code == (200 if exists else 404)
    assert await cache.get(key) == successor.model_dump_json()
    assert await cache.get(lock_key) == "successor-test-request"


async def test_canceled_holder_releases_lock_without_filling(cache_client):
    client, app, detail, _ = cache_client
    entered = asyncio.Event()

    async def query(*_args):
        entered.set()
        await asyncio.Event().wait()

    detail.side_effect = query
    task = asyncio.create_task(client.get(PATH))
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await app.state.cache_redis.exists(lock_for(app), key_for(app, PATH)) == 0


@pytest.mark.parametrize("exists", [True, False])
async def test_cache_hits_do_not_acquire_or_release_locks(cache_client, monkeypatch, exists):
    client, app, detail, _ = cache_client
    cache = app.state.cache_redis
    await cache.set(key_for(app, PATH), SHOP.model_dump_json() if exists else "", ex=60)
    forbidden = AsyncMock(side_effect=AssertionError("缓存命中不能写锁或执行 Lua"))
    monkeypatch.setattr(cache, "set", forbidden)
    monkeypatch.setattr(cache, "eval", forbidden)
    assert (await client.get(PATH)).status_code == (200 if exists else 404)
    assert detail.await_count == 0
    assert forbidden.await_count == 0


@pytest.mark.parametrize("exists", [True, False])
async def test_guarded_fill_failure_keeps_database_response_and_releases_lock(
    cache_client, monkeypatch, exists
):
    client, app, detail, _ = cache_client
    cache = app.state.cache_redis
    detail.return_value = SHOP if exists else None
    original_eval = cache.eval

    async def fail_only_fill(script, numkeys, *args):
        if numkeys == 2:
            raise RedisConnectionError("simulated guarded fill failure")
        return await original_eval(script, numkeys, *args)

    monkeypatch.setattr(cache, "eval", fail_only_fill)
    assert (await client.get(PATH)).status_code == (200 if exists else 404)
    assert detail.await_count == 1
    assert await cache.exists(key_for(app, PATH), lock_for(app)) == 0


async def test_release_failure_does_not_mask_database_error(cache_client, monkeypatch):
    client, app, detail, _ = cache_client
    detail.side_effect = OperationalError("SELECT", {}, RuntimeError("offline"))
    monkeypatch.setattr(
        app.state.cache_redis, "eval", AsyncMock(side_effect=RedisConnectionError("offline"))
    )
    response = await client.get(PATH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert await app.state.cache_redis.exists(key_for(app, PATH)) == 0
    assert 0 < await app.state.cache_redis.pttl(lock_for(app)) <= 15000
