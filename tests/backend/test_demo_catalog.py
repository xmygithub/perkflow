"""增量导入必须保留已有事实、支持安全重跑，并处理数据库提交后的缓存失败。"""

from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, event, select, update

from backend.app.main import create_app
from backend.app.models import Base, SeckillVoucher, Shop, Voucher
from scripts import seed_demo_catalog as importer
from tests.backend.test_shop_cache import cleanup_cache


def test_catalog_has_diverse_fictional_data_and_existing_photos():
    catalog = importer.load_catalog()
    assert len(catalog.shops) == 34 and len(catalog.vouchers) == 12
    assert {shop.type_id for shop in catalog.shops} == set(range(1, 11))
    assert {shop.area for shop in catalog.shops} == {"南开街区", "海河沿线", "五大道", "意式风情区"}
    assert min(shop.avg_price for shop in catalog.shops) == 1800
    assert max(shop.avg_price for shop in catalog.shops) == 23800
    assert all(voucher.type == 0 for voucher in catalog.vouchers)


@pytest_asyncio.fixture
async def catalog_context(mysql_settings):
    catalog = importer.load_catalog()
    shop_ids = [shop.id for shop in catalog.shops]
    voucher_ids = [voucher.id for voucher in catalog.vouchers]
    app = create_app(mysql_settings)
    async with app.router.lifespan_context(app):
        database = app.state.database
        async with database.sessions() as session:
            assert not list(await session.scalars(select(Shop.id).where(Shop.id.in_(shop_ids))))
        try:
            yield app, catalog
        finally:
            # 只删除本测试的独立库增量 ID，不重导种子、不重置原有库存或其他业务表。
            async with database.sessions() as session, session.begin():
                await session.execute(
                    delete(SeckillVoucher).where(SeckillVoucher.voucher_id.in_(voucher_ids))
                )
                await session.execute(delete(Voucher).where(Voucher.id.in_(voucher_ids)))
                await session.execute(delete(Shop).where(Shop.id.in_(shop_ids)))
            await cleanup_cache(app.state.cache_redis, mysql_settings)


async def original_facts(database, catalog):
    facts = {}
    async with database.engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            query = select(table).order_by(*table.primary_key.columns)
            if table.name == Shop.__tablename__:
                query = query.where(table.c.id.not_in([item.id for item in catalog.shops]))
            elif table.name == Voucher.__tablename__:
                query = query.where(table.c.id.not_in([item.id for item in catalog.vouchers]))
            elif table.name == SeckillVoucher.__tablename__:
                query = query.where(
                    table.c.voucher_id.not_in([item.id for item in catalog.vouchers])
                )
            facts[table.name] = [dict(row) for row in (await connection.execute(query)).mappings()]
    return facts


async def imported_ids(database):
    async with database.sessions() as session:
        return (
            list(await session.scalars(select(Shop.id).where(Shop.id.between(1001, 1034)))),
            list(await session.scalars(select(Voucher.id).where(Voucher.id.between(2001, 2012)))),
        )


@pytest.mark.mysql
@pytest.mark.redis
async def test_preview_and_rerun_preserve_existing_rows_and_changed_new_stock(catalog_context):
    app, catalog = catalog_context
    database, settings = app.state.database, app.state.settings
    before = await original_facts(database, catalog)
    preview = await importer.run_import(settings)
    assert preview.mode == "preview" and preview.new_shops == 34 and preview.new_vouchers == 12
    assert preview.cache_invalidated is None
    assert await imported_ids(database) == ([], [])
    assert await original_facts(database, catalog) == before
    result = await importer.run_import(settings, apply=True)
    assert result.new_shops == 34 and result.new_vouchers == 12 and result.cache_invalidated
    # 模拟导入后正常维护/库存变化；重复导入不能恢复成演示初值。
    async with database.sessions() as session, session.begin():
        await session.execute(update(Shop).where(Shop.id == 1001).values(avg_price=5100))
        await session.execute(update(Voucher).where(Voucher.id == 2001).values(stock=49))
    again = await importer.run_import(settings, apply=True)
    assert again.new_shops == again.new_vouchers == 0
    assert again.existing_shops == 34 and again.existing_vouchers == 12
    async with database.sessions() as session:
        assert (await session.get(Shop, 1001)).avg_price == 5100
        assert (await session.get(Voucher, 2001)).stock == 49
    assert await original_facts(database, catalog) == before


