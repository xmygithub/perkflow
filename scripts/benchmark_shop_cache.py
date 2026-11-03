"""独立比较纯 MySQL、Redis 命中和缓存未命中，不启动 HTTP 或修改业务配置。

项目根目录运行：python -m scripts.benchmark_shop_cache
"""

import argparse
import asyncio
import json
import logging
import math
import platform
import secrets
import statistics
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Literal

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.repositories import shops as repository
from backend.app.schemas.shops import ShopOut, ShopTypeList
from backend.app.services import shops as service

Target = Literal["shop", "types"]
Mode = Literal["mysql", "hit", "miss"]
MODES: tuple[Mode, ...] = ("mysql", "hit", "miss")
LABELS = {"mysql": "纯 MySQL", "hit": "Redis 命中", "miss": "Redis 未命中+回填"}


class BenchmarkError(Exception):
    """只放不含凭据的校验提示，其他依赖异常不打印原始内容。"""


def summarize(values: list[float], selects: int) -> dict:
    if not values:
        raise BenchmarkError("没有有效样本，不能计算性能结果")
    ordered = sorted(values)
    # P95 使用 nearest-rank 定义；小样本分位数不代表稳定的尾延迟。
    return {
        "samples": len(values),
        "business_selects": selects,
        "mean_ms": statistics.fmean(values),
        "median_ms": statistics.median(values),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
    }


def verify_sample(mode: Mode, payload: str, expected: str, selects: int) -> None:
    if payload != expected:
        raise BenchmarkError("返回数据发生变化，已停止比较；请保持被测数据不变后重试")
    expected_selects = 0 if mode == "hit" else 1
    if selects != expected_selects:
        raise BenchmarkError(
            f"{LABELS[mode]} 实际业务 SELECT 为 {selects}，预期 {expected_selects}；"
            "可能缓存未成功建立或发生故障回源，不能当作有效性能样本"
        )


def key_for(settings: Settings, target: Target, shop_id: int) -> str:
    suffix = f"shop:{shop_id}" if target == "shop" else "shop-types:all"
    return settings.cache_key_prefix + suffix


async def verify_cache(cache: Redis, key: str, target: Target) -> None:
    expected_type = "string" if target == "shop" else "list"
    if await cache.type(key) != expected_type or await cache.ttl(key) <= 0:
        raise BenchmarkError("测试缓存未成功回填，请检查 Redis 连接和可写状态")


async def invoke(
    database: Database,
    cache: Redis,
    settings: Settings,
    target: Target,
    mode: Mode,
    shop_id: int,
    select_count: Callable[[], int],
) -> tuple[float, str, int]:
    # 1. 每个样本新建请求级 Session，防止复用 ORM identity map 跳过数据库。
    before = select_count()
    started = perf_counter_ns()
    async with asyncio.timeout(settings.mysql_query_timeout):
        async with database.sessions() as session:
            if target == "shop":
                if mode == "mysql":
                    row = await repository.get_shop(session, shop_id)
                    result = ShopOut.model_validate(row) if row is not None else None
                else:
                    result = await service.get_shop(session, shop_id, cache, settings)
                if result is None:
                    raise BenchmarkError("指定商户不存在，请换一个已有商户 ID")
            elif mode == "mysql":
                result = ShopTypeList(items=await repository.list_shop_types(session))
            else:
                result = await service.list_shop_types(session, cache, settings)
            # 2. 两条路径都包含公开模型转换/校验、JSON 序列化和 Session 关闭。
            payload = result.model_dump_json()
    elapsed = (perf_counter_ns() - started) / 1_000_000
    return elapsed, payload, select_count() - before


