"""运行：conda activate perkflow 后，python -m backend.app.workers.orders。"""

import asyncio
import logging

from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.errors import ApiError
from backend.app.core.rabbitmq import OrderBroker, connect_broker
from backend.app.core.redis_connection import create_redis_client
from backend.app.core.redis_orders import RedisOrderIngress
from backend.app.core.seckill_store import SeckillStore
from backend.app.schemas.order_requests import OrderMessage
from backend.app.services import redis_orders as fast_orders
from backend.app.services import seckill
from backend.app.services.order_processing import (
    claim_delivery,
    finish_delivery,
    process_order,
    record_failure,
)

logger = logging.getLogger("perkflow.orders")


async def dispatch_one(
    database: Database,
    settings: Settings,
    broker: OrderBroker,
    store: SeckillStore | None = None,
    ingress: RedisOrderIngress | None = None,
) -> bool:
    claimed = await claim_delivery(database, settings)
    if claimed is None:
        return False
    identifier, token, attempt = claimed
    if settings.order_admission_mode in {"redis", "redis_first"}:
        if store is None:
            raise RuntimeError("Redis 准入模式必须提供 SeckillStore")
        try:
            async with asyncio.timeout(settings.mysql_query_timeout):
                admitted = await seckill.admit(database, store, identifier)
        except (ApiError, TimeoutError):
            await seckill.postpone(database, identifier)
            logger.warning("秒杀准入待恢复 request_id=%s", identifier)
            return False
        if admitted != "admitted":
            if ingress is not None:
                await fast_orders.project_identifier(database, ingress, identifier)
            return True  # 拒绝已持久化，不向 MQ 投递注定失败的请求。
    success = False
    try:
        # 1. 等待发布确认且处理 mandatory 退回；网络结果不确定也保留请求供重投。
        await broker.publish(identifier)
        success = True
    except Exception as exc:
        logger.warning("订单任务发布未确认 request_id=%s error=%s", identifier, type(exc).__name__)
    await finish_delivery(database, settings, identifier, token, attempt, success)
    return True


async def consume_message(
    database: Database,
    settings: Settings,
    message,
    store: SeckillStore | None = None,
    ingress: RedisOrderIngress | None = None,
) -> None:
    try:
        # 1. 无效消息进入独立隔离队列，不解析任意对象或打印原始载荷。
        if len(message.body) > 4096:
            raise ValueError("message too large")
        payload = OrderMessage.model_validate_json(message.body)
        identifier = int(payload.request_id)
        if identifier > 2**63 - 1:
            raise ValueError("identifier out of range")
        if payload.version == 2 and settings.order_admission_mode != "redis_first":
            raise ValueError("Redis 优先消息不能交给旧模式消费者")
    except (ValidationError, ValueError):
        await message.reject(requeue=False)
        return
    try:
        if payload.version == 2:
            # 只有 Lua 放行的任务进入消费者；此时才首次访问 SQL 保存可恢复检查点。
            async with asyncio.timeout(settings.mysql_query_timeout):
                await fast_orders.materialize(database, payload.purchase)
        if settings.order_admission_mode in {"redis", "redis_first"}:
            if store is None:
                raise RuntimeError("Redis 准入模式必须提供 SeckillStore")
            try:
                # 兼容第一阶段遗留消息；超时重试也必须先以同编号回查/恢复预扣。
                async with asyncio.timeout(settings.mysql_query_timeout):
                    await seckill.admit(database, store, identifier)
            except (ApiError, TimeoutError):
                await seckill.postpone(database, identifier)
                await message.ack()
                return  # 准入故障不消耗订单事务的 5 次失败上限。
        try:
            async with asyncio.timeout(settings.mysql_query_timeout):
                if settings.order_admission_mode in {"redis", "redis_first"}:
                    result = await process_order(database, identifier, require_reservation=True)
                else:
                    result = await process_order(database, identifier)
        except Exception as exc:
            logger.warning(
                "订单处理暂未完成 request_id=%s error=%s", identifier, type(exc).__name__
            )
            # 2. 新事务先回查终态，再持久化有限重试；提交成功但回执丢失不会变成失败。
            async with asyncio.timeout(settings.mysql_query_timeout):
                await record_failure(database, identifier, settings)
            result = "retry_recorded"
        if store is not None:
            await synchronize(database, store, identifier=identifier)
        if ingress is not None:
            await fast_orders.project_identifier(database, ingress, identifier)
        # 3. 订单事务或重试/失败状态已持久化才 ACK；未知请求隔离供排查。
        if result == "missing":
            await message.reject(requeue=False)
        else:
            await message.ack()
    except asyncio.CancelledError:
        raise  # 进程退出由连接关闭使未确认消息重新投递。
    except Exception as exc:
        logger.warning("订单消息未确认 request_id=%s error=%s", identifier, type(exc).__name__)
        # 数据库不可用时无法持久化重试；限速后重新入队，不声称已完成或直接丢弃。
        await asyncio.sleep(1)
        try:
            await message.nack(requeue=True)
        except Exception:
            pass  # 原 Channel 已关闭时，由 broker 的未确认投递恢复负责重新交付。


