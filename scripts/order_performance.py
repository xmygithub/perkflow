"""专项命令：pilot验证后suite；每轮新资源，不清库、不修改核心交易代码。"""

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import quote

import httpx
from sqlalchemy import text

from scripts.loadtest import inspect_run, make_app, prepare, settings_for
from scripts.loadtest_contract import ROOT, directory, write_json
from scripts.order_perf_probe import Recorder, calibrate, install

PERF_ROOT = ROOT / ".local" / "order-performance"


def management(settings):
    # 1. 凭据只进请求认证；结果不保存URL、认证头、Cookie或数据库连接串。
    return httpx.Client(
        base_url=f"http://{settings.rabbitmq_host}:15672",
        timeout=5,
        auth=(settings.rabbitmq_user, settings.rabbitmq_password.get_secret_value()),
        trust_env=False,
    )


def mq_sample(client, settings):
    path = f"/api/queues/{quote(settings.rabbitmq_vhost, safe='')}/"
    response = client.get(path + quote(settings.rabbitmq_prefix + ".order.create", safe=""))
    response.raise_for_status()
    row = response.json()
    stats = row.get("message_stats", {})
    return {
        "ready": row.get("messages_ready"),
        "unacked": row.get("messages_unacknowledged"),
        "ack_total": stats.get("ack", 0),
        "publish_total": stats.get("publish", 0),
        "deliver_total": stats.get("deliver_get", 0),
        "redeliver_total": stats.get("redeliver", 0),
        "ack_rate_reported": stats.get("ack_details", {}).get("rate"),
        "publish_rate_reported": stats.get("publish_details", {}).get("rate"),
        "consumers": row.get("consumers"),
        "idle_since": row.get("idle_since"),
    }


async def child(role, run_id, probe):
    from backend.app.core.redis_connection import create_redis_client
    from backend.app.workers.orders import run

    _, settings = settings_for(run_id)
    folder = directory(run_id)
    delivery_plan = folder / "delivery-plan.json"
    variant = (
        json.loads(delivery_plan.read_text(encoding="utf-8"))["variant"]
        if delivery_plan.exists()
        else "baseline"
    )
    scan_study = False
    if delivery_plan.exists():
        scan_study = json.loads(delivery_plan.read_text(encoding="utf-8")).get("study") == "scan-v1"
    if variant not in {"baseline", "sleep0", "batch4", "publish2"} and not scan_study:
        raise ValueError("未知投递实验")
    recorder = Recorder(folder / f"perf-{role}.jsonl", role) if probe else None
    if recorder:
        install(recorder, work_sleep=0 if role == "worker" and variant == "sleep0" else None)
    if role == "worker" and variant in {"batch4", "publish2"}:
        from scripts.order_delivery_variants import install_variant

        install_variant(variant, recorder)
    if scan_study:
        from scripts.order_dispatch_study import (
            VARIANTS,
            install_dispatch_variant,
            install_scan_probe,
        )

        if variant not in VARIANTS:
            raise ValueError("未知扫描专项候选")
        if role == "worker":
            if recorder:
                install_scan_probe(recorder)
            install_dispatch_variant(variant, recorder)
    redis = create_redis_client(settings)
    await calibrate(redis, folder / f"clock-{role}-before.json")
    watcher = None
    runtime_observer = None
    if scan_study and recorder:
        from scripts.order_dispatch_study import observe_runtime

        runtime_observer = asyncio.create_task(observe_runtime(recorder))

    async def watch(callback):
        while not (folder / "perf-stop.request").exists():
            await asyncio.sleep(0.5)
        callback()

    try:
        if role == "api":
            import uvicorn

            server = uvicorn.Server(
                uvicorn.Config(
                    make_app(settings_for(run_id)[0], settings),
                    host="127.0.0.1",
                    port=settings_for(run_id)[0]["port"],
                    access_log=False,
                    log_level="warning",
                )
            )
            watcher = asyncio.create_task(watch(lambda: setattr(server, "should_exit", True)))
            await server.serve()
        else:
            # Windows没有SIGTERM优雅路径，控制器只向自己启动的进程发送CTRL_BREAK。
            task = asyncio.create_task(run(settings))
            watcher = asyncio.create_task(watch(task.cancel))
            loop = asyncio.get_running_loop()

            def stop(*args):
                loop.call_soon_threadsafe(task.cancel)

            signal.signal(signal.SIGBREAK, stop)
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        if runtime_observer:
            runtime_observer.cancel()
            await asyncio.gather(runtime_observer, return_exceptions=True)
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await calibrate(redis, folder / f"clock-{role}-after.json")
        await redis.aclose()
        if recorder:
            recorder.close()


