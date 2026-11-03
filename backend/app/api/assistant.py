"""团购助手接口：只读业务工具与按真实执行事件推送的NDJSON进度。"""

import asyncio
import json
import logging
from contextlib import suppress
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.responses import StreamingResponse

from agent.schemas import ConversationOut, Progress, TurnInput, TurnResult
from agent.workflows.selection import run_selection
from backend.app.core.auth import CsrfSessionDep, CurrentUserDep
from backend.app.core.database import SessionDep
from backend.app.core.errors import ApiError
from backend.app.core.session_refresh import SessionRefreshRoute
from backend.app.schemas.agent_purchases import ConfirmPurchase, PurchaseOut, PurchasePage
from backend.app.schemas.group_buys import EvaluateInput, Evaluation, SearchInput, SearchResult
from backend.app.services import agent_purchases, assistant, group_buys

router = APIRouter(prefix="/api", tags=["团购选购助手"], route_class=SessionRefreshRoute)
logger = logging.getLogger("perkflow.assistant")
ConversationID = Annotated[UUID, Path()]


@router.post("/group-buys/search", response_model=SearchResult)
async def search(
    args: SearchInput,
    session: SessionDep,
    user: CurrentUserDep,
    csrf: CsrfSessionDep,
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    return await group_buys.search(session, args)


@router.post("/group-buys/evaluate", response_model=Evaluation)
async def evaluate(
    args: EvaluateInput,
    session: SessionDep,
    user: CurrentUserDep,
    csrf: CsrfSessionDep,
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    item = await group_buys.detail(session, args.voucher_id)
    return group_buys.evaluate(item, args.requirements)


@router.get("/assistant/status")
async def status(request: Request, user: CurrentUserDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        request.app.state.agent_model.ensure_configured()
    except ApiError:
        return {"configured": False, "mode": "read_only"}
    return {"configured": True, "mode": "controlled_purchase"}


@router.post("/assistant/conversations", response_model=ConversationOut, status_code=201)
async def create(request: Request, user: CurrentUserDep, csrf: CsrfSessionDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await assistant.create(
        request.app.state.database, user.id, request.app.state.settings.mysql_query_timeout
    )


@router.get("/assistant/conversations/{identifier}", response_model=ConversationOut)
async def get(
    identifier: ConversationID, request: Request, user: CurrentUserDep, response: Response
):
    response.headers["Cache-Control"] = "no-store"
    return await assistant.get(
        request.app.state.database,
        user.id,
        str(identifier),
        request.app.state.settings.mysql_query_timeout,
    )


@router.post("/assistant/conversations/{identifier}/turns")
async def turn(
    identifier: ConversationID,
    args: TurnInput,
    request: Request,
    user: CurrentUserDep,
    csrf: CsrfSessionDep,
):
    settings = request.app.state.settings
    model = request.app.state.agent_model
    # 1. 在发送200前检查身份、CSRF、配置和版本。流内失败由error事件明确表达。
    model.ensure_configured()
    database = request.app.state.database
    conversation = await assistant.get(
        database, user.id, str(identifier), settings.mysql_query_timeout
    )
    if conversation.version != args.expected_version:
        raise ApiError(409, "AGENT_VERSION_CONFLICT", "会话已更新，请刷新后继续")
    if conversation.version >= 10000:
        raise ApiError(422, "AGENT_CONVERSATION_LIMIT", "本会话轮数已满，请新建会话")

    async def stream():
        queue = asyncio.Queue()

        async def emit(event: Progress):
            conversation.state.events = (conversation.state.events + [event])[-40:]
            await queue.put({"type": "progress", "event": event.model_dump()})

        async def execute():
            try:
                answer, ids = await run_selection(
                    model,
                    database,
                    settings,
                    conversation.state,
                    args.message,
                    emit,
                    purchase_reader=lambda: purchase_context(request, user.id, str(identifier)),
                )
                await emit(Progress(kind="answer", text=answer))
                saved = await assistant.save(
                    database, user.id, conversation, settings.mysql_query_timeout
                )
                result = TurnResult(
                    conversation=saved,
                    answer=answer,
                    recommended_ids=ids,
                    candidate_ids=saved.state.candidate_ids,
                )
                await queue.put({"type": "done", "result": result.model_dump(mode="json")})
            except ApiError as error:
                await queue.put({"type": "error", "code": error.code, "message": error.message})
            except TimeoutError:
                await queue.put(
                    {
                        "type": "error",
                        "code": "AGENT_TIMEOUT",
                        "message": "本轮对话超时，未执行新的购买；已确认的购买请回查原请求进度",
                    }
                )
            except Exception as error:
                logger.warning("助手本轮失败 error=%s", type(error).__name__)
                await queue.put(
                    {
                        "type": "error",
                        "code": "AGENT_UNAVAILABLE",
                        "message": "助手暂不可用，本轮未保存，请稍后重试",
                    }
                )
            finally:
                await queue.put(None)

        # 2. 连接关闭取消本轮只读执行，不留下常驻Agent任务；已提交轮次可GET回查。
        task = asyncio.create_task(execute())
        try:
            while (item := await queue.get()) is not None:
                yield json.dumps(item, ensure_ascii=False) + "\n"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def purchase_context(request, user_id, conversation_id):
    async with asyncio.timeout(request.app.state.settings.mysql_query_timeout):
        own = await agent_purchases.list_purchases(
            request.app.state.database, request.app.state.order_ingress, user_id, conversation_id
        )
        recent = await agent_purchases.list_account_purchases(
            request.app.state.database, request.app.state.order_ingress, user_id
        )
    items = {item.id: item for item in [*recent.items, *own]}.values()
    # 工具只读业务事实；不向模型暴露用于确认的新入口或凭据。
    return [
        {
            "id": item.id,
            "conversation_id": item.conversation_id,
            "in_current_conversation": item.conversation_id == conversation_id,
            "status": item.status,
            "message": item.message,
            "request_id": item.request.id if item.request else None,
            "order_id": item.request.order.id if item.request and item.request.order else None,
            "order_status": item.request.order.status
            if item.request and item.request.order
            else None,
            "title": item.proposal.evaluation.voucher.title,
            "pay_value": item.proposal.evaluation.voucher.pay_value,
            "error_code": item.error_code,
            "lookup_available": item.lookup_available,
            "lookup_source": item.lookup_source,
            "needs_manual_review": item.needs_manual_review,
            "recovery_message": item.recovery_message,
        }
        for item in items
    ]


@router.get("/assistant/purchases", response_model=PurchasePage)
async def account_purchases(
    request: Request,
    response: Response,
    user: CurrentUserDep,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=10)] = 5,
    scope: Literal["all", "unresolved"] = "all",
):
    response.headers["Cache-Control"] = "no-store"
    async with asyncio.timeout(request.app.state.settings.mysql_query_timeout):
        return await agent_purchases.list_account_purchases(
            request.app.state.database, request.app.state.order_ingress,
            user.id, page, page_size, scope,
        )


@router.post("/assistant/conversations/{identifier}/purchases/confirm", response_model=PurchaseOut)
async def confirm_purchase(
    identifier: ConversationID,
    args: ConfirmPurchase,
    request: Request,
    response: Response,
    user: CurrentUserDep,
    csrf: CsrfSessionDep,
):
    response.headers["Cache-Control"] = "no-store"
    result = await agent_purchases.confirm(
        request.app.state.database,
        request.app.state.order_ingress,
        request.app.state.order_ids,
        request.app.state.settings,
        user.id,
        str(identifier),
        args,
    )
    response.status_code = 202 if result.status in {"submitting", "pending"} else 200
    return result


@router.get("/assistant/conversations/{identifier}/purchases", response_model=list[PurchaseOut])
async def purchases(
    identifier: ConversationID, request: Request, response: Response, user: CurrentUserDep,
    purchase_id: Annotated[UUID | None, Query()] = None,
):
    response.headers["Cache-Control"] = "no-store"
    async with asyncio.timeout(request.app.state.settings.mysql_query_timeout):
        return await agent_purchases.list_purchases(
            request.app.state.database, request.app.state.order_ingress, user.id, str(identifier),
            str(purchase_id) if purchase_id else None,
        )
