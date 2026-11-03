"""交易业务前置条件；不改变消息协议、准入库存、ACK或补偿算法。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from agent.schemas import PurchaseProposal
from backend.app.core.errors import ApiError
from backend.app.models import AgentPurchase, Voucher
from backend.app.services import group_buys


def terms(item):
    # 库存/评分/查询时间会自然变化，不属于价格与权益授权；销售状态另由规则校验。
    fields = (
        "voucher_id",
        "shop_id",
        "title",
        "shop_name",
        "city",
        "area",
        "category",
        "pay_value",
        "face_value",
        "validity_days",
        "rules_text",
        "usage",
    )
    data = item.model_dump(mode="json")
    return {name: data[name] for name in fields}


async def guard(session, task):
    # 1. 模块意图使用UUID5；普通UUID4订单不增加授权查询，不修改原购买入口零SQL保证。
    if UUID(task.request_key).version != 5:
        return
    receipt = await session.scalar(
        select(AgentPurchase).where(AgentPurchase.id == task.request_key)
    )
    if receipt is None:
        return  # 其他调用者合法UUID5仍可使用原手动购买能力。
    if (
        receipt.user_id != task.user_id
        or receipt.voucher_id != task.voucher_id
        or receipt.status not in {"submitting", "pending"}
    ):
        raise ApiError(409, "PURCHASE_AUTHORIZATION_MISMATCH", "购买授权与请求不匹配")
    proposal = PurchaseProposal.model_validate(receipt.snapshot["proposal"])
    # 2. 锁定权益资料到订单事务提交，避免核对价格后又被并发维护修改；仅读授权表，不锁它。
    row = (
        await session.execute(
            group_buys.query()
            .where(Voucher.id == task.voucher_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise ApiError(409, "PURCHASE_TERMS_CHANGED", "商品已不存在")
    latest = group_buys.group_buy(row, datetime.now(UTC))
    evaluation = group_buys.evaluate(latest, proposal.requirements)
    if terms(latest) != terms(proposal.evaluation.voucher) or evaluation.verdict == "ineligible":
        raise ApiError(409, "PURCHASE_TERMS_CHANGED", "价格、规则或适用条件变化，请重新确认")
    if evaluation.unknowns and not receipt.snapshot["acknowledge_unknowns"]:
        raise ApiError(409, "PURCHASE_TERMS_CHANGED", "出现未确认条件，请重新确认")
