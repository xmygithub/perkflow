"""扫描调度实验的只读汇总及允许列表归档；无效轮次不进入有效性能比较。"""

import argparse
import gzip
import hashlib
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

from scripts.loadtest_contract import ROOT, directory, write_json
from scripts.order_dispatch_performance import SOURCES, csv_rows
from scripts.order_dispatch_study import VARIANTS
from scripts.order_perf_report import distribution, json_lines

PUBLIC_FILES = (
    "manifest.json",
    "performance-plan.json",
    "delivery-plan.json",
    "performance-result.json",
    "performance-analysis.json",
    "dispatch-profile.json",
    "observations.json",
    "attempts.jsonl",
    "submissions.jsonl",
    "request-stages.json",
    "request-stages.csv",
    "stage-distributions.csv",
    "sql-scan-series.csv",
    "runtime-series.csv",
    "runtime-intervals.json",
    "message-ack-series.csv",
    "queue-series.json",
    "queue-series.csv",
    "pipeline-series.json",
    "pipeline-series.csv",
    "settlement-checks.json",
    "perf-api.jsonl",
    "perf-worker.jsonl",
    "perf-api.meta.json",
    "perf-worker.meta.json",
    "perf-locust_stats.csv",
    "perf-locust_stats_history.csv",
    "perf-locust_failures.csv",
    "perf-locust_exceptions.csv",
    "perf-locust.html",
) + tuple(
    f"clock-{role}-{edge}.json"
    for role in ("api", "worker", "client", "monitor")
    for edge in ("before", "after")
)


def runtime_intervals(events, interval_ms=250):
    """高精度采样间隔，包含采样自身开销；不把粗粒度loop.time的0当精确零延迟。"""
    samples = sorted(
        (e for e in events if e["stage"] == "runtime_sample"), key=lambda e: e["end_ns"]
    )
    intervals = [(b["end_ns"] - a["end_ns"]) / 1e6 for a, b in zip(samples, samples[1:])]
    return {
        "interval_ms": distribution(intervals),
        "positive_excess_ms": distribution([max(0, value - interval_ms) for value in intervals]),
        "note": "同进程perf_counter相邻观测间隔，含采样开销；不是精确的sleep唤醒延迟",
    }


