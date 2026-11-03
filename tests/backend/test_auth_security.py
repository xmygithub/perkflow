"""Authentication boundaries, using synthetic sessions and no MySQL requests."""

import secrets

import httpx
import pytest
import pytest_asyncio

from backend.app.main import create_app
from backend.app.schemas.auth import CurrentUser
from tests.backend.test_auth_sessions import cleanup

pytestmark = pytest.mark.redis
BASE_URL = "http://perkflow.test"


@pytest_asyncio.fixture
async def security_clients(redis_settings):
    app = create_app(redis_settings)
    async with app.router.lifespan_context(app):
        try:
            transport = httpx.ASGITransport(app=app)
            async with (
                httpx.AsyncClient(transport=transport, base_url=BASE_URL) as owner,
                httpx.AsyncClient(transport=transport, base_url=BASE_URL) as outsider,
            ):
                yield owner, outsider, app.state.session_store
        finally:
            await cleanup(app.state.session_store)


async def test_csrf_alone_and_invented_cookie_cannot_authenticate(security_clients):
    owner, outsider, store = security_clients
    token = (await owner.get("/api/auth/session")).json()["csrf_token"]
    # Knowing another browser's CSRF token does not establish a session or identity.
    headers = {"X-CSRF-Token": token}
    assert (await outsider.get("/api/auth/me", headers=headers)).status_code == 401
    assert (
        await outsider.post("/api/auth/code", json={"phone": "13800138000"}, headers=headers)
    ).status_code == 401
    invented = secrets.token_urlsafe(32)
    outsider.cookies.set(store.settings.session_cookie_name, invented, domain="perkflow.test")
    assert (await outsider.get("/api/auth/me")).status_code == 401
    response = await outsider.get("/api/auth/session")
    assert response.json()["user"] is None
    assert outsider.cookies.get(store.settings.session_cookie_name) != invented
    assert await store.load(invented) is None


async def test_csrf_is_session_bound_and_bad_origin_is_denied(security_clients):
    owner, outsider, store = security_clients
    owner_token = (await owner.get("/api/auth/session")).json()["csrf_token"]
    outsider_token = (await outsider.get("/api/auth/session")).json()["csrf_token"]
    sid = outsider.cookies.get(store.settings.session_cookie_name)
    assert (await outsider.post("/api/auth/logout", json={})).status_code == 403
    assert (
        await outsider.post(
            "/api/auth/logout", json={}, headers={"X-CSRF-Token": owner_token}
        )
    ).status_code == 403
    malicious = {"Origin": "https://evil.example", "X-CSRF-Token": outsider_token}
    assert (
        await outsider.post("/api/auth/logout", json={}, headers=malicious)
    ).status_code == 403
    response = await outsider.get("/api/auth/session", headers=malicious)
    assert response.status_code == 403 and "csrf_token" not in response.json()
    assert await store.load(sid) is not None
    assert (
        await outsider.post(
            "/api/auth/logout", json={}, headers={"X-CSRF-Token": outsider_token}
        )
    ).status_code == 200


async def test_stolen_cookie_can_impersonate_until_session_is_revoked(security_clients):
    owner, outsider, store = security_clients
    anonymous = await store.create_anonymous()
    # Construct only a test Redis record; this does not register a real user.
    logged_in = await store.rotate(
        anonymous, CurrentUser(id=7, nick_name="安全边界测试", phone_masked="138****0000")
    )
    cookie_name = store.settings.session_cookie_name
    owner.cookies.set(cookie_name, logged_in.sid, domain="perkflow.test")
    assert (await outsider.get("/api/auth/me")).status_code == 401
    # Simulate an already stolen credential, not another site's browser capabilities.
    outsider.cookies.set(cookie_name, logged_in.sid, domain="perkflow.test")
    assert (await outsider.get("/api/auth/me")).json()["id"] == 7
    # CSRF is not a second password: the stolen cookie can fetch its matching token.
    response = await outsider.get("/api/auth/session")
    assert response.json()["csrf_token"] == logged_in.data.csrf_token
    assert response.json()["user"]["id"] == 7
    response = await owner.post(
        "/api/auth/logout", json={}, headers={"X-CSRF-Token": logged_in.data.csrf_token}
    )
    assert response.status_code == 200
    assert (await outsider.get("/api/auth/me")).status_code == 401


async def test_cors_only_allows_explicit_frontend_origins(security_clients):
    owner, _, _ = security_clients
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type, X-CSRF-Token",
    }
    denied = await owner.options(
        "/api/auth/logout", headers={**preflight, "Origin": "https://evil.example"}
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers
    allowed_origin = "http://127.0.0.1:5173"
    allowed = await owner.options(
        "/api/auth/logout", headers={**preflight, "Origin": allowed_origin}
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == allowed_origin
    assert allowed.headers["access-control-allow-credentials"] == "true"


@pytest.mark.parametrize("secure", [False, True])
async def test_cookie_flags_and_response_exposure(redis_settings, secure):
    settings = redis_settings.model_copy(update={"session_cookie_secure": secure})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://perkflow.test"
            ) as client:
                response = await client.get("/api/auth/session")
                assert response.status_code == 200
                cookie = response.headers["set-cookie"]
                flags = set(cookie.split("; ")[1:])
                assert {"HttpOnly", "SameSite=lax", "Path=/", "Max-Age=600"} <= flags
                assert ("Secure" in flags) is secure
                assert not any(flag.startswith("Domain=") for flag in flags)
                sid = client.cookies.get(settings.session_cookie_name)
                assert sid not in response.text
                assert set(response.json()) == {"csrf_token", "user"}
                assert response.headers["cache-control"] == "no-store"
        finally:
            await cleanup(app.state.session_store)
