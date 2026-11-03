"""明确授权后复用既有准入；不确定结果只回查，不盲目重发。"""

import asyncio
import json
from datetime import UTC, datetime

from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from agent.schemas import ConversationState, PurchaseProposal
from backend.app.core.errors import ApiError
from backend.app.models import AgentConversation, AgentPurchase, OrderRequest, User
from backend.app.schemas.agent_purchases import PurchaseOut, PurchasePage
from backend.app.schemas.order_requests import RequestOut
from backend.app.services import group_buys, order_requests
from backend.app.services.agent_purchase_policy import terms

MESSAGES = {
    "submitting": "购买提交结果待确认，只回查原意图，不会自动重新下单",
    "pending": "购买请求已受理，等待后台创建订单；尚不能视为购买成功",
    "succeeded": "订单已创建，不代表已支付或已核销",
    "failed": "本次购买已明确失败；替代商品需要新的确认",
}
REJECTIONS = {
    "SECKILL_NOT_READY",
    "PURCHASE_QUALIFICATION_BUSY",
    "SECKILL_NOT_STARTED",
    "SECKILL_ENDED",
    "VOUCHER_OFF_SHELF",
    "OUT_OF_STOCK",
    "IDEMPOTENCY_CONFLICT",
}
PREPARATION_FAILURE = "购买服务暂未准备就绪，本次未创建订单；准备就绪后需重新查看并确认"
UNRESOLVED = ("submitting", "pending")
# 仅提示人工核对的等待阈值，不是订单超时、报价期限或可以重新购买的证据。
MANUAL_REVIEW_SECONDS = 60


def output(row):
    message = MESSAGES[row.status]
    if row.status == "failed":
        reason = (
            PREPARATION_FAILURE
            if row.error_code == "SECKILL_NOT_READY"
            else order_requests.FAILURES.get(row.error_code)
        )
        if reason:
            message = reason
    return PurchaseOut(
        id=row.id,
        conversation_id=row.conversation_id,
        created_at=row.created_at.replace(tzinfo=UTC),
        request_id=str(row.request_id) if row.request_id is not None else None,
        proposal=PurchaseProposal.model_validate(row.snapshot["proposal"]),
        status=row.status,
        error_code=row.error_code,
        request=RequestOut.model_validate(row.snapshot["result"])
        if row.snapshot.get("result")
        else None,
        message=message,
    )


async def owned(database, user_id, conversation_id, identifier):
    async with database.sessions() as session:
        row = await session.scalar(
            select(AgentPurchase).where(
                AgentPurchase.id == identifier,
                AgentPurchase.user_id == user_id,
                AgentPurchase.conversation_id == conversation_id,
            )
        )
        if row is None:
            raise ApiError(404, "PURCHASE_NOT_FOUND", "购买确认记录不存在")
        return output(row)


async def record(database, identifier, result=None, error=None):
    async with database.sessions() as session, session.begin():
        row = await session.get(AgentPurchase, identifier, with_for_update=True)
        # 终态不能被并发到达的旧pending结果覆盖；成功后的取消仅更新订单展示。
        if result is not None:
            if row.status in {"succeeded", "failed"} and result.status == "pending":
                return output(row)
            if row.status in {"succeeded", "failed"} and result.status != row.status:
                return output(row)
            row.status, row.request_id, row.error_code = (
                result.status,
                int(result.id),
                result.error_code,
            )
            row.snapshot = {**row.snapshot, "result": result.model_dump(mode="json")}
        elif error and row.status == "submitting":
            row.status, row.error_code = "failed", error
        return output(row)


