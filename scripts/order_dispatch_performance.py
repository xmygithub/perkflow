"""扫描调度隔离专项：先pilot，再固定负载下四条件各三轮；原压测入口继续保留。"""

import argparse
import asyncio
import csv
import hashlib
import json
import statistics
import sys
import time
from collections import Counter

from scripts.loadtest_contract import ROOT, directory, write_json
from scripts.order_dispatch_study import VARIANTS
from scripts.order_perf_report import distribution, json_lines
from scripts.order_performance import PERF_ROOT, development_fingerprint, execute, management

SOURCES = [
    "scripts/order_dispatch_study.py",
    "scripts/order_dispatch_performance.py",
    "scripts/order_performance.py",
    "scripts/order_perf_probe.py",
    "scripts/order_perf_report.py",
    "scripts/order_delivery_variants.py",
    "scripts/locust_purchase.py",
    "scripts/locust_purchase_decoupled.py",
    "backend/app/workers/orders.py",
    "backend/app/services/order_processing.py",
    "backend/app/services/redis_orders.py",
    "backend/app/core/lua/redis_order_admit.lua",
]


def csv_rows(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def profile(folder, result):
    """原阶段分析不改，补扫描、进程、实际ACK和入口口径；缺测项保留为缺测。"""
    events = [e for role in ("api", "worker") for e in json_lines(folder / f"perf-{role}.jsonl")]
    submissions = json_lines(folder / "submissions.jsonl")
    observations = json.loads((folder / "observations.json").read_text(encoding="utf-8"))
    stages = json.loads((folder / "request-stages.json").read_text(encoding="utf-8"))
    first_client = min(s["start_wall_ns"] for s in stages)
    last_commit = max(s["commit_wall_ns"] for s in stages)
    worker_offset = result["analysis"]["clock"]["worker"]["offset_ns"]
    scans = [e for e in events if e["stage"] == "sql_recovery_scan"]
    completed_scans = [e for e in scans if e.get("ok")]

    def elapsed(e):
        return (e["end_ns"] - e["start_ns"]) / 1e6

    errors = Counter(
        e.get("error", "unknown")
        for e in scans
        if not e.get("ok") and e.get("error") != "CancelledError"
    )
    acquire = [e for e in events if e["stage"] == "connection_acquire"]
    pool = [e for e in events if e["stage"] == "mysql_pool_queue_wait"]
    runtime = [e for e in events if e["stage"] == "runtime_sample"]
    a = result["analysis"]
    summary = {
        "run_id": result["run_id"],
        "variant": result["delivery_variant"],
        "post": distribution([s["submit_ms"] for s in submissions]),
        "post_http_status": dict(Counter(str(s["http_status"]) for s in submissions)),
        "submitted": len(submissions),
        "unique_successes": a["unique_committed_orders"],
        "orders_per_second": a["unique_orders_per_second_full_window"],
        "submission_phase": observations.get("submission_phase"),
        "client_to_commit": a["stages"]["client_to_commit_ms"],
        "admission_to_commit": a["stages"]["admission_to_commit_ms"],
        "first_publish_wait": a["stages"]["admission_to_first_publish_ms"],
        "first_observed_success": distribution([r["elapsed_ms"] for r in observations["attempts"]]),
        "scan_count": len(completed_scans),
        "scan_worked": sum(bool(e.get("worked")) for e in completed_scans),
        "scan_elapsed": distribution([elapsed(e) for e in completed_scans]),
        "scan_total_seconds": sum(elapsed(e) for e in completed_scans) / 1000,
        "scan_in_order_window_seconds": sum(
            max(
                0,
                min(e["end_wall_ns"] + worker_offset, last_commit)
                - max(e["start_wall_ns"] + worker_offset, first_client),
            )
            for e in completed_scans
        )
        / 1e9,
        "scan_errors": dict(errors),
        "scan_retry_count": sum(e["stage"] == "sql_scan_retry" for e in events),
        "scan_sql": distribution(
            [elapsed(e) for e in events if e["stage"] == "sql" and e.get("scan_id")]
        ),
        "pool_wait_per_acquire_ms": sum(elapsed(e) for e in pool) / max(1, len(acquire)),
        "pool_wait": distribution([elapsed(e) for e in pool]),
        "stock_update": distribution(
            [
                elapsed(e)
                for e in events
                if e["stage"] == "sql"
                and e.get("operation") == "UPDATE"
                and e.get("table") == "tb_voucher"
            ]
        ),
        "ack_messages": sum(e["stage"] == "ack_sent" and e.get("ok", False) for e in events),
        "consumer_callbacks_peak": a["active_callbacks_peak"],
        "transactions_peak": a["active_transactions_peak"],
        "publish_tasks_peak": a["delivery_tasks_peak"],
        "pool_checked_out_peak": a["pool_checked_out_peak"],
        "ready_sampled_peak": a["ready_sampled_peak"],
        "unacked_sampled_peak": a["unacked_sampled_peak"],
        "last_publish_to_empty_seconds": a.get("last_publish_to_stable_pipeline_empty_seconds"),
        "unique_first_publish_per_second": a["unique_first_publishes_per_second_full_window"],
        "runtime": {},
    }
    for role in ("api", "worker"):
        subset = [e for e in runtime if e["role"] == role]
        offset = a["clock"][role]["offset_ns"]
        load = [e for e in subset if first_client <= e["end_wall_ns"] + offset <= last_commit]
        summary["runtime"][role] = {
            "loop_lag": distribution([e["loop_lag_ms"] for e in subset]),
            "cpu_percent_one_core_mean": statistics.mean(
                [e["cpu_percent_one_core"] for e in subset]
            )
            if subset
            else None,
            "rss_peak_bytes": max((e["rss_bytes"] for e in subset), default=None),
            "load_loop_lag": distribution([e["loop_lag_ms"] for e in load]),
            "load_cpu_percent_one_core_mean": statistics.mean(
                [e["cpu_percent_one_core"] for e in load]
            )
            if load
            else None,
        }
    csv_rows(
        folder / "sql-scan-series.csv",
        [
            {
                k: e.get(k)
                for k in [
                    "start_ns",
                    "end_ns",
                    "start_wall_ns",
                    "end_wall_ns",
                    "scan_id",
                    "ok",
                    "worked",
                    "error",
                ]
            }
            for e in scans
        ],
    )
    csv_rows(folder / "runtime-series.csv", runtime)
    csv_rows(
        folder / "message-ack-series.csv",
        [
            {"second_from_first_client": key, "message_acks": count}
            for key, count in sorted(
                Counter(
                    int((e["end_wall_ns"] + a["clock"]["worker"]["offset_ns"] - first_client) / 1e9)
                    for e in events
                    if e["stage"] == "ack_sent" and e.get("ok")
                ).items()
            )
        ],
    )
    write_json(folder / "dispatch-profile.json", summary)
    return summary


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def verified_pilot():
    for path in sorted(PERF_ROOT.glob("dispatch-pilot-*.json"), reverse=True):
        saved = json.loads(path.read_text(encoding="utf-8"))
        if (
            saved.get("study") == "scan-v1"
            and saved.get("source_sha256") == source_hashes()
            and saved.get("development_unchanged")
            and len(saved.get("runs", [])) == 7
            and all(r["valid"] for r in saved["runs"])
        ):
            return path.name
    raise ValueError("尚无同源码版本的完整有效小样本，不能开始正式矩阵")


async def run(mode, resume=None):
    from backend.app.core.config import Settings

    # 1. MQ未就绪不创建压测库或发起购买。原开发库只读聚合，不修改本地.env。
    local = Settings()
    pilot_name = verified_pilot() if mode == "suite" else None
    with management(local) as client:
        response = client.get("/api/overview")
        response.raise_for_status()
        mq_version = response.json()["rabbitmq_version"]
    PERF_ROOT.mkdir(exist_ok=True, parents=True)
    if resume:
        path = (PERF_ROOT / resume).resolve()
        if path.parent != PERF_ROOT.resolve() or not path.name.startswith("dispatch-"):
            raise ValueError("只允许恢复当前私有扫描专项清单")
        summary = json.loads(path.read_text(encoding="utf-8"))
        if summary["mode"] != mode or summary["source_sha256"] != source_hashes():
            raise ValueError("阶段或源码变化，必须使用新实验，不混合不同版本")
        if any(not r["valid"] for r in summary["runs"]):
            raise ValueError("清单包含失败轮次，请保留并开启新矩阵，不跳过失败冒充完成")
    else:
        path = PERF_ROOT / f"dispatch-{mode}-{time.time_ns()}.json"
        summary = {
            "study": "scan-v1",
            "mode": mode,
            "prefetch": 20,
            "mysql_pool": [5, 5],
            "development_before": await development_fingerprint(),
            "runs": [],
            "source_sha256": source_hashes(),
            "rabbitmq_version": mq_version,
            "pilot_evidence": pilot_name,
        }
    # 2. 开关观测交错对照；正式轮换顺序。每轮prepare新资源，失败保留证据并停止。
    cases = (
        [("scan_baseline", 100, probe) for probe in (False, True, False, True)]
        + [(v, 100, True) for v in VARIANTS[1:]]
        if mode == "pilot"
        else [
            (v, 1000, True)
            for block in (
                VARIANTS,
                tuple(reversed(VARIANTS)),
                (VARIANTS[2], VARIANTS[0], VARIANTS[3], VARIANTS[1]),
            )
            for v in block
        ]
    )
    write_json(path, summary)
    print(f"专项清单：{path}", flush=True)
    try:
        for variant, attempts, probe in cases[len(summary["runs"]) :]:
            result = await execute(
                20, attempts, probe, f"dispatch-{mode}", variant, study="scan-v1"
            )
            if result["valid"] and probe:
                try:
                    result["dispatch_profile"] = profile(directory(result["run_id"]), result)
                    p = result["dispatch_profile"]
                    if (
                        p["scan_errors"]
                        or p["scan_retry_count"]
                        or p["submitted"] != attempts
                        or not p["scan_count"]
                        or any(
                            not p["runtime"][role]["loop_lag"]["count"]
                            for role in ("api", "worker")
                        )
                    ):
                        result["valid"] = False
                        result["dispatch_metrics_invalid"] = True
                except Exception as exc:
                    result["profile_error"] = type(exc).__name__
                    result["valid"] = False
            summary["runs"].append(result)
            write_json(directory(result["run_id"]) / "performance-result.json", result)
            write_json(path, summary)
            if not result["valid"]:
                print(f"本轮无效，保留数据并停止：{result['run_id']}", flush=True)
                return False
            print(
                json.dumps(
                    {
                        "run_id": result["run_id"],
                        "variant": variant,
                        "orders_per_second": result["analysis"].get(
                            "unique_orders_per_second_full_window"
                        ),
                    }
                ),
                flush=True,
            )
            await asyncio.sleep(2)
        return True
    finally:
        summary["development_after"] = await development_fingerprint()
        summary["development_unchanged"] = (
            summary["development_before"] == summary["development_after"]
        )
        write_json(path, summary)
        if not summary["development_unchanged"]:
            raise ValueError("开发数据聚合发生变化，禁止继续发压或声明完整通过")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pilot", "suite"))
    parser.add_argument("--resume")
    args = parser.parse_args()
    try:
        return 0 if asyncio.run(run(args.mode, args.resume)) else 1
    except Exception as exc:
        print(f"扫描专项未完成：{type(exc).__name__}（不输出配置或凭据）", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
