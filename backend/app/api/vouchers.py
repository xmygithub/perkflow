import asyncio
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response

from backend.app.core.auth import CsrfSessionDep, CurrentUserDep
from backend.app.core.database import SessionDep
from backend.app.core.errors import ApiError
from backend.app.core.session_refresh import SessionRefreshRoute
from backend.app.schemas.order_requests import RequestOut, RequestPage, RequestResult
from backend.app.schemas.vouchers import (
    CancelOrderResult,
    OrderFilters,
    OrderOut,
    OrderPage,
    OrderResult,
    VoucherFilters,
    VoucherOut,
    VoucherPage,
)
from backend.app.services import order_requests, seckill
from backend.app.services import vouchers as service

router = APIRouter(prefix="/api", tags=["优惠券与订单"], route_class=SessionRefreshRoute)
logger = logging.getLogger("perkflow.orders")


@router.get("/vouchers", response_model=VoucherPage)
async def vouchers(
    session: SessionDep, response: Response, filters: Annotated[VoucherFilters, Query()]
) -> VoucherPage:
    response.headers["Cache-Control"] = "no-store"
    return await service.list_vouchers(session, filters)


@router.get("/vouchers/{voucher_id}", response_model=VoucherOut)
async def voucher_detail(
    session: SessionDep, response: Response, voucher_id: Annotated[int, Path(ge=1, le=2**64 - 1)]
) -> VoucherOut:
    response.headers["Cache-Control"] = "no-store"
    return await service.get_voucher(session, voucher_id)


@router.post("/vouchers/{voucher_id}/orders", response_model=RequestResult | OrderResult)
async def create_order(
    request: Request,
    response: Response,
    user: CurrentUserDep,
    csrf: CsrfSessionDep,
    voucher_id: Annotated[int, Path(ge=1, le=2**64 - 1)],
    request_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=36)] = None,
) -> RequestResult | OrderResult:
    # 1. 身份来自 Cookie 会话；写操作同时要求当前会话的 CSRF 和合法来源。
    # 2. 只接收路径中的券 ID，不接受客户端指定用户、订单号、价格或库存。
    if request.app.state.settings.order_mode == "async":
        # 3. 同一购买意图重试使用同一 UUID；身份仍取服务端会话，不接受客户端指定价格。
        try:
            key = str(UUID(request_key)) if request_key else None
        except ValueError:
            key = None
        if key is None:
            raise ApiError(422, "REQUEST_KEY_REQUIRED", "请提供 UUID 格式的 Idempotency-Key")
        if request.app.state.settings.order_admission_mode == "redis_first":
            # 4. 默认路径只有 Redis：缺预热/无库存/重复资格都不连接 MySQL，不先写 pending。
            result = await request.app.state.order_ingress.submit(
                user.id, voucher_id, key, request.app.state.order_ids
            )
        else:
            # 前两阶段学习版本保留在显式分支，不能与默认入口在同一活动混跑。
            async with asyncio.timeout(request.app.state.settings.mysql_query_timeout):
                async with request.app.state.database.sessions() as session:
                    result = await order_requests.submit(
                        session,
                        user.id,
                        voucher_id,
                        key,
                        request.app.state.order_ids,
                        request.app.state.order_lock,
                    )
                    if (
                        request.app.state.settings.order_admission_mode == "redis"
                        and result.request.status == "pending"
                    ):
                        try:
                            await seckill.admit(
                                request.app.state.database,
                                request.app.state.seckill_store,
                                int(result.request.id),
                            )
                        except ApiError as exc:
                            if exc.code != "SECKILL_GATE_UNAVAILABLE":
                                raise
                        result = result.model_copy(
                            update={
                                "request": await order_requests.get_request(
                                    session, user.id, int(result.request.id)
                                )
                            }
                        )
        response.status_code = 202 if result.request.status == "pending" else 200
        response.headers["Location"] = f"/api/order-requests/{result.request.id}"
        response.headers["Cache-Control"] = "no-store"
        return result
    async with asyncio.timeout(request.app.state.settings.mysql_query_timeout):
        async with request.app.state.database.sessions() as session:
            result = await service.create_order(
                session,
                user.id,
                voucher_id,
                request.app.state.order_ids,
                request.app.state.order_lock,
            )
    response.status_code = 200 if result.reused else 201
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/order-requests", response_model=RequestPage)
async def pending_requests(
    request: Request,
    user: CurrentUserDep,
    response: Response,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> RequestPage:
    response.headers["Cache-Control"] = "no-store"
    if request.app.state.settings.order_admission_mode == "redis_first":
        return await request.app.state.order_ingress.list_requests(user.id, page, page_size)
    async with asyncio.timeout(request.app.state.settings.mysql_query_timeout):
        async with request.app.state.database.sessions() as session:
            return await order_requests.list_requests(session, user.id, page, page_size)


@router.get("/order-requests/{request_id}", response_model=RequestOut)
async def request_detail(
    request: Request,
    user: CurrentUserDep,
    response: Response,
    request_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
) -> RequestOut:
    response.headers["Cache-Control"] = "no-store"
    if request.app.state.settings.order_admission_mode == "redis_first":
        if await request.app.state.order_ingress.get_document(request_id) is not None:
            return (await request.app.state.order_ingress.result(user.id, request_id)).request
    # 显式查旧编号可读 SQL 历史；购买入口和已缓存进度不走这个分支。
    async with asyncio.timeout(request.app.state.settings.mysql_query_timeout):
        async with request.app.state.database.sessions() as session:
            return await order_requests.get_request(session, user.id, request_id)


@router.post("/orders/{order_id}/cancel", response_model=CancelOrderResult)
async def cancel_order(
    request: Request,
    session: SessionDep,
    response: Response,
    user: CurrentUserDep,
    csrf: CsrfSessionDep,
    order_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
) -> CancelOrderResult:
    # 当前登录用户才能取消自己的订单；来源和 CSRF 校验与创建订单一致。
    result = await service.cancel_order(session, user.id, order_id)
    if request.app.state.settings.order_admission_mode in {"redis", "redis_first"}:
        # 取消结果以 SQL 提交为准；尽快同步名额，故障时保留待办交给 Worker，不撤销成功取消。
        try:
            await seckill.sync_effects(
                request.app.state.database,
                request.app.state.seckill_store,
                limit=2,
                identifier=order_id,
            )
        except Exception as exc:
            # 持久化待办仍在，下一轮恢复不会重复返库；不打印配置或底层连接字符串。
            logger.warning("取消已提交，库存同步待恢复 error=%s", type(exc).__name__)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/orders", response_model=OrderPage)
async def orders(
    session: SessionDep,
    response: Response,
    user: CurrentUserDep,
    filters: Annotated[OrderFilters, Query()],
) -> OrderPage:
    response.headers["Cache-Control"] = "no-store"
    return await service.list_orders(session, user.id, filters)


@router.get("/orders/{order_id}", response_model=OrderOut)
async def order_detail(
    session: SessionDep,
    response: Response,
    user: CurrentUserDep,
    order_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
) -> OrderOut:
    response.headers["Cache-Control"] = "no-store"
    return await service.get_order(session, user.id, order_id)
