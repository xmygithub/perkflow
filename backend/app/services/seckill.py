"""Redis 准入与 MySQL 持久化恢复协调；不宣称跨系统原子提交。"""

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import Database
from backend.app.core.errors import ApiError
from backend.app.core.seckill_store import SeckillStore
from backend.app.models import (
    OrderRequest,
    SeckillEffect,
    SeckillGate,
    SeckillVoucher,
    Shop,
    Voucher,
    VoucherOrder,
)

logger = logging.getLogger("perkflow.orders")


async def repository_metadata(session: AsyncSession, voucher_id: int):
    row = (
        await session.execute(
            select(Voucher.title, Shop.name)
            .join(Shop, Voucher.shop_id == Shop.id)
            .where(Voucher.id == voucher_id)
        )
    ).one()
    return {"title": row[0], "shop_name": row[1]}


def now_utc():
    return datetime.now(UTC).replace(tzinfo=None)


def queue_effect(session: AsyncSession, task: OrderRequest, action: str) -> None:
    # 1. 仅已确认准入的请求生成效果待办，与订单/失败终态在同一事务提交。
    if task.reservation_generation:
        session.add(
            SeckillEffect(
                voucher_id=task.voucher_id,
                request_id=task.id,
                user_id=task.user_id,
                generation=task.reservation_generation,
                action=action,
                next_attempt_at=now_utc(),
            )
        )


async def snapshot(session: AsyncSession, voucher_id: int, generation: str):
    # 初始化/离线重建锁住真实库存，读取最新订单；正常准入不每次锁库存行。
    voucher = await session.scalar(
        select(Voucher).where(Voucher.id == voucher_id).with_for_update()
    )
    activity = await session.get(SeckillVoucher, voucher_id)
    if voucher is None or activity is None:
        raise ApiError(409, "SECKILL_NOT_CONFIGURED", "优惠券购买活动尚未配置")
    orders = (
        await session.scalars(
            select(VoucherOrder)
            .where(VoucherOrder.voucher_id == voucher_id, VoucherOrder.status == "created")
            .with_for_update()
        )
    ).all()
    mapping: dict[str, str | int] = {
        "generation": generation,
        "available": voucher.stock,
        "begin_ms": int(activity.begin_time.replace(tzinfo=UTC).timestamp() * 1000),
        "end_ms": int(activity.end_time.replace(tzinfo=UTC).timestamp() * 1000),
        "enabled": int(voucher.status == 1),
        "meta": json.dumps(await repository_metadata(session, voucher_id), ensure_ascii=False),
    }
    for order in orders:
        mapping[f"u:{order.user_id}"] = str(order.id)
        mapping[f"r:{order.id}"] = "committed"
    return mapping


async def ensure_gate(database: Database, store: SeckillStore, voucher_id: int) -> str:
    async with database.sessions() as session:
        gate = await session.get(SeckillGate, voucher_id)
        if gate and gate.ready:
            return gate.generation
    async with database.sessions() as session, session.begin():
        # 2. 首次并发创建由主键与锁定读协调；就绪以后绝不因 Redis Key 丢失自动填库存。
        statement = insert(SeckillGate).values(voucher_id=voucher_id, generation=uuid4().hex)
        await session.execute(
            statement.on_duplicate_key_update(voucher_id=statement.inserted.voucher_id)
        )
        gate = await session.scalar(
            select(SeckillGate).where(SeckillGate.voucher_id == voucher_id).with_for_update()
        )
        if not gate.ready:
            # ready 未提交时不能有合法准入；上次 Redis 初始化成功但 SQL 回滚也可安全重做。
            mapping = await snapshot(session, voucher_id, gate.generation)
            await store.replace(voucher_id, mapping)
            gate.ready = True
        return gate.generation


async def admit(database: Database, store: SeckillStore, identifier: int) -> str:
    async with database.sessions() as session:
        task = await session.get(OrderRequest, identifier)
        if task is None:
            return "missing"
        if task.status != "pending":
            return "terminal"
        voucher_id = task.voucher_id
    generation = await ensure_gate(database, store, voucher_id)
    async with database.sessions() as session, session.begin():
        # 3. 持久化意图先于 Lua。崩溃/超时后仍有请求编号，用同一编号恢复，不能直接返库。
        task = await session.scalar(
            select(OrderRequest).where(OrderRequest.id == identifier).with_for_update()
        )
        if task.status != "pending":
            return "terminal"
        if task.reservation_generation and task.reservation_generation != generation:
            raise store.unavailable()
        code = await store.reserve(voucher_id, generation, task.user_id, task.id)
        if code == 0:
            task.reservation_generation = generation
            logger.info("秒杀 Lua 准入通过 request_id=%s voucher_id=%s", task.id, voucher_id)
            return "admitted"
        # 没扣到名额的失败也保存终态，重复同一 UUID 不会在之后突然变成成功。
        task.status = "failed"
        task.error_code = {
            1: "OUT_OF_STOCK",
            2: "PURCHASE_QUALIFICATION_BUSY",
            3: "SECKILL_NOT_STARTED",
            4: "SECKILL_ENDED",
            5: "VOUCHER_OFF_SHELF",
            6: "RESERVATION_RELEASED",
        }[code]
        task.delivery_token = None
        task.lease_until = None
        logger.info("秒杀 Lua 准入拒绝 request_id=%s code=%s", task.id, task.error_code)
        return "rejected"