async def monitor(run_id, stop, samples):
    from backend.app.core.database import Database
    from backend.app.core.redis_connection import create_redis_client

    _, settings = settings_for(run_id)
    redis, db = create_redis_client(settings), Database(settings)
    folder = directory(run_id)
    await calibrate(redis, folder / "clock-monitor-before.json")
    # 2. Management的统计刷新可能慢于采样；保留原始累计值，峰值仅声称为采样峰值。
    try:
        with management(settings) as client:
            while not stop.is_set():
                start = time.perf_counter_ns()
                row = {"wall_ns": time.time_ns(), "mono_ns": start}
                try:
                    row.update(await asyncio.to_thread(mq_sample, client, settings))
                    prefix = settings.seckill_key_prefix
                    row["redis_outbox"] = await redis.zcard(prefix + "outbox")
                    row["redis_outbox_due"] = await redis.zcount(
                        prefix + "outbox", "-inf", int(time.time() * 1000)
                    )
                    # 这里只采集本库连接状态，不高频扫描业务表；最终事实由完整核对负责。
                    async with db.sessions() as session:
                        rows = (
                            await session.execute(
                                text(
                                    "SELECT STATE, COUNT(*) FROM information_schema.PROCESSLIST "
                                    "WHERE DB=:database GROUP BY STATE"
                                ),
                                {"database": settings.mysql_database},
                            )
                        ).all()
                        row["mysql_states"] = {str(state): count for state, count in rows}
                    try:
                        async with db.sessions() as session:
                            waits = (
                                await session.execute(
                                    text(
                                        "SELECT l.OBJECT_NAME, COUNT(*) "
                                        "FROM performance_schema.data_lock_waits w "
                                        "JOIN performance_schema.threads t "
                                        "ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                                        "JOIN performance_schema.data_locks l ON l.ENGINE=w.ENGINE "
                                        "AND l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                                        "WHERE t.PROCESSLIST_DB=:database GROUP BY l.OBJECT_NAME"
                                    ),
                                    {"database": settings.mysql_database},
                                )
                            ).all()
                            row["mysql_lock_wait_tables"] = {
                                str(name): count for name, count in waits
                            }
                            row["mysql_lock_waits"] = sum(count for _, count in waits)
                    except Exception as exc:
                        row["lock_metric_unavailable"] = type(exc).__name__
                    row["sample_ms"] = (time.perf_counter_ns() - start) / 1e6
                except Exception as exc:
                    row["error"] = type(exc).__name__
                samples.append(row)
                await asyncio.sleep(1)
    finally:
        await calibrate(redis, folder / "clock-monitor-after.json")
        await redis.aclose()
        await db.close()


def stop_process(process):
    if process.poll() is None:
        process.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)


def available_port():
    for port in range(8250, 8350):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise ValueError("没有空闲专项端口")


