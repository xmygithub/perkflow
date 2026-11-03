"""RabbitMQ 连接与拓扑；业务状态持久化和消费事务由服务层负责。"""

import logging
from contextlib import asynccontextmanager

import aio_pika
from pamqp.commands import Basic

from backend.app.core.config import Settings
from backend.app.schemas.order_requests import OrderMessage


class OrderBroker:
    def __init__(self, settings: Settings, connection):
        self.settings = settings
        self.connection = connection

    async def initialize(self) -> None:
        # 1. 发送与消费分离 Channel，发布错误不会单独关闭正在 ACK 的消费 Channel。
        self.publisher = await self.connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )
        self.consumer = await self.connection.channel()
        await self.consumer.set_qos(prefetch_count=self.settings.order_worker_concurrency)
        prefix = self.settings.rabbitmq_prefix
        self.exchange = await self.publisher.declare_exchange(
            f"{prefix}.direct",
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        dead = await self.consumer.declare_exchange(
            f"{prefix}.dead",
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        self.dead_queue = await self.consumer.declare_queue(f"{prefix}.invalid", durable=True)
        await self.dead_queue.bind(dead, "order.invalid")
        # 2. 明确持久化 classic 队列；无消息 TTL，任务不能因为排队较久被静默过期。
        self.queue = await self.consumer.declare_queue(
            f"{prefix}.order.create",
            durable=True,
            arguments={
                "x-queue-type": "classic",
                "x-dead-letter-exchange": f"{prefix}.dead",
                "x-dead-letter-routing-key": "order.invalid",
            },
        )
        await self.queue.bind(self.exchange, "order.create")

    async def publish(self, identifier: int) -> None:
        # 3. 消息只标识持久化请求，身份/券/价格回查 MySQL；不发送 Cookie 或个人手机号。
        await self.publish_message(OrderMessage(request_id=str(identifier)))

    async def publish_message(self, message: OrderMessage) -> None:
        # 第二版携带 Redis 已受理的最小身份/意图快照，消费者才首次创建 SQL 检查点。
        body = message.model_dump_json(exclude_none=True).encode()
        result = await self.exchange.publish(
            aio_pika.Message(
                body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=message.request_id,
                type=f"order.create.v{message.version}",
            ),
            routing_key="order.create",
            mandatory=True,
            timeout=self.settings.rabbitmq_timeout,
        )
        if not isinstance(result, Basic.Ack):
            raise RuntimeError("RabbitMQ publication not confirmed")


@asynccontextmanager
async def connect_broker(settings: Settings):
    if not settings.rabbitmq_password.get_secret_value():
        raise ValueError("请先初始化根目录 RabbitMQ 本地配置")
    # SDK 的连接异常可能包含 URL；仅输出本项目安全日志，不泄漏带凭据的连接信息。
    for name in ["aio_pika", "aiormq"]:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    connection = await aio_pika.connect_robust(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_port,
        login=settings.rabbitmq_user,
        password=settings.rabbitmq_password.get_secret_value(),
        virtualhost=settings.rabbitmq_vhost,
        timeout=settings.rabbitmq_timeout,
        reconnect_interval=3,
    )
    try:
        broker = OrderBroker(settings, connection)
        await broker.initialize()
        yield broker
    finally:
        await connection.close()
