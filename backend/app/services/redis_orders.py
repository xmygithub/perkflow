"""Redis 优先链路的后台预热、消息检查点、结果投影与维护。HTTP 准入不调用本文件的 SQL 方法。"""

import json
import logging
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import Database
from backend.app.core.errors import ApiError
from backend.app.core.redis_orders import RedisOrderIngress
from backend.app.models import OrderRequest, SeckillGate, SeckillVoucher
from backend.app.schemas.order_requests import OrderMessage, RedisPurchase
from backend.app.services import seckill
from backend.app.services.order_requests import now_utc, request_out
from backend.app.services.voucher_activities import prepare_normal_activities

logger = logging.getLogger("perkflow.orders")


async def project_task(session, ingress: RedisOrderIngress, task: OrderRequest) -> None:
    result = await request_out(session, task)
    document = await ingress.get_document(task.id)
    if document is None:
        gate = await session.get(SeckillGate, task.voucher_id)
        created = task.created_at.replace(tzinfo=UTC)
        document = {
            "purchase": {
                "request_id": str(task.id),
                "user_id": str(task.user_id),
                "voucher_id": str(task.voucher_id),
                "request_key": task.request_key,
                "generation": task.reservation_generation or (gate.generation if gate else ""),
                "created_at": created.isoformat(),
            },
            "created_ms": int(created.timestamp() * 1000),
            "result": result.model_dump(mode="json"),
        }
    await ingress.project(document, result)


async def project_identifier(
    database: Database, ingress: RedisOrderIngress, identifier: int
) -> None:
    async with database.sessions() as session:
        task = await session.get(OrderRequest, identifier)
        if task is not None:
            await project_task(session, ingress, task)


async def warm(
    database: Database, ingress: RedisOrderIngress, voucher_ids: list[int] | None = None
) -> int:
    # 1. 预热是后台行为。HTTP 遇到未预热券直接拒绝，不临时查询 SQL。
    async with database.sessions() as session, session.begin():
        await prepare_normal_activities(session, voucher_ids)
    if voucher_ids is None:
        async with database.sessions() as session:
            vouchers = list(await session.scalars(select(SeckillVoucher.voucher_id)))
    else:
        vouchers = voucher_ids
    prepared = 0
    for voucher_id in vouchers:
        generation = await seckill.ensure_gate(database, ingress.store, voucher_id)
        # 已用库存丢失时不重建；保留拒绝状态交给显式停写维护。
        if await ingress.redis.hget(ingress.store.key(voucher_id), "generation") != generation:
            logger.warning("秒杀投影缺失，跳过预热 voucher_id=%s", voucher_id)
            continue
        async with database.sessions() as session:
            pending = list(
                await session.scalars(
                    select(OrderRequest.id).where(
                        OrderRequest.voucher_id == voucher_id, OrderRequest.status == "pending"
                    )
                )
            )
        # 升级前 pending 先争取名额，避免新入口绕过历史待办；其他代次正常幂等返回。
        for identifier in pending:
            await seckill.admit(database, ingress.store, identifier)
        last_id = 0
        while True:
            async with database.sessions() as session:
                tasks = list(
                    await session.scalars(
                        select(OrderRequest)
                        .where(OrderRequest.voucher_id == voucher_id, OrderRequest.id > last_id)
                        .order_by(OrderRequest.id)
                        .limit(200)
                    )
                )
                for task in tasks:
                    await project_task(session, ingress, task)
            if not tasks:
                break
            last_id = tasks[-1].id
        async with database.sessions() as session:
            # 只更新不涉及库存数量的入口元数据，并校验代次，不能创建丢失的 Hash。
            row = await seckill.repository_metadata(session, voucher_id)
        script = """
if redis.call('HGET', KEYS[1], 'generation') ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], 'meta', ARGV[2], 'ingress_version', '1')
return 1
"""
        prepared += int(
            await ingress.redis.eval(
                script,
                1,
                ingress.store.key(voucher_id),
                generation,
                json.dumps(row, ensure_ascii=False),
            )
        )
    logger.info("Redis 优先入口预热完成 activities=%s", prepared)
    return prepared