def aggregate(runs):
    """分位数从原始请求合并计算，不平均三轮P95；吞吐报告各轮值及三轮均值。"""
    groups, rows = {}, []
    for variant in VARIANTS:
        selected = [r for r in runs if r["valid"] and r["delivery_variant"] == variant]
        if len(selected) != 3:
            raise ValueError("四条件各三轮有效证据未完成")
        values = {
            key: []
            for key in (
                "post",
                "client_to_commit",
                "first_publish_wait",
                "observed",
                "confirm",
                "scan",
                "stock_update",
            )
        }
        throughput, profiles = [], []
        for run in selected:
            folder = directory(run["run_id"])
            p = run["dispatch_profile"]
            profiles.append(p)
            throughput.append(p["orders_per_second"])
            values["post"].extend(r["submit_ms"] for r in json_lines(folder / "submissions.jsonl"))
            request_rows = json.loads((folder / "request-stages.json").read_text(encoding="utf-8"))
            values["client_to_commit"].extend(r["client_to_commit_ms"] for r in request_rows)
            values["first_publish_wait"].extend(
                r["admission_to_first_publish_ms"] for r in request_rows
            )
            observations = json.loads((folder / "observations.json").read_text(encoding="utf-8"))
            values["observed"].extend(r["elapsed_ms"] for r in observations["attempts"])
            worker_events = json_lines(folder / "perf-worker.jsonl")
            intervals = runtime_intervals(worker_events)
            write_json(folder / "runtime-intervals.json", {"worker": intervals})
            queue_series = json.loads((folder / "queue-series.json").read_text(encoding="utf-8"))
            lock_samples = [
                r["mysql_lock_wait_tables"].get("tb_voucher", 0)
                for r in queue_series
                if "mysql_lock_wait_tables" in r
            ]
            edges = [(e["start_ns"], 1) for e in worker_events if e["stage"] == "publisher_confirm"]
            edges += [(e["end_ns"], -1) for e in worker_events if e["stage"] == "publisher_confirm"]
            active = confirm_peak = 0
            for _, delta in sorted(edges):
                active += delta
                confirm_peak = max(confirm_peak, active)
            for event in worker_events:
                value = (event["end_ns"] - event["start_ns"]) / 1e6
                if event["stage"] == "publisher_confirm":
                    values["confirm"].append(value)
                elif event["stage"] == "sql_recovery_scan" and event.get("ok"):
                    values["scan"].append(value)
                elif (
                    event["stage"] == "sql"
                    and event.get("table") == "tb_voucher"
                    and event.get("operation") == "UPDATE"
                ):
                    values["stock_update"].append(value)
            a = run["analysis"]
            rows.append(
                {
                    "run_id": run["run_id"],
                    "variant": variant,
                    "orders_per_second": p["orders_per_second"],
                    "post_mean_ms": p["post"]["mean_ms"],
                    "post_p95_ms": p["post"]["p95_ms"],
                    "post_per_second": p["submission_phase"]["posts_per_second"],
                    "client_to_commit_mean_ms": p["client_to_commit"]["mean_ms"],
                    "client_to_commit_p95_ms": p["client_to_commit"]["p95_ms"],
                    "first_publish_wait_mean_ms": p["first_publish_wait"]["mean_ms"],
                    "observed_mean_ms": p["first_observed_success"]["mean_ms"],
                    "scan_count": p["scan_count"],
                    "scan_worked": p["scan_worked"],
                    "scan_in_order_window_seconds": p["scan_in_order_window_seconds"],
                    "pool_wait_per_acquire_ms": p["pool_wait_per_acquire_ms"],
                    "stock_update_mean_ms": p["stock_update"]["mean_ms"],
                    "ready_peak": p["ready_sampled_peak"],
                    "unacked_peak": p["unacked_sampled_peak"],
                    "callbacks_peak": p["consumer_callbacks_peak"],
                    "publish_tasks_peak": p["publish_tasks_peak"],
                    "confirm_await_peak": confirm_peak,
                    "stock_lock_relations_sampled_peak": max(lock_samples, default=None),
                    "ack_messages": p["ack_messages"],
                    "unique_orders": p["unique_successes"],
                    "queue_clear_seconds": p["last_publish_to_empty_seconds"],
                    "worker_loop_lag_p95_ms": p["runtime"]["worker"]["load_loop_lag"].get("p95_ms"),
                    "worker_sample_interval_p95_ms": intervals["interval_ms"].get("p95_ms"),
                    "worker_sample_excess_p95_ms": intervals["positive_excess_ms"].get("p95_ms"),
                    "worker_cpu_one_core_mean": p["runtime"]["worker"][
                        "load_cpu_percent_one_core_mean"
                    ],
                    "pool_peak": a["pool_checked_out_peak"],
                }
            )
        groups[variant] = {
            "runs": [r["run_id"] for r in selected],
            "orders": sum(p["unique_successes"] for p in profiles),
            "orders_per_second": throughput,
            "orders_per_second_mean": statistics.mean(throughput),
            "distributions": {key: distribution(v) for key, v in values.items()},
            "pool_wait_per_acquire_mean_ms": statistics.mean(
                p["pool_wait_per_acquire_ms"] for p in profiles
            ),
            "scan_worked": sum(p["scan_worked"] for p in profiles),
            "scan_count": sum(p["scan_count"] for p in profiles),
            "ack_messages": sum(p["ack_messages"] for p in profiles),
        }
    base = groups[VARIANTS[0]]["orders_per_second_mean"]
    references = dict(
        zip(VARIANTS, (VARIANTS[0], VARIANTS[0], VARIANTS[1], VARIANTS[2]), strict=True)
    )
    for key, value in groups.items():
        reference = references[key]
        value["throughput_change_vs_baseline_percent"] = 100 * (
            value["orders_per_second_mean"] / base - 1
        )
        value["single_variable_reference"] = reference
        value["throughput_change_vs_reference_percent"] = 100 * (
            value["orders_per_second_mean"] / groups[reference]["orders_per_second_mean"] - 1
        )
    return groups, rows


