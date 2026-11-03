"""参数校验与隔离 MySQL/Redis 商户维护验证，不修改用户开发库。"""

import argparse
import asyncio
import json
import secrets
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, event
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.errors import ApiError
from backend.app.main import create_app
from backend.app.models import Shop
from backend.app.schemas.shops import ShopOut, ShopUpdate
from backend.app.services import shops as service
from scripts import update_shop as cli
from tests.backend.test_shop_cache import cleanup_cache


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"name": "  "},
        {"name": "店" * 129},
        {"open_hours": None},
        {"avg_price": -1},
        {"avg_price": 4294967296},
        {"avg_price": True},
        {"avg_price": 88.5},
        {"type_id": 2},
    ],
)
def test_update_rejects_invalid_fields_and_overposting(values):
    with pytest.raises(ValidationError):
        ShopUpdate.model_validate(values)


def test_update_preserves_unset_fields_and_accepts_zero_cents():
    assert ShopUpdate(name="  新店名  ").model_dump(exclude_unset=True) == {"name": "新店名"}
    assert ShopUpdate(avg_price=0).model_dump(exclude_unset=True) == {"avg_price": 0}
    assert cli.yuan_to_cents("88.01") == 8801
    assert cli.yuan_to_cents("0") == 0


@pytest.mark.parametrize("value", ["-1", "1.001", "NaN", "1e2"])
def test_money_is_not_silently_rounded_or_interpreted_as_float(value):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.yuan_to_cents(value)


@pytest_asyncio.fixture
async def maintenance_context(mysql_settings):
    app = create_app(mysql_settings)
    shop_id = 9_000_000 + secrets.randbelow(1_000_000)
    async with app.router.lifespan_context(app):
        database = app.state.database
        async with database.sessions() as session, session.begin():
            original = ShopOut.model_validate(await session.get(Shop, 1)).model_dump()
            original.update(id=shop_id, name=f"维护测试店{shop_id}")
            session.add(Shop(**original))
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield app, client, shop_id, original
        finally:
            # 测试行没有券/订单关系，只删除独立测试库中的本次行和随机缓存前缀。
            async with database.sessions() as session, session.begin():
                await session.execute(delete(Shop).where(Shop.id == shop_id))
            await cleanup_cache(app.state.cache_redis, mysql_settings)


