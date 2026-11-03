"""专项结束后冻结公开证据并生成汇总；不连接服务、不修改历史原始事件或凭据。"""

import argparse
import csv
import gzip
import hashlib
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

from scripts.loadtest_contract import ROOT, directory, write_json
from scripts.order_perf_report import analyze, distribution, json_lines

DOC = ROOT / "docs" / "六、购买并发压测"


def archive(summary_path):
    summary_path = summary_path.resolve()
    if summary_path.parent != (ROOT / ".local/order-performance").resolve():
        raise ValueError("只能归档专项私有清单")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    target = DOC / "验证资料" / "2026-10-21_Prefetch分阶段专项"
    target.mkdir(parents=True, exist_ok=False)
    indexed, flattened, grouped, profiles = [], [], defaultdict(list), []
    for run in summary["runs"]:
        source = directory(run["run_id"])
        # 原始错误分析保留，统一用当前已验证的关联算法复算，不重新购买或修改SQL事实。
        old = source / "performance-analysis.json"
        original = source / "performance-analysis.before-common-analysis.json"
        if old.exists() and not original.exists():
            shutil.copy2(old, original)
        analysis = analyze(source)
        observations = json.loads((source / "observations.json").read_text(encoding="utf-8"))
        stats = {row["method"]: row for row in observations["http_and_business_stats"]}
        requests = json.loads((source / "request-stages.json").read_text(encoding="utf-8"))
        events = json_lines(source / "perf-worker.jsonl")
        sleeps = [
            (r["end_ns"] - r["start_ns"]) / 1e6
            for r in events
            if r["stage"] == "dispatch_sleep" and r["requested_seconds"] == 0.01
        ]
        actual_sql = [r for r in events if r["stage"] == "sql" and r.get("request_id")]
        order_update = [
            (r["end_ns"] - r["start_ns"]) / 1e6
            for r in actual_sql
            if r["table"] == "tb_voucher" and r["operation"] == "UPDATE"
        ]
        profile = {
            "run_id": run["run_id"],
            "prefetch": run["prefetch"],
            "valid": run["valid"],
            "actual_work_sleep": distribution(sleeps),
            "stock_update": distribution(order_update),
            "sql_calls_per_admitted_request": len(actual_sql) / 1000,
            "message_publications": analysis["publisher_attempts"],
            "unique_committed_orders": analysis["unique_committed_orders"],
            "stages": analysis["stages"],
            "final_mq": run.get("final_mq"),
        }
        callbacks = [e for e in events if e["stage"] == "consumer"]
        if callbacks:
            profile["active_callbacks_time_average"] = sum(
                e["end_ns"] - e["start_ns"] for e in callbacks
            ) / (max(e["end_ns"] for e in callbacks) - min(e["start_ns"] for e in callbacks))
        profiles.append(profile)
        write_json(source / "supplemental-profile.json", profile)
        if analysis["rates"]:
            with (source / "broker-rates.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, list(analysis["rates"][0]))
                writer.writeheader()
                writer.writerows(analysis["rates"])
        row = {
            "run_id": run["run_id"],
            "prefetch": run["prefetch"],
            "valid": run["valid"],
            "post_mean_ms": stats["POST"]["average_ms"],
            "post_p95_ms": stats["POST"]["p95_ms"],
            "post_p99_ms": stats["POST"]["p99_ms"],
            "observed_mean_ms": stats["BUSINESS"]["average_ms"],
            "commit_mean_ms": analysis["stages"]["client_to_commit_ms"]["mean_ms"],
            "orders_per_second": analysis["unique_orders_per_second_full_window"],
            "ready_peak": analysis["ready_sampled_peak"],
            "unacked_peak": analysis["unacked_sampled_peak"],
            "callbacks_peak": analysis["active_callbacks_peak"],
            "transactions_peak": analysis["active_transactions_peak"],
            "connections_peak": analysis["pool_checked_out_peak"],
            "unsent_peak": analysis["not_first_published_sampled_peak"],
            "publish_per_second": analysis["unique_first_publishes_per_second_full_window"],
            "lock_waits_peak": analysis["mysql_lock_waits_sampled_peak"],
            "last_publish_to_ack_seconds": analysis["last_publish_to_last_ack_sent_seconds"],
            "sampled_pipeline_drain_seconds": analysis[
                "last_publish_to_stable_pipeline_empty_seconds"
            ],
        }
        flattened.append(row)
        if run["valid"]:
            grouped[run["prefetch"]].append(
                {
                    "run": row,
                    "requests": requests,
                    "observations": observations,
                    "analysis": analysis,
                    "profile": profile,
                }
            )
        destination = target / run["run_id"]
        destination.mkdir()
        # 明确允许列表，避免把.env、credentials或任意临时文件拷进仓库。
        names = [
            "manifest.json",
            "performance-plan.json",
            "performance-result.json",
            "performance-analysis.json",
            "performance-analysis.original.json",
            "performance-analysis.before-common-analysis.json",
            "supplemental-profile.json",
            "broker-rates.csv",
            "request-stages.json",
            "request-stages.csv",
            "stage-distributions.csv",
            "pipeline-series.json",
            "pipeline-series.csv",
            "queue-series.json",
            "queue-series.csv",
            "settlement-checks.json",
            "reconciliation.json",
            "effect-inspection.json",
            "perf-api.meta.json",
            "perf-worker.meta.json",
        ]
        names += [
            f"clock-{role}-{edge}.json"
            for role in ["api", "worker", "client", "monitor"]
            for edge in ["before", "after"]
        ]
        names += [
            "perf-locust_stats.csv",
            "perf-locust_stats_history.csv",
            "perf-locust_failures.csv",
            "perf-locust_exceptions.csv",
        ]
        names += [
            "perf-api.jsonl",
            "perf-worker.jsonl",
            "observations.json",
            "attempts.jsonl",
            "submissions.jsonl",
            "perf-locust.html",
        ]
        credentials = json.loads((source / "credentials.json").read_text(encoding="utf-8"))
        forbidden = [
            value.encode()
            for row in credentials
            for key, value in row.items()
            if key in {"sid", "csrf"}
        ]
        for name in names:
            path = source / name
            if not path.exists():
                continue
            raw = path.read_bytes()
            if set(forbidden).intersection(re.findall(rb"[A-Za-z0-9_-]{20,}", raw)):
                raise ValueError("公开证据包含会话凭据，拒绝归档")
            compress = path.suffix == ".jsonl" or name in {"observations.json", "perf-locust.html"}
            output = destination / (name + ".gz" if compress else name)
            output.write_bytes(gzip.compress(raw, mtime=0) if compress else raw)
            indexed.append(
                {
                    "path": str(output.relative_to(target)).replace("\\", "/"),
                    "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": output.stat().st_size,
                }
            )
    # 试运行原始事实保留，尤其是编码/收尾失败和两版采集开销，均不混入正式分位数。
    trials = [
        "de271b9c0477",
        "965e8d0cfd79",
        "f9b5f7bebc2d",
        "15679247cb99",
        "b20306d3eac5",
        "025d8033e02d",
        "c60a834a8b24",
        "f3da404cb7b9",
        "06e4371d91ac",
        "52441c3e55a2",
        "3d0e687c59b3",
    ]
    for identifier in trials:
        source = directory(identifier)
        destination = target / "试运行" / identifier
        destination.mkdir(parents=True)
        names = [
            "manifest.json",
            "performance-plan.json",
            "performance-result.json",
            "performance-analysis.json",
            "observations.json",
            "reconciliation.json",
            "queue-series.json",
            "perf-api.meta.json",
            "perf-worker.meta.json",
            "perf-api.jsonl",
            "perf-worker.jsonl",
            "attempts.jsonl",
            "submissions.jsonl",
        ]
        names += [p.name for p in source.glob("clock-*.json")]
        credentials = json.loads((source / "credentials.json").read_text(encoding="utf-8"))
        forbidden = [v.encode() for c in credentials for k, v in c.items() if k in {"sid", "csrf"}]
        for name in names:
            path = source / name
            if not path.exists():
                continue
            raw = path.read_bytes()
            if set(forbidden).intersection(re.findall(rb"[A-Za-z0-9_-]{20,}", raw)):
                raise ValueError("试运行证据含会话凭据")
            compressed = path.suffix == ".jsonl" or name == "observations.json"
            output = destination / (name + ".gz" if compressed else name)
            output.write_bytes(gzip.compress(raw, mtime=0) if compressed else raw)
            indexed.append(
                {
                    "path": str(output.relative_to(target)).replace("\\", "/"),
                    "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": output.stat().st_size,
                }
            )
    clock = ROOT / ".local/order-performance/clock-implementation.json"
    shutil.copy2(clock, target / clock.name)
    aggregate = {}
    for prefetch, runs in grouped.items():
        records = [r for run in runs for r in run["requests"]]
        attempts = [r for run in runs for r in run["observations"]["attempts"]]
        fields = [k for k in records[0] if k.endswith("_ms")]
        aggregate[prefetch] = {
            "valid_runs": len(runs),
            "orders": len(records),
            "post": distribution([a["submit_ms"] for a in attempts]),
            "observed": distribution([a["elapsed_ms"] for a in attempts]),
            "stages": {field: distribution([r[field] for r in records]) for field in fields},
            "mean_orders_per_second": sum(r["run"]["orders_per_second"] for r in runs) / len(runs),
            "mean_publish_per_second": sum(r["run"]["publish_per_second"] for r in runs)
            / len(runs),
            "ready_peak": max(r["run"]["ready_peak"] for r in runs),
            "unacked_peak": max(r["run"]["unacked_peak"] for r in runs),
            "callbacks_peak": max(r["run"]["callbacks_peak"] for r in runs),
            "transactions_peak": max(r["run"]["transactions_peak"] for r in runs),
            "connections_peak": max(r["run"]["connections_peak"] for r in runs),
            "unsent_peak": max(r["run"]["unsent_peak"] for r in runs),
            "lock_waits_peak": max(r["run"]["lock_waits_peak"] for r in runs),
            "sleep": distribution(
                [v for run in runs for v in [run["profile"]["actual_work_sleep"]["mean_ms"]]]
            ),
        }
    public = {
        "host": summary["host"],
        "rabbitmq_version": summary["rabbitmq_version"],
        "development_unchanged": summary.get("development_unchanged"),
        "development_before": summary.get("development_before"),
        "development_after": summary.get("development_after"),
        "runs": flattened,
        "aggregate": aggregate,
        "profiles": profiles,
        "valid_matrix_complete": all(
            aggregate.get(p, {}).get("valid_runs") == 3 for p in [1, 5, 10, 20]
        ),
    }
    write_json(target / "专项汇总.json", public)
    with (target / "逐轮结果.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, list(flattened[0]))
        writer.writeheader()
        writer.writerows(flattened)
    snapshots = target / "分析代码"
    snapshots.mkdir()
    for name in [
        "order_perf_probe.py",
        "order_performance.py",
        "order_perf_report.py",
        "archive_order_performance.py",
        "order_perf_inspect.py",
        "locust_order_performance.py",
    ]:
        shutil.copy2(ROOT / "scripts" / name, snapshots / name)
    indexed = []
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name == "归档索引.json":
            continue
        raw = path.read_bytes()
        indexed.append(
            {
                "path": str(path.relative_to(target)).replace("\\", "/"),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "uncompressed_sha256": hashlib.sha256(
                    gzip.decompress(raw) if path.suffix == ".gz" else raw
                ).hexdigest(),
                "bytes": len(raw),
            }
        )
    write_json(target / "归档索引.json", indexed)
    lines = [
        "\n\n## 8. 正式矩阵结果\n",
        "\n> 下表每组为3轮、3000笔有效购买；分位数从原始请求合并后按nearest-rank计算，"
        "非Locust分桶估计。无效轮次排除，保留在逐轮结果。\n",
        "\n|Prefetch|有效轮数|POST均值 / P95 / P99 ms|成功观察均值 / P95 / P99 ms|平均唯一订单/秒|Ready / Unacked峰值|实际回调 / 事务峰值|连接峰值|\n",  # noqa: E501 -- Markdown evidence table or link
        "|---|---:|---|---|---:|---|---|---:|\n",
    ]
    for p in [1, 5, 10, 20]:
        a = aggregate.get(p)
        if not a:
            continue

        def fmt(d):
            return f"{d['mean_ms']:.2f} / {d['p95_ms']:.2f} / {d['p99_ms']:.2f}"

        lines.append(
            f"|{p}|{a['valid_runs']}|{fmt(a['post'])}|{fmt(a['observed'])}|{a['mean_orders_per_second']:.2f}|{a['ready_peak']} / {a['unacked_peak']}|{a['callbacks_peak']} / {a['transactions_peak']}|{a['connections_peak']}|\n"  # noqa: E501 -- Markdown evidence table or link
        )
    lines += [
        "\n|Prefetch|待首次投递均值 ms|发布至消费均值 ms|消费开始至提交均值 ms|"
        "订单COMMIT均值 ms|提交至投影均值 ms|投影至观察均值 ms|\n",
        "|---|---:|---:|---:|---:|---:|---:|\n",
    ]
    for p in [1, 5, 10, 20]:
        if p not in aggregate:
            continue
        st = aggregate[p]["stages"]
        values = [
            st[k]["mean_ms"]
            for k in [
                "outbox_wait_approx_ms",
                "publish_to_consumer_ms",
                "consumer_to_commit_ms",
                "order_commit_ms",
                "commit_to_projection_ms",
                "projection_to_observed_ms",
            ]
        ]
        lines.append(f"|{p}|" + "|".join(f"{v:.2f}" for v in values) + "|\n")
    lines += [
        "\n### 逐轮证据\n\n|run_id|Prefetch|有效|订单/秒|Ready峰值|Unacked峰值|首次投递/秒|待首次投递峰值|末次发布至末次ACK发送 秒|采样全链路清空 秒|\n",  # noqa: E501 -- Markdown evidence table or link
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|\n",
    ]
    for r in flattened:
        drain = r["sampled_pipeline_drain_seconds"]
        lines.append(
            f"|{r['run_id']}|{r['prefetch']}|{r['valid']}|{r['orders_per_second']:.2f}|{r['ready_peak']}|{r['unacked_peak']}|{r['publish_per_second']:.2f}|{r['unsent_peak']}|{r['last_publish_to_ack_seconds']:.2f}|{f'{drain:.2f}' if drain is not None else '缺测'}|\n"  # noqa: E501 -- Markdown evidence table or link
        )
    lines.append(
        "\n完整机器可读数据：[专项汇总](验证资料/2026-10-21_Prefetch分阶段专项/专项汇总.json)、[逐轮CSV](验证资料/2026-10-21_Prefetch分阶段专项/逐轮结果.csv)、[归档索引](验证资料/2026-10-21_Prefetch分阶段专项/归档索引.json)。每轮有阶段分布、请求级阶段、队列/流水线时间序列、时钟探测、最终核对、原始事件及Locust输出。大文本使用标准gzip，保留压缩和解压后SHA-256；原私有文件仍在.local/loadtest，凭据不归档。\n"  # noqa: E501 -- Markdown evidence table or link
    )
    report = DOC / "6.异步订单分阶段性能专项分析.md"
    with report.open("a", encoding="utf-8") as handle:
        handle.write("".join(lines))
    print(
        json.dumps(
            {
                "target": str(target),
                "files": len(indexed),
                "valid_matrix_complete": public["valid_matrix_complete"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    archive(parser.parse_args().summary)
