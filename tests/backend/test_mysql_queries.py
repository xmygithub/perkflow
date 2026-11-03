import asyncio
import json
import os
import sys

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, event, text

from backend.app.main import create_app
from backend.app.models import Shop
from backend.app.services import shops as service
from backend.app.services.shops import invalidate_shop_cache
from tests.backend.test_shop_cache import SHOP, cleanup_cache

pytestmark = pytest.mark.mysql


@pytest_asyncio.fixture
async def mysql_client(mysql_settings):
    app = create_app(mysql_settings)
    async with app.router.lifespan_context(app):
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield client, app
        finally:
            await cleanup_cache(app.state.cache_redis, mysql_settings)


async def test_categories_order_and_real_readiness(mysql_client):
    client, app = mysql_client
    assert (await client.get("/health/ready")).status_code == 200
    response = await client.get("/api/shop-types")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [1, 2, 3, 10, 5, 6, 7, 8, 9, 4]
    async with app.state.database.sessions() as session:
        assert await session.scalar(text("SELECT @@session.time_zone")) == "+00:00"


async def test_shop_detail_money_json_and_not_found(mysql_client):
    client, _ = mysql_client
    response = await client.get("/api/shops/1")
    assert response.status_code == 200
    shop = response.json()
    assert shop["name"] == "津巷泰食"
    assert shop["city"] == "天津"
    assert shop["score"] == 4.8
    assert shop["avg_price"] == 8800
    assert shop["images"][0] == "public/assets/photos/02.jpg"
    assert shop["tags"] == ["朋友聚餐", "东南亚风味"]
    assert (await client.get("/api/shops/999999")).status_code == 404


async def test_shop_pagination_filters_and_stable_sort(mysql_client):
    client, _ = mysql_client
    first = (await client.get("/api/shops?page_size=2")).json()
    second = (await client.get("/api/shops?page_size=2&page=2")).json()
    assert first["total"] == second["total"] == 6
    assert [s["id"] for s in first["items"]] == [1, 2]
    assert [s["id"] for s in second["items"]] == [3, 4]
    score = (await client.get("/api/shops?sort=score")).json()
    assert [s["id"] for s in score["items"]] == [2, 1, 5, 3, 6, 4]
    price = (await client.get("/api/shops?sort=price")).json()
    assert [s["id"] for s in price["items"]] == [4, 2, 1, 5, 6, 3]
    filtered = (
        await client.get(
            "/api/shops", params={"area": "南开街区", "city": "天津", "category_id": 1}
        )
    ).json()
    assert filtered["total"] == 2
    assert [s["id"] for s in filtered["items"]] == [1, 4]
    assert (await client.get("/api/shops?category_id=2")).json()["total"] == 0
    assert (await client.get("/api/shops?page=100")).json()["items"] == []
    tea = (await client.get("/api/shops", params={"kind": "茶馆", "page_size": 1})).json()
    assert tea["total"] == 2
    assert [s["id"] for s in tea["items"]] == [2]
    assert (await client.get("/api/shops?kind=")).status_code == 422


async def test_search_handles_chinese_and_treats_wildcards_as_literals(mysql_client):
    client, _ = mysql_client
    response = await client.get("/api/shops", params={"q": "海河"})
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == 2
    for keyword in ["%", "_", "' OR 1=1 --"]:
        response = await client.get("/api/shops", params={"q": keyword})
        assert response.status_code == 200
        assert response.json()["total"] == 0


async def test_real_mysql_cache_hit_skips_business_select(mysql_client):
    client, app = mysql_client
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = app.state.database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        for path, suffix, data_type in [
            ("/api/shops/1", "shop:1", "string"),
            ("/api/shop-types", "shop-types:all", "list"),
        ]:
            statements.clear()
            first = await client.get(path)
            assert first.status_code == 200
            assert len(statements) == 1
            key = app.state.settings.cache_key_prefix + suffix
            assert await app.state.cache_redis.type(key) == data_type
            statements.clear()
            second = await client.get(path)
            assert second.status_code == 200 and second.json() == first.json()
            assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_real_negative_cache_sql_expiration_and_new_shop_invalidation(mysql_client):
    client, app = mysql_client
    # 本夹具只允许独立测试库。模拟新增用于验证旧空值标记的可见性，不新增业务接口。
    shop_id = 999998
    path = f"/api/shops/{shop_id}"
    cache = app.state.cache_redis
    key = app.state.settings.cache_key_prefix + f"shop:{shop_id}"
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = app.state.database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert (await client.get(path)).status_code == 404
        assert len(statements) == 1
        assert await cache.get(key) == ""
        assert 0 < await cache.ttl(key) <= app.state.settings.shop_null_cache_ttl
        statements.clear()
        assert (await client.get(path)).status_code == 404
        assert statements == []

        await cache.pexpire(key, 1)
        await asyncio.sleep(0.02)
        assert await cache.get(key) is None
        assert (await client.get(path)).status_code == 404
        assert len(statements) == 1

        async with app.state.database.sessions() as session, session.begin():
            session.add(Shop(**SHOP.model_copy(update={"id": shop_id}).model_dump()))
        statements.clear()
        assert (await client.get(path)).status_code == 404
        assert statements == []  # 直接写数据库不会通知缓存。
        assert await invalidate_shop_cache(cache, app.state.settings, shop_id)
        response = await client.get(path)
        assert response.status_code == 200 and response.json()["id"] == shop_id
        assert len(statements) == 1
        assert await cache.ttl(key) > 1000
    finally:
        event.remove(engine, "before_cursor_execute", record)
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(delete(Shop).where(Shop.id == shop_id))


