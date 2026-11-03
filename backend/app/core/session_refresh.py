"""成功访问业务 API 时续期登录态，与是否需要登录的权限规则分开。"""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

from backend.app.core.auth import check_origin, set_session_cookie


class SessionRefreshRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def refresh_after_request(request: Request) -> Response:
            # 1. Cookie 只是会话编号；有编号时先校验来源，拒绝非受信任页面。
            store = request.app.state.session_store
            sid = request.cookies.get(store.settings.session_cookie_name)
            if sid:
                check_origin(request)

            # 2. 先执行接口的参数、权限和 CSRF 校验；失败请求不会走到续期。
            response = await handler(request)
            if response.status_code >= 400 or request.url.path in {
                "/api/auth/login", "/api/auth/logout"
            }:
                # 登录负责轮换 Cookie，退出负责删除；不能再写回旧会话 Cookie。
                return response

            # 3. 鉴权接口复用本请求已读取的会话；公开接口按 Cookie 查询。
            #    无 Cookie、匿名或已失效会话都正常放行，不因此要求公开接口登录。
            if not sid:
                return response
            session = getattr(request.state, "browser_session", None)
            if session is None:
                session = await store.load(sid)
            if session is None or session.data.user is None:
                return response

            # 4. 只延长仍存在的 Redis Key，不重写用户快照，避免复活已退出会话。
            if await store.refresh_login(session):
                # 5. Cookie 也要续期，否则 Redis 还有效，浏览器却已停止发送编号。
                set_session_cookie(response, store, session)
                response.headers["Cache-Control"] = "no-store"
            return response

        return refresh_after_request
