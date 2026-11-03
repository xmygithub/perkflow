"""购买提交与结果观察分离；原locust_purchase.py保持不变，两种模式均是有限请求集。"""

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
from gevent.queue import Queue
from locust import HttpUser, events, task
from locust.clients import HttpSession
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
        self.observer_count = int(os.getenv("PERKFLOW_LOADTEST_OBSERVERS", "5"))
        if (
            not 1 <= self.observer_count <= 100
            or not 1 <= self.wait <= 600
            or not 0.1 <= self.poll <= 60
        ):
            raise ValueError("观察参数超出允许范围")
        # 待观察队列最多等于已授权的有限ticket数，不为每个pending创建一个协程。
        self.queue = Queue(maxsize=self.manifest["attempts"])
        self.active, self.submissions, self.observers = {}, [], []
        self.closed = False
        self.first_submit = self.last_submit = None
        self.first_submit_at = self.last_submit_at = None
        self.peak_pending = 0
        self.pending_count = 0

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
            json.dump(
                {
                    "started_at": self.started,
                    "execution": "decoupled",
                    "observers": self.observer_count,
                },
                handle,
            )
        self.journal = (self.folder / "attempts.jsonl").open("x", encoding="utf-8")
        self.submission_journal = (self.folder / "submissions.jsonl").open("x", encoding="utf-8")
        self.observers = [gevent.spawn(self.observe) for _ in range(self.observer_count)]

    def next_ticket(self):
        ticket = next(self.tickets)
        if ticket >= self.manifest["attempts"]:
            raise StopUser()
        index = ticket if self.manifest["scenario"] == "distinct" else 0
        intent = 0 if self.manifest["scenario"] == "same-intent" else ticket
        key = str(uuid5(NAMESPACE_URL, f"perkflow-loadtest:{self.run_id}:{intent}"))
        return ticket, self.credentials[index], key

    def start_attempt(self, row, credential, started):
        # 2. POST发出前登记，进程正常停止时也能保留“结果未知”，绝不自动重发。
        if self.first_submit is None:
            self.first_submit = started
            self.first_submit_at = row["started_at"]
        self.active[row["ticket"]] = (row, credential, started)

    def submitted(self, row):
        if self.closed:
            return
        if row["post_completed"]:
            self.last_submit = time.perf_counter()
            self.last_submit_at = datetime.now(UTC).isoformat()
        # 提交日志保持当时pending事实；终态另写attempts，不能合并两份不同阶段的日志。
        self.submissions.append(dict(row))
        self.submission_journal.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.submission_journal.flush()
        if row["outcome"] == "pending":
            self.pending_count += 1
            self.peak_pending = max(self.peak_pending, self.pending_count)
            self.queue.put_nowait((row["ticket"], time.perf_counter() + self.poll))
        else:
            self.complete(row)

    def complete(self, row):
        if self.closed or row["ticket"] not in self.active:
            return
        _, _, started = self.active.pop(row["ticket"])
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if row["outcome"] == "succeeded":
            self.environment.events.request.fire(
                request_type="BUSINESS",
                name="purchase observed succeeded",
                response_time=row["elapsed_ms"],
                response_length=0,
                exception=None,
                context={},
            )
        self.rows.append(row)
        # 2. 逐条只保存无凭据事实；中断也能由日志找回已结束尝试，不保存响应原文或Cookie。
        self.journal.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.journal.flush()
        if len(self.rows) == self.manifest["attempts"]:
            # 完整有限请求集立即保存；Web进程是否退出不应影响结果可读性。
            self.finish()
            gevent.spawn_later(0.2, self.environment.runner.quit)

    def observe(self):
        # 3. 独立的固定数量观察协程只GET原编号；各自独占HTTP会话，避免串账号Cookie。
        with HttpSession(self.host, self.environment.events.request, user=None) as client:
            client.trust_env = False
            while not self.closed:
                ticket, due = self.queue.get()
                if ticket not in self.active:
                    continue
                row, credential, started = self.active[ticket]
                deadline = started + self.wait
                try:
                    remaining = deadline - time.perf_counter()
                    # 排队期间已经等待过的poll不再重复等待，避免人为放大观察延迟。
                    gevent.sleep(min(max(0, due - time.perf_counter()), max(0, remaining)))
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        row.update(outcome="unresolved", code="OBSERVATION_TIMEOUT")
                    else:
                        row.setdefault(
                            "first_observation_delay_ms",
                            round((time.perf_counter() - started) * 1000 - row["submit_ms"], 3),
                        )
                        client.cookies.clear()
                        client.cookies.set(f"perkflow_lt_{self.run_id}", credential["sid"])
                        with client.get(
                            f"/api/order-requests/{row['request_id']}",
                            timeout=min(10, remaining),
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
                                and isinstance(body, dict)
                                and body.get("id") == row["request_id"]
                                and response.headers.get("X-PerkFlow-Loadtest") == self.run_id
                            )
                            if valid:
                                outcome, code = classify_response(200, {"request": body}, "async")
                                if outcome == "error":
                                    # 格式异常与查询失败都不证明购买已失败，继续有限观察。
                                    response.failure(code)
                                else:
                                    row.update(
                                        outcome=outcome,
                                        code=code,
                                        order_id=(body.get("order") or {}).get("id"),
                                    )
                                    response.success()
                            else:
                                response.failure("STATUS_LOOKUP_UNAVAILABLE")
                except Exception:
                    # 故障记录在HTTP指标中，订单事实仍保留pending直到观察超时。
                    pass
                if row["outcome"] == "pending" and time.perf_counter() >= deadline:
                    row.update(outcome="unresolved", code="OBSERVATION_TIMEOUT")
                if row["outcome"] == "pending":
                    self.queue.put_nowait((ticket, time.perf_counter() + self.poll))
                else:
                    self.pending_count -= 1
                    self.complete(row)

    def stop(self, *, close=False):
        if not self.closed:
            # 4. 时间上限/手动停止不等于订单失败；中止观察并逐条保存未知，不追加POST。
            for observer in self.observers:
                observer.kill(block=False)
            for row, _, _ in list(self.active.values()):
                row.update(outcome="unresolved", code="OBSERVATION_INTERRUPTED")
                self.complete(row)
            self.closed = True
        self.finish(close=close)

    def finish(self, *, close=False):
        if self.saved_count == len(self.rows):
            if close:
                self.journal.close()
                self.submission_journal.close()
            return
        self.journal.flush()
        elapsed = time.perf_counter() - self.clock
        sources = [
            "scripts/loadtest.py",
            "scripts/loadtest_contract.py",
            "scripts/locust_purchase.py",
            "scripts/locust_purchase_decoupled.py",
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
            "execution": "decoupled",
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
                "observers": self.observer_count,
            },
            "submission_phase": {
                "started_at": self.first_submit_at,
                "finished_at": self.last_submit_at,
                "completed_posts": sum(row["post_completed"] for row in self.submissions),
                "recorded_submission_attempts": len(self.submissions),
                "pending_responses": sum(row["outcome"] == "pending" for row in self.submissions),
                "window_seconds": (
                    self.last_submit - self.first_submit if self.last_submit is not None else None
                ),
                "posts_per_second": (
                    sum(row["post_completed"] for row in self.submissions)
                    / (self.last_submit - self.first_submit)
                    if self.last_submit is not None and self.last_submit > self.first_submit
                    else None
                ),
            },
            "observation_phase": {
                "peak_pending_attempts": self.peak_pending,
                "note": "客户端待观察ticket数，不是RabbitMQ队列深度；延迟含观察调度及轮询。",
            },
            "python": platform.python_version(),
            "locust": version("locust"),
            "source_sha256": {
                name: sha256((ROOT / name).read_bytes()).hexdigest() for name in sources
            },
            "http_and_business_stats": stats,
            "attempts": self.rows,
            "note": (
                "提交与观察分离，购买用户仍等待POST；不是固定到达率。"
                "总计混合POST/轮询/BUSINESS，不可当建单QPS。最终SQL核对见check。"
            ),
        }
        write_json(self.folder / "observations.json", report)
        self.saved_count = len(self.rows)
        if close:
            self.journal.close()
            self.submission_journal.close()
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
        experiment.stop()


@events.quitting.add_listener
def finish(environment, **kwargs):
    experiment = getattr(environment, "purchase_experiment", None)
    if experiment is not None:
        experiment.stop(close=True)


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
        experiment.start_attempt(row, credential, started)
        post_completed = False
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
            post_completed = True
        except Exception as error:
            row.update(outcome="unresolved", code=type(error).__name__)
        finally:
            row["post_completed"] = post_completed
            if not post_completed and row["outcome"] in {"error", "pending"}:
                row.update(outcome="unresolved", code="SUBMISSION_INTERRUPTED")
            # 5. 只交接本次结果；pending交给观察协程，当前购买用户立即领取下一个ticket。
            experiment.submitted(row)
