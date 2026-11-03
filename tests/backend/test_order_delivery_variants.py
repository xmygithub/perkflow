"""投递候选：真实Redis租约、故障恢复与有界任务；不改准入/订单事务/ACK。"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeout

from backend.app.core.redis_connection import create_redis_client
from backend.app.core.redis_orders import RedisOrderIngress
from backend.app.core.seckill_store import SeckillStore
from backend.app.services import redis_orders
from backend.app.workers import orders
from scripts.order_delivery_variants import PublishWindow, claim_many, install_variant


@pytest_asyncio.fixture
async def ingress(redis_settings):
    redis = create_redis_client(redis_settings)
    ingress = RedisOrderIngress(SeckillStore(redis, redis_settings.seckill_key_prefix))
    try:
        yield ingress
    finally:
        keys = [key async for key in redis.scan_iter(match=redis_settings.seckill_key_prefix + "*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


async def seed(ingress, count):
    await ingress.redis.zadd(ingress.outbox, {f"1:{1000 + i}": 0 for i in range(count)})
    for i in range(count):
        await ingress.redis.set(
            ingress.document_key(str(1000 + i)),
            json.dumps(
                {
                    "purchase": {
                        "request_id": str(1000 + i),
                        "user_id": str(i + 1),
                        "voucher_id": "1",
                        "request_key": str(uuid4()),
                        "generation": uuid4().hex,
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                    "result": {"status": "pending"},
                }
            ),
        )


@pytest.mark.redis
async def test_batch_claims_are_exclusive_with_individual_owners(ingress):
    await seed(ingress, 12)
    results = await asyncio.gather(*(claim_many(ingress, 2) for _ in range(10)))
    rows = [r for batch in results for r in batch]
    assert len(rows) == len({r[0] for r in rows}) == len({r[1] for r in rows}) == 12
    assert all(r[2] == 1 for r in rows)
    assert await ingress.redis.zcard(ingress.outbox) == 12
    assert await claim_many(ingress, 2) == []


@pytest.mark.redis
async def test_expired_batch_and_old_owner_cannot_finish_successor(ingress):
    await seed(ingress, 4)
    old = await claim_many(ingress, 0.1)
    await asyncio.sleep(0.25)
    new = await claim_many(ingress, 2)
    for first, second in zip(old, new, strict=True):
        assert first[0] == second[0] and first[1] != second[1] and second[2] == 2
        await ingress.finish(first[0], first[1], delay=None)
        assert await ingress.redis.hget(ingress.owners, second[0]) == second[1]
        assert await ingress.redis.zscore(ingress.outbox, second[0]) is not None


@pytest.mark.redis
async def test_batch_validation_before_any_write(ingress):
    await seed(ingress, 4)
    await ingress.redis.hset(ingress.attempts, "1:1003", "damaged")
    with pytest.raises(ResponseError):
        await claim_many(ingress, 2)
    assert await ingress.redis.hlen(ingress.owners) == 0
    assert all(
        score == 0
        for _, score in await ingress.redis.zrange(ingress.outbox, 0, -1, withscores=True)
    )


@pytest.mark.redis
async def test_batch_reply_lost_recovers_same_members_after_lease(ingress):
    await seed(ingress, 4)
    await claim_many(ingress, 0.1, count=1)
    await ingress.redis.zadd(ingress.outbox, {"1:1000": 0})
    original = ingress._batch_claim_script

    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise RedisTimeout("simulated reply lost")

    ingress._batch_claim_script = lost
    with pytest.raises(RedisTimeout):
        await claim_many(ingress, 0.1)
    assert await ingress.redis.hlen(ingress.owners) == 4
    ingress._batch_claim_script = original
    await asyncio.sleep(0.25)
    recovered = await claim_many(ingress, 2)
    assert len(recovered) == 4 and all(r[2] >= 2 for r in recovered)


@pytest.mark.redis
async def test_process_exit_after_batch_claim_keeps_all_members_recoverable(
    ingress, redis_settings
):
    from scripts.loadtest_contract import ROOT

    await seed(ingress, 4)
    config = {
        "redis_host": redis_settings.redis_host,
        "redis_port": redis_settings.redis_port,
        "redis_db": redis_settings.redis_db,
        "redis_password": redis_settings.redis_password.get_secret_value(),
        "seckill_key_prefix": redis_settings.seckill_key_prefix,
    }
    code = """
