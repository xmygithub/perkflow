import asyncio
import secrets

import httpx
import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.models import User
from backend.app.services.auth import find_or_register
from tests.backend.test_auth_sessions import cleanup

pytestmark = [pytest.mark.mysql, pytest.mark.redis]


class CaptureSms:
    def __init__(self):
        self.codes = {}

    async def send(self, phone, code):
        self.codes[phone] = code


@pytest_asyncio.fixture
async def auth_client(mysql_settings, redis_settings):
    settings = mysql_settings.model_copy(
        update={
            name: getattr(redis_settings, name)
            for name in (
                "redis_host",
                "redis_port",
                "redis_db",
                "redis_password",
                "redis_key_prefix",
            )
        }
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.sms_sender = CaptureSms()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield client, app
        finally:
            await cleanup(app.state.session_store)


async def bootstrap(client):
    response = await client.get("/api/auth/session")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return {"X-CSRF-Token": response.json()["csrf_token"]}


async def sign_in(client, app, phone="13800138000"):
    headers = await bootstrap(client)
    response = await client.post("/api/auth/code", json={"phone": phone}, headers=headers)
    assert response.status_code == 200
    return await client.post(
        "/api/auth/login",
        json={"phone": phone, "code": app.state.sms_sender.codes[phone]},
        headers=headers,
    )


async def test_first_login_registers_cookie_rotation_and_logout(auth_client):
    client, app = auth_client
    phone = f"139{secrets.randbelow(100_000_000):08d}"
    assert (await client.get("/api/auth/me")).status_code == 401
    headers = await bootstrap(client)
    old_sid = client.cookies.get("perkflow_session")
    assert (await client.get("/api/auth/me")).status_code == 401
    response = await client.post("/api/auth/code", json={"phone": phone}, headers=headers)
    assert response.status_code == 200 and "code" not in response.json()
    code = app.state.sms_sender.codes[phone]
    assert len(code) == 6 and code.isascii() and code.isdigit()
    response = await client.post(
        "/api/auth/login", json={"phone": phone, "code": code}, headers=headers
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=3600" in cookie
    assert client.cookies.get("perkflow_session") != old_sid
    assert await app.state.session_store.load(old_sid) is None
    user = response.json()["user"]
    assert user["phone_masked"] == f"{phone[:3]}****{phone[-4:]}"
    assert "phone" not in user
    assert (await client.get("/api/auth/me")).json() == user
    assert (await client.get("/api/auth/session")).json()["user"] == user
    logout = await client.post(
        "/api/auth/logout", json={}, headers={"X-CSRF-Token": response.json()["csrf_token"]}
    )
    assert logout.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.post("/api/auth/logout", json={})).status_code == 200
    # Simulate the resend interval elapsing without a minute-long test sleep.
    await app.state.session_store.redis.expire(
        app.state.session_store.key("phone-cooldown", phone), 0
    )
    again = await sign_in(client, app, phone)
    assert again.status_code == 200 and again.json()["user"]["id"] == user["id"]
    async with app.state.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(User).where(User.phone == phone))
            == 1
        )


async def test_validation_csrf_origin_throttle_and_replay(auth_client):
    client, app = auth_client
    headers = await bootstrap(client)
    assert (
        await client.post("/api/auth/code", json={"phone": "bad"}, headers=headers)
    ).status_code == 422
    assert (await client.post("/api/auth/code", json={"phone": "13800138000"})).status_code == 403
    denied = await client.post(
        "/api/auth/code",
        json={"phone": "13800138000"},
        headers={**headers, "Origin": "https://evil.example"},
    )
    assert denied.status_code == 403
    assert (
        await client.post("/api/auth/code", json={"phone": "13800138000"}, headers=headers)
    ).status_code == 200
    throttled = await client.post("/api/auth/code", json={"phone": "13800138000"}, headers=headers)
    assert throttled.status_code == 429 and int(throttled.headers["retry-after"]) > 0
    body = {"phone": "13800138000", "code": app.state.sms_sender.codes["13800138000"]}
    replies = await asyncio.gather(
        *(client.post("/api/auth/login", json=body, headers=headers) for _ in range(2))
    )
    assert sorted(response.status_code for response in replies) == [200, 400]


async def test_two_clients_have_isolated_user_context(auth_client):
    first, app = auth_client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as second:
        assert (await sign_in(first, app, "13800138000")).status_code == 200
        phone = f"139{secrets.randbelow(100_000_000):08d}"
        assert (await sign_in(second, app, phone)).status_code == 200
        replies = await asyncio.gather(
            *(client.get("/api/auth/me") for client in [first, second] * 5)
        )
        expected = [
            (await first.get("/api/auth/me")).json()["id"],
            (await second.get("/api/auth/me")).json()["id"],
        ]
        assert expected[0] != expected[1]
        assert [r.json()["id"] for r in replies] == expected * 5


async def test_concurrent_first_registration_uses_one_user_row(auth_client):
    _, app = auth_client
    phone = f"139{secrets.randbelow(100_000_000):08d}"

    async def register():
        async with app.state.database.sessions() as session:
            return await find_or_register(session, phone)

    users = await asyncio.gather(*(register() for _ in range(8)))
    assert len({u.id for u in users}) == 1
    async with app.state.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(User).where(User.phone == phone))
            == 1
        )


async def test_redis_failure_returns_503_without_mock_login(auth_client, monkeypatch):
    client, app = auth_client
    headers = await bootstrap(client)

    async def unavailable(*args):
        raise RedisConnectionError("private connection detail")

    monkeypatch.setattr(app.state.session_store, "save_code", unavailable)
    response = await client.post("/api/auth/code", json={"phone": "13800138000"}, headers=headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SESSION_UNAVAILABLE"
    assert "private connection detail" not in response.text
    assert not app.state.sms_sender.codes


async def test_session_save_failure_after_mysql_commit_is_not_login_success(
    auth_client, monkeypatch
):
    client, app = auth_client
    headers = await bootstrap(client)
    phone = f"139{secrets.randbelow(100_000_000):08d}"
    response = await client.post("/api/auth/code", json={"phone": phone}, headers=headers)
    assert response.status_code == 200
    code = app.state.sms_sender.codes[phone]

    async def unavailable(*args):
        raise RedisConnectionError("unavailable")

    monkeypatch.setattr(app.state.session_store, "rotate", unavailable)
    body = {"phone": phone, "code": code}
    response = await client.post("/api/auth/login", json=body, headers=headers)
    assert response.status_code == 503
    assert (await client.get("/api/auth/me")).status_code == 401
    async with app.state.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(User).where(User.phone == phone))
            == 1
        )
    assert (await client.post("/api/auth/login", json=body, headers=headers)).status_code == 400
