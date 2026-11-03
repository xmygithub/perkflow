"""复用原分离场景，仅追加测试前后时钟校准；原两个Locust脚本保持不变。"""

import json
import os
import time

from locust import events
from redis import Redis

from scripts.loadtest import settings_for
from scripts.loadtest_contract import directory
from scripts.locust_purchase_decoupled import PurchaseUser  # noqa: F401


def clock(label):
    run_id = os.environ["PERKFLOW_LOADTEST_RUN"]
    _, settings = settings_for(run_id)
    samples = []
    with Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password.get_secret_value() or None,
        socket_timeout=2,
    ) as client:
        for _ in range(9):
            wall, mono = time.time_ns(), time.perf_counter_ns()
            seconds, micros = client.time()
            end, end_mono = time.time_ns(), time.perf_counter_ns()
            samples.append(
                {
                    "offset_ns": seconds * 10**9 + micros * 1000 - (wall + end) // 2,
                    "rtt_ns": end_mono - mono,
                    "wall_step_ns": (end - wall) - (end_mono - mono),
                }
            )
    (directory(run_id) / f"clock-client-{label}.json").write_text(
        json.dumps(
            {
                "probes": samples,
                "best": min(samples, key=lambda row: row["rtt_ns"]),
                "wall_ns": time.time_ns(),
            }
        ),
        encoding="utf-8",
    )


@events.init.add_listener
def before(environment, **kwargs):
    clock("before")


@events.quitting.add_listener
def after(environment, **kwargs):
    clock("after")
