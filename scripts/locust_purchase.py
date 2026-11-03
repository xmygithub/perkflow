"""单机有限购买请求集；必须由scripts.loadtest启动，不能直接指向开发API。"""

import itertools
import json
import os
import platform
import time
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from uuid import NAMESPACE_URL, uuid5

import gevent
import requests
from locust import HttpUser, events, task
from locust.exception import StopUser
from locust.runners import LocalRunner

from scripts.loadtest_contract import (
    ROOT,
    classify_response,
    directory,
    load_manifest,
    target,
    write_json,
)


class Experiment:
    def __init__(self, environment):
        self.environment = environment
        self.run_id = os.environ["PERKFLOW_LOADTEST_RUN"]
        self.folder = directory(self.run_id)
        self.manifest = load_manifest(self.run_id)
        self.host = target(self.manifest)
        self.wait = int(os.getenv("PERKFLOW_LOADTEST_WAIT", "60"))
        self.poll = float(os.getenv("PERKFLOW_LOADTEST_POLL", "1"))
        self.credentials = json.loads(
            (self.folder / "credentials.json").read_text(encoding="utf-8")
        )
        if len(self.credentials) != self.manifest["accounts"]:
            raise ValueError("测试会话数量不符合清单")
        if environment.host != self.host or not isinstance(environment.runner, LocalRunner):
            raise ValueError("只允许本轮回环地址和单机Locust，不能覆盖目标或分布式重复分配账号")
        self.tickets, self.rows = itertools.count(), []
        self.started = datetime.now(UTC).isoformat()
        self.clock = time.perf_counter()
        self.saved_count = None

    def begin(self):
        # 1. 先无凭据核对专用API身份，绝不能向错误端口发送Cookie或购买请求。
        with requests.Session() as client:
            client.trust_env = False
            response = client.get(self.host + "/_loadtest", timeout=5, allow_redirects=False)
            if response.status_code != 200 or response.json() != {
                "run_id": self.run_id,
                "mode": self.manifest["mode"],
                "database": self.manifest["resources"]["mysql_database"],
            }:
                raise ValueError("目标不是本轮压测API")
            response = client.get(self.host + "/_loadtest/ready", timeout=5, allow_redirects=False)
            if response.status_code != 200 or response.json().get("ready") is not True:
                raise ValueError("本轮初始数据已被修改或活动未预热，请重新prepare")
        # 每轮只跑一次，禁止Web界面停止后重启，把已扣库存当成新的初始状态。
        with (self.folder / "started.json").open("x", encoding="utf-8") as handle:
            json.dump({"started_at": self.started}, handle)
        self.journal = (self.folder / "attempts.jsonl").open("x", encoding="utf-8")

    def next_ticket(self):
        ticket = next(self.tickets)
        if ticket >= self.manifest["attempts"]:
            raise StopUser()
        index = ticket if self.manifest["scenario"] == "distinct" else 0
        intent = 0 if self.manifest["scenario"] == "same-intent" else ticket
        key = str(uuid5(NAMESPACE_URL, f"perkflow-loadtest:{self.run_id}:{intent}"))
        return ticket, self.credentials[index], key

    def record(self, row):
        self.rows.append(row)
        # 2. 逐条只保存无凭据事实；中断也能由日志找回已结束尝试，不保存响应原文或Cookie。
        self.journal.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.journal.flush()
        if len(self.rows) == self.manifest["attempts"]:
            # 完整有限请求集立即保存；Web进程是否退出不应影响结果可读性。
            self.finish()
            gevent.spawn_later(0.2, self.environment.runner.quit)

    def finish(self, *, close=False):
        if self.saved_count == len(self.rows):
            if close:
                self.journal.close()
            return
        self.journal.flush()
        elapsed = time.perf_counter() - self.clock
        sources = [
            "scripts/loadtest.py",
            "scripts/loadtest_contract.py",
            "scripts/locust_purchase.py",
            "backend/app/api/vouchers.py",
            "backend/app/services/vouchers.py",
            "backend/app/services/order_processing.py",
            "backend/app/core/redis_orders.py",
            "backend/app/workers/orders.py",
        ]
        sources += [
            str(path.relative_to(ROOT)).replace("\\", "/")
            for path in (ROOT / "backend/app/core/lua").glob("*.lua")
        ]
        stats = [
            {
                "method": stat.method,
                "name": stat.name,
                "requests": stat.num_requests,
                "failures": stat.num_failures,
                "average_ms": stat.avg_response_time,
                "p95_ms": stat.get_response_time_percentile(0.95),
                "p99_ms": stat.get_response_time_percentile(0.99),
            }
            for stat in self.environment.stats.entries.values()
        ]
        report = {
            "run_id": self.run_id,
            "mode": self.manifest["mode"],
            "scenario": self.manifest["scenario"],
            "started_at": self.started,
            "finished_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": elapsed,
            "planned_attempts": self.manifest["attempts"],
            "completed_attempts": len(self.rows),
            "outcomes": dict(Counter(row["outcome"] for row in self.rows)),
            "distinct_observed_order_ids": len(
                {row["order_id"] for row in self.rows if row.get("order_id")}
            ),
            "parameters": {
                "users": self.environment.parsed_options.num_users,
                "spawn_rate": self.environment.parsed_options.spawn_rate,
                "poll_seconds": self.poll,
                "observation_wait_seconds": self.wait,
            },
            "python": platform.python_version(),
            "locust": version("locust"),
            "source_sha256": {
                name: sha256((ROOT / name).read_bytes()).hexdigest() for name in sources
            },
            "http_and_business_stats": stats,
            "attempts": self.rows,
            "note": (
                "闭环有限请求集；总计混合POST/轮询/BUSINESS，不可当建单QPS。最终SQL核对见check。"
            ),
        }
        write_json(self.folder / "observations.json", report)
        self.saved_count = len(self.rows)
        if close:
            self.journal.close()
        print(
            f"购买尝试完成 {len(self.rows)}/{self.manifest['attempts']}；结果={report['outcomes']}"
        )
        print(f"数据核对：python -m scripts.loadtest check {self.run_id}")
        if len(self.rows) != self.manifest["attempts"] or any(
            row["outcome"] not in {"succeeded", "rejected"} for row in self.rows
        ):
            self.environment.process_exit_code = 1


