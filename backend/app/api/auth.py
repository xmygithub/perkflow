from typing import Annotated

from fastapi import APIRouter, Header, Request, Response

from backend.app.core.auth import (
    CsrfSessionDep,
    CurrentUserDep,
    StoreDep,
    check_origin,
    require_csrf,
    set_session_cookie,
)
from backend.app.core.database import SessionDep
from backend.app.core.session_refresh import SessionRefreshRoute
from backend.app.schemas.auth import (
    CodeResponse,
    CurrentUser,
    LoginRequest,
    LoginResponse,
    PhoneRequest,
    SessionResponse,
)
from backend.app.services import auth as service

router = APIRouter(prefix="/api/auth", tags=["登录与注册"], route_class=SessionRefreshRoute)


@router.get("/session", response_model=SessionResponse)
async def browser_session(request: Request, response: Response, store: StoreDep) -> SessionResponse:
    # 1. 校验页面来源，认证响应不允许缓存。
    check_origin(request)
    response.headers["Cache-Control"] = "no-store"
    # 2. 浏览器提供 SID，后端查询 Redis 中对应的真实会话。
    session = await store.load(request.cookies.get(store.settings.session_cookie_name))
    if session is None:
        # 3. 没有有效会话时创建匿名会话，并让浏览器保存新 Cookie。
        session = await store.create_anonymous()
        set_session_cookie(response, store, session)
    # 4. 保存本请求快照并返回 CSRF/用户；已登录会话由统一路由续期。
    request.state.browser_session = session
    return SessionResponse(csrf_token=session.data.csrf_token, user=session.data.user)


@router.post("/code", response_model=CodeResponse)
async def code(
    body: PhoneRequest,
    request: Request,
    response: Response,
    store: StoreDep,
    session: CsrfSessionDep,
):
    await service.send_code(store, request.app.state.sms_sender, session, body.phone)
    response.headers["Cache-Control"] = "no-store"
    return CodeResponse(
        expires_in=store.settings.sms_code_ttl, retry_after=store.settings.sms_send_interval
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest, response: Response, store: StoreDep, session: CsrfSessionDep, db: SessionDep
):
    session = await service.login(store, db, session, body.phone, body.code)
    set_session_cookie(response, store, session)
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(user=session.data.user, csrf_token=session.data.csrf_token)


@router.get("/me", response_model=CurrentUser)
async def me(response: Response, user: CurrentUserDep) -> CurrentUser:
    response.headers["Cache-Control"] = "no-store"
    return user


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    store: StoreDep,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
):
    # 1. 校验来源，查找浏览器对应的会话；已过期时退出仍可重复执行。
    check_origin(request)
    session = await store.load(request.cookies.get(store.settings.session_cookie_name))
    if session is not None:
        # 2. 有效会话必须验证 CSRF，再撤销服务端登录态和剩余验证码。
        require_csrf(request, session, csrf_token)
        await store.revoke(session)
    # 3. 清除浏览器 Cookie；统一续期路由跳过退出，避免再写回该编号。
    response.delete_cookie(
        store.settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=store.settings.session_cookie_secure,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"message": "已退出登录"}
