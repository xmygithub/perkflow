"""使用本地连接配置创建随机测试库，运行后端测试并清理；不修改开发库。"""

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.services.voucher_activities import prepare_normal_activities

ROOT = Path(__file__).resolve().parents[1]


def run_tests_safely(arguments: list[str], env: dict, local: Settings) -> int:
    # SDK 异常回溯也可能包含连接 URL；不能只依赖应用日志过滤，所有测试输出统一脱敏。
    values = [
        getattr(local, name).get_secret_value()
        for name in ["mysql_password", "redis_password", "rabbitmq_password", "llm_api_key"]
    ]
    private = sorted(
        {
            version
            for value in values
            if value
            for version in (value, quote(value, safe=""), quote_plus(value))
        },
        key=len,
        reverse=True,
    )
    env["PYTHONIOENCODING"] = "utf-8"
    with subprocess.Popen(
        [sys.executable, "-m", "pytest", "--tb=short", *arguments],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        for line in process.stdout:
            for value in private:
                line = line.replace(value, "[REDACTED]")
            print(line, end="", flush=True)
        return process.wait()


def migrate_schema(connection, revision="head") -> None:
    # 真实执行迁移链，避免只用 ORM create_all 而漏测已有库升级。
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


async def provision(settings: Settings) -> None:
    database = Database(settings)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(migrate_schema)
        # 种子脚本仅在新创建的空测试库执行；去掉 USE，不能切回用户开发库。
        source = (ROOT / "scripts/sql/002_demo_seed.sql").read_text(encoding="utf-8")
        lines = [line for line in source.splitlines() if not line.lstrip().startswith("--")]
        async with database.engine.connect() as connection:
            for statement in "\n".join(lines).split(";"):
                statement = statement.strip()
                if statement and not statement.upper().startswith("USE "):
                    await connection.execute(text(statement))
            await connection.commit()
        # 0005 可能先于空库种子执行；导入后补普通券活动，仍保留冻结的初始 SQL。
        async with database.sessions() as session, session.begin():
            await prepare_normal_activities(session)
    finally:
        await database.close()


async def main() -> int:
    # 1. 库名由脚本生成并检查，绝不接受开发库名用于建库或删除。
    local = Settings()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    database_name = f"perkflow_test_orders_{secrets.token_hex(6)}"
    assert database_name.startswith("perkflow_test_") and database_name.isidentifier()
    namespace = f"perkflow:test:orders:{secrets.token_hex(8)}:"
    admin = create_async_engine(local.database_url.set(database=None), isolation_level="AUTOCOMMIT")
    created = False
    try:
        async with admin.connect() as connection:
            await connection.execute(
                text(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
            created = True
        await provision(local.model_copy(update={"mysql_database": database_name}))
        # 2. 凭据仅通过子进程环境传递，不拼接命令行、不打印配置或完整异常。
        env = os.environ.copy()
        arguments = list(sys.argv[1:])
        # MQ 测试必须显式开启，使用随机独立队列/交换机，绝不消费业务队列。
        if "--with-rabbitmq" in arguments:
            arguments.remove("--with-rabbitmq")
            env["PERKFLOW_TEST_RABBITMQ"] = "1"
        for key, value in {
            "MYSQL_DATABASE": database_name,
            "PERKFLOW_TEST_MYSQL_DATABASE": database_name,
            "PERKFLOW_TEST_MYSQL_HOST": local.mysql_host,
            "PERKFLOW_TEST_MYSQL_PORT": local.mysql_port,
            "PERKFLOW_TEST_MYSQL_USER": local.mysql_user,
            "PERKFLOW_TEST_MYSQL_PASSWORD": local.mysql_password.get_secret_value(),
            "PERKFLOW_TEST_REDIS_HOST": local.redis_host,
            "PERKFLOW_TEST_REDIS_PORT": local.redis_port,
            "PERKFLOW_TEST_REDIS_DB": local.redis_db,
            "PERKFLOW_TEST_REDIS_PASSWORD": local.redis_password.get_secret_value(),
            "PERKFLOW_TEST_REDIS_PREFIX": namespace,
        }.items():
            env[key] = str(value)
        print("Running backend tests in a new isolated MySQL database.", flush=True)
        return await asyncio.to_thread(
            run_tests_safely, arguments or ["tests/backend", "-q"], env, local
        )
    except Exception as error:
        print(
            f"Isolated test setup failed ({type(error).__name__}); "
            "verify local services and permissions."
        )
        return 1
    finally:
        # 3. 只删除本次随机测试库；各测试的 Redis Key 使用随机隔离前缀。
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f"DROP DATABASE `{database_name}`"))
            print("Isolated test database removed.", flush=True)
        await admin.dispose()
        redis = create_redis_client(local)
        try:
            keys = [key async for key in redis.scan_iter(match=namespace + "*")]
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
