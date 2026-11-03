import asyncio

import pytest
import pytest_asyncio

from backend.app.core.errors import ApiError
from backend.app.core.session_store import SessionStore
from backend.app.schemas.auth import CurrentUser

pytestmark = pytest.mark.redis


async def cleanup(store):
    assert store.settings.redis_key_prefix.startswith("perkflow:test:")
    keys = [key async for key in store.redis.scan_iter(match=store.settings.redis_key_prefix + "*")]
    if keys:
        await store.redis.delete(*keys)


@pytest_asyncio.fixture
async def session_store(redis_settings):
    store = SessionStore(redis_settings)
    try:
        await store.redis.ping()
        yield store
    finally:
        await cleanup(store)
        await store.close()


async def test_session_expiry_and_opaque_identifier(session_store):
    store = session_store
    session = await store.create_anonymous()
    assert await store.load("forged-cookie") is None
    assert await store.load(session.sid) == session
    assert 0 < await store.redis.ttl(store.key("session", session.sid)) <= 600
    await store.redis.expire(store.key("session", session.sid), 0)
    assert await store.load(session.sid) is None


async def test_phone_and_browser_binding_and_send_throttle(session_store):
    store = session_store
    first, second = await store.create_anonymous(), await store.create_anonymous()
    await store.save_code(first, "13800138000", "000007")
    raw = await store.redis.get(store.key("code", first.sid))
    assert "000007" not in raw and "13800138000" not in raw
    with pytest.raises(ApiError, match="频繁"):
        await store.save_code(second, "13800138000", "000008")
    with pytest.raises(ApiError):
        await store.consume_code(second, "13800138000", "000007")
    with pytest.raises(ApiError):
        await store.consume_code(first, "13900139000", "000007")
    await store.consume_code(first, "13800138000", "000007")


async def test_wrong_code_limit_and_expiry(session_store):
    store = session_store
    session = await store.create_anonymous()
    await store.save_code(session, "13800138000", "000007")
    for _ in range(store.settings.sms_max_attempts - 1):
        with pytest.raises(ApiError) as error:
            await store.consume_code(session, "13800138000", "000008")
        assert error.value.status == 400
    with pytest.raises(ApiError) as error:
        await store.consume_code(session, "13800138000", "000008")
    assert error.value.code == "CODE_ATTEMPTS_EXCEEDED"
    with pytest.raises(ApiError):
        await store.consume_code(session, "13800138000", "000007")
    other = await store.create_anonymous()
    await store.save_code(other, "13900139000", "000007")
    await store.redis.expire(store.key("code", other.sid), 0)
    with pytest.raises(ApiError):
        await store.consume_code(other, "13900139000", "000007")


async def test_code_consumption_is_atomic_and_single_use(session_store):
    store = session_store
    session = await store.create_anonymous()
    await store.save_code(session, "13800138000", "000007")
    results = await asyncio.gather(
        *(store.consume_code(session, "13800138000", "000007") for _ in range(8)),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, ApiError) for result in results) == 7


async def test_session_rotation_read_only_load_and_revocation(session_store):
    store = session_store
    old = await store.create_anonymous()
    user = CurrentUser(id=1, nick_name="测试用户", phone_masked="138****8000")
    new = await store.rotate(old, user)
    assert new.sid != old.sid and new.data.csrf_token != old.data.csrf_token
    assert await store.load(old.sid) is None
    await store.redis.expire(store.key("session", new.sid), 30)
    assert (await store.load(new.sid)).data.user == user
    assert await store.redis.ttl(store.key("session", new.sid)) <= 30
    await store.revoke(new)
    assert await store.load(new.sid) is None
    with pytest.raises(ApiError):
        await store.rotate(old, user)


async def test_refresh_only_extends_login_and_preserves_other_state(session_store):
    store = session_store
    anonymous = await store.create_anonymous()
    await store.redis.expire(store.key("session", anonymous.sid), 30)
    assert not await store.refresh_login(anonymous)
    assert await store.redis.ttl(store.key("session", anonymous.sid)) <= 30
    logged_in = await store.rotate(
        anonymous, CurrentUser(id=1, nick_name="续期测试", phone_masked="138****0000")
    )
    await store.save_code(logged_in, "13800138000", "000007")
    untouched = [
        store.key("code", logged_in.sid),
        store.key("phone-cooldown", "13800138000"),
        store.key("session-cooldown", logged_in.sid),
    ]
    for key in untouched:
        await store.redis.expire(key, 20)
    session_key = store.key("session", logged_in.sid)
    raw = await store.redis.get(session_key)
    await store.redis.expire(session_key, 30)
    assert await store.refresh_login(logged_in)
    assert await store.redis.ttl(session_key) > 3500
    assert await store.redis.get(session_key) == raw
    remaining = await asyncio.gather(*(store.redis.ttl(key) for key in untouched))
    assert all(0 < ttl <= 20 for ttl in remaining)


async def test_concurrent_refresh_and_logout_never_restore_session(session_store):
    store = session_store
    logged_in = await store.rotate(
        await store.create_anonymous(),
        CurrentUser(id=1, nick_name="并发退出测试", phone_masked="138****0000"),
    )
    await asyncio.gather(
        *(store.refresh_login(logged_in) for _ in range(8)), store.revoke(logged_in)
    )
    assert await store.load(logged_in.sid) is None
    # 请求持有的旧快照不能复活退出或到期的 Redis Key。
    assert not await store.refresh_login(logged_in)
    assert await store.load(logged_in.sid) is None
