"""扫描分离的真实调度、边界与观测合同；不以吞吐断言代替正式压测。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.app.services import redis_orders
from backend.app.workers import orders
from scripts.order_dispatch_study import install_dispatch_variant, install_scan_probe, sql_scan_loop
from scripts.order_perf_probe import CURRENT, Recorder


async def test_baseline_keeps_original_dispatch_loop():
    original = orders.dispatch_loop
    install_dispatch_variant("scan_baseline")
    assert orders.dispatch_loop is original


async def test_scan_probe_isolates_previous_request_and_records_worked(monkeypatch, tmp_path):
    recorder = Recorder(tmp_path / "scan.jsonl", "worker")

    async def scan(*args):
        scope = CURRENT.get()
        assert scope["request_id"] is None and scope["scan_id"] is not None
        recorder.emit("sql", operation="SELECT", table="tb_order_request")
        return False

    monkeypatch.setattr(orders, "dispatch_one", scan)
    install_scan_probe(recorder)
    token = CURRENT.set({"request_id": "previous"})
    try:
        assert await orders.dispatch_one() is False
        assert CURRENT.get() == {"request_id": "previous"}
    finally:
        CURRENT.reset(token)
        recorder.close()
    events = [json.loads(s) for s in recorder.path.read_text().splitlines()]
    assert all(e["request_id"] is None and e["scan_id"] for e in events)
    assert events[-1]["worked"] is False and events[-1]["ok"]


async def test_scan_probe_does_not_swallow_exceptions(monkeypatch, tmp_path):
    recorder = Recorder(tmp_path / "scan.jsonl", "worker")

    async def scan():
        raise ValueError("sensitive diagnostic")

    monkeypatch.setattr(orders, "dispatch_one", scan)
    install_scan_probe(recorder)
    with pytest.raises(ValueError):
        await orders.dispatch_one()
    assert CURRENT.get() is None
    recorder.close()
    event = json.loads(recorder.path.read_text())
    assert event["error"] == "ValueError" and not event["ok"]
    assert "sensitive diagnostic" not in recorder.path.read_text()


async def test_split_scanner_cannot_block_redis_dispatch_and_is_joined(monkeypatch, tmp_path):
    entered, stopped = asyncio.Event(), asyncio.Event()
    sent = 0

    async def scan(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def publish(*args):
        nonlocal sent
        sent += 1
        return True

    monkeypatch.setattr(orders, "dispatch_one", scan)
    monkeypatch.setattr(orders, "dispatch_loop", orders.dispatch_loop)
    monkeypatch.setattr(redis_orders, "dispatch_one", publish)
    recorder = Recorder(tmp_path / "events.jsonl", "worker")
    install_dispatch_variant("scan_split1", recorder)
    task = asyncio.create_task(orders.dispatch_loop(None, None, None, None, object()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(100):
            if sent >= 3:
                break
            await asyncio.sleep(0.01)
        assert sent >= 3
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        recorder.close()
    assert stopped.is_set()
    assert json.loads(recorder.path.read_text().splitlines()[-1])["scanner_stopped"]


@pytest.mark.parametrize("limit", [2, 4])
async def test_split_window_bounds_claims_before_publish_finishes(monkeypatch, limit):
    started, stopped = 0, 0

    async def publish(*args):
        nonlocal started, stopped
        started += 1
        try:
            await asyncio.Event().wait()
        finally:
            stopped += 1

    ingress = SimpleNamespace(claim=AsyncMock(return_value=("1:1000", "token", 1)))
    settings = SimpleNamespace(order_delivery_lease=30)
    monkeypatch.setattr(orders, "dispatch_loop", orders.dispatch_loop)
    monkeypatch.setattr(orders, "dispatch_one", AsyncMock(return_value=False))
    monkeypatch.setattr(redis_orders, "dispatch_claimed", publish)
    install_dispatch_variant(f"scan_split{limit}")
    task = asyncio.create_task(orders.dispatch_loop(None, settings, None, None, ingress))
    try:
        for _ in range(100):
            if started == limit:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.04)
        assert started == ingress.claim.await_count == limit
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stopped == limit


async def test_scan_failure_retries_without_overlapping(monkeypatch):
    calls, active, peak = 0, 0, 0

    async def scan(*args):
        nonlocal calls, active, peak
        active += 1
        peak = max(active, peak)
        try:
            calls += 1
            if calls == 1:
                raise ConnectionError("no credentials")
        finally:
            active -= 1

    monkeypatch.setattr(orders, "dispatch_one", scan)
    task = asyncio.create_task(sql_scan_loop(None, None, None, None, None))
    try:
        for _ in range(90):
            if calls >= 2:
                break
            await asyncio.sleep(0.01)
        assert calls >= 2 and peak == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_runtime_samples_are_bounded_fields_and_cancellable(tmp_path):
    from scripts.order_dispatch_study import observe_runtime

    recorder = Recorder(tmp_path / "runtime.jsonl", "worker")
    task = asyncio.create_task(observe_runtime(recorder, interval=0.01))
    await asyncio.sleep(0.06)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    recorder.close()
    rows = [json.loads(s) for s in recorder.path.read_text().splitlines()]
    assert rows and all(r["rss_bytes"] > 0 and r["loop_lag_ms"] >= 0 for r in rows)
    assert all("command_line" not in r and "environment" not in r for r in rows)


@pytest.mark.redis
@pytest.mark.parametrize("limit", [2, 4])
async def test_split_publish_cancel_preserves_real_redis_leases(monkeypatch, redis_settings, limit):
    from datetime import UTC, datetime

    from backend.app.core.redis_connection import create_redis_client
    from backend.app.core.redis_orders import RedisOrderIngress
    from backend.app.core.seckill_store import SeckillStore

    redis = create_redis_client(redis_settings)
    ingress = RedisOrderIngress(SeckillStore(redis, redis_settings.seckill_key_prefix))
    entered = 0

    async def publish(message):
        nonlocal entered
        entered += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(orders, "dispatch_loop", orders.dispatch_loop)
    monkeypatch.setattr(orders, "dispatch_one", AsyncMock(return_value=False))
    install_dispatch_variant(f"scan_split{limit}")
    task = None
    try:
        for i in range(limit):
            identifier = str(1000 + i)
            await redis.zadd(ingress.outbox, {f"1:{identifier}": 0})
            await redis.set(
                ingress.document_key(identifier),
                json.dumps(
                    {
                        "purchase": {
                            "request_id": identifier,
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
        settings = SimpleNamespace(order_delivery_lease=0.2)
        task = asyncio.create_task(
            orders.dispatch_loop(
                None, settings, SimpleNamespace(publish_message=publish), None, ingress
            )
        )
        for _ in range(100):
            if entered == limit:
                break
            await asyncio.sleep(0.01)
        assert entered == limit
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await redis.zcard(ingress.outbox) == await redis.hlen(ingress.owners) == limit
        await asyncio.sleep(0.3)
        recovered = [await ingress.claim(2) for _ in range(limit)]
        assert len({r[0] for r in recovered}) == limit
        assert all(r[2] == 2 for r in recovered)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        keys = [k async for k in redis.scan_iter(match=redis_settings.seckill_key_prefix + "*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


def test_profile_keeps_message_acks_separate_from_unique_orders(tmp_path):
    from scripts.order_dispatch_performance import profile

    def save(name, value):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    def event(stage, begin, end, **fields):
        return {
            "stage": stage,
            "role": "worker",
            "start_ns": begin,
            "end_ns": end,
            "start_wall_ns": begin,
            "end_wall_ns": end,
            **fields,
        }

    events = [
        event("sql_recovery_scan", 1100000000, 1300000000, ok=True, worked=False),
        event("sql", 1100000000, 1150000000, operation="SELECT", scan_id=7),
        event("ack_sent", 3000000000, 3000000000, ok=True),
        event("ack_sent", 3100000000, 3100000000, ok=True),
        event(
            "runtime_sample",
            2000000000,
            2000000000,
            cpu_percent_one_core=5,
            rss_bytes=1024,
            loop_lag_ms=2,
        ),
    ]
    (tmp_path / "perf-worker.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )
    (tmp_path / "perf-api.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "submissions.jsonl").write_text(
        json.dumps({"submit_ms": 8, "http_status": 202}), encoding="utf-8"
    )
    save("observations.json", {"attempts": [{"elapsed_ms": 2200}]})
    save("request-stages.json", [{"start_wall_ns": 1000000000, "commit_wall_ns": 3000000000}])
    analysis = {
        "clock": {r: {"offset_ns": 0} for r in ("api", "worker")},
        "stages": {
            k: {"count": 1, "mean_ms": 2}
            for k in (
                "client_to_commit_ms",
                "admission_to_commit_ms",
                "admission_to_first_publish_ms",
            )
        },
        **{
            k: 1
            for k in (
                "unique_committed_orders",
                "unique_orders_per_second_full_window",
                "active_callbacks_peak",
                "active_transactions_peak",
                "delivery_tasks_peak",
                "pool_checked_out_peak",
                "ready_sampled_peak",
                "unacked_sampled_peak",
                "unique_first_publishes_per_second_full_window",
            )
        },
    }
    p = profile(
        tmp_path, {"run_id": "fake", "delivery_variant": "scan_baseline", "analysis": analysis}
    )
    assert p["ack_messages"] == 2 and p["unique_successes"] == 1
    assert p["scan_count"] == 1 and p["scan_worked"] == 0
    assert p["scan_in_order_window_seconds"] == pytest.approx(0.2)
    assert p["scan_sql"]["mean_ms"] == 50 and p["post"]["mean_ms"] == 8
    assert p["runtime"]["worker"]["load_loop_lag"]["count"] == 1


def test_incomplete_matrix_cannot_generate_valid_comparison():
    from scripts.archive_order_dispatch import PUBLIC_FILES, aggregate

    with pytest.raises(ValueError):
        aggregate([])
    assert "credentials.json" not in PUBLIC_FILES
    assert not any("env" in p or "process.log" in p for p in PUBLIC_FILES)