async def execute(prefetch, attempts, probe=True, label="formal", variant="baseline", study=None):
    from backend.app.core.config import Settings

    settings = Settings().model_copy(
        update={"order_worker_concurrency": prefetch, "mysql_pool_size": 5, "mysql_max_overflow": 5}
    )
    # 3. prepare只创建随机新库，settings_for在进程启动前再次校验全部隔离身份。
    run_id = await prepare(
        "async", "distinct", attempts, attempts * 2, available_port(), local=settings
    )
    folder = directory(run_id)
    write_json(folder / "delivery-plan.json", {"variant": variant, "study": study})
    extra_sources = (
        [ROOT / "scripts/order_dispatch_study.py", ROOT / "scripts/order_dispatch_performance.py"]
        if study == "scan-v1"
        else []
    )
    write_json(
        folder / "performance-plan.json",
        {
            "prefetch": prefetch,
            "attempts": attempts,
            "probe": probe,
            "label": label,
            "delivery_variant": variant,
            "users": 20,
            "spawn_rate": 10,
            "observers": 10,
            "poll": 0.5,
            "wait": 120,
            "sample_seconds": 1,
            "sources": {
                str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in [
                    ROOT / "scripts/order_perf_probe.py",
                    ROOT / "scripts/order_performance.py",
                    ROOT / "scripts/order_perf_report.py",
                    ROOT / "scripts/order_delivery_variants.py",
                    ROOT / "scripts/lua/order_perf_claim_batch.lua",
                    ROOT / "backend/app/services/redis_orders.py",
                    ROOT / "scripts/locust_order_performance.py",
                    ROOT / "scripts/locust_purchase_decoupled.py",
                    ROOT / "backend/app/workers/orders.py",
                    ROOT / "backend/app/core/rabbitmq.py",
                    ROOT / "backend/app/services/order_processing.py",
                ]
                + extra_sources
            },
        },
    )
    processes, handles, samples = [], [], []
    stop, monitoring = asyncio.Event(), None
    result = {
        "delivery_variant": variant,
        "run_id": run_id,
        "prefetch": prefetch,
        "attempts": attempts,
        "probe": probe,
        "label": label,
        "valid": False,
    }
    env = os.environ.copy() | {"PYTHONIOENCODING": "utf-8"}
    try:
        for role in ("api", "worker"):
            handle = (folder / f"perf-{role}-process.log").open("x", encoding="utf-8")
            handles.append(handle)
            command = [sys.executable, "-m", "scripts.order_performance", "child", role, run_id]
            if probe:
                command.append("--probe")
            processes.append(
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            )
        manifest, private = settings_for(run_id)
        with httpx.Client(trust_env=False, timeout=2) as client:
            for _ in range(80):
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("专项子进程提前退出")
                try:
                    response = await asyncio.to_thread(
                        client.get, f"http://127.0.0.1:{manifest['port']}/_loadtest/ready"
                    )
                    if response.status_code == 200 and response.json().get("ready"):
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("专项服务未就绪")
        with management(private) as client:
            for _ in range(30):
                initial = await asyncio.to_thread(mq_sample, client, private)
                if initial["ready"] == 0 and initial["unacked"] == 0 and initial["consumers"] == 1:
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("MQ初始资源不是空队列和一个专属消费者")
        monitoring = asyncio.create_task(monitor(run_id, stop, samples))
        await asyncio.sleep(2)
        env.update(
            PERKFLOW_LOADTEST_RUN=run_id,
            PERKFLOW_LOADTEST_WAIT="120",
            PERKFLOW_LOADTEST_POLL="0.5",
            PERKFLOW_LOADTEST_OBSERVERS="10",
        )
        command = [
            sys.executable,
            "-m",
            "locust",
            "-f",
            "scripts/locust_order_performance.py",
            "--host",
            f"http://127.0.0.1:{manifest['port']}",
            "--headless",
            "--users",
            "20",
            "--spawn-rate",
            "10",
            "--run-time",
            "150s",
            "--csv",
            str(folder / "perf-locust"),
            "--html",
            str(folder / "perf-locust.html"),
        ]
        with (folder / "perf-locust-process.log").open("x", encoding="utf-8") as handle:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            processes.append(process)
            try:
                result["locust_exit"] = await asyncio.to_thread(process.wait, 180)
            except subprocess.TimeoutExpired:
                result["locust_exit"] = "timeout"
                stop_process(process)
        # 4. 保持Worker运行完成收尾，不把Ready短暂为0当完成；至少等待Management刷新。
        await asyncio.sleep(8)
        stable, settling = 0, []
        for _ in range(30):
            reconciliation = await inspect_run(run_id)
            with management(private) as client:
                final = await asyncio.to_thread(mq_sample, client, private)
            clean = (
                reconciliation["acceptance_pass"] and final["ready"] == 0 and final["unacked"] == 0
            )
            settling.append(
                {
                    "wall_ns": time.time_ns(),
                    "clean": clean,
                    "checks": reconciliation["checks"],
                    "mq": final,
                }
            )
            stable = stable + 1 if clean else 0
            if stable >= 2:
                break
            await asyncio.sleep(2)
        write_json(folder / "settlement-checks.json", settling)
        result["final_mq"] = final
        result["reconciliation"] = reconciliation
        result["valid"] = (
            result["locust_exit"] == 0
            and reconciliation["acceptance_pass"]
            and final["ready"] == 0
            and final["unacked"] == 0
            and stable >= 2
            and not any("error" in row for row in samples)
        )
    except Exception as exc:
        result["error"] = type(exc).__name__
    finally:
        stop.set()
        if monitoring:
            await monitoring
        # 私有停止文件让API/Worker优雅收尾、完成时钟复测和缓冲落盘。
        (folder / "perf-stop.request").touch()
        for process in processes[:2]:
            if process.poll() is None:
                try:
                    await asyncio.to_thread(process.wait, 15)
                except subprocess.TimeoutExpired:
                    pass
        # 只退出本轮自己创建的子进程；不接触用户日常API/Worker及其他轮次资源。
        for process in reversed(processes):
            await asyncio.to_thread(stop_process, process)
        for handle in handles:
            handle.close()
        write_json(folder / "queue-series.json", samples)
        if samples:
            columns = [
                "wall_ns",
                "ready",
                "unacked",
                "ack_total",
                "publish_total",
                "deliver_total",
                "redis_outbox",
                "redis_outbox_due",
                "mysql_lock_waits",
                "sample_ms",
                "error",
            ]
            with (folder / "queue-series.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(samples)
        for role in ("api", "worker"):
            path = folder / f"perf-{role}.meta.json"
            if probe and (
                not path.exists()
                or json.loads(path.read_text(encoding="utf-8"))["dropped"] != 0
                or json.loads(path.read_text(encoding="utf-8"))["writer_alive"]
            ):
                result["valid"] = False
                result["probe_incomplete"] = role
        from scripts.order_perf_report import analyze

        try:
            result["analysis"] = analyze(folder, probe)
            if not result["analysis"]["coverage_pass"]:
                result["valid"] = False
            if (
                result["analysis"].get("delivery_window_errors")
                or result["analysis"].get("nack_count")
                or result["analysis"].get("reject_count")
                or result["analysis"].get("request_span_errors")
            ):
                result["valid"] = False
        except Exception as exc:
            result["analysis_error"] = type(exc).__name__
            result["valid"] = False
        write_json(folder / "performance-result.json", result)
        print(
            json.dumps(
                {k: result[k] for k in ["run_id", "prefetch", "attempts", "label", "valid"]}
            ),
            flush=True,
        )
    return result


async def batch(mode, resume=None):
    import platform

    import psutil

    # 首先只读验证Management可用，避免统计必需服务缺失时仍大量创建数据。
    from backend.app.core.config import Settings

    with management(Settings()) as client:
        overview = await asyncio.to_thread(client.get, "/api/overview")
        overview.raise_for_status()
        broker_version = overview.json().get("rabbitmq_version")
    PERF_ROOT.mkdir(exist_ok=True, parents=True)
    stamp = str(time.time_ns())
    summary = {
        "mode": mode,
        "rabbitmq_version": broker_version,
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "memory_bytes": psutil.virtual_memory().total,
        },
        "runs": [],
    }
    summary["development_before"] = await development_fingerprint()
    path = PERF_ROOT / f"{mode}-{stamp}.json"
    if resume:
        path = (PERF_ROOT / resume).resolve()
        if path.parent != PERF_ROOT.resolve() or not path.name.startswith("suite-"):
            raise ValueError("只允许恢复私有专项目录内的suite清单")
        summary = json.loads(path.read_text(encoding="utf-8"))
        if summary["mode"] != "suite":
            raise ValueError("不是正式矩阵")
    cases = (
        [
            (5, 100, False),
            (5, 100, True),
            (5, 100, True),
            (5, 100, False),
            (1, 40, True),
            (20, 40, True),
        ]
        if mode == "pilot"
        else [
            (p, 1000, True)
            for block in [(1, 5, 10, 20), (10, 20, 1, 5), (20, 10, 5, 1)]
            for p in block
        ]
    )
    for prefetch, attempts, probe in cases[len(summary["runs"]) :]:
        result = await execute(prefetch, attempts, probe, mode)
        summary["runs"].append(result)
        write_json(path, summary)
        if not result["valid"] and mode == "pilot":
            print("本轮未通过，已保存证据，停止后续发压。", flush=True)
            break
        await asyncio.sleep(2)
    # 正式失败样例保留，不计入三次有效重复；最多补两轮全新资源，不重放旧购买。
    if mode == "suite":
        for _ in range(2):
            missing = next(
                (
                    p
                    for p in [1, 5, 10, 20]
                    if sum(r["valid"] and r["prefetch"] == p for r in summary["runs"]) < 3
                ),
                None,
            )
            if missing is None:
                break
            summary["runs"].append(await execute(missing, 1000, True, "suite-replacement"))
            write_json(path, summary)
    summary["development_after"] = await development_fingerprint()
    summary["development_unchanged"] = summary["development_before"] == summary["development_after"]
    write_json(path, summary)
    print(f"专项汇总：{path}", flush=True)


async def development_fingerprint():
    """只读聚合指纹，不导出开发订单内容；不代表共享机器性能隔离。"""
    from backend.app.core.config import Settings
    from backend.app.core.database import Database

    settings = Settings()
    database = Database(settings)
    try:
        async with database.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT (SELECT COUNT(*) FROM tb_voucher_order), "
                        "(SELECT MAX(updated_at) FROM tb_voucher_order), "
                        "(SELECT COUNT(*) FROM tb_voucher), (SELECT SUM(stock) FROM tb_voucher)"
                    )
                )
            ).one()
        return {
            "database": settings.mysql_database,
            "aggregate_sha256": hashlib.sha256(str(tuple(row)).encode()).hexdigest(),
        }
    finally:
        await database.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("pilot")
    suite = sub.add_parser("suite")
    suite.add_argument("--resume")
    entry = sub.add_parser("child")
    entry.add_argument("role", choices=["api", "worker"])
    entry.add_argument("run_id")
    entry.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(
            child(args.role, args.run_id, args.probe)
            if args.action == "child"
            else batch(args.action, getattr(args, "resume", None))
        )
    except Exception as exc:
        print(f"专项命令失败：{type(exc).__name__}（不输出连接凭据）", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
