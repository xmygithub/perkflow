import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.core.config import Settings
from backend.app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=Settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    settings = Settings()
    engine = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.mysql_connect_timeout,
            "init_command": "SET SESSION time_zone = '+00:00'",
        },
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # 测试及初始化可传入已选定的连接，避免读取本地配置后误切回开发库。
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        migrate(supplied_connection)
    else:
        asyncio.run(run_migrations_online())
