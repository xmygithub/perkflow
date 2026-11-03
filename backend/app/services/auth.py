import logging
import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import ApiError
from backend.app.core.session_store import BrowserSession, SessionStore
from backend.app.models import User
from backend.app.repositories import users
from backend.app.schemas.auth import CurrentUser


class ConsoleSmsSender:
    """Explicit local simulation. No real SMS is sent; no code is returned by the API."""

    async def send(self, phone: str, code: str) -> None:
        logging.getLogger("uvicorn.error").info(
            "[LOCAL SMS] phone=%s code=%s (simulation only)", mask_phone(phone), code
        )


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


async def send_code(
    store: SessionStore, sms: ConsoleSmsSender, session: BrowserSession, phone: str
):
    # 1. 生成六位随机验证码，保留可能出现的前导零。
    code = f"{secrets.randbelow(1_000_000):06d}"
    # 2. 原子保存验证码校验信息和冷却状态，拒绝过于频繁的请求。
    await store.save_code(session, phone, code)
    # 3. 调用本地模拟短信，验证码只输出在开发终端，不通过接口返回。
    await sms.send(phone, code)


async def find_or_register(session: AsyncSession, phone: str) -> CurrentUser:
    try:
        async with session.begin():
            user = await users.find_by_phone(session, phone)
            if user is None:
                user = await users.add_user(session, phone, f"津城用户{secrets.token_hex(4)}")
            result = public_user(user)
        return result
    except IntegrityError as exc:
        if not exc.orig.args or exc.orig.args[0] != 1062:
            raise
        # Rollback releases duplicate-key locks and the old REPEATABLE READ snapshot.
        # Read the concurrently committed user in a new transaction.
        async with session.begin():
            user = await users.find_by_phone(session, phone)
            if user is None:
                raise ApiError(
                    503, "REGISTRATION_RETRY", "用户创建暂时失败，请重新获取验证码"
                ) from exc
            return public_user(user)


def public_user(user: User) -> CurrentUser:
    return CurrentUser(
        id=user.id, nick_name=user.nick_name, icon=user.icon, phone_masked=mask_phone(user.phone)
    )


async def login(
    store: SessionStore, db: AsyncSession, session: BrowserSession, phone: str, code: str
):
    # 1. 校验并一次性消费验证码，后续数据库或 Redis 失败也不允许重放。
    await store.consume_code(session, phone, code)
    # 2. 在 MySQL 事务中查找或注册用户，手机号唯一约束处理并发首次登录。
    user = await find_or_register(db, phone)
    # MySQL and Redis do not share a transaction. On failure the code stays consumed;
    # request a new code. A committed user may exist, but no false login success is returned.
    # 3. 创建新的登录会话并撤销旧会话，路由再把新的 SID 写入 Cookie。
    return await store.rotate(session, user)
