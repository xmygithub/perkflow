"""同步订单正确性验证：独立 MySQL 测试库和随机 Redis 命名空间。"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.errors import ApiError
from backend.app.core.order_id import RedisOrderId
from backend.app.core.redis_connection import create_redis_client
from backend.app.main import create_app
from backend.app.models import SeckillVoucher, User, Voucher, VoucherOrder
from backend.app.repositories import vouchers as repository
from backend.app.services import vouchers as service
from tests.backend.test_auth_api import CaptureSms, sign_in


@pytest.mark.redis
async def test_id_sequence_shared_atomic_no_ttl_and_utc_bits(redis_settings):
    cache = create_redis_client(redis_settings)
    first = RedisOrderId(cache, redis_settings.order_id_key_prefix)
    second = RedisOrderId(cache, redis_settings.order_id_key_prefix)
    keys = []
    try:
        ids = []
        for _ in range(6):
            ids.extend(
                await asyncio.gather(*[(first if i % 2 else second).next_id() for i in range(10)])
            )
        assert len(set(ids)) == 60
        assert sorted(value & (2**32 - 1) for value in ids) == list(range(1, 61))
        assert all(0 < value < 2**63 for value in ids)
        keys = [
            key async for key in cache.scan_iter(match=redis_settings.order_id_key_prefix + "*")
        ]
        assert len(keys) == 1 and keys[0].endswith(datetime.now(UTC).strftime("%Y%m%d"))
        assert await cache.type(keys[0]) == "string" and await cache.ttl(keys[0]) == -1
        assert await cache.get(keys[0]) == "60"
    finally:
        if keys:
            await cache.delete(*keys)
        await cache.aclose()


async def test_id_failure_has_business_error_and_no_local_fallback():
    cache = AsyncMock()
    cache.incr.side_effect = RedisConnectionError("private connection detail")
    with pytest.raises(ApiError) as error:
        await RedisOrderId(cache, "perkflow:test:id:").next_id()
    assert error.value.code == "ORDER_ID_UNAVAILABLE"
    assert "private" not in error.value.message


@pytest.mark.parametrize("sequence", [0, -1, 2**32])
async def test_id_rejects_invalid_or_exhausted_sequence(sequence):
    cache = AsyncMock()
    cache.incr.return_value = sequence
    with pytest.raises(ApiError) as error:
        await RedisOrderId(cache, "perkflow:test:id:").next_id()
    assert error.value.code == "ORDER_ID_EXHAUSTED"


@pytest_asyncio.fixture
async def order_context(mysql_settings):
    settings = mysql_settings.model_copy(
        update={
            "redis_key_prefix": f"{mysql_settings.cache_key_prefix}auth:",
        }
    )
    app = create_app(settings)
    base = 20_000_000 + secrets.randbelow(1_000_000)
    phones = [f"139{secrets.randbelow(100_000_000):08d}" for _ in range(2)]
    now = datetime.now(UTC).replace(tzinfo=None)
    async with app.router.lifespan_context(app):
        db = app.state.database
        app.state.sms_sender = CaptureSms()
        async with db.sessions() as session, session.begin():
            for i, phone in enumerate(phones):
                session.add(User(id=base + i, phone=phone, nick_name=f"订单测试{i}"))
            for i in range(8):
                session.add(
                    Voucher(
                        id=base + i,
                        shop_id=1,
                        title=f"同步测试券{i}",
                        sub_title="虚构测试",
                        rules=["测试规则"],
                        pay_value=5900,
                        actual_value=8800,
                        type=0 if i == 1 else 1,
                        status=0 if i == 5 else 1,
                        total_stock=3,
                        stock=0 if i == 6 else 3,
                        validity_days=30,
                    )
                )
            await session.flush()
            for i in [0, 1, 2, 3, 5, 6, 7]:
                begin = now + timedelta(hours=1) if i == 2 else now - timedelta(hours=2)
                end = now - timedelta(hours=1) if i == 3 else now + timedelta(hours=2)
                session.add(SeckillVoucher(voucher_id=base + i, begin_time=begin, end_time=end))
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield app, client, base, phones
        finally:
            async with db.sessions() as session, session.begin():
                await session.execute(
                    delete(VoucherOrder).where(VoucherOrder.voucher_id.between(base, base + 7))
                )
                await session.execute(
                    delete(SeckillVoucher).where(SeckillVoucher.voucher_id.between(base, base + 7))
                )
                await session.execute(delete(Voucher).where(Voucher.id.between(base, base + 7)))
                await session.execute(delete(User).where(User.id.in_([base, base + 1])))
            for prefix in [
                settings.redis_key_prefix,
                settings.order_id_key_prefix,
                settings.order_lock_key_prefix,
            ]:
                assert prefix.startswith("perkflow:test:")
                keys = [key async for key in app.state.cache_redis.scan_iter(match=prefix + "*")]
                if keys:
                    await app.state.cache_redis.delete(*keys)


async def login(client, app, phone):
    result = await sign_in(client, app, phone)
    assert result.status_code == 200
    return {"X-CSRF-Token": result.json()["csrf_token"]}


async def facts(app, base):
    async with app.state.database.sessions() as session:
        stock = await session.scalar(select(Voucher.stock).where(Voucher.id == base))
        count = await session.scalar(
            select(func.count()).select_from(VoucherOrder).where(VoucherOrder.voucher_id == base)
        )
        return stock, count


class TestSyncOrder:
    pytestmark = [pytest.mark.mysql, pytest.mark.redis]

    async def test_public_query_real_stock_filters_states_and_utc(self, order_context):
        app, client, base, _ = order_context
        states = ["active", "active", "upcoming", "ended", "unavailable", "off_shelf", "sold_out"]
        for i, state in enumerate(states):
            response = await client.get(f"/api/vouchers/{base + i}")
            assert response.status_code == 200
            assert response.json()["state"] == state
            assert response.headers["cache-control"] == "no-store"
            if i == 0:
                assert response.json()["begin_time"].endswith("Z")
        result = await client.get("/api/vouchers?shop_id=1&type=1&page_size=2")
        assert result.status_code == 200 and len(result.json()["items"]) == 2
        assert all(row["shop_id"] == 1 and row["type"] == 1 for row in result.json()["items"])
        for query in ["page=0", "page_size=51", "type=2", "shop_id=0", "user_id=1"]:
            assert (await client.get(f"/api/vouchers?{query}")).status_code == 422
        assert (await client.get("/api/vouchers/18446744073709551616")).status_code == 422
        assert (await client.get("/api/vouchers/999999999")).status_code == 404

    async def test_login_csrf_origin_required_before_any_stock_change(self, order_context):
        app, client, base, phones = order_context
        url = f"/api/vouchers/{base}/orders"
        assert (await client.post(url)).status_code == 401
        headers = await login(client, app, phones[0])
        assert (await client.post(url)).status_code == 403
        assert (await client.post(url, headers={"X-CSRF-Token": "forged"})).status_code == 403
        assert (
            await client.post(url, headers={**headers, "Origin": "https://untrusted.invalid"})
        ).status_code == 403
        assert await facts(app, base) == (3, 0)

    async def test_success_repeat_snapshot_own_list_and_other_user_404(self, order_context):
        app, client, base, phones = order_context
        headers = await login(client, app, phones[0])
        url = f"/api/vouchers/{base}/orders"
        response = await client.post(url, headers=headers)
        assert response.status_code == 201 and response.json()["reused"] is False
        order = response.json()["order"]
        assert isinstance(order["id"], str) and int(order["id"]) > 2**53
        assert order["pay_value"] == 5900 and order["status"] == "created"
        assert order["created_at"].endswith("Z") and order["expires_at"].endswith("Z")
        assert await facts(app, base) == (2, 1)
        repeat = await client.post(url, headers=headers)
        assert repeat.status_code == 200 and repeat.json()["reused"]
        assert repeat.json()["order"]["id"] == order["id"] and await facts(app, base) == (2, 1)
        # 修改券当前价格不改变订单金额快照；读取名称仍是关联的当前展示信息。
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(update(Voucher).where(Voucher.id == base).values(pay_value=6000))
        listed = (await client.get("/api/orders")).json()
        assert listed["total"] == 1 and listed["items"][0]["pay_value"] == 5900
        assert (await client.get(f"/api/orders/{order['id']}")).status_code == 200
        await login(client, app, phones[1])
        assert (await client.get("/api/orders")).json()["total"] == 0
        assert (await client.get(f"/api/orders/{order['id']}")).status_code == 404
        assert (await client.get("/api/orders?user_id=1")).status_code == 422

    @pytest.mark.parametrize(
        "offset,code",
        [
            (2, "SECKILL_NOT_STARTED"),
            (3, "SECKILL_ENDED"),
            (4, "SECKILL_NOT_CONFIGURED"),
            (5, "VOUCHER_OFF_SHELF"),
            (6, "OUT_OF_STOCK"),
        ],
    )
    async def test_unavailable_coupon_never_creates_order(self, order_context, offset, code):
        app, client, base, phones = order_context
        headers = await login(client, app, phones[0])
        response = await client.post(f"/api/vouchers/{base + offset}/orders", headers=headers)
        assert response.status_code == 409 and response.json()["error"]["code"] == code
        assert await facts(app, base + offset) == (0 if offset == 6 else 3, 0)

    async def test_write_failure_rolls_stock_back_but_id_is_not_recycled(
        self, order_context, monkeypatch
    ):
        app, client, base, phones = order_context
        headers = await login(client, app, phones[0])
        real_insert = repository.insert_order
        monkeypatch.setattr(repository, "insert_order", AsyncMock(side_effect=SQLAlchemyError()))
        response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        assert response.status_code == 503 and await facts(app, base) == (3, 0)
        monkeypatch.setattr(repository, "insert_order", real_insert)
        response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        assert response.status_code == 201
        assert int(response.json()["order"]["id"]) & (2**32 - 1) == 2

    async def test_redis_id_failure_does_not_decrement_stock(self, order_context, monkeypatch):
        app, client, base, phones = order_context
        headers = await login(client, app, phones[0])
        monkeypatch.setattr(
            app.state.order_ids.redis, "incr", AsyncMock(side_effect=RedisConnectionError())
        )
        response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        assert (
            response.status_code == 503
            and response.json()["error"]["code"] == "ORDER_ID_UNAVAILABLE"
        )
        assert await facts(app, base) == (3, 0)

    async def test_activity_rechecked_at_inventory_write(self, order_context, monkeypatch):
        app, client, base, phones = order_context
        headers = await login(client, app, phones[0])
        real_next = app.state.order_ids.next_id

        async def end_activity_before_update():
            async with app.state.database.sessions() as session, session.begin():
                await session.execute(
                    update(SeckillVoucher)
                    .where(SeckillVoucher.voucher_id == base)
                    .values(end_time=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1))
                )
            return await real_next()

        monkeypatch.setattr(app.state.order_ids, "next_id", end_activity_before_update)
        response = await client.post(f"/api/vouchers/{base}/orders", headers=headers)
        assert response.status_code == 409 and await facts(app, base) == (3, 0)

    async def test_minimal_parallel_sanity_same_user_one_order(self, order_context):
        app, _, base, _ = order_context

        async def buy():
            async with app.state.database.sessions() as session:
                return await service._create_order_with_retry(
                    session, base, base, app.state.order_ids
                )

        results = await asyncio.gather(*[buy() for _ in range(4)])
        assert len({row.order.id for row in results}) == 1
        assert sum(not row.reused for row in results) == 1
        assert await facts(app, base) == (2, 1)

    async def test_basic_stock_sanity_two_buyers_last_one(self, order_context):
        app, _, base, _ = order_context
        async with app.state.database.sessions() as session, session.begin():
            await session.execute(update(Voucher).where(Voucher.id == base).values(stock=1))

        async def buy(user_id):
            async with app.state.database.sessions() as session:
                try:
                    return await service._create_order_with_retry(
                        session, user_id, base, app.state.order_ids
                    )
                except ApiError as error:
                    return error

        results = await asyncio.gather(buy(base), buy(base + 1))
        assert sum(not isinstance(row, ApiError) for row in results) == 1
        assert await facts(app, base) == (0, 1)

    async def test_failed_commit_rolls_back_stock_and_order(self, order_context):
        app, _, base, _ = order_context

        def fail_commit(_session):
            raise SQLAlchemyError("synthetic failure before COMMIT")

        async with app.state.database.sessions() as session:
            event.listen(session.sync_session, "before_commit", fail_commit)
            try:
                with pytest.raises(SQLAlchemyError):
                    await service._create_order_with_retry(session, base, base, app.state.order_ids)
            finally:
                event.remove(session.sync_session, "before_commit", fail_commit)
        assert await facts(app, base) == (3, 0)

    async def test_id_collision_is_not_success_and_inventory_rolls_back(
        self, order_context, monkeypatch
    ):
        app, _, base, _ = order_context
        async with app.state.database.sessions() as session:
            first = await service._create_order_with_retry(session, base, base, app.state.order_ids)
        monkeypatch.setattr(
            app.state.order_ids, "next_id", AsyncMock(return_value=int(first.order.id))
        )
        async with app.state.database.sessions() as session:
            with pytest.raises(ApiError) as error:
                await service._create_order_with_retry(session, base + 1, base, app.state.order_ids)
        assert error.value.code == "ORDER_ID_CONFLICT"
        assert await facts(app, base) == (2, 1)
