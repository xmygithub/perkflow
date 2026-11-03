"""Server-side browser sessions. The cookie contains only an opaque random identifier."""

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from backend.app.core.config import Settings
from backend.app.core.errors import ApiError
from backend.app.core.redis_connection import create_redis_client
from backend.app.schemas.auth import CurrentUser

SEND_CODE = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {-2, 0} end
local wait = math.max(redis.call('TTL', KEYS[3]), redis.call('TTL', KEYS[4]))
if wait > 0 then return {-1, wait} end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
redis.call('SET', KEYS[3], '1', 'EX', ARGV[3])
redis.call('SET', KEYS[4], '1', 'EX', ARGV[3])
return {1, tonumber(ARGV[2])}
"""

CONSUME_CODE = """
if redis.call('EXISTS', KEYS[1]) == 0 then return -4 end
local raw = redis.call('GET', KEYS[2])
local ttl = redis.call('TTL', KEYS[2])
if not raw or ttl <= 0 then return -1 end
local ok, code = pcall(cjson.decode, raw)
if not ok or type(code.attempts) ~= 'number' then
    redis.call('DEL', KEYS[2]); return -1
end
if code.phone ~= ARGV[1] or code.digest ~= ARGV[2] then
    code.attempts = code.attempts + 1
    if code.attempts >= tonumber(ARGV[3]) then
        redis.call('DEL', KEYS[2]); return -3
    end
    redis.call('SET', KEYS[2], cjson.encode(code), 'EX', ttl)
    return -2
end
redis.call('DEL', KEYS[2])
return 1
"""

ROTATE_SESSION = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if redis.call('EXISTS', KEYS[3]) == 1 then return -1 end
redis.call('SET', KEYS[3], ARGV[1], 'EX', ARGV[2])
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""


class SessionData(BaseModel):
    csrf_token: str
    user: CurrentUser | None = None


@dataclass(frozen=True)
class BrowserSession:
    sid: str
    data: SessionData


class SessionStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.redis = create_redis_client(settings)

    def key(self, kind: str, value: str) -> str:
        digest = hashlib.sha256(value.encode()).hexdigest()
        return f"{self.settings.redis_key_prefix}{kind}:{digest}"

    @staticmethod
    def code_digest(sid: str, phone: str, code: str) -> str:
        return hmac.new(sid.encode(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()

    async def load(self, sid: str | None) -> BrowserSession | None:
        # 1. 拒绝格式不合法的编号；符合格式也必须查到真实 Redis 会话。
        if not sid or not re.fullmatch(r"[A-Za-z0-9_-]{43}", sid):
            return None
        # 2. 读取快照，不在此处续期；校验失败和匿名访问不应延长登录态。
        value = await self.redis.get(self.key("session", sid))
        if value is None:
            return None
        try:
            return BrowserSession(sid, SessionData.model_validate_json(value))
        except ValidationError:
            return None

    async def refresh_login(self, session: BrowserSession) -> bool:
        # 1. 只有登录会话可续期，验证码、匿名会话和冷却 Key 不受影响。
        if session.data.user is None:
            return False
        # 2. EXPIRE 是单条原子命令：Key 已删除/到期则返回 False，不会新建它。
        #    不能用读取到的旧快照 SET EX，否则并发退出后可能被重新写回。
        return bool(
            await self.redis.expire(
                self.key("session", session.sid), self.settings.login_session_ttl
            )
        )

    async def create_anonymous(self) -> BrowserSession:
        for _ in range(3):
            session = BrowserSession(
                secrets.token_urlsafe(32), SessionData(csrf_token=secrets.token_urlsafe(32))
            )
            created = await self.redis.set(
                self.key("session", session.sid),
                session.data.model_dump_json(),
                ex=self.settings.anonymous_session_ttl,
                nx=True,
            )
            if created:
                return session
        raise ApiError(503, "SESSION_UNAVAILABLE", "暂时无法创建登录会话，请重试")

    async def save_code(self, session: BrowserSession, phone: str, code: str) -> None:
        payload = json.dumps(
            {
                "phone": hashlib.sha256(phone.encode()).hexdigest(),
                "digest": self.code_digest(session.sid, phone, code),
                "attempts": 0,
            }
        )
        status, wait = await self.redis.eval(
            SEND_CODE,
            4,
            self.key("session", session.sid),
            self.key("code", session.sid),
            self.key("phone-cooldown", phone),
            self.key("session-cooldown", session.sid),
            payload,
            self.settings.sms_code_ttl,
            self.settings.sms_send_interval,
        )
        if status == -2:
            raise ApiError(401, "SESSION_EXPIRED", "登录会话已过期，请刷新页面")
        if status == -1:
            raise ApiError(429, "CODE_SEND_TOO_FAST", "验证码发送过于频繁，请稍后重试", wait)

    async def consume_code(self, session: BrowserSession, phone: str, code: str) -> None:
        status = await self.redis.eval(
            CONSUME_CODE,
            2,
            self.key("session", session.sid),
            self.key("code", session.sid),
            hashlib.sha256(phone.encode()).hexdigest(),
            self.code_digest(session.sid, phone, code),
            self.settings.sms_max_attempts,
        )
        if status == -4:
            raise ApiError(401, "SESSION_EXPIRED", "登录会话已过期，请刷新页面")
        if status == -3:
            raise ApiError(429, "CODE_ATTEMPTS_EXCEEDED", "验证码尝试次数已用完，请重新获取验证码")
        if status != 1:
            raise ApiError(400, "CODE_INVALID", "验证码错误、已过期或未为当前会话发送")

    async def rotate(self, old: BrowserSession, user: CurrentUser) -> BrowserSession:
        # 1. 身份从匿名变成已登录时，更换 SID 和 CSRF，防止沿用旧凭证。
        session = BrowserSession(
            secrets.token_urlsafe(32), SessionData(csrf_token=secrets.token_urlsafe(32), user=user)
        )
        # 2. Lua 将创建新会话和删除旧会话一起执行，避免并发重复轮换。
        status = await self.redis.eval(
            ROTATE_SESSION,
            3,
            self.key("session", old.sid),
            self.key("code", old.sid),
            self.key("session", session.sid),
            session.data.model_dump_json(),
            self.settings.login_session_ttl,
        )
        if status != 1:
            raise ApiError(
                401, "SESSION_EXPIRED", "登录会话已过期或已被使用，请刷新后重新获取验证码"
            )
        return session

    async def revoke(self, session: BrowserSession) -> None:
        # 退出删除会话和残余验证码；冷却标记留到自然到期，避免退出绕过限流。
        await self.redis.delete(self.key("session", session.sid), self.key("code", session.sid))

    async def close(self) -> None:
        await self.redis.aclose()