import asyncio,json,sys,os
from backend.app.core.config import Settings
from backend.app.core.redis_connection import create_redis_client
from backend.app.core.redis_orders import RedisOrderIngress
from backend.app.core.seckill_store import SeckillStore
from scripts.order_delivery_variants import claim_many
async def main():
 s=Settings(_env_file=None,**json.loads(sys.stdin.read()))
 r=create_redis_client(s)
 rows=await claim_many(RedisOrderIngress(SeckillStore(r,s.seckill_key_prefix)),.3)
 print(len(rows),flush=True)
 os._exit(0)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        cwd=ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await asyncio.wait_for(process.communicate(json.dumps(config).encode()), timeout=10)
    assert process.returncode == 0 and output.strip() == b"4"
    await asyncio.sleep(0.5)
    rows = await claim_many(ingress, 2)
    assert len(rows) == 4 and all(row[2] == 2 for row in rows)


@pytest.mark.redis
async def test_publish_timeout_keeps_request_and_schedules_retry(ingress):
    await seed(ingress, 1)
    claimed = (await claim_many(ingress, 2))[0]
    broker = AsyncMock()
    broker.publish_message.side_effect = TimeoutError("confirm unknown")
    from backend.app.core.config import Settings

    assert await redis_orders.dispatch_claimed(Settings(), broker, ingress, claimed)
    assert await ingress.get_document("1000") is not None
    assert await ingress.redis.zscore(ingress.outbox, claimed[0]) is not None
    assert await ingress.redis.hget(ingress.owners, claimed[0]) is None
    assert broker.publish_message.call_args.args[0].request_id == "1000"


@pytest.mark.redis
async def test_cancel_during_confirm_leaves_lease_recoverable(ingress):
    await seed(ingress, 1)
    claimed = (await claim_many(ingress, 0.1))[0]
    entered = asyncio.Event()

    async def publish(message):
        entered.set()
        await asyncio.Event().wait()

    from backend.app.core.config import Settings

    task = asyncio.create_task(
        redis_orders.dispatch_claimed(
            Settings(),
            type("Broker", (), {"publish_message": staticmethod(publish)})(),
            ingress,
            claimed,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await ingress.redis.hget(ingress.owners, claimed[0]) == claimed[1]
    await asyncio.sleep(0.25)
    recovered = (await claim_many(ingress, 2))[0]
    assert recovered[0] == claimed[0] and recovered[2] == 2


async def test_publish_window_waits_before_claim_and_joins_tasks(monkeypatch):
    monkeypatch.setattr(redis_orders, "dispatch_one", redis_orders.dispatch_one)
    monkeypatch.setattr(orders, "dispatch_loop", orders.dispatch_loop)
    release = asyncio.Event()

    async def process(*args):
        await release.wait()

    monkeypatch.setattr(redis_orders, "dispatch_claimed", process)
    window = install_variant("publish2")
    ingress = type("Ingress", (), {"claim": AsyncMock(return_value=("1:1000", "token", 1))})()
    settings = type("Settings", (), {"order_delivery_lease": 30})()
    await redis_orders.dispatch_one(settings, None, ingress)
    await redis_orders.dispatch_one(settings, None, ingress)
    third = asyncio.create_task(redis_orders.dispatch_one(settings, None, ingress))
    await asyncio.sleep(0.03)
    assert ingress.claim.await_count == 2 and window.peak == 2
    release.set()
    assert await third
    await window.close()
    assert not window.active and window.peak == 2


async def test_window_cancellation_propagates_and_does_not_swallow_errors():
    window = PublishWindow()
    task_started = asyncio.Event()
    stopped = asyncio.Event()

    async def work():
        task_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    window.start(work())
    await task_started.wait()
    await window.close()
    assert stopped.is_set() and not window.active