async def materialize(database: Database, purchase: RedisPurchase) -> None:
    # 2. 只有到达消费者的任务才首次登记 MySQL 请求；消息保留恢复所需的服务端身份与编号。
    try:
        async with database.sessions() as session, session.begin():
            task = await session.get(OrderRequest, int(purchase.request_id))
            if task:
                if (
                    task.user_id != int(purchase.user_id)
                    or task.voucher_id != int(purchase.voucher_id)
                    or task.request_key != purchase.request_key
                ):
                    raise ApiError(409, "ORDER_ID_CONFLICT", "消息编号与已有请求不匹配")
                return  # 终态或维护更新过的代次不被旧消息覆盖。
            session.add(
                OrderRequest(
                    id=int(purchase.request_id),
                    user_id=int(purchase.user_id),
                    voucher_id=int(purchase.voucher_id),
                    request_key=purchase.request_key,
                    reservation_generation=purchase.generation,
                    created_at=purchase.created_at.astimezone(UTC).replace(tzinfo=None),
                    next_delivery_at=now_utc(),
                )
            )
    except IntegrityError:
        # 重复交付的插入竞争可以回查同一编号；不同归属的约束冲突不能假报成功。
        async with database.sessions() as session:
            task = await session.get(OrderRequest, int(purchase.request_id))
            if (
                task is None
                or task.user_id != int(purchase.user_id)
                or task.voucher_id != int(purchase.voucher_id)
                or task.request_key != purchase.request_key
            ):
                raise


async def dispatch_one(settings, broker, ingress: RedisOrderIngress) -> bool:
    # 这一条投递路径没有 Database 参数，也不执行 SQL。
    claimed = await ingress.claim(settings.order_delivery_lease)
    if claimed is None:
        return False
    return await dispatch_claimed(settings, broker, ingress, claimed)


async def dispatch_claimed(settings, broker, ingress: RedisOrderIngress, claimed) -> bool:
    # 领取与已领取项处理分开，默认仍串行调用；实验复用同一确认/租约收尾，不复制业务逻辑。
    member, token, attempt = claimed
    voucher_id, identifier = member.split(":", 1)
    document = await ingress.get_document(identifier)
    if document is None:
        raw = await ingress.redis.hget(ingress.store.key(int(voucher_id)), f"p:{identifier}")
        if raw:
            document = json.loads(raw)
            await ingress.repair(document)
    if document is None:
        await ingress.finish(member, token, delay=30)
        logger.warning("待投递载荷缺失 request_id=%s", identifier)
        return False
    if document["result"]["status"] != "pending":
        await ingress.finish(member, token, delay=None)
        return True
    success = False
    try:
        message = OrderMessage(
            version=2,
            request_id=identifier,
            purchase=RedisPurchase.model_validate(document["purchase"]),
        )
        await broker.publish_message(message)
        success = True
    except Exception as exc:
        logger.warning(
            "Redis 待办发布未确认 request_id=%s error=%s", identifier, type(exc).__name__
        )
    delay = settings.order_redelivery_seconds if success else min(2 ** min(attempt, 5), 30)
    await ingress.finish(member, token, delay=delay)
    return True


async def rebuild(database: Database, ingress: RedisOrderIngress, voucher_id: int):
    # 3. 显式停写后，先把未到 MQ/SQL 的 Redis 载荷导入检查点，再运行既有 SQL 事实重建。
    documents = {}
    if await ingress.redis.type(ingress.store.key(voucher_id)) == "hash":
        async for _, raw in ingress.redis.hscan_iter(ingress.store.key(voucher_id), match="p:*"):
            document = json.loads(raw)
            documents[document["purchase"]["request_id"]] = document
    # 整个券 Key 丢失时，独立结果文档仍可提供尚未到 SQL 的购买载荷；只在停写维护扫描。
    async for key in ingress.redis.scan_iter(match=ingress.prefix + "request:*"):
        document = json.loads(await ingress.redis.get(key))
        if (
            document["purchase"]["voucher_id"] == str(voucher_id)
            and document["result"]["status"] == "pending"
        ):
            documents[document["purchase"]["request_id"]] = document
    for document in documents.values():
        await materialize(database, RedisPurchase.model_validate(document["purchase"]))
    result = await seckill.rebuild(database, ingress.store, voucher_id)
    async with database.sessions() as session:
        tasks = list(
            await session.scalars(select(OrderRequest).where(OrderRequest.voucher_id == voucher_id))
        )
        for task in tasks:
            await project_task(session, ingress, task)
            if task.status == "pending":
                document = await ingress.get_document(task.id)
                document["purchase"]["generation"] = task.reservation_generation
                await ingress.redis.hset(
                    ingress.store.key(voucher_id), f"p:{task.id}", json.dumps(document)
                )
                await ingress.repair(document)
    await warm(database, ingress)
    return result