@events.test_start.add_listener
def begin(environment, **kwargs):
    try:
        experiment = Experiment(environment)
        experiment.begin()
        environment.purchase_experiment = experiment
    except Exception as error:
        # 连接异常可能带URL/上下文，只保留安全类型；失败后不发任何购买。
        print(f"压测启动检查拒绝：{type(error).__name__}；检查专用API或为下一轮重新prepare。")
        environment.process_exit_code = 2
        gevent.spawn(environment.runner.quit)


@events.test_stop.add_listener
def stopped(environment, **kwargs):
    experiment = getattr(environment, "purchase_experiment", None)
    if experiment is not None:
        experiment.finish()


@events.quitting.add_listener
def finish(environment, **kwargs):
    experiment = getattr(environment, "purchase_experiment", None)
    if experiment is not None:
        experiment.finish(close=True)


class PurchaseUser(HttpUser):
    @task
    def purchase(self):
        experiment = getattr(self.environment, "purchase_experiment", None)
        if experiment is None:
            raise StopUser()
        ticket, credential, intent = experiment.next_ticket()
        # 3. 每个ticket只有一次POST，不因超时盲目补发；同意图场景由预定ticket明确重放。
        self.client.trust_env = False
        self.client.cookies.clear()
        self.client.cookies.set(f"perkflow_lt_{experiment.run_id}", credential["sid"])
        headers = {
            "X-CSRF-Token": credential["csrf"],
            "Origin": experiment.host,
            "Idempotency-Key": intent,
        }
        started = time.perf_counter()
        row = {
            "ticket": ticket,
            "user_id": credential["user_id"],
            "request_key": intent,
            "outcome": "error",
            "code": None,
            "request_id": None,
            "order_id": None,
            "started_at": datetime.now(UTC).isoformat(),
        }
        try:
            with self.client.post(
                "/api/vouchers/1/orders",
                headers=headers,
                timeout=15,
                allow_redirects=False,
                name="POST purchase",
                catch_response=True,
            ) as response:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                outcome, code = classify_response(
                    response.status_code, body, experiment.manifest["mode"]
                )
                if response.headers.get("X-PerkFlow-Loadtest") != experiment.run_id:
                    outcome, code = "error", "WRONG_TARGET"
                row.update(
                    outcome=outcome,
                    code=code,
                    http_status=response.status_code,
                    submit_ms=round((time.perf_counter() - started) * 1000, 3),
                )
                if outcome == "rejected":
                    response.request_meta["name"] = f"POST purchase [{code}]"
                    response.success()
                elif outcome == "error":
                    response.failure(code)
                else:
                    response.success()
                    result = body.get("request", body)
                    row["request_id"] = result.get("id") if "request" in body else None
                    row["order_id"] = (result.get("order") or {}).get("id")
            # 4. pending只查询原编号，不当成失败重买；轮询单独统计，等待占用闭环用户。
            deadline = started + experiment.wait
            while row["outcome"] == "pending" and time.perf_counter() < deadline:
                gevent.sleep(min(experiment.poll, max(0, deadline - time.perf_counter())))
                with self.client.get(
                    f"/api/order-requests/{row['request_id']}",
                    timeout=10,
                    allow_redirects=False,
                    name="GET purchase status",
                    catch_response=True,
                ) as response:
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    valid = (
                        response.status_code == 200
                        and body.get("id") == row["request_id"]
                        and response.headers.get("X-PerkFlow-Loadtest") == experiment.run_id
                    )
                    if valid:
                        outcome, code = classify_response(200, {"request": body}, "async")
                        row.update(
                            outcome=outcome, code=code, order_id=(body.get("order") or {}).get("id")
                        )
                        if outcome == "error":
                            response.failure(code)
                        else:
                            response.success()
                    else:
                        # 查询失败不能证明购买失败；继续有限回查，最终仍未知则unresolved。
                        response.failure("STATUS_LOOKUP_UNAVAILABLE")
            if row["outcome"] == "pending":
                row["outcome"], row["code"] = "unresolved", "OBSERVATION_TIMEOUT"
        except Exception as error:
            row.update(outcome="unresolved", code=type(error).__name__)
        finally:
            if row["outcome"] == "pending":
                row.update(outcome="unresolved", code="OBSERVATION_INTERRUPTED")
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            if row["outcome"] == "succeeded":
                # 独立业务样本是首次观察成功的耗时上界，不能与HTTP样本混算。
                self.environment.events.request.fire(
                    request_type="BUSINESS",
                    name="purchase observed succeeded",
                    response_time=row["elapsed_ms"],
                    response_length=0,
                    exception=None,
                    context={},
                )
            experiment.record(row)
