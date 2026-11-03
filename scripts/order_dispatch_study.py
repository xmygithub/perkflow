"""隔离专项的扫描观测和调度候选；不由正常API/Worker启动路径加载。"""

import asyncio
import contextlib
import functools
import time

from scripts.order_delivery_variants import PublishWindow
from scripts.order_perf_probe import CURRENT

VARIANTS = ("scan_baseline", "scan_split1", "scan_split2", "scan_split4")


def install_scan_probe(recorder):
    """仅包实际SQL恢复入口；单独上下文避免把扫描SQL归到上一笔Redis请求。"""
    from backend.app.workers import orders

    original = orders.dispatch_one

    @functools.wraps(original)
    async def scan(*args, **kwargs):
        token = CURRENT.set({"request_id": None, "scan_id": time.perf_counter_ns()})
        start, wall = time.perf_counter_ns(), time.time_ns()
        try:
            worked = await original(*args, **kwargs)
        except BaseException as exc:
            recorder.emit(
                "sql_recovery_scan",
                start,
                wall,
                request_id=None,
                ok=False,
                error=type(exc).__name__,
            )
            raise
        else:
            recorder.emit(
                "sql_recovery_scan", start, wall, request_id=None, ok=True, worked=bool(worked)
            )
            return worked
        finally:
            CURRENT.reset(token)

    orders.dispatch_one = scan


async def sql_scan_loop(database, settings, broker, store, ingress, recorder=None):
    """每0.5秒至多发起一次，慢扫描不重叠、不补发错过的tick，不随购买量创建task。"""
    from backend.app.workers import orders

    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        try:
            await orders.dispatch_one(database, settings, broker, store, ingress)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 与原串行扫描一样保留待办并稍后重试；不删除租约、不假定投递成功。
            orders.logger.warning("专项SQL恢复扫描待重试 error=%s", type(exc).__name__)
            if recorder:
                recorder.emit("sql_scan_retry", error=type(exc).__name__)
        await asyncio.sleep(max(0.0, 0.5 - (loop.time() - started)))


def install_dispatch_variant(variant, recorder=None):
    from backend.app.services import redis_orders
    from backend.app.workers import orders

    if variant not in VARIANTS:
        raise ValueError("未知扫描调度候选")
    if variant == "scan_baseline":
        return  # 基线完全委托原来的串行投递循环，仅增加观测。
    limit = int(variant.removeprefix("scan_split"))
    original_loop = orders.dispatch_loop

    async def dispatch_loop(database, settings, broker, store=None, ingress=None):
        if ingress is None:
            return await original_loop(database, settings, broker, store, ingress)
        # 1. 扫描生命周期限制在当前MQ连接内，重连前取消并等待，不遗留旧broker任务。
        scanner = asyncio.create_task(
            sql_scan_loop(database, settings, broker, store, ingress, recorder),
            name="isolated-sql-delivery-scan",
        )
        window = PublishWindow(limit=limit, recorder=recorder) if limit > 1 else None
        try:
            while True:
                if scanner.done():
                    await scanner  # 意外退出不能静默丢失恢复能力，交给原Worker重连路径。
                    raise RuntimeError("扫描任务意外结束")
                worked = False
                try:
                    if window is None:
                        worked = await redis_orders.dispatch_one(settings, broker, ingress)
                    else:
                        # 2. 必须先获得在途槽位再领取租约；不建立无限内存购买队列。
                        await window.capacity()
                        token = CURRENT.set({"request_id": None})
                        try:
                            claimed = await ingress.claim(settings.order_delivery_lease)
                        finally:
                            CURRENT.reset(token)
                        if claimed is not None:
                            window.start(
                                redis_orders.dispatch_claimed(settings, broker, ingress, claimed)
                            )
                            worked = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    orders.logger.warning("专项Redis投递待重试 error=%s", type(exc).__name__)
                # 3. 保留原工作/空闲休眠，仅改SQL扫描位置与显式在途上限。
                await orders.asyncio.sleep(0.01 if worked else 0.5)
        finally:
            scanner.cancel()
            await asyncio.gather(scanner, return_exceptions=True)
            if window:
                await window.close()
            if recorder:
                recorder.emit(
                    "delivery_window_final",
                    peak=window.peak if window else 1,
                    errors=window.errors if window else 0,
                    configured_limit=limit,
                    scanner_stopped=scanner.done(),
                )

    orders.dispatch_loop = dispatch_loop


async def observe_runtime(recorder, interval=0.25):
    """进程CPU/内存和事件循环延迟；热路径仅入队，不读取业务内容或打印逐笔日志。"""
    import psutil

    process = psutil.Process()
    previous_wall = time.perf_counter()
    previous_cpu = sum(process.cpu_times()[:2])
    while True:
        due = asyncio.get_running_loop().time() + interval
        await asyncio.sleep(interval)
        now, cpu = time.perf_counter(), sum(process.cpu_times()[:2])
        # 100%代表一个逻辑核；共享主机/容器IO不伪装成本进程数据库磁盘耗时。
        fields = {
            "loop_lag_ms": max(0.0, asyncio.get_running_loop().time() - due) * 1000,
            "cpu_percent_one_core": 100 * (cpu - previous_cpu) / (now - previous_wall),
            "rss_bytes": process.memory_info().rss,
        }
        with contextlib.suppress(psutil.Error, AttributeError):
            fields["io_read_bytes"] = process.io_counters().read_bytes
            fields["io_write_bytes"] = process.io_counters().write_bytes
        recorder.emit("runtime_sample", **fields)
        previous_wall, previous_cpu = now, cpu
