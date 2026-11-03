"""普通券与秒杀券共享活动配置和购买链路，保留旧库存、订单及已配置时间。

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 保留历史表名/外键，更新语义；此 DDL 不删除记录或重建表。
    op.create_table_comment(
        "tb_seckill_voucher", "优惠券购买活动时间；普通券与秒杀券共用，保留历史表名"
    )
    for column, comment in [
        ("begin_time", "购买开始时间，UTC，含此时间点"),
        ("end_time", "购买结束时间，UTC，不含此时间点"),
    ]:
        op.alter_column(
            "tb_seckill_voucher",
            column,
            existing_type=mysql.DATETIME(fsp=6),
            existing_nullable=False,
            comment=comment,
        )
    # 2. 为尚未使用的普通券补一次固定30天窗口；已有活动/秒杀配置一律不覆盖。
    op.execute(
        sa.text("""
        INSERT INTO tb_seckill_voucher(voucher_id, begin_time, end_time)
        SELECT v.id, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6) + INTERVAL 30 DAY
        FROM tb_voucher v
        WHERE v.type = 0
          AND NOT EXISTS (SELECT 1 FROM tb_seckill_voucher a WHERE a.voucher_id=v.id)
          AND NOT EXISTS (SELECT 1 FROM tb_seckill_gate g WHERE g.voucher_id=v.id AND g.ready=1)
          AND NOT EXISTS (SELECT 1 FROM tb_order_request r WHERE r.voucher_id=v.id)
          AND NOT EXISTS (SELECT 1 FROM tb_voucher_order o WHERE o.voucher_id=v.id)
    """)
    )
    # 3. 仅修正上一批自有数据的精确旧提示；不重写自定义规则/金额/库存。
    op.execute(
        sa.text("""
        UPDATE tb_voucher
        SET rules=JSON_REPLACE(rules,
            JSON_UNQUOTE(JSON_SEARCH(rules, 'one',
                '虚构演示普通券，仅展示，不支持领取、支付或核销')),
            '虚构演示优惠券，可创建与取消订单，不涉及真实支付或核销')
        WHERE type=0 AND JSON_SEARCH(rules, 'one',
            '虚构演示普通券，仅展示，不支持领取、支付或核销') IS NOT NULL
    """)
    )
    op.execute(
        sa.text("""
        UPDATE tb_voucher SET sub_title='先读使用规则，再安排你的优惠'
        WHERE type=0 AND sub_title='普通展示券，先读使用规则再安排'
    """)
    )


def downgrade() -> None:
    connection = op.get_bind()
    # 4. 普通券可能已有 Redis-only 预占；就绪标记同样阻止回退，不能只检查 SQL 订单。
    for table in ["tb_order_request", "tb_voucher_order", "tb_seckill_gate"]:
        if connection.scalar(
            sa.text(
                f"SELECT COUNT(*) FROM {table} r JOIN tb_voucher v ON v.id=r.voucher_id "
                "WHERE v.type=0"
            )
        ):
            raise RuntimeError("普通券已有购买或预热历史，不能无损回退至 0004")
    op.execute(
        sa.text(
            "DELETE a FROM tb_seckill_voucher a JOIN tb_voucher v "
            "ON v.id=a.voucher_id WHERE v.type=0"
        )
    )
    op.create_table_comment(
        "tb_seckill_voucher", "秒杀时间配置；仅用于 type=1 的券，由 Service 保证"
    )
    for column, comment in [
        ("begin_time", "秒杀开始时间，UTC，含此时间点"),
        ("end_time", "秒杀结束时间，UTC，不含此时间点"),
    ]:
        op.alter_column(
            "tb_seckill_voucher",
            column,
            existing_type=mysql.DATETIME(fsp=6),
            existing_nullable=False,
            comment=comment,
        )
