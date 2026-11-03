import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=settings.mysql_pool_size,
            max_overflow=settings.mysql_max_overflow,
            pool_timeout=settings.mysql_connect_timeout,
            connect_args={
                "connect_timeout": settings.mysql_connect_timeout,
                "init_command": "SET SESSION time_zone = '+00:00'",
            },
            hide_parameters=True,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # One session per request; closing it rolls back any uncommitted transaction.
    async with asyncio.timeout(request.app.state.settings.mysql_query_timeout):
        async with request.app.state.database.sessions() as session:
            yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
