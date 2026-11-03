"""仅专项私有进程显式安装的观察器；不被业务启动路径导入，不保存业务载荷。"""

import asyncio
import contextvars
import functools
import json
import queue
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

CURRENT = contextvars.ContextVar("order_perf_scope", default=None)


class Recorder:
    def __init__(self, path, role, capacity=100000):
        self.role, self.path = role, path
        self.queue = queue.Queue(maxsize=capacity)
        self.dropped = self.written = 0
        self.active = self.peak_active = 0
        self.thread = threading.Thread(target=self._write, daemon=True)

    def emit(self, stage, start_ns=None, start_wall=None, **fields):
        now, wall = time.perf_counter_ns(), time.time_ns()
        scope = CURRENT.get() or {}
        row = {
            "stage": stage,
            "role": self.role,
            "request_id": scope.get("request_id"),
            "publish_id": scope.get("publish_id"),
            "scan_id": scope.get("scan_id"),
            "start_ns": now if start_ns is None else start_ns,
            "end_ns": now,
            "start_wall_ns": wall if start_wall is None else start_wall,
            "end_wall_ns": wall,
            **fields,
        }
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    @contextmanager
    def span(self, stage, **fields):
        start, wall = time.perf_counter_ns(), time.time_ns()
        try:
            yield
        except BaseException as exc:
            self.emit(stage, start, wall, ok=False, error=type(exc).__name__, **fields)
            raise
        else:
            self.emit(stage, start, wall, ok=True, **fields)

    def _write(self):
        # 1. 有限专项热路径只入有界内存；收尾才编码落盘，避免后台JSON线程争抢GIL。
        with self.path.open("x", encoding="utf-8") as handle:
            while True:
                first = self.queue.get()
                if first is None:
                    break
                rows = [first]
                for _ in range(255):
                    try:
                        item = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        self.queue.put(None)
                        break
                    rows.append(item)
                handle.write("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
                self.written += len(rows)
                handle.flush()

    def close(self):
        self.thread.start()
        self.queue.put(None)
        self.thread.join(timeout=15)
        self.path.with_suffix(".meta.json").write_text(
            json.dumps(
                {
                    "dropped": self.dropped,
                    "written": self.written,
                    "writer_alive": self.thread.is_alive(),
                    "peak_active": self.peak_active,
                }
            ),
            encoding="utf-8",
        )


def async_span(recorder, owner, name, stage, identifier=None, scope=False):
    original = getattr(owner, name)

    @functools.wraps(original)
    async def wrapped(*args, **kwargs):
        token = None
        if scope:
            token = CURRENT.set({"request_id": identifier(args) if identifier else None})
        elif identifier and CURRENT.get() is not None:
            CURRENT.get()["request_id"] = identifier(args)
        try:
            with recorder.span(stage):
                return await original(*args, **kwargs)
        finally:
            if token is not None:
                CURRENT.reset(token)

    setattr(owner, name, wrapped)


def install(recorder, work_sleep=None):
    """2. 仅在已验证隔离身份的专用API/Worker里调用，包装原调用且原样传播返回值与异常。"""
    from sqlalchemy import event

    from backend.app.core.database import Database
    from backend.app.core.order_id import RedisOrderId
    from backend.app.core.rabbitmq import OrderBroker
    from backend.app.core.redis_orders import RedisOrderIngress
    from backend.app.services import redis_orders, seckill
    from backend.app.workers import orders

    original_id = RedisOrderId.next_id

    async def next_id(self):
        result = await original_id(self)
        if CURRENT.get() is not None:
            CURRENT.get()["request_id"] = str(result)
        return result

    RedisOrderId.next_id = next_id
    async_span(recorder, RedisOrderIngress, "submit", "redis_admission", scope=True)
    original_init = RedisOrderIngress.__init__

    def ingress_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        original = self.admit_script

        async def lua(*a, **kw):
            with recorder.span("redis_lua"):
                return await original(*a, **kw)

        self.admit_script = lua

    RedisOrderIngress.__init__ = ingress_init
    original_claim = RedisOrderIngress.claim

    async def claim(self, *args, **kwargs):
        start, wall = time.perf_counter_ns(), time.time_ns()
        result = await original_claim(self, *args, **kwargs)
        recorder.emit("claim_rpc", start, wall, claimed_count=1 if result else 0)
        if result:
            if CURRENT.get() is not None:
                CURRENT.get()["request_id"] = result[0].split(":", 1)[1]
            recorder.emit("outbox_claim", start, wall)
        return result

    RedisOrderIngress.claim = claim
    async_span(recorder, redis_orders, "dispatch_one", "dispatch", scope=True)
    async_span(
        recorder,
        redis_orders,
        "dispatch_claimed",
        "dispatch_claimed",
        identifier=lambda a: a[3][0].split(":", 1)[1],
        scope=True,
    )
    async_span(
        recorder, redis_orders, "materialize", "materialize", identifier=lambda a: a[1].request_id
    )
    async_span(recorder, seckill, "admit", "consumer_admit", identifier=lambda a: str(a[2]))
    # orders.py按值导入process_order，包装该实际调用位置，不改变事务上下文边界。
    async_span(
        recorder, orders, "process_order", "order_transaction", identifier=lambda a: str(a[1])
    )
    async_span(recorder, orders, "synchronize", "effects_sync")
    async_span(recorder, redis_orders, "project_identifier", "project_identifier")
    async_span(recorder, redis_orders, "project_task", "project_task")
    async_span(recorder, RedisOrderIngress, "project", "redis_projection")
    async_span(recorder, RedisOrderIngress, "finish", "outbox_finish")

    class AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, seconds):
            # 仅观察dispatch_loop自己的休眠，其他恢复/重连等待原样委托。
            import sys

            if sys._getframe(1).f_code.co_name == "dispatch_loop":
                effective = work_sleep if seconds == 0.01 and work_sleep is not None else seconds
                with recorder.span(
                    "dispatch_sleep", requested_seconds=seconds, effective_seconds=effective
                ):
                    return await asyncio.sleep(effective)
            return await asyncio.sleep(seconds)

    orders.asyncio = AsyncioProxy()

    original_publish = OrderBroker.publish_message

    async def publish(self, message):
        scope = CURRENT.get()
        token = CURRENT.set({"request_id": message.request_id, "publish_id": uuid4().hex})
        try:
            with recorder.span("publisher_confirm"):
                return await original_publish(self, message)
        finally:
            CURRENT.reset(token)
            if scope is not None:
                scope["request_id"] = message.request_id

    OrderBroker.publish_message = publish
    original_initialize = OrderBroker.initialize

    class ExchangeProxy:
        def __init__(self, original):
            self.original = original

        async def publish(self, message, *args, **kwargs):
            scope = CURRENT.get() or {}
            # 3. 只加AMQP诊断header，不改Pydantic购买载荷、Lua或幂等标识。
            message.headers = dict(message.headers or {}) | {
                "pf_perf_publish_id": scope.get("publish_id", ""),
                "pf_perf_send_wall_ns": str(time.time_ns()),
            }
            return await self.original.publish(message, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.original, name)

    async def initialize(self):
        await original_initialize(self)
        self.exchange = ExchangeProxy(self.exchange)

    OrderBroker.initialize = initialize
    original_consume = orders.consume_message

    class MessageProxy:
        def __init__(self, message):
            self.message = message

        def __getattr__(self, name):
            return getattr(self.message, name)

        async def ack(self, *args, **kwargs):
            with recorder.span("ack_sent"):
                return await self.message.ack(*args, **kwargs)

        async def nack(self, *args, **kwargs):
            recorder.emit("nack")
            return await self.message.nack(*args, **kwargs)

        async def reject(self, *args, **kwargs):
            recorder.emit("reject")
            return await self.message.reject(*args, **kwargs)

    async def consume(database, settings, message, *args, **kwargs):
        headers = message.headers or {}
        token = CURRENT.set(
            {"request_id": message.message_id, "publish_id": headers.get("pf_perf_publish_id")}
        )
        recorder.active += 1
        recorder.peak_active = max(recorder.peak_active, recorder.active)
        recorder.emit(
            "consume_start",
            active=recorder.active,
            send_wall_ns=headers.get("pf_perf_send_wall_ns"),
            redelivered=message.redelivered,
        )
        try:
            with recorder.span("consumer"):
                return await original_consume(
                    database, settings, MessageProxy(message), *args, **kwargs
                )
        finally:
            recorder.active -= 1
            recorder.emit("consume_end", active=recorder.active)
            CURRENT.reset(token)

    orders.consume_message = consume
    original_database = Database.__init__

    def database_init(self, settings):
        original_database(self, settings)
        engine = self.engine.sync_engine
        original_connect, original_commit = engine.pool.connect, engine.dialect.do_commit
        original_get = engine.pool._pool.get

        def pool_get(block=True, timeout=None):
            # SQLAlchemy 2.0专项适配：只包容量满时的阻塞get，不把pre-ping算作排队。
            if block:
                with recorder.span("mysql_pool_queue_wait"):
                    return original_get(block, timeout)
            return original_get(block, timeout)

        engine.pool._pool.get = pool_get

        def connect():
            with recorder.span("connection_acquire"):
                return original_connect()

        def commit(connection):
            # 4. 驱动COMMIT返回才记录成功，不拿before_commit或created_at冒充提交完成。
            with recorder.span("mysql_commit"):
                return original_commit(connection)

        engine.pool.connect, engine.dialect.do_commit = connect, commit

        @event.listens_for(engine, "before_cursor_execute")
        def before(conn, cursor, statement, parameters, context, many):
            context._perf_start = (time.perf_counter_ns(), time.time_ns())

        @event.listens_for(engine, "after_cursor_execute")
        def after(conn, cursor, statement, parameters, context, many):
            text = statement.lower()
            table = next(
                (
                    table
                    for table in [
                        "tb_voucher_order",
                        "tb_order_request",
                        "tb_seckill_effect",
                        "tb_seckill_gate",
                        "tb_voucher",
                    ]
                    if table in text
                ),
                "other",
            )
            recorder.emit(
                "sql",
                *context._perf_start,
                operation=statement.strip().split()[0].upper(),
                table=table,
            )

        @event.listens_for(engine.pool, "checkout")
        def checkout(*args):
            recorder.emit("pool", checked_out=engine.pool.checkedout())

    Database.__init__ = database_init


async def calibrate(redis, path):
    # 5. 各进程独立记录Redis TIME往返区间；跨进程分析保留误差，不相减单调时钟。
    probes = []
    for _ in range(9):
        wall, mono = time.time_ns(), time.perf_counter_ns()
        seconds, micros = await redis.time()
        end, end_mono = time.time_ns(), time.perf_counter_ns()
        remote = int(seconds) * 10**9 + int(micros) * 1000
        probes.append(
            {
                "offset_ns": remote - (wall + end) // 2,
                "rtt_ns": end_mono - mono,
                "wall_step_ns": (end - wall) - (end_mono - mono),
            }
        )
        await asyncio.sleep(0.01)
    path.write_text(
        json.dumps(
            {
                "probes": probes,
                "best": min(probes, key=lambda x: x["rtt_ns"]),
                "wall_ns": time.time_ns(),
            }
        ),
        encoding="utf-8",
    )
