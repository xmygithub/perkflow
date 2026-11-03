"""仅隔离Worker进程安装的单变量候选；默认启动路径不导入，确认/收尾复用生产函数。"""

import asyncio
from collections import deque
from uuid import uuid4

from scripts.loadtest_contract import ROOT
from scripts.order_perf_probe import CURRENT


async def claim_many(ingress, lease_seconds, count=4, recorder=None):
    if not 1 <= count <= 16:
        raise ValueError("批次超限")
    if not hasattr(ingress, "_batch_claim_script"):
        ingress._batch_claim_script = ingress.redis.register_script(
            (ROOT / "scripts/lua/order_perf_claim_batch.lua").read_text(encoding="utf-8")
        )
    script = ingress._batch_claim_script
    if recorder:
        with recorder.span("batch_claim_rpc", batch_limit=count):
            rows = await script(
                keys=[ingress.outbox, ingress.owners, ingress.attempts],
                args=[count, int(lease_seconds * 1000), *[uuid4().hex for _ in range(count)]],
            )
    else:
        rows = await script(
            keys=[ingress.outbox, ingress.owners, ingress.attempts],
            args=[count, int(lease_seconds * 1000), *[uuid4().hex for _ in range(count)]],
        )
    return [(r[0], r[1], int(r[2])) for r in rows]


class PublishWindow:
    """先等槽位再领取，最大两项租约任务；不创建无限task或未领取的内存购买队列。"""

    def __init__(self, limit=2, recorder=None):
        self.limit, self.recorder = limit, recorder
        self.active = set()
        self.peak = 0
        self.errors = 0

    async def capacity(self):
        if len(self.active) >= self.limit:
            await asyncio.wait(self.active, return_when=asyncio.FIRST_COMPLETED)
        self.reap()

    def reap(self):
        for task in list(self.active):
            if task.done():
                self.active.remove(task)
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self.errors += 1
                    if self.recorder:
                        self.recorder.emit("delivery_task_error", error=type(exc).__name__)

    def start(self, coroutine):
        if len(self.active) >= self.limit:
            raise RuntimeError("没有空槽，禁止领取额外租约")
        self.active.add(asyncio.create_task(coroutine))
        self.peak = max(self.peak, len(self.active))
        if self.recorder:
            self.recorder.emit("delivery_tasks", active=len(self.active))

    async def close(self):
        # 停止时不假造成功、不删预占；取消未知发布后保留原租约，由既有重投恢复。
        for task in self.active:
            task.cancel()
        await asyncio.gather(*self.active, return_exceptions=True)
        self.reap()


def install_variant(variant, recorder=None):
    from backend.app.core.redis_orders import RedisOrderIngress
    from backend.app.services import redis_orders
    from backend.app.workers import orders

    if variant == "batch4":
        original = RedisOrderIngress.claim

        async def claim(self, lease_seconds):
            # 缓存的是已落Redis租约的最多4项；崩溃只丢本地句柄，租约到期仍可重新领取。
            if not hasattr(self, "_delivery_batch"):
                self._delivery_batch = deque()
            if not self._delivery_batch:
                rows = await claim_many(self, lease_seconds, recorder=recorder)
                self._delivery_batch.extend(rows)
                if recorder:
                    for row in rows:
                        recorder.emit("outbox_claim", request_id=row[0].split(":", 1)[1])
            if not self._delivery_batch:
                return None
            result = self._delivery_batch.popleft()
            if CURRENT.get() is not None:
                CURRENT.get()["request_id"] = result[0].split(":", 1)[1]
            return result

        RedisOrderIngress.claim = claim
        return original
    if variant != "publish2":
        raise ValueError("未知投递候选")
    window = PublishWindow(recorder=recorder)

    async def dispatch_one(settings, broker, ingress):
        await window.capacity()
        token = CURRENT.set({"request_id": None})
        try:
            claimed = await ingress.claim(settings.order_delivery_lease)
        finally:
            CURRENT.reset(token)
        if claimed is None:
            return False
        window.start(redis_orders.dispatch_claimed(settings, broker, ingress, claimed))
        return True

    redis_orders.dispatch_one = dispatch_one
    original_loop = orders.dispatch_loop

    async def loop(*args, **kwargs):
        try:
            return await original_loop(*args, **kwargs)
        finally:
            await window.close()
            if recorder:
                recorder.emit("delivery_window_final", peak=window.peak, errors=window.errors)

    orders.dispatch_loop = loop
    return window
