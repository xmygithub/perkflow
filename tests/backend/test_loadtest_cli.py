"""真实HTTP和Locust子进程的购买联通验收，非容量压测。"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import Settings
from backend.app.core.rabbitmq import connect_broker
from backend.app.core.redis_connection import create_redis_client
from scripts.loadtest import inspect_run, prepare, settings_for
from scripts.loadtest_contract import ROOT, directory, identity, target, write_json


@pytest.mark.mysql
@pytest.mark.redis
@pytest.mark.rabbitmq
@pytest.mark.parametrize(
    ("mode", "scenario", "web", "execution"),
    [
        ("sync", "distinct", False, "closed-loop"),
        ("async", "distinct", False, "closed-loop"),
        ("async", "same-intent", False, "closed-loop"),
        ("async", "same-user", False, "closed-loop"),
        ("sync", "distinct", True, "closed-loop"),
        ("sync", "distinct", False, "decoupled"),
        ("async", "distinct", False, "decoupled"),
        ("async", "same-intent", False, "decoupled"),
        ("async", "same-user", False, "decoupled"),
        ("async", "distinct", True, "decoupled"),
    ],
)
async def test_real_locust_processes(
    mysql_settings, redis_settings, mode, scenario, web, execution
):
    if os.getenv("PERKFLOW_TEST_RABBITMQ") != "1":
        pytest.skip("CLI烟测需显式--with-rabbitmq")
    if importlib.util.find_spec("locust") is None:
        pytest.skip("需要安装requirements-loadtest.lock")
    # 不在pytest进程导入Locust：gevent monkey patch会影响已有asyncio/MySQL事件循环。
    local = Settings()
    settings = mysql_settings.model_copy(
        update={
            **{
                name: getattr(redis_settings, name)
                for name in (
                    "redis_host",
                    "redis_port",
                    "redis_db",
                    "redis_password",
                )
            },
            **{
                name: getattr(local, name)
                for name in (
                    "rabbitmq_host",
                    "rabbitmq_port",
                    "rabbitmq_user",
                    "rabbitmq_password",
                    "rabbitmq_vhost",
                )
            },
        }
    )
    run_id = await prepare(mode, scenario, 6, 3, 8097, local=settings)
    manifest, settings = settings_for(run_id)
    folder = directory(run_id)
    processes, handles = [], []
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        # 1. 只启动本测试自己持有Popen句柄的API/Worker，收尾不按端口或进程名批量终止。
        for action in ["api", "worker"] if mode == "async" else ["api"]:
            output = (folder / f"{action}.log").open("w", encoding="utf-8")
            handles.append(output)
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "scripts.loadtest", action, run_id],
                    cwd=ROOT,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
            )
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            for _ in range(100):
                if any(process.poll() is not None for process in processes):
                    pytest.fail("本测试专用API/Worker启动失败，查看本轮本地日志")
                try:
                    reply = await client.get(target(manifest) + "/_loadtest")
                    if reply.status_code == 200 and reply.json()["run_id"] == run_id:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("专用API未就绪")
        if mode == "async":
            for _ in range(100):
                async with connect_broker(settings) as broker:
                    queue = await broker.consumer.declare_queue(broker.queue.name, passive=True)
                    if queue.declaration_result.consumer_count:
                        break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("专用Worker未就绪")
        # 2. 六个有限ticket通过真实HTTP提交，保留正常售罄与重复资格分类，再核对最终SQL。
        if web:
            # Web结束任务但保留进程：必须在quitting尚未发生时就能核对完整结果。
            env.update(
                PERKFLOW_LOADTEST_RUN=run_id,
                PERKFLOW_LOADTEST_WAIT="10",
                PERKFLOW_LOADTEST_POLL="0.1",
                PERKFLOW_LOADTEST_OBSERVERS="2",
            )
            output = (folder / "locust-web.log").open("w", encoding="utf-8")
            handles.append(output)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "locust",
                    "-f",
                    str(
                        ROOT
                        / "scripts"
                        / (
                            "locust_purchase_decoupled.py"
                            if execution == "decoupled"
                            else "locust_purchase.py"
                        )
                    ),
                    "--host",
                    target(manifest),
                    "--web-host",
                    "127.0.0.1",
                    "--web-port",
                    "8096",
                ],
                cwd=ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            processes.append(process)
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                for _ in range(100):
                    try:
                        if (await client.get("http://127.0.0.1:8096/")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                response = await client.post(
                    "http://127.0.0.1:8096/swarm",
                    data={
                        "user_count": "6",
                        "spawn_rate": "20",
                        "host": target(manifest),
                    },
                )
                assert response.status_code == 200
                for _ in range(200):
                    stats = (await client.get("http://127.0.0.1:8096/stats/requests")).json()
                    if stats.get("state") == "stopped":
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Web有限请求集未自动结束")
            assert process.poll() is None, "必须验证Web进程仍在，不依赖进程退出保存"
            assert (folder / "observations.json").exists()
        else:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "scripts.loadtest",
                    "locust",
                    run_id,
                    "--headless",
                    "--execution",
                    execution,
                    "--observers",
                    "2",
                    "--users",
                    "6",
                    "--spawn-rate",
                    "20",
                    "--seconds",
                    "20",
                    "--wait",
                    "10",
                    "--poll",
                    "0.1",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=50,
            )
            assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]
        observations = json.loads((folder / "observations.json").read_text(encoding="utf-8"))
        assert observations["completed_attempts"] == 6
        if execution == "decoupled":
            assert observations["execution"] == "decoupled"
            assert observations["submission_phase"]["completed_posts"] == 6
            assert observations["parameters"]["observers"] == 2
            assert len((folder / "submissions.jsonl").read_text(encoding="utf-8").splitlines()) == 6
        for _ in range(50):
            report = await inspect_run(run_id)
            if report["acceptance_pass"]:
                break
            await asyncio.sleep(0.1)
        assert report["acceptance_pass"], report
        assert report["active_orders"] == (3 if scenario == "distinct" else 1)
        if scenario == "same-intent":
            assert len({row["request_id"] for row in observations["attempts"]}) == 1
        # 3. 保存无凭据烟测证据；不把六次请求的计时写成正式容量或优化倍率。
        evidence = ROOT / "docs/六、购买并发压测/验证资料"
        evidence.mkdir(parents=True, exist_ok=True)
        write_json(
            evidence / f"烟测-{mode}-{scenario}{'-web' if web else ''}"
            f"{'-decoupled' if execution == 'decoupled' else ''}.json",
            {
                "scope": "6次真实HTTP购买联通，不是容量压测",
                "reconciliation": report,
                "locust_version": observations["locust"],
                "source_sha256": observations["source_sha256"],
            },
        )
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
        for handle in handles:
            handle.close()
        if mode == "async":
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
