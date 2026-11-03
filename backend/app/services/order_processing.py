"""Worker 的事务与投递恢复；不依赖 HTTP 会话或进程内队列保存任务。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.errors import ApiError
from backend.app.models import OrderRequest, VoucherOrder
from backend.app.repositories import vouchers as repository
from backend.app.services.agent_purchase_policy import guard as guard_agent_purchase
from backend.app.services.order_requests import now_utc
from backend.app.services.seckill import queue_effect
from backend.app.services.vouchers import require_orderable


def finish(
    task: OrderRequest, session, *, order_id: int | None = None, error: str | None = None
) -> None:
    task.status = "succeeded" if order_id is not None else "failed"
    task.order_id = order_id
    task.error_code = error
    task.delivery_token = None
    task.lease_until = None
    # 与 SQL 终态一起留下确认/补偿待办；网络同步失败也不会遗忘应返还的名额。
    queue_effect(session, task, "confirm" if order_id == task.id else "release")


async def process_order(
    database: Database, identifier: int, *, require_reservation: bool = False
) -> str:
    async with database.sessions() as session, session.begin():
        # 1. 同一请求的重复消息/多消费者在数据库行锁处串行；状态与订单一起提交。
        task = await session.scalar(
            select(OrderRequest)
            .where(OrderRequest.id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            return "missing"
        if task.status != "pending":
            return "terminal"
        if require_reservation and task.reservation_generation is None:
            raise ApiError(503, "RESERVATION_REQUIRED", "购买请求尚未完成 Redis 准入")
        task.attempts += 1
        own = await repository.get_order(session, task.user_id, task.id)
        if own is not None:
            if own[0].voucher_id != task.voucher_id:
                finish(task, session, error="ORDER_ID_CONFLICT")
            else:
                # 旧订单即使已取消也只回查，绝不再次激活、扣库存或覆盖重买结果。
                finish(task, session, order_id=own[0].id)
            return "completed"
        existing = await repository.find_order(session, task.user_id, task.voucher_id)
        if existing is not None:
            finish(task, session, order_id=existing[0].id)
            return "completed"
        try:
            # Agent订单增加授权权益校验；失败沿用原终态/同事务补偿，不改变消费可靠性流程。
            await guard_agent_purchase(session, task)
            row = await repository.get_voucher(session, task.voucher_id)
            # 2. Redis 预扣不是订单事实。消费时仍校验 SQL 活动/库存，失败后持久化释放待办。
            if row is None:
                raise ApiError(404, "VOUCHER_NOT_FOUND", "优惠券不存在")
            require_orderable(row, datetime.now(UTC))
            if not await repository.decrement_stock(session, task.voucher_id):
                raise ApiError(409, "ORDER_NOT_AVAILABLE", "库存或活动状态已变化")
        except ApiError as exc:
            finish(task, session, error=exc.code)
            return "failed"
        voucher = row[0]
        now = now_utc()
        # 3. 使用消息对应的既有编号；不调用同步入口重新分配 ID。
        order = VoucherOrder(
            id=task.id,
            user_id=task.user_id,
            voucher_id=task.voucher_id,
            pay_value=voucher.pay_value,
            status="created",
            created_at=now,
            expires_at=now + timedelta(days=voucher.validity_days),
        )
        await repository.insert_order(session, order)
        finish(task, session, order_id=order.id)
        # 4. 唯一约束/SQL 错误会回滚扣减和请求状态，外层持久化重试后才能确认原消息。
    return "completed"


async def record_failure(database: Database, identifier: int, settings: Settings) -> None:
    async with database.sessions() as session, session.begin():
        task = await session.scalar(
            select(OrderRequest)
            .where(OrderRequest.id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        # 连接错误可能发生在提交回执阶段；先读持久化终态，不把已成功的任务改成失败。
        if task is None or task.status != "pending":
            return
        task.attempts += 1
        task.delivery_token = None
        task.lease_until = None
        if task.attempts >= settings.order_retry_limit:
            finish(task, session, error="ORDER_PROCESSING_FAILED")
        else:
            task.next_delivery_at = now_utc() + timedelta(seconds=min(2**task.attempts, 30))


async def claim_delivery(database: Database, settings: Settings) -> tuple[int, str, int] | None:
    async with database.sessions() as session, session.begin():
        now = now_utc()
        task = await session.scalar(
            select(OrderRequest)
            .where(
                OrderRequest.status == "pending",
                OrderRequest.next_delivery_at <= now,
                or_(OrderRequest.lease_until.is_(None), OrderRequest.lease_until <= now),
            )
            .order_by(OrderRequest.next_delivery_at, OrderRequest.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if task is None:
            return None
        # 1. 多投递进程用短事务领取租约；网络发送不占着数据库事务和行锁等待。
        token = uuid4().hex
        task.delivery_token = token
        task.lease_until = now + timedelta(seconds=settings.order_delivery_lease)
        task.deliveries += 1
        return task.id, token, task.deliveries


async def finish_delivery(
    database: Database,
    settings: Settings,
    identifier: int,
    token: str,
    attempt: int,
    success: bool,
) -> None:
    delay = settings.order_redelivery_seconds if success else min(2 ** min(attempt, 5), 30)
    async with database.sessions() as session, session.begin():
        # 2. 仅更新本次领取代次；终态、租约过期的新持有者与消费重试不被旧发送者覆盖。
        await session.execute(
            update(OrderRequest)
            .where(
                OrderRequest.id == identifier,
                OrderRequest.status == "pending",
                OrderRequest.delivery_token == token,
            )
            .values(
                delivery_token=None,
                lease_until=None,
                next_delivery_at=now_utc() + timedelta(seconds=delay),
            )
        )
        # 3. broker 已确认但迟迟没有终态仍定期重投；允许重复，以消费端事务幂等兜底。