async def postpone(database: Database, identifier: int) -> None:
    async with database.sessions() as session, session.begin():
        task = await session.scalar(
            select(OrderRequest).where(OrderRequest.id == identifier).with_for_update()
        )
        if task and task.status == "pending":
            task.next_delivery_at = now_utc() + timedelta(seconds=2)
            task.delivery_token = None
            task.lease_until = None


async def sync_effects(
    database: Database, store: SeckillStore, *, limit: int = 10, identifier: int | None = None
) -> int:
    completed = 0
    for _ in range(limit):
        async with database.sessions() as session, session.begin():
            query = (
                select(SeckillEffect)
                .where(SeckillEffect.applied.is_(False), SeckillEffect.next_attempt_at <= now_utc())
                .order_by(SeckillEffect.next_attempt_at, SeckillEffect.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if identifier is not None:
                query = query.where(SeckillEffect.request_id == identifier)
            event = await session.scalar(query)
            if event is None:
                break
            # 4. 待办行锁防止多恢复者同时操作；网络调用有 Redis 超时，不持有库存行锁。
            event.attempts += 1
            try:
                await store.effect(
                    event.voucher_id,
                    event.generation,
                    event.user_id,
                    event.request_id,
                    event.action,
                )
            except ApiError:
                event.next_attempt_at = now_utc() + timedelta(
                    seconds=min(2 ** min(event.attempts, 5), 30)
                )
                logger.warning(
                    "Redis 库存同步待恢复 effect_id=%s action=%s", event.id, event.action
                )
            else:
                # SQL 已有结果再投影查询状态；同一效果待办覆盖 Redis 结果同步失败窗口。
                from backend.app.core.redis_orders import RedisOrderIngress
                from backend.app.services.redis_orders import project_task

                if event.action == "cancel":
                    # 旧版本可能有多笔请求映射同一订单；取消要同步这些历史回执的订单状态。
                    tasks = list(
                        await session.scalars(
                            select(OrderRequest).where(OrderRequest.order_id == event.request_id)
                        )
                    )
                else:
                    task = await session.get(OrderRequest, event.request_id)
                    tasks = [task] if task is not None else []
                try:
                    ingress = RedisOrderIngress(store)
                    for task in tasks:
                        await project_task(session, ingress, task)
                except Exception:
                    event.next_attempt_at = now_utc() + timedelta(seconds=2)
                    continue
                event.applied = True
                completed += 1
                logger.info(
                    "Redis 库存同步完成 request_id=%s action=%s", event.request_id, event.action
                )
    return completed


async def queue_cancel(session: AsyncSession, order: VoucherOrder) -> None:
    # 5. 与首次初始化协调。未初始化时以后直接读返库后的 SQL，已初始化则必须留返还待办。
    gate = await session.scalar(
        select(SeckillGate).where(SeckillGate.voucher_id == order.voucher_id).with_for_update()
    )
    if gate and gate.ready:
        session.add(
            SeckillEffect(
                voucher_id=order.voucher_id,
                request_id=order.id,
                user_id=order.user_id,
                generation=gate.generation,
                action="cancel",
                next_attempt_at=now_utc(),
            )
        )


async def rebuild(database: Database, store: SeckillStore, voucher_id: int) -> dict:
    """只能在该环境 API/所有 Worker 已停止时调用，单靠 SQL 行锁不能协调跨系统重建。"""
    async with database.sessions() as session, session.begin():
        statement = insert(SeckillGate).values(voucher_id=voucher_id, generation=uuid4().hex)
        await session.execute(
            statement.on_duplicate_key_update(voucher_id=statement.inserted.voucher_id)
        )
        gate = await session.scalar(
            select(SeckillGate).where(SeckillGate.voucher_id == voucher_id).with_for_update()
        )
        generation = uuid4().hex
        mapping = await snapshot(session, voucher_id, generation)
        tasks = (
            await session.scalars(
                select(OrderRequest)
                .where(OrderRequest.voucher_id == voucher_id, OrderRequest.status == "pending")
                .order_by(OrderRequest.created_at, OrderRequest.id)
                .with_for_update()
            )
        ).all()
        held = failed = 0
        for task in tasks:
            existing = mapping.get(f"u:{task.user_id}")
            if existing:
                task.status, task.order_id = "succeeded", int(existing)
            elif int(mapping["available"]) > 0:
                mapping["available"] = int(mapping["available"]) - 1
                mapping[f"u:{task.user_id}"] = str(task.id)
                mapping[f"r:{task.id}"] = "held"
                task.reservation_generation = generation
                held += 1
            else:
                task.status, task.error_code = "failed", "OUT_OF_STOCK"
                task.reservation_generation = None
                failed += 1
            task.delivery_token, task.lease_until = None, None
            task.next_delivery_at = now_utc()
        # 快照已按 SQL 订单/取消事实和 pending 重建，旧效果不能再作用于新代次。
        events = (
            await session.scalars(
                select(SeckillEffect)
                .where(SeckillEffect.voucher_id == voucher_id, SeckillEffect.applied.is_(False))
                .with_for_update()
            )
        ).all()
        for event in events:
            event.applied = True
        await store.replace(voucher_id, mapping)
        gate.generation, gate.ready = generation, True
        # Redis 成功但 SQL 提交失败会形成错代并拒绝准入；停写状态下重新运行恢复，不自动放量。
        return {
            "voucher_id": voucher_id,
            "available": int(mapping["available"]),
            "pending_reserved": held,
            "pending_failed": failed,
        }