async def refresh(database, ingress, user_id, conversation_id, identifier):
    current = await owned(database, user_id, conversation_id, identifier)
    source = "not_found"
    result = None
    try:
        # 1. 先核对SQL检查点与当前订单；Redis旧pending不能遮住已提交的SQL终态。
        async with database.sessions() as session:
            task = await session.scalar(
                select(OrderRequest).where(
                    OrderRequest.user_id == user_id,
                    OrderRequest.request_key == identifier,
                )
            )
            if task is not None:
                if task.voucher_id != current.proposal.evaluation.voucher.voucher_id:
                    raise ApiError(409, "PURCHASE_AUTHORIZATION_MISMATCH", "请求关联异常")
                result = await order_requests.request_out(session, task)
                source = "mysql"
        # 2. 尚未进入SQL时精确读取原Redis载荷。辅助索引局部丢失可查同券字段，不SCAN/repair。
        if result is None:
            voucher_id = current.proposal.evaluation.voucher.voucher_id
            gate = ingress.store.key(voucher_id)
            raw = await ingress.redis.get(ingress.intent_key(user_id, identifier))
            request_id = current.request_id
            if raw:
                intent = json.loads(raw)
                if intent["voucher_id"] != str(voucher_id):
                    raise ApiError(409, "PURCHASE_AUTHORIZATION_MISMATCH", "请求关联异常")
                request_id = intent["id"]
            if request_id is None:
                request_id = await ingress.redis.hget(gate, f"i:{user_id}:{identifier}")
            if request_id is not None:
                document = await ingress.get_document(request_id)
                if document is None:
                    raw = await ingress.redis.hget(gate, f"p:{request_id}")
                    document = json.loads(raw) if raw else None
                if document is not None:
                    purchase = document["purchase"]
                    if (
                        purchase["user_id"] != str(user_id)
                        or purchase["voucher_id"] != str(voucher_id)
                        or purchase["request_key"] != identifier
                        or purchase["request_id"] != str(request_id)
                    ):
                        raise ApiError(409, "PURCHASE_AUTHORIZATION_MISMATCH", "请求关联异常")
                    result = RequestOut.model_validate(document["result"])
                    if result.id != str(request_id) or result.voucher_id != voucher_id:
                        raise ApiError(409, "PURCHASE_AUTHORIZATION_MISMATCH", "结果关联异常")
                    source = "redis"
    except (RedisError, SQLAlchemyError, TimeoutError, ApiError, ValueError, KeyError, TypeError):
        # 查验失败不接受部分/归属不匹配的结果，不泄露载荷，也不把未知判失败。
        source, result = "unavailable", None
    if result is not None:
        current = await record(database, identifier, result=result)
    checked_at = datetime.now(UTC)
    missing = source in {"not_found", "unavailable"}
    manual = (
        current.status in UNRESOLVED
        and missing
        and (checked_at - current.created_at).total_seconds() >= MANUAL_REVIEW_SECONDS
    )
    message = {
        "mysql": "已核对数据库中的原请求与当前订单，未重新提交购买。",
        "redis": "已找到原Redis受理记录，未重新预扣或投递。",
        "not_found": "尚未找到原请求，不能证明未受理或购买失败；只回查，不重新购买。",
        "unavailable": "本次核对暂不可用，保留上次状态；只回查，不重新购买。",
    }[source]
    if missing and current.status not in UNRESOLVED:
        message = "暂无法核对原请求的最新状态，保留已保存的最终结果；未重新提交购买。"
        if source == "not_found":
            source = "saved"
    if manual:
        message += " 等待已超过1分钟，请按授权编号和原会话交由维护者核对；不要直接重试购买。"
    return current.model_copy(update={
        "lookup_available": source != "unavailable",
        "lookup_source": source,
        "checked_at": checked_at,
        "needs_manual_review": manual,
        "recovery_message": message,
    })


async def refresh_many(database, ingress, user_id, rows):
    # 每条核对独立Session，最多4条并行，避免页面分页耗尽连接池或累计Redis故障等待。
    semaphore = asyncio.Semaphore(4)

    async def one(row):
        async with semaphore:
            return await refresh(database, ingress, user_id, row.conversation_id, row.id)

    return await asyncio.gather(*(one(row) for row in rows))


async def refresh_unresolved(database, ingress, user_id):
    async with database.sessions() as session:
        rows = (await session.execute(
            select(AgentPurchase.id, AgentPurchase.conversation_id)
            .where(AgentPurchase.user_id == user_id, AgentPurchase.status.in_(UNRESOLVED))
            .order_by(AgentPurchase.created_at, AgentPurchase.id)
            .limit(20)
        )).all()
    # 最多核对20条，异常历史超量时仍由确认事务中的全量存在性检查安全阻止新购买。
    return await refresh_many(database, ingress, user_id, rows)


