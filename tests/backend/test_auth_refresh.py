"""使用真实 Redis 检查活跃续期；商户返回值替换为合成数据，不连接 MySQL。"""

import asyncio

import httpx
import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.app.core.database import get_session
from backend.app.main import create_app
from backend.app.schemas.auth import CurrentUser
from backend.app.schemas.shops import ShopTypeList
from backend.app.services import shops
from tests.backend.test_auth_sessions import cleanup

pytestmark = pytest.mark.redis


@pytest_asyncio.fixture
async def refresh_client(redis_settings, monkeypatch):
    app = create_app(redis_settings)
    app.dependency_overrides[get_session] = lambda: None

    async def list_types(_session, _cache, _settings):
        return ShopTypeList(items=[])

    monkeypatch.setattr(shops, "list_shop_types", list_types)
    async with app.router.lifespan_context(app):
        store = app.state.session_store
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://perkflow.test"
            ) as client:
                yield client, store
        finally:
            await cleanup(store)


async def sign_in_session(client, store):
    # 直接构造隔离的登录会话，避免本次续期验证改动用户表。
    session = await store.rotate(
        await store.create_anonymous(),
        CurrentUser(id=7, nick_name="续期测试", phone_masked="138****0000"),
    )
    client.cookies.set(store.settings.session_cookie_name, session.sid, domain="perkflow.test")
    await store.redis.expire(store.key("session", session.sid), 30)
    return session


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/auth/session", "/api/shop-types"])
async def test_active_login_renews_redis_and_cookie_on_public_and_private_api(refresh_client, path):
    client, store = refresh_client
    session = await sign_in_session(client, store)
    response = await client.get(path)
    assert response.status_code == 200
    assert await store.redis.ttl(store.key("session", session.sid)) > 3500
    assert "Max-Age=3600" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    assert client.cookies.get(store.settings.session_cookie_name) == session.sid
    assert (await store.load(session.sid)).data.csrf_token == session.data.csrf_token


async def test_anonymous_visits_do_not_extend_session(refresh_client):
    client, store = refresh_client
    response = await client.get("/api/auth/session")
    assert response.status_code == 200
    sid = client.cookies.get(store.settings.session_cookie_name)
    await store.redis.expire(store.key("session", sid), 30)
    response = await client.get("/api/auth/session")
    assert response.status_code == 200 and response.json()["user"] is None
    assert "set-cookie" not in response.headers
    assert await store.redis.ttl(store.key("session", sid)) <= 30


async def test_idle_expired_cookie_cannot_regain_login(refresh_client):
    client, store = refresh_client
    session = await sign_in_session(client, store)
    await store.redis.expire(store.key("session", session.sid), 0)
    assert (await client.get("/api/auth/me")).status_code == 401
    response = await client.get("/api/auth/session")
    assert response.status_code == 200 and response.json()["user"] is None
    assert client.cookies.get(store.settings.session_cookie_name) != session.sid
    assert await store.load(session.sid) is None


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("POST", "/api/auth/logout", {}, 403),
        ("GET", "/api/auth/session", {"Origin": "https://evil.example"}, 403),
        ("GET", "/api/shops?page=0", {}, 422),
        ("GET", "/api/not-a-route", {}, 404),
        ("GET", "/health/live", {}, 200),
    ],
)
async def test_rejected_requests_and_health_probes_do_not_renew(
    refresh_client, method, path, headers, status
):
    client, store = refresh_client
    session = await sign_in_session(client, store)
    response = await client.request(method, path, headers=headers)
    assert response.status_code == status
    assert "set-cookie" not in response.headers
    assert await store.redis.ttl(store.key("session", session.sid)) <= 30


async def test_logout_clears_cookie_instead_of_renewing_it(refresh_client):
    client, store = refresh_client
    session = await sign_in_session(client, store)
    response = await client.post(
        "/api/auth/logout", json={}, headers={"X-CSRF-Token": session.data.csrf_token}
    )
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert len(response.headers.get_list("set-cookie")) == 1
    assert await store.load(session.sid) is None


async def test_logout_during_inflight_refresh_cannot_restore_session(refresh_client, monkeypatch):
    client, store = refresh_client
    session = await sign_in_session(client, store)
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = store.refresh_login

    async def pause_refresh(snapshot):
        entered.set()
        await proceed.wait()
        return await original(snapshot)

    monkeypatch.setattr(store, "refresh_login", pause_refresh)
    response_task = asyncio.create_task(client.get("/api/auth/me"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await store.revoke(session)
    finally:
        proceed.set()
    response = await response_task
    assert response.status_code == 200  # 请求已经通过鉴权，不宣称取消在途业务。
    assert "set-cookie" not in response.headers
    assert await store.load(session.sid) is None
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_refresh_failure_does_not_claim_cookie_was_renewed(refresh_client, monkeypatch):
    client, store = refresh_client
    session = await sign_in_session(client, store)

    async def unavailable(_):
        raise RedisConnectionError("private redis detail")

    monkeypatch.setattr(store, "refresh_login", unavailable)
    response = await client.get("/api/auth/me")
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert "private redis detail" not in response.text
    assert await store.redis.ttl(store.key("session", session.sid)) <= 30
