"""购买压测准备、专用进程启动及事后核对；不提供清库、取消或生产模式切换。"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from dotenv import dotenv_values
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.core.session_store import SessionStore
from backend.app.models import (
    OrderRequest,
    SeckillEffect,
    SeckillVoucher,
    Shop,
    ShopType,
    User,
    Voucher,
    VoucherOrder,
)
from scripts.loadtest_contract import (
    ROOT,
    directory,
    identity,
    load_manifest,
    read_attempts,
    target,
    write_json,
)
from scripts.run_backend_tests import migrate_schema


def save_settings(path, settings):
    # 1. 凭据只写本轮被Git忽略的.env，不上命令行；不携带真实模型Key。
    lines = []
    for name in Settings.model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            value = str(value).lower()
        lines.append(f"{name.upper()}={json.dumps(str(value), ensure_ascii=False)}")
    with path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def settings_for(run_id):
    manifest = load_manifest(run_id)
    values = dotenv_values(directory(run_id) / ".env", interpolate=False)
    # 全字段显式传入，屏蔽终端MYSQL_DATABASE等旧环境变量误指向开发资源。
    if set(values) != {key.upper() for key in Settings.model_fields}:
        raise ValueError("压测配置字段缺失或版本变化，请重新准备")
    args = {name: values[name.upper()] for name in Settings.model_fields}
    for name in ("cors_origins", "llm_http_allowed_origins", "llm_extra_body"):
        args[name] = json.loads(args[name])
    settings = Settings(_env_file=None, **args)
    for name, expected in identity(run_id).items():
        if getattr(settings, name) != expected:
            raise ValueError("压测配置与隔离清单不一致")
    if settings.order_mode != manifest["mode"] or settings.order_admission_mode != "redis_first":
        raise ValueError("压测业务模式不一致")
    if (
        settings.session_cookie_name != f"perkflow_lt_{run_id}"
        or settings.llm_api_key.get_secret_value()
    ):
        raise ValueError("压测会话或模型配置不符合隔离合同")
    return manifest, settings


async def provision_fixture(settings, accounts, stock):
    database = Database(settings)
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(migrate_schema)
        # 2. 只建一店一券，避免演示活动和历史订单给比较引入不同的后台扫描量。
        async with database.sessions() as session, session.begin():
            session.add(ShopType(id=1, name="压测餐饮", icon="test.png", sort=1))
            await session.flush()
            session.add(
                Shop(
                    id=1,
                    type_id=1,
                    name="虚构压测商户",
                    kind="火锅",
                    city="天津",
                    area="南开区",
                    address="仅用于隔离测试",
                    images=["test.png"],
                    tags=[],
                    avg_price=10000,
                    open_hours="00:00-24:00",
                    description="不是实际商户",
                )
            )
            await session.flush()
            session.add(
                Voucher(
                    id=1,
                    shop_id=1,
                    title="隔离压测券",
                    rules=["测试券，不可实际消费"],
                    pay_value=1000,
                    actual_value=2000,
                    type=1,
                    status=1,
                    stock=stock,
                    total_stock=stock,
                    validity_days=30,
                )
            )
            await session.flush()
            session.add(
                SeckillVoucher(
                    voucher_id=1,
                    begin_time=now - timedelta(hours=1),
                    end_time=now + timedelta(days=2),
                )
            )
            for offset in range(0, accounts, 1000):
                session.add_all(
                    [
                        User(id=i, phone=f"199{i:08d}", nick_name=f"压测用户{i}")
                        for i in range(offset + 1, min(offset + 1000, accounts) + 1)
                    ]
                )
                await session.flush()
    finally:
        await database.close()


async def prepare(mode, scenario, attempts, stock, port, *, local=None):
    # 3. 库名只能随机生成；CREATE不带IF NOT EXISTS，不会复用或覆盖任何已有库。
    run_id = secrets.token_hex(6)
    folder = directory(run_id)
    folder.mkdir(parents=True, exist_ok=False)
    local = local or Settings()
    settings = local.model_copy(
        update={
            **identity(run_id),
            "order_mode": mode,
            "order_admission_mode": "redis_first",
            "session_cookie_name": f"perkflow_lt_{run_id}",
            "session_cookie_secure": False,
            "llm_api_key": SecretStr(""),
            "llm_base_url": "",
            "llm_model": "",
            "llm_http_allowed_origins": [],
            "llm_extra_body": {},
            "cors_origins": [],
        }
    )
    save_settings(folder / ".env", settings)
    manifest = {
        "version": 1,
        "run_id": run_id,
        "status": "preparing",
        "mode": mode,
        "scenario": scenario,
        "attempts": attempts,
        "accounts": attempts if scenario == "distinct" else 1,
        "stock": stock,
        "port": port,
        "voucher_id": 1,
        "resources": identity(run_id),
        "created_at": datetime.now(UTC).isoformat(),
        "session_ttl_seconds": settings.login_session_ttl,
        "activity_hours": 48,
        "runtime_parameters": {
            "api_processes": 1,
            "worker_processes_expected": 1 if mode == "async" else 0,
            "worker_concurrency": settings.order_worker_concurrency,
            "mysql_pool_size_per_process": settings.mysql_pool_size,
            "mysql_max_overflow_per_process": settings.mysql_max_overflow,
            "mysql_query_timeout": settings.mysql_query_timeout,
            "redis_timeout": settings.redis_timeout,
        },
    }
    write_json(folder / "manifest.json", manifest)
    admin = create_async_engine(
        settings.database_url.set(database="mysql"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": settings.mysql_connect_timeout},
        hide_parameters=True,
    )
    store = SessionStore(settings)
    try:
        await store.redis.ping()
        async with admin.connect() as connection:
            await connection.execute(
                text(
                    f"CREATE DATABASE `{settings.mysql_database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
        await provision_fixture(settings, manifest["accounts"], stock)
        credentials = []
        # 4. 使用生产会话创建/轮换机制准备身份；不关闭验证码验证，不新增绕过认证的HTTP接口。
        from backend.app.schemas.auth import CurrentUser

        for i in range(1, manifest["accounts"] + 1):
            anonymous = await store.create_anonymous()
            login = await store.rotate(
                anonymous,
                CurrentUser(
                    id=i,
                    nick_name=f"压测用户{i}",
                    phone_masked="199****0000",
                ),
            )
            credentials.append({"user_id": i, "sid": login.sid, "csrf": login.data.csrf_token})
        write_json(folder / "credentials.json", credentials)
        if mode == "async":
            from backend.app.core.redis_orders import RedisOrderIngress
            from backend.app.core.seckill_store import SeckillStore
            from backend.app.services.redis_orders import warm

            database = Database(settings)
            try:
                await warm(
                    database,
                    RedisOrderIngress(SeckillStore(store.redis, settings.seckill_key_prefix)),
                )
            finally:
                await database.close()
        manifest["status"] = "ready"
        write_json(folder / "manifest.json", manifest)
    except BaseException:
        manifest["status"] = "preparation_failed"
        write_json(folder / "manifest.json", manifest)
        print(f"准备失败，保留本轮资源供排错，编号={run_id}；未清理或覆盖其他环境。")
        raise
    finally:
        await store.close()
        await admin.dispose()
    print(f"压测环境已准备：{run_id}（{mode}/{scenario}）")
    print(f"独立数据库：{settings.mysql_database}；测试账号={manifest['accounts']}；库存={stock}")
    print(f"API：python -m scripts.loadtest api {run_id}")
    if mode == "async":
        print(f"Worker：python -m scripts.loadtest worker {run_id}")
    print(f"Locust：python -m scripts.loadtest locust {run_id}")
    print(f"核对：python -m scripts.loadtest check {run_id}")
    return run_id


def make_app(manifest, settings):
    from fastapi.responses import JSONResponse

    from backend.app.main import create_app

    app = create_app(settings)

    @app.middleware("http")
    async def restrict_loadtest(request, call_next):
        # 5. 只在专用API包装层允许购买/回查，不向日常API添加测试入口或取消权限。
        path = request.url.path
        permitted = (
            request.method == "GET"
            and (
                path in {"/_loadtest", "/_loadtest/ready"}
                or path.startswith("/api/order-requests/")
                or path.startswith("/health/")
            )
        ) or (request.method == "POST" and path == "/api/vouchers/1/orders")
        if not permitted:
            return JSONResponse({"error": {"code": "LOADTEST_SCOPE_ONLY"}}, status_code=403)
        response = await call_next(request)
        response.headers["X-PerkFlow-Loadtest"] = manifest["run_id"]
        return response

    @app.get("/_loadtest")
    async def loadtest_identity():
        return {
            "run_id": manifest["run_id"],
            "mode": manifest["mode"],
            "database": settings.mysql_database,
        }

    @app.get("/_loadtest/ready")
    async def fresh_baseline():
        # 开始计时前核对本轮尚未被手动购买；此读SQL不放在实际购买热路径中。
        from backend.app.core.seckill_store import SeckillStore

        async with app.state.database.sessions() as session:
            stock = await session.scalar(select(Voucher.stock).where(Voucher.id == 1))
            orders = await session.scalar(select(func.count()).select_from(VoucherOrder))
            requests = await session.scalar(select(func.count()).select_from(OrderRequest))
        ready = stock == manifest["stock"] and orders == 0 and requests == 0
        if manifest["mode"] == "async":
            from backend.app.core.rabbitmq import connect_broker

            key = SeckillStore(app.state.cache_redis, settings.seckill_key_prefix).key(1)
            available = await app.state.cache_redis.hget(key, "available")
            ready = ready and available == str(manifest["stock"])
            try:
                async with connect_broker(settings) as broker:
                    queue = await broker.consumer.declare_queue(broker.queue.name, passive=True)
                    ready = ready and queue.declaration_result.consumer_count > 0
            except Exception:
                return JSONResponse({"ready": False}, status_code=503)
        return {"ready": ready}

    return app


async def inspect_run(run_id):
    manifest, settings = settings_for(run_id)
    database, redis = Database(settings), create_redis_client(settings)
    try:
        async with database.sessions() as session:
            stock = await session.scalar(select(Voucher.stock).where(Voucher.id == 1))
            orders = list(
                (
                    await session.execute(
                        select(
                            VoucherOrder.id,
                            VoucherOrder.user_id,
                            VoucherOrder.status,
                        )
                    )
                ).all()
            )
            requests = Counter(await session.scalars(select(OrderRequest.status)))
            effects = await session.scalar(
                select(func.count()).select_from(SeckillEffect).where(SeckillEffect.applied == 0)
            )
        active = [row for row in orders if row.status == "created"]
        duplicate = any(count > 1 for count in Counter(row.user_id for row in active).values())
        checks = {
            "no_duplicate_active_orders": not duplicate,
            "stock_balances_orders": stock + len(active) == manifest["stock"],
            "no_cancelled_orders": len(active) == len(orders),
            "no_pending_sql_requests": requests["pending"] == 0,
            "no_pending_effects": effects == 0,
            "same_intent_has_one_sql_request": (
                manifest["scenario"] != "same-intent" or sum(requests.values()) <= 1
            ),
        }
        redis_facts, mq = {}, {}
        if manifest["mode"] == "async":
            from backend.app.core.rabbitmq import connect_broker
            from backend.app.core.seckill_store import SeckillStore

            gate = await redis.hgetall(SeckillStore(redis, settings.seckill_key_prefix).key(1))
            documents = [
                key
                async for key in redis.scan_iter(match=settings.seckill_key_prefix + "request:*")
            ]
            pending = 0
            for offset in range(0, len(documents), 500):
                pending += sum(
                    json.loads(raw)["result"]["status"] == "pending"
                    for raw in await redis.mget(documents[offset : offset + 500])
                    if raw
                )
            held = sum(value == "held" for key, value in gate.items() if key.startswith("r:"))
            outbox = await redis.zcard(settings.seckill_key_prefix + "outbox")
            available = int(gate["available"]) if "available" in gate else None
            redis_facts = {
                "available": available,
                "held": held,
                "pending_documents": pending,
                "outbox": outbox,
            }
            checks.update(
                {
                    "redis_matches_mysql": available == stock,
                    "no_held_reservations": held == 0,
                    "no_pending_redis_requests": pending == 0,
                    "outbox_drained": outbox == 0,
                }
            )
            async with connect_broker(settings) as broker:
                declaration = await broker.consumer.declare_queue(broker.queue.name, passive=True)
                invalid = await broker.consumer.declare_queue(broker.dead_queue.name, passive=True)
                mq = {
                    "ready_messages": declaration.declaration_result.message_count,
                    "invalid_messages": invalid.declaration_result.message_count,
                }
            checks.update(
                {
                    "mq_ready_drained": mq["ready_messages"] == 0,
                    "mq_invalid_empty": mq["invalid_messages"] == 0,
                }
            )
        folder = directory(run_id)
        rows, evidence = read_attempts(folder, manifest)
        complete = len(rows) == manifest["attempts"] and {row.get("ticket") for row in rows} == set(
            range(manifest["attempts"])
        )
        expected = min(manifest["accounts"], manifest["stock"])
        report = {
            "run_id": run_id,
            "mode": manifest["mode"],
            "scenario": manifest["scenario"],
            "checked_at": datetime.now(UTC).isoformat(),
            "checks": checks,
            "integrity_pass": all(checks.values()),
            "all_attempts_recorded": complete,
            "expected_orders": expected,
            "active_orders": len(active),
            "mysql_stock": stock,
            "sql_requests": dict(requests),
            "redis": redis_facts,
            "rabbitmq": mq,
            "outcomes": dict(Counter(row["outcome"] for row in rows)),
            "observation_evidence": evidence,
            "target_orders_met": len(active) == expected,
            "note": "事后非原子采样；ready不包含unacked。仍有任务时保持Worker运行后重新核对。",
        }
        canonical = {str(row.id) for row in active}
        checks["observed_successes_exist_in_sql"] = all(
            str(row.get("order_id")) in canonical for row in rows if row["outcome"] == "succeeded"
        )
        if manifest["scenario"] == "same-intent":
            checks["same_intent_observed_one_request"] = (
                len({row["request_id"] for row in rows if row.get("request_id")}) <= 1
            )
        report["integrity_pass"] = all(checks.values())
        report["acceptance_pass"] = (
            report["integrity_pass"]
            and complete
            and evidence["valid"]
            and report["target_orders_met"]
            and all(row["outcome"] in {"succeeded", "rejected"} for row in rows)
        )
        write_json(folder / "reconciliation.json", report)
        return report
    finally:
        await database.close()
        await redis.aclose()


def start_locust(run_id, args):
    manifest, _ = settings_for(run_id)
    env = os.environ.copy()
    env["PERKFLOW_LOADTEST_RUN"] = run_id
    env["PYTHONIOENCODING"] = "utf-8"
    # 新增独立入口，默认仍使用原闭环脚本；两种方式都只能在新轮次上运行。
    execution = getattr(args, "execution", "closed-loop")
    script = "locust_purchase_decoupled.py" if execution == "decoupled" else "locust_purchase.py"
    command = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        str(ROOT / "scripts" / script),
        "--host",
        target(manifest),
        "--web-host",
        "127.0.0.1",
        "--web-port",
        "8089",
        "--csv",
        str(directory(run_id) / "locust"),
        "--html",
        str(directory(run_id) / "locust.html"),
        "--stop-timeout",
        str(args.wait + 15),
    ]
    env["PERKFLOW_LOADTEST_WAIT"] = str(args.wait)
    env["PERKFLOW_LOADTEST_POLL"] = str(args.poll)
    if execution == "decoupled":
        env["PERKFLOW_LOADTEST_OBSERVERS"] = str(args.observers)
    if args.headless:
        command += [
            "--headless",
            "-u",
            str(args.users),
            "-r",
            str(args.spawn_rate),
            "--run-time",
            f"{args.seconds}s",
        ]
    return subprocess.call(command, cwd=ROOT, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare_parser = sub.add_parser("prepare", help="新建本轮随机隔离环境；不清理旧轮次")
    prepare_parser.add_argument("--mode", choices=["sync", "async"], default="async")
    prepare_parser.add_argument(
        "--scenario", choices=["distinct", "same-intent", "same-user"], default="distinct"
    )
    prepare_parser.add_argument("--attempts", type=int, default=100)
    prepare_parser.add_argument("--stock", type=int, default=100)
    prepare_parser.add_argument("--port", type=int, default=8001)
    for action in ("api", "worker", "check", "locust"):
        entry = sub.add_parser(action)
        entry.add_argument("run_id")
        if action == "locust":
            entry.add_argument(
                "--execution",
                choices=["closed-loop", "decoupled"],
                default="closed-loop",
                help="closed-loop保留原等待结果模式；decoupled独立观察，不占购买用户",
            )
            entry.add_argument("--observers", type=int, default=5, help="分离模式的并发回查上限")
            entry.add_argument("--headless", action="store_true")
            entry.add_argument("--users", type=int, default=10)
            entry.add_argument("--spawn-rate", type=float, default=10)
            entry.add_argument("--seconds", type=int, default=120)
            entry.add_argument("--wait", type=int, default=60)
            entry.add_argument("--poll", type=float, default=1)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            if not 1 <= args.attempts <= 100000 or not 1 <= args.stock <= 100000:
                parser.error("attempts和stock必须在1至100000之间")
            if not 8001 <= args.port <= 8999:
                parser.error("专用端口必须在8001至8999之间")
            asyncio.run(prepare(args.mode, args.scenario, args.attempts, args.stock, args.port))
        else:
            manifest, settings = settings_for(args.run_id)
            if args.action == "api":
                import uvicorn

                uvicorn.run(
                    make_app(manifest, settings),
                    host="127.0.0.1",
                    port=manifest["port"],
                    access_log=False,
                )
            elif args.action == "worker":
                from backend.app.workers.orders import run

                asyncio.run(run(settings))
            elif args.action == "check":
                report = asyncio.run(inspect_run(args.run_id))
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report["acceptance_pass"] else 1
            else:
                if not (
                    1 <= args.users <= 10000
                    and 0 < args.spawn_rate <= 10000
                    and 1 <= args.seconds <= 86400
                    and 1 <= args.wait <= 600
                    and 0.1 <= args.poll <= 60
                    and 1 <= args.observers <= 100
                ):
                    parser.error("并发/启动速度/时长/轮询参数超出允许范围")
                return start_locust(args.run_id, args)
    except KeyboardInterrupt:
        print("已停止当前命令；保留数据和待办，不会删除未决购买。")
        return 130
    except Exception as error:
        # 供应商连接异常可能含密码；只输出类型，具体连接配置留在本地.env。
        print(f"压测命令未完成：{type(error).__name__}；检查本轮清单、服务和端口。")
        return 2
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
