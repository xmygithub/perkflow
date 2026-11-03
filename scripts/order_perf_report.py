"""专项原始证据分析：唯一提交与消息交付分开，阶段重叠不求和。"""

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean_ms": sum(values) / len(values),
        "p50_ms": values[max(0, math.ceil(len(values) * 0.5) - 1)],
        "p95_ms": values[max(0, math.ceil(len(values) * 0.95) - 1)],
        "p99_ms": values[max(0, math.ceil(len(values) * 0.99) - 1)],
        "min_ms": values[0],
        "max_ms": values[-1],
    }


def json_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def analyze(folder, probe=True):
    from scripts.loadtest_contract import write_json

    clocks, missing = {}, []
    for role in ("api", "worker", "monitor", "client"):
        try:
            rows = [
                json.loads((folder / f"clock-{role}-{when}.json").read_text(encoding="utf-8"))
                for when in ("before", "after")
            ]
            clocks[role] = {
                "offset_ns": sum(row["best"]["offset_ns"] for row in rows) / 2,
                "uncertainty_ns": max(row["best"]["rtt_ns"] / 2 for row in rows)
                + abs(rows[0]["best"]["offset_ns"] - rows[1]["best"]["offset_ns"]) / 2,
                "max_wall_step_ns": max(abs(p["wall_step_ns"]) for r in rows for p in r["probes"]),
            }
        except (OSError, ValueError):
            missing.append("clock:" + role)

    def wall(row, edge="end"):
        return row[f"{edge}_wall_ns"] + clocks.get(row["role"], {}).get("offset_ns", 0)

    events = []
    for role in ("api", "worker"):
        path = folder / f"perf-{role}.jsonl"
        if path.exists():
            events.extend(json_lines(path))
        elif probe:
            missing.append("events:" + role)
    stages, by_request = defaultdict(list), defaultdict(lambda: defaultdict(list))
    for row in events:
        if row.get("request_id"):
            by_request[row["request_id"]][row["stage"]].append(row)
        if row["stage"] not in {"pool", "consume_start", "consume_end"}:
            stages[row["stage"]].append((row["end_ns"] - row["start_ns"]) / 1e6)
    observations_path = folder / "observations.json"
    observations = (
        json.loads(observations_path.read_text(encoding="utf-8"))
        if observations_path.exists()
        else {}
    )
    attempts = observations.get("attempts", [])
    result = {
        "clock": clocks,
        "missing": missing,
        "probe": probe,
        "stages": {name: distribution(values) for name, values in stages.items()},
    }
    unique, request_rows, missing_requests = {}, [], []
    for attempt in attempts:
        identifier = attempt.get("request_id")
        if attempt["outcome"] != "succeeded":
            continue
        grouped = by_request.get(identifier, {})
        required = [
            "redis_lua",
            "outbox_claim",
            "publisher_confirm",
            "consume_start",
            "order_transaction",
            "redis_projection",
            "ack_sent",
        ]
        if probe and any(not grouped.get(name) for name in required):
            missing_requests.append(identifier)
            continue
        if not probe:
            continue
        # 同一request_id可能被重复交付；按publish_id和事务跨度选真正INSERT的那次执行。
        candidates = creation_transactions(grouped)
        if len(candidates) != 1:
            missing_requests.append(identifier)
            continue
        transaction, commit = candidates[0]
        unique[attempt["order_id"]] = wall(commit)
        admission, claim = grouped["redis_lua"][0], grouped["outbox_claim"][0]
        consume = next(
            r for r in grouped["consume_start"] if r["publish_id"] == transaction["publish_id"]
        )
        publish = next(
            (r for r in grouped["publisher_confirm"] if r["publish_id"] == consume["publish_id"]),
            None,
        )
        if publish is None:
            missing_requests.append(identifier)
            continue
        projected = min(
            (
                row
                for row in grouped["redis_projection"]
                if row.get("ok") and wall(row) >= wall(commit)
            ),
            key=wall,
            default=None,
        )
        if projected is None:
            missing_requests.append(identifier)
            continue
        started = datetime.fromisoformat(attempt["started_at"]).timestamp() * 1e9
        started += clocks.get("client", {}).get("offset_ns", 0)
        observed = started + attempt["elapsed_ms"] * 1e6
        row = {
            "request_id": identifier,
            "order_id": attempt["order_id"],
            "outbox_wait_approx_ms": (wall(claim) - wall(admission)) / 1e6,
            "publish_to_consumer_ms": (wall(consume, "start") - wall(publish, "start")) / 1e6,
            "admission_to_commit_ms": (wall(commit) - wall(admission)) / 1e6,
            "consumer_to_commit_ms": (commit["end_ns"] - consume["start_ns"]) / 1e6,
            "claim_to_publish_ms": (wall(publish, "start") - wall(claim)) / 1e6,
            "admission_to_first_publish_ms": (
                min(wall(r, "start") for r in grouped["publisher_confirm"]) - wall(admission)
            )
            / 1e6,
            "order_commit_ms": (commit["end_ns"] - commit["start_ns"]) / 1e6,
            "commit_to_projection_ms": (wall(projected) - wall(commit)) / 1e6,
            "projection_to_observed_ms": (observed - wall(projected)) / 1e6,
            "client_to_commit_ms": (wall(commit) - started) / 1e6,
            "commit_wall_ns": wall(commit),
            "start_wall_ns": started,
            "publish_start_wall_ns": wall(publish, "start"),
        }
        request_rows.append(row)
    result["correlated_requests"] = len(request_rows)
    result["missing_request_ids"] = missing_requests
    expected = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["attempts"]
    result["coverage_pass"] = not missing and (not probe or len(request_rows) == expected)
    result["unique_committed_orders"] = len(unique)
    if request_rows:
        for name in [key for key in request_rows[0] if key.endswith("_ms")]:
            result["stages"][name] = distribution([row[name] for row in request_rows])
        origin = min(row["start_wall_ns"] for row in request_rows)
        last = max(unique.values())
        result["submission_to_last_commit_seconds"] = (last - origin) / 1e9
        result["unique_orders_per_second_full_window"] = len(unique) / ((last - origin) / 1e9)
        seconds = Counter(int((stamp - origin) / 1e9) for stamp in unique.values())
        result["unique_commits_per_second"] = dict(sorted(seconds.items()))
        with (folder / "request-stages.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, list(request_rows[0]))
            writer.writeheader()
            writer.writerows(request_rows)
    result["active_callbacks_peak"] = max(
        (r.get("active", 0) for r in events if r["stage"] == "consume_start"), default=0
    )
    result["delivery_tasks_peak"] = max(
        (r.get("active", 0) for r in events if r["stage"] == "delivery_tasks"), default=1
    )
    result["delivery_window_errors"] = sum(
        r.get("errors", 0) for r in events if r["stage"] == "delivery_window_final"
    )
    result["nack_count"] = sum(r["stage"] == "nack" for r in events)
    result["reject_count"] = sum(r["stage"] == "reject" for r in events)
    result["request_span_errors"] = dict(
        Counter(r["stage"] for r in events if r.get("request_id") and r.get("ok") is False)
    )
    # 用事务span的进入/退出求并发，Prefetch/回调活跃数/事务活跃数分别报告。
    edges = [(r["start_ns"], 1) for r in events if r["stage"] == "order_transaction"]
    edges += [(r["end_ns"], -1) for r in events if r["stage"] == "order_transaction"]
    active = peak = 0
    for _, change in sorted(edges):
        active += change
        peak = max(active, peak)
    result["active_transactions_peak"] = peak
    result["pool_checked_out_peak"] = max((r.get("checked_out", 0) for r in events), default=0)
    samples = json.loads((folder / "queue-series.json").read_text(encoding="utf-8"))
    for key in ("ready", "unacked", "redis_outbox", "redis_outbox_due", "mysql_lock_waits"):
        values = [r[key] for r in samples if isinstance(r.get(key), (int, float))]
        result[key + "_sampled_peak"] = max(values, default=None)
    rates = []
    for previous, current in zip(samples, samples[1:]):
        seconds = (current["mono_ns"] - previous["mono_ns"]) / 1e9
        if all(k in r for r in (previous, current) for k in ("ack_total", "publish_total")):
            rates.append(
                {
                    "wall_ns": current["wall_ns"],
                    "window_seconds": seconds,
                    "ack_per_second": (current["ack_total"] - previous["ack_total"]) / seconds,
                    "broker_publish_per_second": (
                        current["publish_total"] - previous["publish_total"]
                    )
                    / seconds,
                }
            )
    result["rates"] = rates
    # 首次投递积压由准入与首发布事件求出；Redis outbox总数还包括等待结果/重投的项目。
    admissions, publications, deliveries, acked = {}, {}, {}, {}
    for identifier, grouped in by_request.items():
        if grouped.get("redis_lua"):
            admissions[identifier] = wall(grouped["redis_lua"][0])
        if grouped.get("publisher_confirm"):
            publications[identifier] = min(wall(r, "start") for r in grouped["publisher_confirm"])
        if grouped.get("consume_start"):
            deliveries[identifier] = min(wall(r, "start") for r in grouped["consume_start"])
        if grouped.get("ack_sent"):
            acked[identifier] = min(wall(r) for r in grouped["ack_sent"])
    if admissions:
        origin = min(admissions.values())
        result["first_publish_per_second"] = dict(
            sorted(Counter(int((stamp - origin) / 1e9) for stamp in publications.values()).items())
        )
        result["ack_sent_per_second"] = dict(
            sorted(Counter(int((stamp - origin) / 1e9) for stamp in acked.values()).items())
        )
        result["publisher_unique_requests"] = len(publications)
        result["publisher_attempts"] = sum(len(g["publisher_confirm"]) for g in by_request.values())
        result["first_to_last_publish_seconds"] = (
            (max(publications.values()) - min(publications.values())) / 1e9
            if len(publications) > 1
            else None
        )
        result["first_admission_to_last_publish_seconds"] = (
            max(publications.values()) - origin
        ) / 1e9
        result["unique_first_publishes_per_second_full_window"] = (
            len(publications) / result["first_admission_to_last_publish_seconds"]
        )
        result["last_publish_to_last_ack_sent_seconds"] = (
            (max(acked.values()) - max(publications.values())) / 1e9 if acked else None
        )
        derived = []
        for row in samples:
            stamp = row["wall_ns"] + clocks.get("monitor", {}).get("offset_ns", 0)
            derived.append(
                {
                    "wall_ns": row["wall_ns"],
                    "not_first_published": sum(
                        start <= stamp < publications.get(key, float("inf"))
                        for key, start in admissions.items()
                    ),
                    "active_callbacks": sum(
                        start <= stamp < acked.get(key, float("inf"))
                        for key, start in deliveries.items()
                    ),
                }
            )
        result["not_first_published_sampled_peak"] = max(r["not_first_published"] for r in derived)
        write_json(folder / "pipeline-series.json", derived)
        with (folder / "pipeline-series.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, ["wall_ns", "not_first_published", "active_callbacks"])
            writer.writeheader()
            writer.writerows(derived)
    if request_rows:
        last_publish = max(row["publish_start_wall_ns"] for row in request_rows)
        settled = next(
            (
                r
                for i, r in enumerate(samples[:-2])
                if r["wall_ns"] >= last_publish
                and all(
                    s.get("ready") == 0 and s.get("unacked") == 0 and s.get("redis_outbox") == 0
                    for s in samples[i : i + 3]
                )
            ),
            None,
        )
        result["last_publish_to_stable_pipeline_empty_seconds"] = (
            (settled["wall_ns"] - last_publish) / 1e9 if settled else None
        )
    result["sql_groups"] = {
        str(key): distribution(values) for key, values in _sql_groups(events).items()
    }
    write_json(folder / "performance-analysis.json", result)
    write_json(folder / "request-stages.json", request_rows)
    with (folder / "stage-distributions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, ["stage", "count", "mean_ms", "p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms"]
        )
        writer.writeheader()
        writer.writerows({"stage": name, **value} for name, value in result["stages"].items())
    return result


def _sql_groups(events):
    groups = defaultdict(list)
    for row in events:
        if row["stage"] == "sql":
            groups[(row["operation"], row["table"])].append((row["end_ns"] - row["start_ns"]) / 1e6)
    return groups


def creation_transactions(grouped):
    candidates = []
    for transaction in grouped["order_transaction"]:
        commits = [
            r
            for r in grouped.get("mysql_commit", [])
            if r.get("ok")
            and r["publish_id"] == transaction["publish_id"]
            and transaction["start_ns"] <= r["start_ns"] <= r["end_ns"] <= transaction["end_ns"]
        ]
        inserts = [
            r
            for r in grouped.get("sql", [])
            if r["operation"] == "INSERT"
            and r["table"] == "tb_voucher_order"
            and r["publish_id"] == transaction["publish_id"]
            and transaction["start_ns"] <= r["start_ns"] <= transaction["end_ns"]
        ]
        if len(commits) == len(inserts) == 1:
            candidates.append((transaction, commits[0]))
    return candidates