async def test_independent_python_process_waits_for_rebuild_without_sql(mysql_client, monkeypatch):
    _, app = mysql_client
    settings, cache = app.state.settings, app.state.cache_redis
    assert settings.mysql_database.startswith("perkflow_test_")
    entered, release = asyncio.Event(), asyncio.Event()
    original_get = service.repository.get_shop
    queries = 0

    async def slow_query(session, shop_id):
        nonlocal queries
        queries += 1
        row = await original_get(session, shop_id)
        entered.set()
        await asyncio.wait_for(release.wait(), 10)
        return row

    monkeypatch.setattr(service.repository, "get_shop", slow_query)

    async def first_reader():
        async with app.state.database.sessions() as session:
            return await service.get_shop(session, 1, cache, settings)

    # 第二个真正独立的 Python 进程，通过同一测试前缀与父进程协调，而不是共用进程内锁。
    child_code = """
import asyncio, json, os, sys
from sqlalchemy import event
from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.services.shops import get_shop
async def run():
    settings = Settings(_env_file=None)
    assert settings.mysql_database.startswith("perkflow_test_")
    db, cache = Database(settings), create_redis_client(settings)
    count = 0
    def record(_conn, _cursor, statement, *_args):
        nonlocal count
        if statement.lstrip().upper().startswith("SELECT"):
            count += 1
    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    original_set = cache.set
    async def signal_attempt(*args, **kwargs):
        result = await original_set(*args, **kwargs)
        if kwargs.get("nx"):
            await original_set(os.environ["MUTEX_TEST_READY_KEY"], "ready", ex=30)
        return result
    cache.set = signal_attempt
    try:
        async with db.sessions() as session:
            shop = await get_shop(session, 1, cache, settings)
        print(json.dumps({"id": shop.id, "selects": count}))
    finally:
        await cache.aclose()
        await db.close()
try:
    asyncio.run(run())
except Exception as exc:
    print(type(exc).__name__)
    sys.exit(1)
"""
    ready_key = settings.cache_key_prefix + "mutex-process-ready"
    env = os.environ.copy()
    env.update(
        MYSQL_DATABASE=settings.mysql_database,
        MYSQL_HOST=settings.mysql_host,
        MYSQL_PORT=str(settings.mysql_port),
        MYSQL_USER=settings.mysql_user,
        MYSQL_PASSWORD=settings.mysql_password.get_secret_value(),
        REDIS_HOST=settings.redis_host,
        REDIS_PORT=str(settings.redis_port),
        REDIS_DB=str(settings.redis_db),
        REDIS_PASSWORD=settings.redis_password.get_secret_value(),
        CACHE_KEY_PREFIX=settings.cache_key_prefix,
        CACHE_LOCK_KEY_PREFIX=settings.cache_lock_key_prefix,
        SHOP_CACHE_LOCK_WAIT_MS="2000",
        MUTEX_TEST_READY_KEY=ready_key,
        PYTHONUTF8="1",
    )
    task = asyncio.create_task(first_reader())
    process = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            child_code,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(8):
            while await cache.get(ready_key) != "ready":
                await asyncio.sleep(0.01)
        release.set()
        snapshot = await asyncio.wait_for(task, 3)
        stdout, _stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0  # 不将子进程完整日志/配置回显到测试输出。
        assert json.loads(stdout) == {"id": snapshot.id, "selects": 0}
        assert queries == 1
        assert await cache.exists(settings.cache_lock_key_prefix + "shop:1") == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await cache.delete(ready_key)
