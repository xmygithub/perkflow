"""独立空测试库验证冻结基线、新迁移保留数据和可回退边界，不改开发库。"""

import secrets
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import Database
from backend.app.core.schema_check import compare_schema, initial_metadata
from scripts.run_backend_tests import ROOT, migrate_schema, provision

pytestmark = pytest.mark.mysql


def downgrade_schema(sync):
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.attributes["connection"] = sync
    command.downgrade(config, "0001")


def downgrade_to_sync(sync):
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.attributes["connection"] = sync
    command.downgrade(config, "0002")


@pytest_asyncio.fixture
async def migration_database(mysql_settings):
    name = f"perkflow_test_migration_{secrets.token_hex(6)}"
    assert name.startswith("perkflow_test_") and name.isidentifier()
    admin = create_async_engine(
        mysql_settings.database_url.set(database=None), isolation_level="AUTOCOMMIT"
    )
    database = Database(mysql_settings.model_copy(update={"mysql_database": name}))
    created = False
    try:
        async with admin.connect() as connection:
            await connection.execute(
                text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            )
            created = True
        yield database
    finally:
        await database.close()
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f"DROP DATABASE `{name}`"))
        await admin.dispose()


async def test_upgrade_preserves_created_order_and_latest_schema_matches_orm(migration_database):
    database = migration_database
    async with database.engine.begin() as connection:
        await connection.run_sync(lambda sync: migrate_schema(sync, "0001"))
        assert (
            await connection.run_sync(lambda sync: compare_schema(sync, initial_metadata())) == []
        )
        # 在旧结构插入真实关联行；迁移不得删除记录、重编号或重置库存。
        await connection.execute(
            text("INSERT INTO tb_user(id,phone,nick_name) VALUES(1,'13900000001','迁移测试')")
        )
        await connection.execute(
            text("INSERT INTO tb_shop_type(id,name,icon,sort) VALUES(1,'测试','icon.png',1)")
        )
        await connection.execute(
            text(
                "INSERT INTO tb_shop(id,type_id,name,subtitle,kind,city,area,address,images,tags,"
                "score,avg_price,open_hours,description) VALUES(1,1,'测试','测试','测试','天津',"
                "'测试','测试',JSON_ARRAY('image.png'),JSON_ARRAY(),4.8,100,'全天','测试')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO tb_voucher(id,shop_id,title,sub_title,rules,pay_value,actual_value,"
                "type,status,total_stock,stock,validity_days) "
                "VALUES(1,1,'测试','测试',JSON_ARRAY(),100,200,1,1,2,1,30)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO tb_voucher_order(id,user_id,voucher_id,pay_value,status,expires_at) "
                "VALUES(987654321,1,1,100,'created',UTC_TIMESTAMP(6)+INTERVAL 30 DAY)"
            )
        )
    async with database.engine.begin() as connection:
        await connection.run_sync(migrate_schema)
        assert await connection.run_sync(compare_schema) == []
        row = (
            await connection.execute(
                text("SELECT id,status,active_voucher_id,cancelled_at FROM tb_voucher_order")
            )
        ).one()
        assert tuple(row) == (987654321, "created", 1, None)
        assert await connection.scalar(text("SELECT stock FROM tb_voucher WHERE id=1")) == 1
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
        await connection.execute(
            text("UPDATE tb_voucher_order SET status='cancelled',cancelled_at=UTC_TIMESTAMP(6)")
        )
    with pytest.raises(RuntimeError, match="不能无损回退"):
        # 分别提交空请求表的回退，再验证旧迁移拒绝删除取消历史；避免跨多段 DDL 回退。
        async with database.engine.begin() as connection:
            await connection.run_sync(downgrade_to_sync)
        async with database.engine.begin() as connection:
            await connection.run_sync(downgrade_schema)
    async with database.engine.connect() as connection:
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
        assert await connection.scalar(text("SELECT status FROM tb_voucher_order")) == "cancelled"


async def test_empty_database_can_downgrade_and_upgrade_again(
    migration_database,
):
    database = migration_database

    async with database.engine.begin() as connection:
        await connection.run_sync(migrate_schema)
    async with database.engine.begin() as connection:
        await connection.run_sync(downgrade_schema)
        assert (
            await connection.run_sync(lambda sync: compare_schema(sync, initial_metadata())) == []
        )
    # 空库无历史损失时可逆，随后重新升级。
    async with database.engine.begin() as connection:
        await connection.run_sync(migrate_schema)
        assert await connection.run_sync(compare_schema) == []


async def test_shared_activity_upgrade_preserves_stock_windows_and_blocks_used_normal_downgrade(
    migration_database, mysql_settings
):
    database = migration_database
    await provision(
        mysql_settings.model_copy(update={"mysql_database": database.engine.url.database})
    )

    def previous(sync):
        config = Config(str(ROOT / "backend/alembic.ini"))
        config.attributes["connection"] = sync
        command.downgrade(config, "0004")

    async with database.engine.begin() as connection:
        await connection.run_sync(previous)
        await connection.execute(text("UPDATE tb_voucher SET stock=29 WHERE id=101"))
        await connection.execute(
            text("UPDATE tb_voucher SET sub_title=:subtitle, rules=JSON_ARRAY(:rule) WHERE id=101"),
            {
                "subtitle": "普通展示券，先读使用规则再安排",
                "rule": "虚构演示普通券，仅展示，不支持领取、支付或核销",
            },
        )
        before = (
            await connection.execute(
                text(
                    "SELECT voucher_id,begin_time,end_time FROM tb_seckill_voucher "
                    "ORDER BY voucher_id"
                )
            )
        ).all()
    async with database.engine.begin() as connection:
        await connection.run_sync(migrate_schema)
        assert await connection.run_sync(compare_schema) == []
        assert await connection.scalar(text("SELECT stock FROM tb_voucher WHERE id=101")) == 29
        for identifier, begin, end in before:
            row = (
                await connection.execute(
                    text("SELECT begin_time,end_time FROM tb_seckill_voucher WHERE voucher_id=:id"),
                    {"id": identifier},
                )
            ).one()
            assert tuple(row) == (begin, end)
        normal = (
            await connection.execute(
                text("SELECT begin_time,end_time FROM tb_seckill_voucher WHERE voucher_id=101")
            )
        ).one()
        assert (normal.end_time - normal.begin_time).days == 30
        assert "仅展示" not in await connection.scalar(
            text("SELECT rules FROM tb_voucher WHERE id=101")
        )
    async with database.engine.begin() as connection:
        await connection.run_sync(migrate_schema)
        assert tuple(
            (
                await connection.execute(
                    text("SELECT begin_time,end_time FROM tb_seckill_voucher WHERE voucher_id=101")
                )
            ).one()
        ) == tuple(normal)
        await connection.execute(
            text(
                "INSERT INTO tb_seckill_gate(voucher_id,generation,ready) VALUES(101,:generation,1)"
            ),
            {"generation": uuid4().hex},
        )

    # 本例只验证0005的保护：先单独提交空助手表回退，避免跨多版本失败后的DDL边界混淆。
    def assistant_previous(sync):
        config = Config(str(ROOT / "backend/alembic.ini"))
        config.attributes["connection"] = sync
        command.downgrade(config, "0005")

    async with database.engine.begin() as connection:
        await connection.run_sync(assistant_previous)
    with pytest.raises(RuntimeError, match="不能无损回退"):
        async with database.engine.begin() as connection:
            await connection.run_sync(previous)
    async with database.engine.connect() as connection:
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
