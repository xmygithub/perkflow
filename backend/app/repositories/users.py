from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import User


async def find_by_phone(session: AsyncSession, phone: str) -> User | None:
    return await session.scalar(select(User).where(User.phone == phone))


async def add_user(session: AsyncSession, phone: str, nick_name: str) -> User:
    user = User(phone=phone, nick_name=nick_name)
    session.add(user)
    await session.flush()
    return user
