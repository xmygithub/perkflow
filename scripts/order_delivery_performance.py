"""单变量投递实验：Prefetch20/池5+5固定，baseline、sleep0、batch4、publish2。"""

import argparse
import asyncio
import hashlib
import json
import sys
import time

from scripts.loadtest_contract import ROOT, write_json
from scripts.order_performance import PERF_ROOT, development_fingerprint, execute, management


async def run(mode, resume=None):
    from backend.app.core.config import Settings

    with management(Settings()) as client:
        check = client.get("/api/overview")
        check.raise_for_status()
    PERF_ROOT.mkdir(exist_ok=True, parents=True)
    if resume:
        path = (PERF_ROOT / resume).resolve()
        if path.parent != PERF_ROOT.resolve() or not path.name.startswith("delivery-"):
            raise ValueError("仅允许恢复私有投递实验清单")
        summary = json.loads(path.read_text(encoding="utf-8"))
        if summary["mode"] != mode:
            raise ValueError("阶段不匹配")
    else:
        path = PERF_ROOT / f"delivery-{mode}-{time.time_ns()}.json"
        summary = {
            "mode": mode,
            "prefetch": 20,
            "mysql_pool": [5, 5],
            "development_before": await development_fingerprint(),
            "runs": [],
            "rabbitmq_version": check.json()["rabbitmq_version"],
            "source_sha256": {
                str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in [
                    ROOT / "scripts/order_delivery_variants.py",
                    ROOT / "scripts/order_delivery_performance.py",
                    ROOT / "scripts/order_perf_probe.py",
                    ROOT / "scripts/order_performance.py",
                    ROOT / "scripts/lua/order_perf_claim_batch.lua",
                    ROOT / "backend/app/services/redis_orders.py",
                ]
            },
        }
    variants = ["baseline", "sleep0", "batch4", "publish2"]
    cases = (
        [("baseline", 100, False)] + [(v, 100, True) for v in variants]
        if mode == "pilot"
        else [
            (v, 1000, True)
            for block in [
                variants,
                list(reversed(variants)),
                ["batch4", "baseline", "publish2", "sleep0"],
            ]
            for v in block
        ]
    )
    for variant, attempts, probe in cases[len(summary["runs"]) :]:
        result = await execute(20, attempts, probe, f"delivery-{mode}", variant=variant)
        summary["runs"].append(result)
        write_json(path, summary)
        if not result["valid"]:
            print("投递试验未通过，保留资源与证据，停止后续发压。", flush=True)
            break
        await asyncio.sleep(2)
    summary["development_after"] = await development_fingerprint()
    summary["development_unchanged"] = summary["development_before"] == summary["development_after"]
    write_json(path, summary)
    print(f"投递实验清单：{path}", flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["pilot", "suite"])
    parser.add_argument("--resume")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.mode, args.resume))
    except Exception as exc:
        print(f"投递实验失败：{type(exc).__name__}（不输出凭据）", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