async def benchmark(
    settings: Settings, target: str, shop_id: int, samples: int, warmup: int
) -> dict:
    # 3. 随机运行 ID 隔开业务缓存和锁，只操作本次明确的缓存 Key 与商户重建锁。
    namespace = f"perkflow:benchmark:{secrets.token_hex(16)}:"
    isolated = settings.model_copy(
        # 固定实验期限，关闭业务抖动，保持报告中的 600 秒口径可复现。
        update={
            "cache_key_prefix": namespace,
            "cache_lock_key_prefix": f"{namespace}lock:",
            "shop_cache_ttl": 600,
            "shop_type_cache_ttl": 600,
            "shop_cache_ttl_jitter": 0,
            "shop_type_cache_ttl_jitter": 0,
        }
    )
    targets: list[Target] = ["shop", "types"] if target == "all" else [target]
    keys = [key_for(isolated, item, shop_id) for item in targets]
    if "shop" in targets:
        keys.append(f"{isolated.cache_lock_key_prefix}shop:{shop_id}")
    database = Database(isolated)
    cache = create_redis_client(isolated)
    selects = 0

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal selects
        # 仅统计业务 SELECT，不输出 SQL 或参数；连接池 ping 不计作业务查询。
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    logger = logging.getLogger("uvicorn.error")
    original_level = logger.level
    logger.setLevel(logging.WARNING)  # 只影响独立脚本进程，避免每条 HIT 日志干扰计时。
    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    results = {}
    try:
        # 4. 预先确认真实 Redis 可用，不把应用的回源降级算作缓存成功。
        await cache.ping()
        for item in targets:
            key = key_for(isolated, item, shop_id)
            _, expected, count = await invoke(
                database, cache, isolated, item, "mysql", shop_id, lambda: selects
            )
            verify_sample("mysql", expected, expected, count)
            if item == "types" and not json.loads(expected)["items"]:
                raise BenchmarkError("分类为空，现有实现不缓存空 List，无法比较分类命中")
            await invoke(database, cache, isolated, item, "miss", shop_id, lambda: selects)
            await verify_cache(cache, key, item)

            async def sample(mode: Mode) -> tuple[float, int]:
                # 删除仅在 MISS 场景执行且在计时外；纯 MySQL 不访问 Redis。
                if mode == "miss":
                    await cache.delete(key)
                elapsed, payload, count = await invoke(
                    database, cache, isolated, item, mode, shop_id, lambda: selects
                )
                verify_sample(mode, payload, expected, count)
                if mode == "miss":
                    # 应用会容错回填异常；计时外检查，不能把失败回填标作成功。
                    await verify_cache(cache, key, item)
                return elapsed, count

            # 5. 连接/查询/JSON 预热不计入统计；之后轮换三种模式的执行次序。
            for _ in range(warmup):
                for mode in MODES:
                    await sample(mode)
            timings: dict[Mode, list[float]] = {mode: [] for mode in MODES}
            sql_counts = dict.fromkeys(MODES, 0)
            for index in range(samples):
                offset = index % len(MODES)
                order = MODES[offset:] + MODES[:offset]
                for mode in order:
                    elapsed, count = await sample(mode)
                    timings[mode].append(elapsed)
                    sql_counts[mode] += count
            results[item] = {mode: summarize(timings[mode], sql_counts[mode]) for mode in MODES}
            print(f"{'商户详情' if item == 'shop' else '店铺分类'}：采样完成。", flush=True)
        return {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "scope": "sequential_service_calls_including_session_and_json_without_http_or_auth",
            "shop_id": shop_id,
            "samples_per_mode": samples,
            "warmup_per_mode": warmup,
            "cache_ttl_seconds": 600,
            "results": results,
        }
    finally:
        # 6. 正常结束或异常退出均只清理明确测试 Key；正常写入的缓存有 10 分钟 TTL。
        try:
            await cache.delete(*keys)
        except RedisError:
            print(
                f"测试 Key 清理暂未成功：{namespace}；正常写入时 TTL 为 600 秒，"
                "请恢复 Redis 后检查该测试前缀。",
                flush=True,
            )
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)
            logger.setLevel(original_level)
            try:
                await cache.aclose()
            finally:
                await database.close()


def print_report(report: dict) -> None:
    print("\n单位：毫秒；每项串行测试，不含 HTTP、Node 代理、登录、图片或页面渲染。")
    for target, modes in report["results"].items():
        print(f"\n{'商户详情' if target == 'shop' else '店铺分类'}")
        print("模式                     样本     平均     中位数      P95     业务SELECT")
        for mode, row in modes.items():
            print(
                f"{LABELS[mode]:<20} {row['samples']:>5} {row['mean_ms']:>9.3f}"
                f" {row['median_ms']:>9.3f} {row['p95_ms']:>9.3f} {row['business_selects']:>10}"
            )
        baseline = modes["mysql"]["median_ms"]
        hit = modes["hit"]["median_ms"]
        if baseline > 0:
            reduction = (baseline - hit) / baseline * 100
            print(f"本轮 Redis 命中中位数相对纯 MySQL 的耗时减少：{reduction:.1f}%（负数表示更慢）")
    print("\n本机少量热点数据的单次实验，不是生产压测，也不能推导 QPS 或通用提升比例。")


def main() -> None:
    # Windows 管道可能默认使用系统编码；统一 UTF-8，保证中文终端/报告可读。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="比较商户详情/分类纯 MySQL 与 Redis 缓存耗时")
    parser.add_argument("--target", choices=["shop", "types", "all"], default="all")
    parser.add_argument("--shop-id", type=int, default=1, help="已存在的商户 ID，默认 1")
    parser.add_argument("--samples", type=int, default=100, help="每种模式样本数，1–5000")
    parser.add_argument("--warmup", type=int, default=10, help="每种模式预热次数，1–100")
    parser.add_argument("--output", type=Path, help="可选：保存无凭据的 JSON 统计报告")
    args = parser.parse_args()
    if args.shop_id < 1 or not 1 <= args.samples <= 5000 or not 1 <= args.warmup <= 100:
        parser.error("shop-id 必须为正整数，samples 为 1–5000，warmup 为 1–100")
    print(f"开始对比：每种模式 {args.samples} 次，预热 {args.warmup} 次。", flush=True)
    print("只读 MySQL；Redis 使用随机 benchmark 前缀，不修改现有业务缓存或会话。")
    try:
        report = asyncio.run(
            benchmark(Settings(), args.target, args.shop_id, args.samples, args.warmup)
        )
        print_report(report)
        if args.output:
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"统计报告已保存：{args.output}")
    except BenchmarkError as exc:
        parser.exit(1, f"对比已停止：{exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "已停止对比。\n")
    except (RedisError, SQLAlchemyError, ValidationError, TimeoutError, OSError) as exc:
        parser.exit(
            1,
            f"对比未完成（{type(exc).__name__}），请检查 MySQL/Redis、backend/.env 或输出路径。\n",
        )


if __name__ == "__main__":
    main()
