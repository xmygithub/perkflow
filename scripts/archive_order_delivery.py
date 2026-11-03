"""冻结投递单变量实验；只读私有文件，公开允许列表不包含环境或会话凭据。"""

import argparse
import bisect
import csv
import gzip
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from scripts.loadtest_contract import ROOT, directory, write_json
from scripts.order_perf_report import distribution, json_lines

DOC = ROOT / "docs/六、购买并发压测"
VARIANTS = ["baseline", "sleep0", "batch4", "publish2"]


def csv_rows(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def loop_gaps(events):
    """离线配对串行Redis投递与其后的sleep，间隙包含SQL恢复扫描和事件循环调度。"""
    sleeps = sorted(
        (e for e in events if e["stage"] == "dispatch_sleep"), key=lambda e: e["start_ns"]
    )
    starts = [e["start_ns"] for e in sleeps]
    rows = []
    for e in events:
        if e["stage"] != "dispatch" or not e.get("request_id"):
            continue
        i = bisect.bisect_left(starts, e["end_ns"])
        if i < len(sleeps) and sleeps[i]["requested_seconds"] == 0.01:
            sleep = sleeps[i]
            rows.append(
                {
                    "request_id": e["request_id"],
                    "dispatch_ms": (e["end_ns"] - e["start_ns"]) / 1e6,
                    "scan_and_scheduling_gap_ms": (sleep["start_ns"] - e["end_ns"]) / 1e6,
                    "work_sleep_ms": (sleep["end_ns"] - sleep["start_ns"]) / 1e6,
                }
            )
    return {
        "supported_serial_trace": bool(rows),
        "rows": rows,
        "sum_seconds": {
            k: sum(r[k] for r in rows) / 1000
            for k in ["dispatch_ms", "scan_and_scheduling_gap_ms", "work_sleep_ms"]
        },
        "gap": distribution([r["scan_and_scheduling_gap_ms"] for r in rows]),
        "gap_above_1ms": sum(r["scan_and_scheduling_gap_ms"] > 1 for r in rows),
    }


def archive(summary_path, pilot_path):
    base = ROOT / ".local/order-performance"
    if any(p.resolve().parent != base.resolve() for p in [summary_path, pilot_path]):
        raise ValueError("只接受本地专项清单")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
    if any(
        sum(r["valid"] and r["delivery_variant"] == v for r in summary["runs"]) != 3
        for v in VARIANTS
    ) or not summary.get("development_unchanged"):
        raise ValueError("正式矩阵未完整收敛，禁止生成完成归档")
    target = DOC / "验证资料/2026-10-21_待办投递单变量对照"
    target.mkdir(parents=True, exist_ok=False)
    groups, profiles, rows, gaps = defaultdict(list), [], [], []
    formal_ids = {r["run_id"] for r in summary["runs"]}
    for run in summary["runs"] + pilot["runs"]:
        source = directory(run["run_id"])
        is_formal = run["run_id"] in formal_ids
        destination = target / ("正式" if is_formal else "小样本") / run["run_id"]
        destination.mkdir(parents=True)
        variant = run["delivery_variant"]
        analysis = run["analysis"]
        observations = json.loads((source / "observations.json").read_text(encoding="utf-8"))
        if is_formal:
            events = [
                e for role in ["api", "worker"] for e in json_lines(source / f"perf-{role}.jsonl")
            ]
            gap = loop_gaps(events)
            csv_rows(source / "dispatch-loop-gaps.csv", gap["rows"])
            gaps.append(
                {
                    "run_id": run["run_id"],
                    "variant": variant,
                    **{k: v for k, v in gap.items() if k != "rows"},
                }
            )
            stages = json.loads((source / "request-stages.json").read_text(encoding="utf-8"))
            owned = [e for e in events if e.get("request_id")]
            sql_update = [
                (e["end_ns"] - e["start_ns"]) / 1e6
                for e in owned
                if e["stage"] == "sql" and e["table"] == "tb_voucher" and e["operation"] == "UPDATE"
            ]
            pool = [
                (e["end_ns"] - e["start_ns"]) / 1e6
                for e in owned
                if e["stage"] == "mysql_pool_queue_wait"
            ]
            acquire = [e for e in owned if e["stage"] == "connection_acquire"]
            claims = [
                (e["end_ns"] - e["start_ns"]) / 1e6
                for e in events
                if e["stage"] in {"claim_rpc", "batch_claim_rpc"}
            ]
            confirms = [
                (e["end_ns"] - e["start_ns"]) / 1e6
                for e in owned
                if e["stage"] == "publisher_confirm"
            ]
            ack_events = [e for e in events if e["stage"] == "ack_sent" and e.get("ok")]
            publish_events = [e for e in events if e["stage"] == "publisher_confirm"]
            active_confirms = peak_confirms = 0
            for _, change in sorted(
                [(e["start_ns"], 1) for e in publish_events]
                + [(e["end_ns"], -1) for e in publish_events]
            ):
                active_confirms += change
                peak_confirms = max(peak_confirms, active_confirms)
            origin = min(e["start_wall_ns"] for e in stages)
            offset = analysis["clock"]["worker"]["offset_ns"]
            ack_seconds = Counter(
                int((e["end_wall_ns"] + offset - origin) / 1e9) for e in ack_events
            )
            csv_rows(
                source / "message-ack-series.csv",
                [
                    {"second_from_first_client": second, "message_acks": count}
                    for second, count in sorted(ack_seconds.items())
                ],
            )
            work_sleeps = [
                (e["end_ns"] - e["start_ns"]) / 1e6
                for e in events
                if e["stage"] == "dispatch_sleep" and e["requested_seconds"] == 0.01
            ]
            delivery_parts = {}
            for name in ["dispatch_claimed", "outbox_finish", "dispatch"]:
                delivery_parts[name] = distribution(
                    [(e["end_ns"] - e["start_ns"]) / 1e6 for e in events if e["stage"] == name]
                )
            # 只对同一领取工作项内嵌的确认/收尾做差；不是直接GET计时，包含调度和校验。
            other = []
            delivery_children = defaultdict(list)
            for e in owned:
                if e["stage"] in {"publisher_confirm", "outbox_finish"}:
                    delivery_children[e["request_id"]].append(e)
            for parent in [e for e in events if e["stage"] == "dispatch_claimed"]:
                children = [
                    e
                    for e in delivery_children[parent["request_id"]]
                    if parent["start_ns"] <= e["start_ns"] <= e["end_ns"] <= parent["end_ns"]
                ]
                if len(children) == 2:
                    other.append(
                        (
                            parent["end_ns"]
                            - parent["start_ns"]
                            - sum(e["end_ns"] - e["start_ns"] for e in children)
                        )
                        / 1e6
                    )
            series = json.loads((source / "queue-series.json").read_text(encoding="utf-8"))
            lock_peak = max(
                (s.get("mysql_lock_wait_tables", {}).get("tb_voucher", 0) for s in series),
                default=0,
            )
            lock_rows = [
                {"wall_ns": s["wall_ns"], "table": table, "wait_relations": count}
                for s in series
                for table, count in s.get("mysql_lock_wait_tables", {}).items()
            ]
            csv_rows(source / "lock-table-series.csv", lock_rows)
            csv_rows(source / "broker-rates.csv", analysis["rates"])
            profile = {
                "run_id": run["run_id"],
                "variant": variant,
                "valid": run["valid"],
                "stock_update": distribution(sql_update),
                "pure_pool_queue": distribution(pool),
                "pool_wait_ms_per_acquire": sum(pool) / len(acquire),
                "blocked_acquire_fraction": len(pool) / len(acquire),
                "claims": distribution(claims),
                "confirm": distribution(confirms),
                "confirm_await_peak": peak_confirms,
                "message_acks": len(ack_events),
                "last_any_publish_to_last_message_ack_seconds": (
                    max(e["end_ns"] for e in ack_events)
                    - max(e["start_ns"] for e in publish_events)
                )
                / 1e9,
                "actual_work_sleep": distribution(work_sleeps),
                "delivery_parts": delivery_parts,
                "claimed_other_including_document_and_validation": distribution(other),
                "stock_lock_relations_peak": lock_peak,
                "delivery_tasks_peak": analysis["delivery_tasks_peak"],
                "errors": {
                    k: analysis[k]
                    for k in [
                        "delivery_window_errors",
                        "nack_count",
                        "reject_count",
                        "request_span_errors",
                    ]
                },
            }
            write_json(source / "delivery-profile.json", profile)
            profiles.append(profile)
            row = {
                "run_id": run["run_id"],
                "variant": variant,
                "valid": run["valid"],
                "orders_per_second": analysis["unique_orders_per_second_full_window"],  # noqa: E501 -- evidence table
                "observed_mean_ms": sum(r["elapsed_ms"] for r in observations["attempts"])  # noqa: E501 -- evidence table
                / len(observations["attempts"]),
                "commit_mean_ms": analysis["stages"]["client_to_commit_ms"]["mean_ms"],  # noqa: E501 -- evidence table
                "first_publish_wait_ms": analysis["stages"]["admission_to_first_publish_ms"][  # noqa: E501 -- evidence table
                    "mean_ms"
                ],
                "ready_peak": analysis["ready_sampled_peak"],
                "unacked_peak": analysis["unacked_sampled_peak"],
                "unsent_peak": analysis["not_first_published_sampled_peak"],
                "redis_outbox_peak": analysis["redis_outbox_sampled_peak"],
                "redis_outbox_due_peak": analysis["redis_outbox_due_sampled_peak"],
                "ack_sent_count": len(ack_events),
                "publisher_attempts": analysis["publisher_attempts"],
                "mq_ack_rate_sampled_peak": max(
                    (s.get("ack_rate_reported") or 0 for s in series), default=0
                ),
                "first_publish_per_second": analysis[
                    "unique_first_publishes_per_second_full_window"
                ],
                "claim_rpc_count": len(claims),
                "claim_mean_ms": profile["claims"]["mean_ms"],
                "confirm_mean_ms": profile["confirm"]["mean_ms"],
                "actual_work_sleep_ms": profile["actual_work_sleep"]["mean_ms"],
                "pool_wait_ms_per_acquire": profile["pool_wait_ms_per_acquire"],
                "stock_update_mean_ms": profile["stock_update"]["mean_ms"],
                "stock_lock_relations_peak": lock_peak,
                "pool_occupancy_peak": analysis["pool_checked_out_peak"],
                "delivery_tasks_peak": analysis["delivery_tasks_peak"],
                "last_publish_to_last_ack_seconds": analysis[
                    "last_publish_to_last_ack_sent_seconds"
                ],
                "sampled_pipeline_empty_seconds": analysis[
                    "last_publish_to_stable_pipeline_empty_seconds"
                ],
            }
            rows.append(row)
            if run["valid"]:
                groups[variant].append(
                    {
                        "row": row,
                        "stages": stages,
                        "observations": observations,
                        "profile": profile,
                        "pool_samples": pool,
                        "update_samples": sql_update,
                        "confirm_samples": confirms,
                        "claim_samples": claims,
                        "sleep_samples": work_sleeps,
                    }
                )
        credentials = json.loads((source / "credentials.json").read_text(encoding="utf-8"))
        forbidden = {v.encode() for c in credentials for k, v in c.items() if k in {"sid", "csrf"}}
        names = [
            "manifest.json",
            "delivery-plan.json",
            "performance-plan.json",
            "performance-result.json",
            "performance-analysis.json",
            "delivery-profile.json",
            "observations.json",
            "attempts.jsonl",
            "submissions.jsonl",
            "queue-series.json",
            "queue-series.csv",
            "pipeline-series.json",
            "pipeline-series.csv",
            "request-stages.json",
            "request-stages.csv",
            "stage-distributions.csv",
            "reconciliation.json",
            "post-matrix-reconciliation.json",
            "settlement-checks.json",
            "perf-api.jsonl",
            "perf-worker.jsonl",
            "perf-api.meta.json",
            "perf-worker.meta.json",
            "lock-table-series.csv",
            "broker-rates.csv",
            "message-ack-series.csv",
            "dispatch-loop-gaps.csv",
            "perf-locust.html",
            "perf-locust_stats.csv",
            "perf-locust_stats_history.csv",
            "perf-locust_failures.csv",
            "perf-locust_exceptions.csv",
        ]
        names += [p.name for p in source.glob("clock-*.json")]
        for name in names:
            path = source / name
            if not path.exists():
                continue
            raw = path.read_bytes()
            if forbidden.intersection(re.findall(rb"[A-Za-z0-9_-]{20,}", raw)):
                raise ValueError("证据含测试认证值")
            compress = path.suffix == ".jsonl" or name in {"observations.json", "perf-locust.html"}
            (destination / (name + ".gz" if compress else name)).write_bytes(
                gzip.compress(raw, mtime=0) if compress else raw
            )
    aggregate = {}
    for variant, runs in groups.items():
        stages = [r for run in runs for r in run["stages"]]
        attempts = [r for run in runs for r in run["observations"]["attempts"]]
        aggregate[variant] = {
            "valid_runs": len(runs),
            "orders": len(stages),
            "mean_orders_per_second": sum(r["row"]["orders_per_second"] for r in runs) / len(runs),  # noqa: E501 -- evidence table
            "observed": distribution([r["elapsed_ms"] for r in attempts]),
            "post": distribution([r["submit_ms"] for r in attempts]),
            "stages": {
                k: distribution([r[k] for r in stages]) for k in stages[0] if k.endswith("_ms")
            },
            "stock_update": distribution([v for r in runs for v in r["update_samples"]]),  # noqa: E501 -- evidence table
            "pure_pool_queue": distribution([v for r in runs for v in r["pool_samples"]]),  # noqa: E501 -- evidence table
            "confirm": distribution([v for r in runs for v in r["confirm_samples"]]),  # noqa: E501 -- evidence table
            "claim": distribution([v for r in runs for v in r["claim_samples"]]),  # noqa: E501 -- evidence table
            "work_sleep": distribution([v for r in runs for v in r["sleep_samples"]]),  # noqa: E501 -- evidence table
            "mean_claim_rpcs": sum(r["row"]["claim_rpc_count"] for r in runs) / len(runs),  # noqa: E501 -- evidence table
            "mean_pool_wait_ms_per_acquire": sum(r["row"]["pool_wait_ms_per_acquire"] for r in runs)  # noqa: E501 -- evidence table
            / len(runs),
            "ready_peak": max(r["row"]["ready_peak"] for r in runs),
            "unacked_peak": max(r["row"]["unacked_peak"] for r in runs),
            "stock_lock_relations_peak": max(r["row"]["stock_lock_relations_peak"] for r in runs),  # noqa: E501 -- evidence table
            "max_delivery_tasks": max(r["row"]["delivery_tasks_peak"] for r in runs),  # noqa: E501 -- evidence table
            "redis_outbox_due_peak": max(r["row"]["redis_outbox_due_peak"] for r in runs),
            "redis_outbox_peak": max(r["row"]["redis_outbox_peak"] for r in runs),
            "mean_first_publish_per_second": sum(r["row"]["first_publish_per_second"] for r in runs)
            / len(runs),
            "mq_ack_rate_sampled_peak": max(r["row"]["mq_ack_rate_sampled_peak"] for r in runs),
            "total_ack_sent": sum(r["row"]["ack_sent_count"] for r in runs),
            "total_publisher_attempts": sum(r["row"]["publisher_attempts"] for r in runs),
        }
    baseline = aggregate["baseline"]
    for a in aggregate.values():
        a["throughput_change_pct"] = (
            a["mean_orders_per_second"] / baseline["mean_orders_per_second"] - 1
        ) * 100
        a["observed_reduction_pct"] = (
            1 - a["observed"]["mean_ms"] / baseline["observed"]["mean_ms"]
        ) * 100
        a["first_publish_wait_reduction_pct"] = (
            1
            - a["stages"]["admission_to_first_publish_ms"]["mean_ms"]
            / baseline["stages"]["admission_to_first_publish_ms"]["mean_ms"]
        ) * 100
    public = {
        "aggregate": aggregate,
        "runs": rows,
        "profiles": profiles,
        "development_before": summary["development_before"],
        "development_after": summary["development_after"],
        "development_unchanged": summary["development_unchanged"],
        "valid_matrix_complete": all(aggregate.get(v, {}).get("valid_runs") == 3 for v in VARIANTS),  # noqa: E501 -- evidence table
        "source_sha256": summary["source_sha256"],
    }
    write_json(target / "投递对照汇总.json", public)
    write_json(target / "投递循环间隙分析.json", gaps)
    write_json(target / "正式实验清单.json", summary)
    write_json(target / "小样本实验清单.json", pilot)
    audit = base / "delivery-final-audit.json"
    if audit.exists():
        audit_data = json.loads(audit.read_text(encoding="utf-8"))
        if {r["run_id"] for r in audit_data["runs"]} != formal_ids:
            raise ValueError("收尾审计轮次不匹配")
        shutil.copy2(audit, target / "矩阵后只读复核与运行环境.json")
    csv_rows(target / "逐轮结果.csv", rows)
    snapshots = target / "分析代码"
    snapshots.mkdir()
    for name in [
        "order_delivery_performance.py",
        "order_delivery_variants.py",
        "order_performance.py",
        "order_perf_probe.py",
        "order_perf_report.py",
        "archive_order_delivery.py",
    ]:
        shutil.copy2(ROOT / "scripts" / name, snapshots / name)
    shutil.copy2(
        ROOT / "scripts/lua/order_perf_claim_batch.lua", snapshots / "order_perf_claim_batch.lua"
    )
    shutil.copy2(
        ROOT / "backend/app/services/redis_orders.py", snapshots / "redis_orders_service.py"
    )
    shutil.copy2(
        ROOT / "tests/backend/test_order_delivery_variants.py",
        snapshots / "test_order_delivery_variants.py",
    )
    index = []
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        index.append(
            {
                "path": str(path.relative_to(target)).replace("\\", "/"),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "uncompressed_sha256": hashlib.sha256(
                    gzip.decompress(raw) if path.suffix == ".gz" else raw
                ).hexdigest(),
            }
        )
    write_json(target / "归档索引.json", index)
    lines = [
        "\n\n## 7. 正式结果\n\n每组3轮、每轮1000单；下列分位数合并3000条原始请求后nearest-rank计算。吞吐为各轮唯一提交订单批次吞吐的均值，不是最大QPS。\n",  # noqa: E501
        "\n|条件|订单/秒|相对Baseline|首次观察均值/P95/P99 秒|观察均值降幅|首次投递等待均值 秒|\n",
        "|---|---:|---:|---|---:|---:|\n",
    ]
    for v in VARIANTS:
        a = aggregate[v]
        d = a["observed"]
        lines.append(
            f"|{v}|{a['mean_orders_per_second']:.2f}|{a['throughput_change_pct']:+.2f}%|{d['mean_ms'] / 1000:.2f}/{d['p95_ms'] / 1000:.2f}/{d['p99_ms'] / 1000:.2f}|{a['observed_reduction_pct']:+.2f}%|{a['stages']['admission_to_first_publish_ms']['mean_ms'] / 1000:.2f}|\n"  # noqa: E501 -- evidence table
        )  # noqa: E501
    lines += [
        "\n|条件|claim RPC/轮，含空轮询|发布确认均值/P95 ms|工作后实际休眠均值 ms|Ready/Unacked采样峰值|纯池队列等待/每次获取 ms|库存UPDATE均值/P95 ms|库存锁关系峰值|\n",  # noqa: E501
        "|---|---:|---|---:|---|---:|---|---:|\n",
    ]
    for v in VARIANTS:
        a = aggregate[v]
        lines.append(
            f"|{v}|{a['mean_claim_rpcs']:.1f}|{a['confirm']['mean_ms']:.2f}/{a['confirm']['p95_ms']:.2f}|{a['work_sleep']['mean_ms']:.3f}|{a['ready_peak']}/{a['unacked_peak']}|{a['mean_pool_wait_ms_per_acquire']:.2f}|{a['stock_update']['mean_ms']:.2f}/{a['stock_update']['p95_ms']:.2f}|{a['stock_lock_relations_peak']}|\n"  # noqa: E501 -- evidence table
        )  # noqa: E501
    lines += [
        "\n### 逐轮结果\n\n|run_id|条件|有效|订单/秒|首次观察均值 秒|首次投递等待 秒|Ready/Unacked|库存锁关系峰值|\n",  # noqa: E501 -- evidence table
        "|---|---|---|---:|---:|---:|---|---:|\n",
    ]  # noqa: E501
    for r in rows:
        lines.append(
            f"|{r['run_id']}|{r['variant']}|{r['valid']}|{r['orders_per_second']:.2f}|{r['observed_mean_ms'] / 1000:.2f}|{r['first_publish_wait_ms'] / 1000:.2f}|{r['ready_peak']}/{r['unacked_peak']}|{r['stock_lock_relations_peak']}|\n"  # noqa: E501 -- evidence table
        )  # noqa: E501
    lines.append(
        "\n完整数据：[汇总JSON](验证资料/2026-10-21_待办投递单变量对照/投递对照汇总.json)、[逐轮CSV](验证资料/2026-10-21_待办投递单变量对照/逐轮结果.csv)、[文件索引](验证资料/2026-10-21_待办投递单变量对照/归档索引.json)。每轮含所有阶段分位数、原始事件gzip、请求阶段CSV、队列/按表锁等待/ACK速率序列、收敛核对与时钟探测。凭据与.env不归档。\n"  # noqa: E501 -- evidence table
    )  # noqa: E501
    with (DOC / "7.Redis待办投递单变量优化实验.md").open("a", encoding="utf-8") as handle:
        handle.write("".join(lines))
    print(json.dumps({"files": len(index), "complete": public["valid_matrix_complete"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("pilot", type=Path)
    args = parser.parse_args()
    archive(args.summary, args.pilot)
