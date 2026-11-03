"""压测隔离、鉴权与库存核对；小规模联通用例不当成性能成绩。"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import Settings
from backend.app.core.redis_connection import create_redis_client
from scripts.loadtest import inspect_run, make_app, prepare, save_settings, settings_for
from scripts.loadtest_contract import classify_response, directory, identity, write_json


@pytest.mark.parametrize("value", ["perkflow", "../perkflow", "a" * 11, "A" * 12, "f" * 12 + "*"])
def test_reject_external_or_development_identifiers(value):
    with pytest.raises(ValueError):
        directory(value)


def test_settings_ignore_shell_overrides_and_reject_unsafe_namespace(tmp_path, monkeypatch):
    import scripts.loadtest as module
    import scripts.loadtest_contract as contract

    monkeypatch.setattr(contract, "ROOT", tmp_path)
    monkeypatch.setattr(contract, "RUNS", tmp_path / ".local/loadtest")
    run_id = "a" * 12
    folder = directory(run_id)
    folder.mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        **identity(run_id),
        session_cookie_name=f"perkflow_lt_{run_id}",
        mysql_password='fake$()"\\\nwith-special',
        llm_api_key="",
    )
    save_settings(folder / ".env", settings)
    manifest = {
        "run_id": run_id,
        "resources": identity(run_id),
        "status": "ready",
        "mode": "async",
        "scenario": "distinct",
        "accounts": 2,
        "attempts": 2,
        "stock": 2,
        "port": 8001,
    }
    write_json(folder / "manifest.json", manifest)
    monkeypatch.setenv("MYSQL_DATABASE", "perkflow")
    monkeypatch.setenv("REDIS_KEY_PREFIX", "perkflow:auth:")
    monkeypatch.setenv("ORDER_MODE", "sync")
    _, loaded = module.settings_for(run_id)
    assert loaded.mysql_database == identity(run_id)["mysql_database"]
    assert loaded.redis_key_prefix == identity(run_id)["redis_key_prefix"]
    assert loaded.order_mode == "async"
    assert loaded.mysql_password == settings.mysql_password
    content = (folder / ".env").read_text(encoding="utf-8")
    (folder / ".env").write_text(
        content.replace(identity(run_id)["mysql_database"], "perkflow"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="隔离"):
        module.settings_for(run_id)


@pytest.mark.parametrize(
    ("status", "body", "mode", "outcome"),
    [
        (409, {"error": {"code": "OUT_OF_STOCK"}}, "async", "rejected"),
        (409, {"error": {"code": "SECKILL_NOT_STARTED"}}, "async", "error"),
        (503, {"error": {"code": "OUT_OF_STOCK"}}, "async", "error"),
        (401, {"error": {"code": "SESSION_EXPIRED"}}, "async", "error"),
        (202, {"request": {"id": "1", "status": "pending"}}, "async", "pending"),
        (200, {"request": {"id": "1", "status": "succeeded", "order": None}}, "async", "error"),
        (201, {"order": {"id": "1", "status": "created"}}, "sync", "succeeded"),
        (200, {"order": {"id": "1", "status": "cancelled"}}, "sync", "error"),
    ],
)
def test_classification_preserves_rejections_errors_and_pending(status, body, mode, outcome):
    assert classify_response(status, body, mode)[0] == outcome


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.mysql
@pytest.mark.redis
async def test_real_isolated_purchase_and_reconcile(mysql_settings, redis_settings, mode):
    if mode == "async":
        import os

        if os.getenv("PERKFLOW_TEST_RABBITMQ") != "1":
            pytest.skip("真实异步联通需--with-rabbitmq")
    # 1. 使用测试连接信息创建额外随机压测库；开发库和测试运行器的种子库均不修改。
    updates = {
        name: getattr(redis_settings, name)
        for name in (
            "redis_host",
            "redis_port",
            "redis_password",
            "redis_db",
        )
    }
    broker_settings = Settings()
    updates.update(
        {
            name: getattr(broker_settings, name)
            for name in (
                "rabbitmq_host",
                "rabbitmq_port",
                "rabbitmq_user",
                "rabbitmq_password",
                "rabbitmq_vhost",
            )
        }
    )
    local = mysql_settings.model_copy(update=updates)
    run_id = await prepare(mode, "distinct", 3, 2, 8098, local=local)
    manifest, settings = settings_for(run_id)
    folder = directory(run_id)
    worker = None
    try:
        if mode == "async":
            from backend.app.core.rabbitmq import connect_broker
            from backend.app.workers.orders import run

            worker = asyncio.create_task(run(settings))
            for _ in range(100):
                async with connect_broker(settings) as broker:
                    queue = await broker.consumer.declare_queue(broker.queue.name, passive=True)
                    if queue.declaration_result.consumer_count:
                        break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("隔离Worker未就绪")
        app = make_app(manifest, settings)
        credentials = json.loads((folder / "credentials.json").read_text(encoding="utf-8"))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)

            async def buy(credential):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    client.cookies.set(settings.session_cookie_name, credential["sid"])
                    headers = {"X-CSRF-Token": credential["csrf"], "Idempotency-Key": str(uuid4())}
                    # 2. 仍走生产Cookie/CSRF依赖；无Token拒绝，取消在专用包装层拒绝。
                    assert (await client.post("/api/vouchers/1/orders")).status_code == 403
                    assert (
                        await client.post("/api/orders/1/cancel", headers=headers)
                    ).status_code == 403
                    response = await client.post("/api/vouchers/1/orders", headers=headers)
                    outcome, code = classify_response(response.status_code, response.json(), mode)
                    body = response.json().get("request", response.json())
                    for _ in range(200):
                        if outcome != "pending":
                            break
                        await asyncio.sleep(0.05)
                        response = await client.get(f"/api/order-requests/{body['id']}")
                        body = response.json()
                        outcome, code = classify_response(
                            response.status_code, {"request": body}, mode
                        )
                    return {
                        "outcome": outcome,
                        "code": code,
                        "order_id": (body.get("order") or {}).get("id"),
                    }

            rows = await asyncio.gather(*(buy(row) for row in credentials))
            for index, row in enumerate(rows):
                row["ticket"] = index
        assert sorted(row["outcome"] for row in rows) == ["rejected", "succeeded", "succeeded"]
        write_json(folder / "observations.json", {"run_id": run_id, "attempts": rows})
        for _ in range(50):
            report = await inspect_run(run_id)
            if report["acceptance_pass"]:
                break
            await asyncio.sleep(0.1)
        assert report["acceptance_pass"], report
        assert report["active_orders"] == 2 and report["mysql_stock"] == 0
        pending = [dict(row, outcome="pending") for row in rows]
        write_json(folder / "observations.json", {"run_id": run_id, "attempts": pending})
        assert not (await inspect_run(run_id))["acceptance_pass"]
        # 未完成的尝试不能只因SQL库存平衡就被报告成压测成功。
        write_json(folder / "observations.json", {"run_id": run_id, "attempts": rows[:1]})
        assert not (await inspect_run(run_id))["acceptance_pass"]
    finally:
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        # 3. 仅测试收尾清理本用例自己创建的随机资源；用户CLI没有清库选项。
        if mode == "async":
            from backend.app.core.rabbitmq import connect_broker

            async with connect_broker(settings) as broker:
                await broker.queue.delete(if_unused=True, if_empty=True)
                await broker.dead_queue.delete(if_unused=True, if_empty=True)
                await broker.exchange.delete(if_unused=True)
                dead = await broker.consumer.get_exchange(settings.rabbitmq_prefix + ".dead")
                await dead.delete(if_unused=True)
        assert settings.mysql_database == identity(run_id)["mysql_database"]
        admin = create_async_engine(
            settings.database_url.set(database="mysql"), isolation_level="AUTOCOMMIT"
        )
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f"DROP DATABASE `{settings.mysql_database}`"))
        finally:
            await admin.dispose()
        redis = create_redis_client(settings)
        try:
            keys = [key async for key in redis.scan_iter(match=f"perkflow:loadtest:{run_id}:*")]
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()
