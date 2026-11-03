"""真实 Redis + 合成数据库返回值：验证数据类型、回源、TTL 和并发回填边界。"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from backend.app.core.database import get_session
from backend.app.main import create_app
from backend.app.schemas.shops import ShopOut, ShopTypeOut
from backend.app.services import shops as service
from backend.app.services.shops import invalidate_shop_cache, repository

pytestmark = pytest.mark.redis

SHOP = ShopOut(
    id=1,
    type_id=1,
    name="缓存测试店",
    subtitle="测试",
    kind="茶馆",
    city="天津",
    area="海河沿线",
    address="测试地址",
    images=["public/assets/photos/02.jpg"],
    tags=["测试"],
    score=4.8,
    avg_price=8800,
    open_hours="10:00–20:00",
    description="合成数据",
)
TYPES = [
    ShopTypeOut(id=10, name="美甲", icon="nail.png", sort=1),
    ShopTypeOut(id=1, name="美食", icon="food.png", sort=2),
]
PATHS = ["/api/shops/1", "/api/shop-types"]


async def cleanup_cache(cache, settings):
    keys = set()
    for prefix in {settings.cache_key_prefix, settings.cache_lock_key_prefix}:
        assert prefix.startswith("perkflow:test:") and not any(c in prefix for c in "*?[]")
        keys.update([key async for key in cache.scan_iter(match=f"{prefix}*")])
    if keys:
        await cache.delete(*keys)


@pytest_asyncio.fixture
async def cache_client(redis_settings, monkeypatch):
    app = create_app(redis_settings)
    # 请求仍走实际 Router 和 Service；仅替换 SQL 查询，绝不连接用户开发库。
    app.dependency_overrides[get_session] = lambda: None
    detail = AsyncMock(return_value=SHOP)
    categories = AsyncMock(return_value=TYPES)
    monkeypatch.setattr(repository, "get_shop", detail)
    monkeypatch.setattr(repository, "list_shop_types", categories)
    async with app.router.lifespan_context(app):
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://perkflow.test"
            ) as client:
                yield client, app, detail, categories
        finally:
            await cleanup_cache(app.state.cache_redis, redis_settings)


def key_for(app, path):
    suffix = "shop:1" if path == "/api/shops/1" else "shop-types:all"
    return app.state.settings.cache_key_prefix + suffix


async def test_detail_string_round_trip_and_hit_skips_sql(cache_client):
    client, app, detail, _ = cache_client
    cache = app.state.cache_redis
    key = key_for(app, PATHS[0])
    first = await client.get(PATHS[0])
    assert first.status_code == 200 and first.json() == SHOP.model_dump()
    assert await cache.type(key) == "string"
    assert ShopOut.model_validate_json(await cache.get(key)) == SHOP
    settings = app.state.settings
    assert 0 < await cache.ttl(key) <= settings.shop_cache_ttl + settings.shop_cache_ttl_jitter
    detail.side_effect = AssertionError("缓存命中不应查询 SQL")
    second = await client.get(PATHS[0])
    assert second.json() == first.json()
    assert detail.await_count == 1


async def test_categories_list_order_round_trip_and_hit_skips_sql(cache_client):
    client, app, _, categories = cache_client
    cache = app.state.cache_redis
    key = key_for(app, PATHS[1])
    first = await client.get(PATHS[1])
    assert first.status_code == 200
    assert first.json() == {"items": [item.model_dump() for item in TYPES]}
    assert await cache.type(key) == "list"
    assert [ShopTypeOut.model_validate_json(v) for v in await cache.lrange(key, 0, -1)] == TYPES
    settings = app.state.settings
    assert (
        0
        < await cache.ttl(key)
        <= settings.shop_type_cache_ttl + settings.shop_type_cache_ttl_jitter
    )
    categories.side_effect = AssertionError("缓存命中不应查询 SQL")
    assert (await client.get(PATHS[1])).json() == first.json()
    assert categories.await_count == 1


@pytest.mark.parametrize("path", PATHS)
async def test_refill_ttl_boundaries_resampled_but_hits_do_not_draw(
    cache_client, monkeypatch, path
):
    client, app, detail, categories = cache_client
    settings = app.state.settings
    is_shop = path == PATHS[0]
    ttl_field = "shop_cache_ttl" if is_shop else "shop_type_cache_ttl"
    jitter_field = ttl_field + "_jitter"
    # 为两种缓存设置不同参数，验证回填使用各自配置，而不是共用错误的 TTL。
    setattr(settings, ttl_field, 60)
    setattr(settings, jitter_field, 17 if is_shop else 29)
    offsets = [0, getattr(settings, jitter_field)]
    calls = []

    def draw(lower, upper):
        calls.append((lower, upper))
        return offsets.pop(0)

    monkeypatch.setattr(service, "randint", draw)
    cache = app.state.cache_redis
    key = key_for(app, path)
    query = detail if is_shop else categories
    first = await client.get(path)
    assert first.status_code == 200
    assert 59_000 <= await cache.pttl(key) <= 60_000
    await cache.pexpire(key, 9000)
    before = await cache.pttl(key)
    assert (await client.get(path)).json() == first.json()
    assert 0 < await cache.pttl(key) <= before
    assert len(calls) == query.await_count == 1

    await cache.pexpire(key, 1)
    await asyncio.sleep(0.02)
    assert await cache.exists(key) == 0
    assert (await client.get(path)).json() == first.json()
    expected_ms = (60 + getattr(settings, jitter_field)) * 1000
    assert expected_ms - 1000 <= await cache.pttl(key) <= expected_ms
    assert calls == [(0, getattr(settings, jitter_field))] * 2
    assert query.await_count == 2 and offsets == []


@pytest.mark.parametrize("path", PATHS)
async def test_zero_jitter_preserves_configured_base_ttl(cache_client, path):
    client, app, _, _ = cache_client
    field = "shop_cache_ttl" if path == PATHS[0] else "shop_type_cache_ttl"
    setattr(app.state.settings, field, 60)
    setattr(app.state.settings, field + "_jitter", 0)
    assert (await client.get(path)).status_code == 200
    assert 59_000 <= await app.state.cache_redis.pttl(key_for(app, path)) <= 60_000


async def test_batch_shop_refills_have_different_expiration_deadlines(cache_client, monkeypatch):
    client, app, detail, _ = cache_client
    app.state.settings.shop_cache_ttl = 60
    app.state.settings.shop_cache_ttl_jitter = 30
    offsets = iter([0, 10, 20, 30])
    monkeypatch.setattr(service, "randint", lambda _lower, _upper: next(offsets))

    async def query(_session, shop_id):
        return SHOP.model_copy(update={"id": shop_id})

    detail.side_effect = query
    responses = await asyncio.gather(*(client.get(f"/api/shops/{i}") for i in range(1, 5)))
    assert all(r.status_code == 200 for r in responses)
    ttls = sorted(
        [
            await app.state.cache_redis.pttl(app.state.settings.cache_key_prefix + f"shop:{i}")
            for i in range(1, 5)
        ]
    )
    assert 59_000 <= ttls[0] <= 60_000 and 89_000 <= ttls[-1] <= 90_000
    assert all(b - a >= 9000 for a, b in zip(ttls, ttls[1:]))
    assert detail.await_count == 4  # 演示分散到期，不宣称并发请求已经被合并。


async def test_negative_and_empty_categories_do_not_use_positive_jitter(cache_client, monkeypatch):
    client, app, detail, categories = cache_client

    def forbidden(*_args):
        raise AssertionError("空值和空分类不能使用正常缓存的随机期限")

    monkeypatch.setattr(service, "randint", forbidden)
    detail.return_value = None
    categories.return_value = []
    assert (await client.get(PATHS[0])).status_code == 404
    assert 119_000 <= await app.state.cache_redis.pttl(key_for(app, PATHS[0])) <= 120_000
    assert (await client.get(PATHS[1])).json() == {"items": []}
    assert await app.state.cache_redis.exists(key_for(app, PATHS[1])) == 0


@pytest.mark.parametrize("path", PATHS)
async def test_hit_does_not_renew_ttl_and_expiration_reloads(cache_client, path):
    client, app, detail, categories = cache_client
    cache = app.state.cache_redis
    query = detail if path == PATHS[0] else categories
    key = key_for(app, path)
    first = await client.get(path)
    await cache.pexpire(key, 9000)
    before = await cache.pttl(key)
    assert (await client.get(path)).json() == first.json()
    assert 0 < await cache.pttl(key) <= before
    assert query.await_count == 1
    await cache.pexpire(key, 1)
    await asyncio.sleep(0.02)
    assert await cache.exists(key) == 0
    assert (await client.get(path)).json() == first.json()
    assert query.await_count == 2
    assert await cache.ttl(key) > 1000


async def test_absent_shop_returns_404_and_negative_hit_skips_sql(cache_client):
    client, app, detail, _ = cache_client
    detail.return_value = None
    for _ in range(2):
        response = await client.get("/api/shops/999999")
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "商户不存在"
    assert detail.await_count == 1
    cache = app.state.cache_redis
    key = app.state.settings.cache_key_prefix + "shop:999999"
    assert await cache.type(key) == "string"
    assert await cache.get(key) == ""  # 和不存在 Key 返回 None 不同。
    assert 0 < await cache.ttl(key) <= app.state.settings.shop_null_cache_ttl


async def test_negative_hit_does_not_renew_and_expiration_rechecks_database(cache_client):
    client, app, detail, _ = cache_client
    detail.return_value = None
    path = PATHS[0]
    cache = app.state.cache_redis
    key = key_for(app, path)
    assert (await client.get(path)).status_code == 404
    await cache.pexpire(key, 9000)
    before = await cache.pttl(key)
    assert (await client.get(path)).status_code == 404
    assert 0 < await cache.pttl(key) <= before
    assert detail.await_count == 1
    await cache.pexpire(key, 1)
    await asyncio.sleep(0.02)
    assert await cache.get(key) is None
    detail.return_value = SHOP
    assert (await client.get(path)).json() == SHOP.model_dump()
    assert detail.await_count == 2
    assert ShopOut.model_validate_json(await cache.get(key)) == SHOP
    assert await cache.ttl(key) > 1000


async def test_manual_invalidation_removes_negative_marker(cache_client):
    client, app, detail, _ = cache_client
    cache = app.state.cache_redis
    key = key_for(app, PATHS[0])
    detail.return_value = None
    assert (await client.get(PATHS[0])).status_code == 404
    detail.return_value = SHOP
    assert (await client.get(PATHS[0])).status_code == 404  # 不会自动感知数据库新增。
    assert await invalidate_shop_cache(cache, app.state.settings, 1)
    assert await cache.get(key) is None
    assert (await client.get(PATHS[0])).json() == SHOP.model_dump()
    assert detail.await_count == 2


@pytest.mark.parametrize("failure", ["read", "write"])
async def test_negative_cache_failure_keeps_404_and_requeries(cache_client, monkeypatch, failure):
    client, app, detail, _ = cache_client
    detail.return_value = None
    cache = app.state.cache_redis
    method = "get" if failure == "read" else "set"
    with monkeypatch.context() as patch:
        patch.setattr(cache, method, AsyncMock(side_effect=RedisConnectionError("offline")))
        for _ in range(2):
            assert (await client.get(PATHS[0])).status_code == 404
    assert detail.await_count == 2
    assert await cache.get(key_for(app, PATHS[0])) == ("" if failure == "read" else None)


async def test_database_timeout_is_not_negative_cached(cache_client):
    client, app, detail, _ = cache_client
    detail.side_effect = TimeoutError()
    response = await client.get(PATHS[0])
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_TIMEOUT"
    assert await app.state.cache_redis.get(key_for(app, PATHS[0])) is None
    assert (
        await app.state.cache_redis.exists(app.state.settings.cache_lock_key_prefix + "shop:1") == 0
    )


async def test_random_absent_ids_each_require_first_database_query(cache_client):
    client, app, detail, _ = cache_client
    detail.return_value = None
    for shop_id in [999991, 999992, 999993]:
        for _ in range(2):
            assert (await client.get(f"/api/shops/{shop_id}")).status_code == 404
    assert detail.await_count == 3  # 空值缓存保护重复 ID，没有拦住随机 ID 首次访问。


async def test_invalid_shop_ids_do_not_query_or_fill_cache(cache_client):
    client, app, detail, _ = cache_client
    for shop_id in ["0", "-1", "not-an-id"]:
        assert (await client.get(f"/api/shops/{shop_id}")).status_code == 422
    assert detail.await_count == 0
    assert [
        k
        async for k in app.state.cache_redis.scan_iter(
            match=app.state.settings.cache_key_prefix + "*"
        )
    ] == []


async def test_empty_categories_are_not_cached(cache_client):
    client, app, _, categories = cache_client
    categories.return_value = []
    for _ in range(2):
        response = await client.get(PATHS[1])
        assert response.status_code == 200 and response.json() == {"items": []}
    assert categories.await_count == 2
    assert await app.state.cache_redis.exists(key_for(app, PATHS[1])) == 0


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("damage", ["json", "wrong_type"])
async def test_damaged_cache_falls_back_and_is_replaced(cache_client, path, damage):
    client, app, detail, categories = cache_client
    cache = app.state.cache_redis
    key = key_for(app, path)
    if (path == PATHS[0]) == (damage == "json"):
        await cache.set(key, "{broken-json")
    else:
        await cache.rpush(key, "{broken-json")
    response = await client.get(path)
    assert response.status_code == 200
    assert await cache.type(key) == ("string" if path == PATHS[0] else "list")
    assert await cache.ttl(key) > 1000
    assert (await client.get(path)).json() == response.json()
    assert (detail if path == PATHS[0] else categories).await_count == 1


async def test_detail_id_mismatch_cannot_return_other_shop(cache_client):
    client, app, detail, _ = cache_client
    await app.state.cache_redis.set(
        key_for(app, PATHS[0]), SHOP.model_copy(update={"id": 2}).model_dump_json()
    )
    assert (await client.get(PATHS[0])).json()["id"] == 1
    assert detail.await_count == 1


@pytest.mark.parametrize("path", PATHS)
async def test_cache_read_failure_returns_database_result(cache_client, monkeypatch, path):
    client, app, detail, categories = cache_client
    method = "get" if path == PATHS[0] else "lrange"
    monkeypatch.setattr(
        app.state.cache_redis, method, AsyncMock(side_effect=RedisConnectionError("offline"))
    )
    response = await client.get(path)
    assert response.status_code == 200
    assert (detail if path == PATHS[0] else categories).await_count == 1


@pytest.mark.parametrize("path", PATHS)
async def test_cache_write_failure_keeps_fresh_response(cache_client, monkeypatch, path):
    client, app, _, _ = cache_client
    cache = app.state.cache_redis
    if path == PATHS[0]:
        monkeypatch.setattr(cache, "set", AsyncMock(side_effect=RedisConnectionError("offline")))
    else:
        monkeypatch.setattr(
            type(cache.pipeline()),
            "execute",
            AsyncMock(side_effect=RedisConnectionError("offline")),
        )
    response = await client.get(path)
    assert response.status_code == 200
    assert response.json() == (
        SHOP.model_dump() if path == PATHS[0] else {"items": [t.model_dump() for t in TYPES]}
    )
    assert await cache.exists(key_for(app, path)) == 0


@pytest.mark.parametrize("path", PATHS)
async def test_database_failure_is_503_and_not_cached(cache_client, path):
    client, app, detail, categories = cache_client
    query = detail if path == PATHS[0] else categories
    query.side_effect = OperationalError("SELECT private", {}, RuntimeError("offline"))
    response = await client.get(path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "SELECT" not in response.text
    assert await app.state.cache_redis.exists(key_for(app, path)) == 0
    if path == PATHS[0]:
        assert (
            await app.state.cache_redis.exists(app.state.settings.cache_lock_key_prefix + "shop:1")
            == 0
        )


async def test_concurrent_list_refills_replace_without_duplicates(cache_client):
    client, app, _, categories = cache_client
    count = 8
    arrived = 0
    release = asyncio.Event()

    async def synchronized_query(_):
        nonlocal arrived
        arrived += 1
        if arrived == count:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=3)
        return TYPES

    categories.side_effect = synchronized_query
    cache = app.state.cache_redis
    key = key_for(app, PATHS[1])
    expected = [t.model_dump_json() for t in TYPES]

    async def observe():
        # 回填期间一次 LRANGE 只能看到空列表或完整列表，不能看到追加重复的数据。
        for _ in range(30):
            assert await cache.lrange(key, 0, -1) in ([], expected)
            await asyncio.sleep(0)

    results = await asyncio.gather(*(client.get(PATHS[1]) for _ in range(count)), observe())
    assert all(r.status_code == 200 for r in results[:-1])
    assert categories.await_count == count  # 基础缓存没有合并并发回源。
    assert await cache.lrange(key, 0, -1) == expected
    assert await cache.ttl(key) > 1000


async def test_cache_fallback_does_not_bypass_session_failure(cache_client, monkeypatch):
    client, app, _, _ = cache_client
    client.cookies.set(app.state.settings.session_cookie_name, "x" * 43)
    monkeypatch.setattr(
        app.state.session_store, "load", AsyncMock(side_effect=RedisConnectionError("offline"))
    )
    response = await client.get(PATHS[0])
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SESSION_UNAVAILABLE"