def archive(summary_path, pilot_path, label):
    private = (ROOT / ".local/order-performance").resolve()
    if any(p.resolve().parent != private for p in (summary_path, pilot_path)):
        raise ValueError("只接受私有专项清单")
    if not label or any(c in label for c in "/\\:") or label in {".", ".."}:
        raise ValueError("非法归档标签")
    summary, pilot = [json.loads(p.read_text(encoding="utf-8")) for p in (summary_path, pilot_path)]
    if (
        summary["study"] != "scan-v1"
        or summary["mode"] != "suite"
        or not summary.get("development_unchanged")
    ):
        raise ValueError("正式矩阵或开发数据核对尚未通过")
    if (
        pilot["mode"] != "pilot"
        or not pilot.get("development_unchanged")
        or not all(r["valid"] for r in pilot["runs"])
    ):
        raise ValueError("试运行未通过")
    groups, rows = aggregate(summary["runs"])
    audit_path = summary_path.with_name(summary_path.stem + "-audit.json")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if (
        audit.get("suite") != summary_path.name
        or not audit.get("all_clean")
        or not audit.get("development_unchanged")
        or not audit.get("source_matches")
        or not all(audit.get("preserved_sources", {}).values())
    ):
        raise ValueError("矩阵后只读复查或原脚本保留检查未通过")
    destination = ROOT / "docs/六、购买并发压测/验证资料" / label
    destination.mkdir(parents=True, exist_ok=False)
    # 1. SID/CSRF及本机长凭据只用来阻止泄漏，不写入归档；配置/凭据文件不在允许列表。
    from backend.app.core.config import Settings

    local = Settings()
    secrets = [
        getattr(local, k).get_secret_value().encode()
        for k in ("mysql_password", "redis_password", "rabbitmq_password", "llm_api_key")
    ]
    index, session_tokens = [], set()
    for run in summary["runs"] + pilot["runs"]:
        credentials = json.loads(
            (directory(run["run_id"]) / "credentials.json").read_text(encoding="utf-8")
        )
        for credential in credentials:
            for key in ("sid", "csrf"):
                token = credential[key].encode()
                if re.fullmatch(rb"[A-Za-z0-9_-]{20,256}", token) is None:
                    raise ValueError("测试会话令牌格式不符合允许列表，停止公开")
                session_tokens.add(token)

    def save(path, data):
        if any(value and len(value) >= 8 and value in data for value in secrets):
            raise ValueError("归档发现潜在凭据，停止公开")
        if any(m.group() in session_tokens for m in re.finditer(rb"[A-Za-z0-9_-]{20,256}", data)):
            raise ValueError("归档发现测试会话凭据，停止公开")
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = data
        if len(data) > 150000:
            path = path.with_suffix(path.suffix + ".gz")
            data = gzip.compress(data, mtime=0)
        path.write_bytes(data)
        index.append(
            {
                "path": path.relative_to(destination).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    for phase, collection in (("正式", summary["runs"]), ("小样本", pilot["runs"])):
        for run in collection:
            folder = directory(run["run_id"])
            for name in PUBLIC_FILES:
                path = folder / name
                if path.exists():
                    save(destination / phase / run["run_id"] / name, path.read_bytes())
    for name in SOURCES + [
        "scripts/archive_order_dispatch.py",
        "tests/backend/test_order_dispatch_study.py",
    ]:
        save(destination / "源码" / name, (ROOT / name).read_bytes())
    write_json(destination / "分组汇总.json", groups)
    csv_rows(destination / "逐轮结果.csv", rows)
    public = {
        "created_at": datetime.now().astimezone().isoformat(),
        "study": summary["study"],
        "mode": summary["mode"],
        "valid_rounds": sum(r["valid"] for r in summary["runs"]),
        "invalid_rounds": [
            {
                "run_id": r["run_id"],
                "error": r.get("error"),
                "analysis_error": r.get("analysis_error"),
            }
            for r in summary["runs"]
            if not r["valid"]
        ],
        "source_sha256": summary["source_sha256"],
        "development_unchanged": True,
        "development_fingerprint": summary["development_before"]["aggregate_sha256"],
        "rabbitmq_version": summary["rabbitmq_version"],
        "pilot_rounds": len(pilot["runs"]),
    }
    write_json(destination / "实验清单.json", public)
    runtime = private / "dispatch-runtime-20261031.json"
    if runtime.exists():
        save(destination / "运行时版本.json", runtime.read_bytes())
    save(destination / "矩阵后最终复查.json", audit_path.read_bytes())
    # 2. 生成的汇总与逐轮CSV也纳入最终索引，索引自身不递归计算自身哈希。
    complete_index = []
    for path in sorted(p for p in destination.rglob("*") if p.is_file()):
        data = path.read_bytes()
        raw = gzip.decompress(data) if path.suffix == ".gz" else data
        complete_index.append(
            {
                "path": path.relative_to(destination).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    write_json(destination / "归档索引.json", complete_index)
    print(f"公开归档：{destination}")
    return destination


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("pilot", type=Path)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    archive(args.summary, args.pilot, args.label)


if __name__ == "__main__":
    main()
