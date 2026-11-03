"""真实模型与真实MQ购买闭环验收；仅--live创建独立虚构库并执行一次测试确认。"""

import argparse
import asyncio
import json
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.rabbitmq import connect_broker
from backend.app.core.redis_connection import create_redis_client
from backend.app.main import create_app
from backend.app.services.redis_orders import warm
from backend.app.workers.orders import run as run_worker
from scripts.run_backend_tests import provision
from scripts.seed_demo_catalog import load_catalog
from scripts.seed_demo_catalog import seed as seed_catalog
from scripts.seed_group_buy_rules import load_entries
from scripts.seed_group_buy_rules import seed as seed_rules


async def evaluate(output):
    local = Settings()
    token = secrets.token_hex(8)
    name, prefix = "perkflow_test_agent_buy_" + token, "perkflow:test:agent_buy:" + token + ":"
    settings = local.model_copy(
        update={
            "mysql_database": name,
            "redis_key_prefix": prefix + "auth:",
            "cache_key_prefix": prefix + "cache:",
            "seckill_key_prefix": prefix + "sale:",
            "order_id_key_prefix": prefix + "id:",
            "order_lock_key_prefix": prefix + "lock:",
            "rabbitmq_prefix": "perkflow.test.agent_buy." + token,
            "order_mode": "async",
            "order_admission_mode": "redis_first",
        }
    )
    database = Database(settings)
    admin = create_async_engine(local.database_url, isolation_level="AUTOCOMMIT")
    cache = create_redis_client(settings)
    app = create_app(settings)
    app.state.agent_model = None
    worker, created, topology = None, False, False
    report = {
        "complete": False,
        "model": settings.llm_model,
        "isolation": "random fictional MySQL, Redis prefix and MQ topology",
        "checks": {},
    }

    class Sms:
        async def send(self, phone, code):
            self.code = code  # 仅独立虚构账号，不记录或输出验证码。

    try:
        async with admin.connect() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            )
            created = True
        await provision(settings)
        await seed_catalog(database, load_catalog(), apply=True)
        await seed_rules(database, load_entries(), apply=True)
        async with app.router.lifespan_context(app):
            app.state.agent_model.ensure_configured()
            app.state.sms_sender = Sms()
            await warm(app.state.database, app.state.order_ingress, [3001])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                session = (await client.get("/api/auth/session")).json()
                headers = {"X-CSRF-Token": session["csrf_token"]}
                assert (
                    await client.post(
                        "/api/auth/code", headers=headers, json={"phone": "13800138000"}
                    )
                ).status_code == 200
                signed = (
                    await client.post(
                        "/api/auth/login",
                        headers=headers,
                        json={"phone": "13800138000", "code": app.state.sms_sender.code},
                    )
                ).json()
                headers = {"X-CSRF-Token": signed["csrf_token"]}
                conversation = (
                    await client.post("/api/assistant/conversations", headers=headers, json={})
                ).json()
                path = f"/api/assistant/conversations/{conversation['id']}"
                use_date = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
                response = await client.post(
                    path + "/turns",
                    headers=headers,
                    json={
                        "message": (
                            f"{use_date}18点，南开区两个人吃火锅，整单最多200，不接受预约。"
                            "就买暖锅里双人围炉套餐，请先展示确认内容。"
                        ),
                        "expected_version": 0,
                    },
                )
                event = json.loads(response.text.splitlines()[-1])
                assert event["type"] == "done", event.get("code")
                conversation = event["result"]["conversation"]
                proposal = conversation["state"]["purchase_proposal"]
                assert proposal and proposal["evaluation"]["voucher"]["voucher_id"] == 3001
                report["proposal_answer"] = event["result"]["answer"]
                report["checks"]["native_model_prepared_single_coupon"] = proposal["quantity"] == 1
                async with database.engine.connect() as connection:
                    count = await connection.scalar(text("SELECT COUNT(*) FROM tb_voucher_order"))
                    approvals = await connection.scalar(
                        text("SELECT COUNT(*) FROM tb_agent_purchase")
                    )
                report["checks"]["model_did_not_buy"] = count == approvals == 0
                body = {
                    "proposal_id": proposal["id"],
                    "expected_version": conversation["version"],
                    "acknowledge_unknowns": False,
                }
                pending = await client.post(path + "/purchases/confirm", headers=headers, json=body)
                assert pending.status_code == 202, pending.status_code
                first = pending.json()
                repeated = (
                    await client.post(path + "/purchases/confirm", headers=headers, json=body)
                ).json()
                report["checks"]["confirmation_returns_pending"] = first["status"] == "pending"
                report["checks"]["duplicate_confirmation_same_request"] = (
                    first["request"]["id"] == repeated["request"]["id"]
                )
                # 先观察pending，再启动实际Worker；队列/交换机前缀由本脚本随机生成。
                async with connect_broker(settings):
                    topology = True
                worker = asyncio.create_task(run_worker(settings))
                async with asyncio.timeout(25):
                    while True:
                        latest = (await client.get(path + "/purchases")).json()[0]
                        if latest["status"] in {"succeeded", "failed"}:
                            break
                        await asyncio.sleep(0.1)
                report["result"] = latest
                report["checks"]["real_worker_created_order"] = (
                    latest["status"] == "succeeded"
                    and latest["request"]["order"]["pay_value"] == 15800
                )
                follow = await client.post(
                    path + "/turns",
                    headers=headers,
                    json={
                        "message": "刚才的订单怎么样了？不要重复购买。",
                        "expected_version": conversation["version"],
                    },
                )
                follow_event = json.loads(follow.text.splitlines()[-1])
                report["checks"]["agent_observed_result"] = follow_event["type"] == "done"
                if follow_event["type"] == "done":
                    report["status_answer"] = follow_event["result"]["answer"]
                    report["checks"]["agent_observed_result"] = (
                        latest["request"]["order"]["id"] in report["status_answer"]
                        and "已创建" in report["status_answer"]
                        and "扫码" not in report["status_answer"]
                    )
                async with database.engine.connect() as connection:
                    orders = await connection.scalar(text("SELECT COUNT(*) FROM tb_voucher_order"))
                    stock = await connection.scalar(
                        text("SELECT stock FROM tb_voucher WHERE id=3001")
                    )
                report["checks"]["one_order_one_stock"] = orders == 1 and stock == 29
                report.update(order_count=orders, stock_3001=stock)
    except Exception as error:
        report["error"] = type(error).__name__  # 不打印可能含连接信息/凭据的原始异常。
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await database.close()
        if topology:
            async with connect_broker(settings) as broker:
                await broker.queue.delete(if_unused=False, if_empty=False)
                await broker.dead_queue.delete(if_unused=False, if_empty=False)
                await broker.exchange.delete(if_unused=False)
                await (
                    await broker.consumer.get_exchange(settings.rabbitmq_prefix + ".dead")
                ).delete(if_unused=False)
        keys = [key async for key in cache.scan_iter(match=prefix + "*")]
        if keys:
            await cache.delete(*keys)
        await cache.aclose()
        if created:
            assert name.startswith("perkflow_test_agent_buy_") and name.isidentifier()
            async with admin.connect() as connection:
                await connection.execute(text(f"DROP DATABASE `{name}`"))
        await admin.dispose()
        report.update(complete=True, cleaned=True, generated_at=datetime.now(UTC).isoformat())
        report["passed"] = (
            not report.get("error") and bool(report["checks"]) and all(report["checks"].values())
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="真实模型可能计费，仅在独立虚构库确认测试订单"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/五、团购选购助手/验证资料/受控购买真实闭环验收.json"),
    )
    args = parser.parse_args()
    if not args.live:
        print("Preview only. --live confirms one TEST purchase in an isolated fictional database.")
        return 0
    try:
        report = asyncio.run(evaluate(args.output))
        print(
            "Controlled purchase live checks:",
            report["checks"],
            "passed:",
            report["passed"],
            "cleaned:",
            report["cleaned"],
        )
        return 0 if report["passed"] else 1
    except Exception as error:
        print("Evaluation failed:", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
