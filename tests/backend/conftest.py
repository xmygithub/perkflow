import os
import secrets

import pytest

from backend.app.core.config import Settings


@pytest.fixture
def mysql_settings():
    database = os.getenv("PERKFLOW_TEST_MYSQL_DATABASE", "")
    if not database:
        pytest.skip("Set PERKFLOW_TEST_MYSQL_DATABASE to an isolated, seeded test database")
    if not database.startswith("perkflow_test_"):
        pytest.fail("Integration tests only accept database names starting with perkflow_test_")
    cache_namespace = f"perkflow:test:mysql-cache:{secrets.token_hex(8)}:"
    return Settings(
        _env_file=None,
        order_mode="sync",  # 历史专项继续验证同步基线；异步专项显式覆盖。
        order_admission_mode="database",  # 第一阶段/同步专项，第二阶段另行显式覆盖。
        seckill_key_prefix=f"{cache_namespace}seckill:",
        mysql_database=database,
        mysql_host=os.getenv("PERKFLOW_TEST_MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.getenv("PERKFLOW_TEST_MYSQL_PORT", "3306")),
        mysql_user=os.getenv("PERKFLOW_TEST_MYSQL_USER", "root"),
        mysql_password=os.getenv("PERKFLOW_TEST_MYSQL_PASSWORD", ""),
        cache_key_prefix=cache_namespace,
        cache_lock_key_prefix=f"{cache_namespace}lock:",
        order_id_key_prefix=f"{cache_namespace}id:",
        order_lock_key_prefix=f"{cache_namespace}order-lock:",
        redis_host=os.getenv("PERKFLOW_TEST_REDIS_HOST", "127.0.0.1"),
        redis_port=int(os.getenv("PERKFLOW_TEST_REDIS_PORT", "6379")),
        redis_db=int(os.getenv("PERKFLOW_TEST_REDIS_DB", "0")),
        redis_password=os.getenv("PERKFLOW_TEST_REDIS_PASSWORD", ""),
    )


@pytest.fixture
def redis_settings():
    prefix = os.getenv("PERKFLOW_TEST_REDIS_PREFIX", "")
    if not prefix:
        pytest.skip("Set PERKFLOW_TEST_REDIS_PREFIX to an isolated namespace")
    if not prefix.startswith("perkflow:test:") or any(c in prefix for c in "*?[]"):
        pytest.fail("Redis tests only accept literal prefixes starting with perkflow:test:")
    namespace = f"{prefix}{secrets.token_hex(8)}:"
    return Settings(
        _env_file=None,
        redis_key_prefix=namespace,
        seckill_key_prefix=f"{namespace}seckill:",
        cache_key_prefix=f"{namespace}cache:",
        cache_lock_key_prefix=f"{namespace}lock:",
        order_id_key_prefix=f"{namespace}id:",
        order_lock_key_prefix=f"{namespace}order-lock:",
        redis_host=os.getenv("PERKFLOW_TEST_REDIS_HOST", "127.0.0.1"),
        redis_port=int(os.getenv("PERKFLOW_TEST_REDIS_PORT", "6379")),
        redis_db=int(os.getenv("PERKFLOW_TEST_REDIS_DB", "0")),
        redis_password=os.getenv("PERKFLOW_TEST_REDIS_PASSWORD", ""),
    )
