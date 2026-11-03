import hmac
from typing import Annotated

from fastapi import Depends, Header, Request, Response

from backend.app.core.errors import ApiError
from backend.app.core.session_store import BrowserSession, SessionStore
from backend.app.schemas.auth import CurrentUser


def get_session_store(request: Request) -> SessionStore:
    return request.app.state.session_store


StoreDep = Annotated[SessionStore, Depends(get_session_store)]


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    same_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin and origin not in {*request.app.state.settings.cors_origins, same_origin}:
        raise ApiError(403, "ORIGIN_NOT_ALLOWED", "请求来源不被允许")


async def get_browser_session(request: Request, store: StoreDep) -> BrowserSession:
    # 1. 按 Cookie 编号查询 Redis；不接受客户端提交的用户 ID 作为登录身份。
    session = await store.load(request.cookies.get(store.settings.session_cookie_name))
    if session is None:
        raise ApiError(401, "SESSION_EXPIRED", "登录会话已过期，请刷新页面")
    # 2. 本请求保存会话，供返回响应时续期复用；不同请求的 state 相互隔离。
    request.state.browser_session = session
    return session


BrowserSessionDep = Annotated[BrowserSession, Depends(get_browser_session)]


def require_csrf(request: Request, session: BrowserSession, token: str | None = None) -> None:
    # 1. 校验页面来源，拒绝来自非受信任网站的认证操作。
    check_origin(request)
    # 2. 比较 Token 是否属于当前会话，不能只检查请求中有没有 Token。
    token = token if token is not None else request.headers.get("x-csrf-token", "")
    if not hmac.compare_digest(token.encode(), session.data.csrf_token.encode()):
        raise ApiError(403, "CSRF_INVALID", "登录校验已失效，请刷新页面")


def csrf_session(
    request: Request,
    session: BrowserSessionDep,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> BrowserSession:
    require_csrf(request, session, csrf_token)
    return session


CsrfSessionDep = Annotated[BrowserSession, Depends(csrf_session)]


def get_current_user(request: Request, session: BrowserSessionDep) -> CurrentUser:
    # 1. 有匿名会话不等于已登录，需要确认会话中已有用户。
    if session.data.user is None:
        raise ApiError(401, "NOT_AUTHENTICATED", "请先登录")
    # 2. 将已登录用户交给本次业务请求，不使用进程级全局用户变量。
    request.state.user = session.data.user
    return session.data.user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def set_session_cookie(response: Response, store: SessionStore, session: BrowserSession) -> None:
    # Cookie 有效期跟随会话规则；登录访问续期时再次发送相同 SID，不重新登录。
    ttl = (
        store.settings.login_session_ttl
        if session.data.user
        else store.settings.anonymous_session_ttl
    )
    response.set_cookie(
        key=store.settings.session_cookie_name,
        value=session.sid,
        max_age=ttl,
        httponly=True,
        secure=store.settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