@pytest.mark.mysql
@pytest.mark.redis
async def test_occupied_id_rejects_entire_batch_without_overwriting(catalog_context):
    app, catalog = catalog_context
    database = app.state.database
    async with database.sessions() as session, session.begin():
        session.add(Shop(**{**catalog.shops[0].model_dump(), "name": "其他用户维护的商户"}))
    with pytest.raises(ValueError, match="商户 ID 1001"):
        await importer.run_import(app.state.settings, apply=True)
    assert await imported_ids(database) == ([1001], [])
    async with database.sessions() as session:
        assert (await session.get(Shop, 1001)).name == "其他用户维护的商户"


@pytest.mark.mysql
@pytest.mark.redis
async def test_voucher_insert_failure_rolls_back_already_flushed_shops(catalog_context):
    app, catalog = catalog_context
    database = app.state.database
    before = await original_facts(database, catalog)

    def reject_vouchers(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO tb_voucher "):
            raise RuntimeError("synthetic voucher failure")

    event.listen(database.engine.sync_engine, "before_cursor_execute", reject_vouchers)
    try:
        with pytest.raises(RuntimeError, match="synthetic voucher failure"):
            await importer.seed(database, catalog, apply=True)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", reject_vouchers)
    assert await imported_ids(database) == ([], [])
    assert await original_facts(database, catalog) == before


@pytest.mark.mysql
@pytest.mark.redis
async def test_commit_then_exact_cache_invalidation_and_api_visibility(
    catalog_context, monkeypatch
):
    app, _catalog = catalog_context
    database, settings, cache = app.state.database, app.state.settings, app.state.cache_redis
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/shops/1001")).status_code == 404  # 预先产生空值缓存。
        await client.get("/api/shops/1")
        await client.get("/api/shop-types")
        old_shop = await cache.get(settings.cache_key_prefix + "shop:1")
        old_types = await cache.lrange(settings.cache_key_prefix + "shop-types:all", 0, -1)
        outside = settings.cache_key_prefix + "outside:key"
        await cache.set(outside, "preserve")

        class CacheProbe:
            async def delete(self, *keys):
                # 用另一连接确认 SQL 已提交，且 DEL 仅包含固定增量商户详情 Key。
                shops, vouchers = await imported_ids(database)
                assert len(shops) == 34 and len(vouchers) == 12
                assert set(keys) == {
                    f"{settings.cache_key_prefix}shop:{i}" for i in range(1001, 1035)
                }
                return await cache.delete(*keys)

            aclose = AsyncMock()

        monkeypatch.setattr(importer, "create_redis_client", lambda _settings: CacheProbe())
        assert (await importer.run_import(settings, apply=True)).cache_invalidated
        assert await cache.get(settings.cache_key_prefix + "shop:1001") is None
        assert await cache.get(settings.cache_key_prefix + "shop:1") == old_shop
        assert await cache.lrange(settings.cache_key_prefix + "shop-types:all", 0, -1) == old_types
        assert await cache.get(outside) == "preserve"
        assert (await client.get("/api/shops/1001")).json()["name"] == "津味小灶"
        assert (await client.get("/api/shops?category_id=2")).json()["total"] == 2
        assert (await client.get("/api/shops?page_size=50")).json()["total"] == 40
        assert (await client.get("/api/vouchers?shop_id=1001")).json()["items"][0]["type"] == 0


@pytest.mark.mysql
@pytest.mark.redis
async def test_cache_failure_reports_partial_success_and_rerun_recovers(
    catalog_context, monkeypatch
):
    app, _catalog = catalog_context
    real_factory = importer.create_redis_client

    class BrokenCache:
        delete = AsyncMock(side_effect=RedisConnectionError("synthetic cache failure"))
        aclose = AsyncMock()

    monkeypatch.setattr(importer, "create_redis_client", lambda _settings: BrokenCache())
    result = await importer.run_import(app.state.settings, apply=True)
    assert result.mode == "applied" and result.cache_invalidated is False
    shops, vouchers = await imported_ids(app.state.database)
    assert len(shops) == 34 and len(vouchers) == 12
    monkeypatch.setattr(importer, "create_redis_client", real_factory)
    again = await importer.run_import(app.state.settings, apply=True)
    assert again.new_shops == again.new_vouchers == 0 and again.cache_invalidated