class TestMaintenanceIntegration:
    pytestmark = [pytest.mark.mysql, pytest.mark.redis]

    async def test_commit_precedes_delete_and_next_query_refills(
        self, maintenance_context, monkeypatch
    ):
        app, client, shop_id, _ = maintenance_context
        database, cache, settings = app.state.database, app.state.cache_redis, app.state.settings
        key = f"{settings.cache_key_prefix}shop:{shop_id}"
        assert (await client.get(f"/api/shops/{shop_id}")).status_code == 200
        await client.get("/api/shop-types")
        await client.get("/api/shops/2")
        types_key = f"{settings.cache_key_prefix}shop-types:all"
        other_key = f"{settings.cache_key_prefix}shop:2"
        before_types = await cache.lrange(types_key, 0, -1)
        before_other = await cache.get(other_key)
        original_delete = cache.delete
        observed = []

        async def check_committed(*keys):
            # DEL 发生时用新连接读到新值，证明不是 UPDATE 完就提前删、稍后再提交。
            async with database.sessions() as session:
                row = await session.get(Shop, shop_id)
                assert row.avg_price == 9900 and row.open_hours == "10:00–22:00"
            observed.append(keys)
            return await original_delete(*keys)

        with monkeypatch.context() as patch:
            patch.setattr(cache, "delete", check_committed)
            async with database.sessions() as session:
                result = await service.update_shop(
                    session,
                    shop_id,
                    ShopUpdate(avg_price=9900, open_hours="10:00–22:00"),
                    cache,
                    settings,
                )
        assert result.database_committed and result.cache_invalidated
        assert set(result.changed_fields) == {"avg_price", "open_hours"}
        assert observed == [(key,)] and await cache.exists(key) == 0
        assert await cache.get(other_key) == before_other
        assert await cache.lrange(types_key, 0, -1) == before_types
        refreshed = await client.get(f"/api/shops/{shop_id}")
        assert refreshed.json()["avg_price"] == 9900
        assert ShopOut.model_validate_json(await cache.get(key)).avg_price == 9900
        assert await cache.ttl(key) > 0

    @pytest.mark.parametrize("failure", ["flush", "commit"])
    async def test_rollback_keeps_old_database_and_cache(
        self, maintenance_context, monkeypatch, failure
    ):
        app, client, shop_id, original = maintenance_context
        database, cache, settings = app.state.database, app.state.cache_redis, app.state.settings
        await client.get(f"/api/shops/{shop_id}")
        key = f"{settings.cache_key_prefix}shop:{shop_id}"
        before_cache = await cache.get(key)
        original_update = service.repository.update_shop

        async def fail_after_flush(*args):
            await original_update(*args)
            raise SQLAlchemyError("synthetic failure after UPDATE")

        def fail_commit(_session):
            raise SQLAlchemyError("synthetic failure before COMMIT")

        with monkeypatch.context() as patch:
            spy = AsyncMock(wraps=cache.delete)
            patch.setattr(cache, "delete", spy)
            if failure == "flush":
                patch.setattr(service.repository, "update_shop", fail_after_flush)
            async with database.sessions() as session:
                if failure == "commit":
                    event.listen(session.sync_session, "before_commit", fail_commit)
                try:
                    with pytest.raises(SQLAlchemyError):
                        await service.update_shop(
                            session, shop_id, ShopUpdate(name="不应提交"), cache, settings
                        )
                finally:
                    if failure == "commit":
                        event.remove(session.sync_session, "before_commit", fail_commit)
            assert spy.await_count == 0
        async with database.sessions() as session:
            assert (await session.get(Shop, shop_id)).name == original["name"]
        assert await cache.get(key) == before_cache

    async def test_failed_invalidation_is_partial_success_and_repair_does_not_rewrite_db(
        self, maintenance_context, monkeypatch, capsys
    ):
        app, client, shop_id, _ = maintenance_context
        cache, settings = app.state.cache_redis, app.state.settings
        await client.get(f"/api/shops/{shop_id}")
        key = f"{settings.cache_key_prefix}shop:{shop_id}"
        old = await cache.get(key)
        create_client = cli.create_redis_client

        def failing_client(settings):
            client = create_client(settings)
            client.delete = AsyncMock(side_effect=RedisConnectionError("synthetic"))
            return client

        with monkeypatch.context() as patch:
            patch.setattr(cli, "create_redis_client", failing_client)
            assert (
                await cli.maintain(settings, shop_id, ShopUpdate(avg_price=10001), apply=True) == 2
            )
        output = capsys.readouterr().out
        assert '"database_committed": true' in output
        assert '"cache_invalidated": false' in output
        assert "--invalidate-only" in output
        async with app.state.database.sessions() as session:
            assert (await session.get(Shop, shop_id)).avg_price == 10001
        assert await cache.get(key) == old

        def no_database(_settings):
            raise AssertionError("仅修复缓存不能连接或重写数据库")

        with monkeypatch.context() as patch:
            patch.setattr(cli, "Database", no_database)
            assert await cli.maintain(settings, shop_id, None, invalidate_only=True) == 0
        assert "缓存删除已确认" in capsys.readouterr().out
        assert await cache.exists(key) == 0
        assert await cli.maintain(settings, shop_id, None, invalidate_only=True) == 0

    async def test_preview_is_read_only_and_apply_changes_real_data(
        self, maintenance_context, monkeypatch, capsys
    ):
        app, client, shop_id, original = maintenance_context
        cache, settings = app.state.cache_redis, app.state.settings
        await client.get(f"/api/shops/{shop_id}")
        key = f"{settings.cache_key_prefix}shop:{shop_id}"
        old_cache = await cache.get(key)
        changes = ShopUpdate(name="脚本维护后的店名")

        def no_cache(_settings):
            raise AssertionError("预览不需要 Redis 连接")

        with monkeypatch.context() as patch:
            patch.setattr(cli, "create_redis_client", no_cache)
            assert await cli.maintain(settings, shop_id, changes) == 0
        assert '"mode": "preview"' in capsys.readouterr().out
        assert await cache.get(key) == old_cache
        async with app.state.database.sessions() as session:
            assert (await session.get(Shop, shop_id)).name == original["name"]
        assert await cli.maintain(settings, shop_id, changes, apply=True) == 0
        assert '"database_committed": true' in capsys.readouterr().out
        assert (await client.get(f"/api/shops/{shop_id}")).json()["name"] == changes.name

    async def test_noop_still_removes_cache_and_missing_id_does_not(self, maintenance_context):
        app, client, shop_id, original = maintenance_context
        cache, settings = app.state.cache_redis, app.state.settings
        key = f"{settings.cache_key_prefix}shop:{shop_id}"
        await client.get(f"/api/shops/{shop_id}")
        async with app.state.database.sessions() as session:
            result = await service.update_shop(
                session, shop_id, ShopUpdate(avg_price=original["avg_price"]), cache, settings
            )
        assert result.changed_fields == [] and result.cache_invalidated
        assert await cache.exists(key) == 0
        missing_id = 999_999_999
        missing_key = f"{settings.cache_key_prefix}shop:{missing_id}"
        await cache.set(missing_key, "orphan", ex=60)
        async with app.state.database.sessions() as session:
            with pytest.raises(ApiError, match="商户不存在"):
                await service.update_shop(
                    session, missing_id, ShopUpdate(avg_price=1), cache, settings
                )
        assert await cache.get(missing_key) == "orphan"

    async def test_consumer_http_has_no_maintenance_endpoint(self, maintenance_context):
        _, client, shop_id, _ = maintenance_context
        for method in ("PATCH", "PUT", "POST"):
            assert (
                await client.request(method, f"/api/shops/{shop_id}", json={"name": "无权修改"})
            ).status_code == 405

    async def test_old_inflight_read_can_refill_after_invalidation(
        self, maintenance_context, monkeypatch
    ):
        # 复现课程右图的已知窗口，不把提交后删除夸大成强一致或读写互斥。
        app, _, shop_id, original = maintenance_context
        database, cache, settings = app.state.database, app.state.cache_redis, app.state.settings
        captured, release = asyncio.Event(), asyncio.Event()
        original_get = service.repository.get_shop

        async def delayed_read(session, requested_id):
            row = await original_get(session, requested_id)
            captured.set()
            await asyncio.wait_for(release.wait(), 3)
            return row

        async def reader():
            async with database.sessions() as session:
                return await service.get_shop(session, shop_id, cache, settings)

        with monkeypatch.context() as patch:
            patch.setattr(service.repository, "get_shop", delayed_read)
            task = asyncio.create_task(reader())
            try:
                await asyncio.wait_for(captured.wait(), 3)
                async with database.sessions() as session:
                    result = await service.update_shop(
                        session, shop_id, ShopUpdate(avg_price=9901), cache, settings
                    )
                assert result.cache_invalidated
            finally:
                release.set()
                snapshot = await asyncio.wait_for(task, 3)
        assert snapshot.avg_price == original["avg_price"]
        async with database.sessions() as session:
            assert (await session.get(Shop, shop_id)).avg_price == 9901
        cached = json.loads(await cache.get(f"{settings.cache_key_prefix}shop:{shop_id}"))
        assert cached["avg_price"] == original["avg_price"]