async def synchronize(
    database: Database, store: SeckillStore, *, identifier: int | None = None
) -> None:
    try:
        await seckill.sync_effects(
            database, store, limit=2 if identifier else 10, identifier=identifier
        )
    except Exception as exc:
        logger.warning("库存补偿扫描待恢复 error=%s", type(exc).__name__)


async def recovery_loop(
    database: Database, store: SeckillStore, ingress: RedisOrderIngress | None = None
) -> None:
    # Redis 归还/确认不依赖 MQ 在线，订单 Worker 的重连等待期间也持续恢复。
    while True:
        await synchronize(database, store)
        if ingress is not None:
            try:
                await ingress.repair_some()
            except Exception as exc:
                logger.warning("Redis 投递索引待恢复 error=%s", type(exc).__name__)
        await asyncio.sleep(0.5)


async def dispatch_loop(
    database: Database,
    settings: Settings,
    broker: OrderBroker,
    store: SeckillStore | None = None,
    ingress: RedisOrderIngress | None = None,
) -> None:
    last_sql_scan = 0.0
    while True:
        try:
            if ingress is None:
                worked = await dispatch_one(database, settings, broker, store)
            else:
                worked = await fast_orders.dispatch_one(settings, broker, ingress)
                # SQL 扫描仅处理遗留/消费者重试检查点，频率受控，不随购买请求数增长。
                now = asyncio.get_running_loop().time()
                if now - last_sql_scan >= 0.5:
                    last_sql_scan = now
                    worked = (
                        await dispatch_one(database, settings, broker, store, ingress) or worked
                    )
        except Exception as exc:
            logger.warning("待投递扫描暂不可用 error=%s", type(exc).__name__)
            worked = False
        await asyncio.sleep((0.01 if ingress else 0.05) if worked else 0.5)


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    if settings.order_mode != "async":
        raise ValueError("Worker 仅用于 ORDER_MODE=async 的环境")
    database = Database(settings)
    redis = (
        create_redis_client(settings)
        if settings.order_admission_mode in {"redis", "redis_first"}
        else None
    )
    store = SeckillStore(redis, settings.seckill_key_prefix) if redis is not None else None
    ingress = RedisOrderIngress(store) if settings.order_admission_mode == "redis_first" else None
    recovery = (
        asyncio.create_task(recovery_loop(database, store, ingress)) if store is not None else None
    )
    warmed = ingress is None
    try:
        while True:
            try:
                if not warmed:
                    await fast_orders.warm(database, ingress)
                    warmed = True
                async with connect_broker(settings) as broker:
                    # 4. broker QoS 限制未确认数量；每个回调有独立数据库 Session/事务。
                    active = set()

                    async def consume(msg):
                        task = asyncio.current_task()
                        active.add(task)
                        try:
                            await consume_message(database, settings, msg, store, ingress)
                        finally:
                            active.discard(task)

                    consumer_tag = await broker.queue.consume(consume)
                    logger.info(
                        "RabbitMQ 订单 Worker 已就绪，并发上限=%s",
                        settings.order_worker_concurrency,
                    )
                    try:
                        await dispatch_loop(database, settings, broker, store, ingress)
                    finally:
                        # 停止新交付，等待已开始的事务；超时取消并随连接关闭重投未 ACK 消息。
                        try:
                            await broker.queue.cancel(consumer_tag)
                        except Exception:
                            pass
                        if active:
                            _, pending = await asyncio.wait(
                                list(active), timeout=settings.mysql_query_timeout + 1
                            )
                            for task in pending:
                                task.cancel()
                            await asyncio.gather(*pending, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("订单 Worker 等待服务恢复 error=%s", type(exc).__name__)
                await asyncio.sleep(3)
    finally:
        if recovery is not None:
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        if redis is not None:
            await redis.aclose()
        await database.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