async def list_account_purchases(database, ingress, user_id, page=1, page_size=5, scope="all"):
    # 账号来自登录，来源会话来自SQL；分页不依赖浏览器保存的最后一个会话ID。
    filters = [AgentPurchase.user_id == user_id]
    if scope == "unresolved":
        filters.append(AgentPurchase.status.in_(UNRESOLVED))
    async with database.sessions() as session:
        total = await session.scalar(
            select(func.count()).select_from(AgentPurchase).where(*filters)
        )
        rows = (await session.execute(
            select(AgentPurchase.id, AgentPurchase.conversation_id)
            .where(*filters)
            .order_by(AgentPurchase.created_at.desc(), AgentPurchase.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )).all()
    items = await refresh_many(database, ingress, user_id, rows)
    async with database.sessions() as session:
        unresolved = await session.scalar(select(func.count()).select_from(AgentPurchase).where(
            AgentPurchase.user_id == user_id, AgentPurchase.status.in_(UNRESOLVED)
        ))
    return PurchasePage(
        items=items, total=total, page=page, page_size=page_size, unresolved_count=unresolved
    )


async def list_purchases(database, ingress, user_id, conversation_id, identifier=None):
    async with database.sessions() as session:
        owner = await session.scalar(
            select(AgentConversation.id).where(
                AgentConversation.id == conversation_id,
                AgentConversation.user_id == user_id,
            )
        )
        if owner is None:
            raise ApiError(404, "AGENT_CONVERSATION_NOT_FOUND", "会话不存在")
        rows = (
            await session.execute(
                select(AgentPurchase.id, AgentPurchase.conversation_id)
                .where(
                    AgentPurchase.user_id == user_id,
                    AgentPurchase.conversation_id == conversation_id,
                )
                .order_by(
                    AgentPurchase.status.in_(UNRESOLVED).desc(),
                    AgentPurchase.created_at.desc(), AgentPurchase.id,
                )
                .limit(5)
            )
        ).all()
    items = await refresh_many(database, ingress, user_id, rows)
    # 从账号历史打开的目标可能早于最近5条，精确查其归属，不把用户引回错误记录。
    if identifier and identifier not in {item.id for item in items}:
        selected = await refresh(database, ingress, user_id, conversation_id, identifier)
        items.insert(0, selected)
    return items


async def confirm(database, ingress, ids, settings, user_id, conversation_id, args):
    if settings.order_mode != "async" or settings.order_admission_mode != "redis_first":
        raise ApiError(
            503, "AGENT_PURCHASE_MODE_UNAVAILABLE", "助手购买只支持当前Redis优先异步模式"
        )
    identifier = str(args.proposal_id)
    first = False
    # 先在短事务外核对旧未决记录，避免持账号行锁等待Redis；锁内仍检查是否有新未决记录。
    async with asyncio.timeout(settings.mysql_query_timeout):
        await refresh_unresolved(database, ingress, user_id)
    # 1. 同账号确认短事务串行，跨会话也不绕过未决购买；模型请求本身不持有这些锁。
    async with (
        asyncio.timeout(settings.mysql_query_timeout),
        database.sessions() as session,
        session.begin(),
    ):
        await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        conversation = await session.scalar(
            select(AgentConversation)
            .where(
                AgentConversation.id == conversation_id,
                AgentConversation.user_id == user_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise ApiError(404, "AGENT_CONVERSATION_NOT_FOUND", "会话不存在")
        prior = await session.get(AgentPurchase, identifier)
        if prior is not None:
            if prior.user_id != user_id or prior.conversation_id != conversation_id:
                raise ApiError(404, "PURCHASE_NOT_FOUND", "购买确认记录不存在")
            # 重复确认无论版本/有效期是否变化，都只回查已关联意图。
        else:
            active = await session.scalar(
                select(AgentPurchase.id)
                .where(
                    AgentPurchase.user_id == user_id,
                    AgentPurchase.status.in_(["submitting", "pending"]),
                )
                .limit(1)
            )
            if active:
                raise ApiError(
                    409, "PURCHASE_PENDING", "仍有未决购买，请先回查原会话结果，不重新购买"
                )
            state = ConversationState.model_validate(conversation.state)
            proposal = state.purchase_proposal
            if conversation.version != args.expected_version:
                raise ApiError(409, "AGENT_VERSION_CONFLICT", "会话已更新，请重新查看确认内容")
            if proposal is None or proposal.id != identifier:
                raise ApiError(409, "PURCHASE_PROPOSAL_INVALID", "确认内容已失效，请重新选择")
            if proposal.expires_at <= datetime.now(UTC):
                raise ApiError(
                    409, "PURCHASE_PROPOSAL_EXPIRED", "确认内容已过期，请重新查看价格和规则"
                )
            latest = await group_buys.detail(session, proposal.evaluation.voucher.voucher_id)
            evaluation = group_buys.evaluate(latest, state.requirements)
            if (
                proposal.requirements != state.requirements
                or terms(latest) != terms(proposal.evaluation.voucher)
                or evaluation.verdict == "ineligible"
            ):
                raise ApiError(
                    409, "PURCHASE_TERMS_CHANGED", "商品、价格或适用条件变化，请重新确认"
                )
            if evaluation.unknowns and not args.acknowledge_unknowns:
                raise ApiError(
                    409, "PURCHASE_UNKNOWNS_REQUIRED", "存在未确认条件，请先阅读并明确知悉"
                )
            session.add(
                AgentPurchase(
                    id=identifier,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    voucher_id=latest.voucher_id,
                    status="submitting",
                    snapshot={
                        "proposal": proposal.model_dump(mode="json"),
                        "acknowledge_unknowns": args.acknowledge_unknowns,
                    },
                )
            )
            first = True
    # 2. 只有本次成功新建授权收据的调用可执行一次准入。SQL已提交才进入Redis。
    if first:
        try:
            result = await ingress.submit(
                user_id, proposal.evaluation.voucher.voucher_id, identifier, ids
            )
            await record(database, identifier, result=result.request)
        except ApiError as error:
            if error.code in REJECTIONS:
                await record(database, identifier, error=error.code)
            # 503/超时/连接中断可能已经预扣。保留submitting，不返库、不自动重试。
        except (TimeoutError, RedisError):
            pass
    return await refresh(database, ingress, user_id, conversation_id, identifier)
