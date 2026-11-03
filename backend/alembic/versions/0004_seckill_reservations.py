"""Redis 预扣代次和持久化确认/补偿待办，不重置原库存。

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
        ),
    ]


def upgrade() -> None:
    # 1. 只扩展请求并新建恢复表；历史订单和券库存保持原值，首次按当前事实初始化。
    op.add_column(
        "tb_order_request",
        sa.Column(
            "reservation_generation",
            sa.String(32),
            comment="成功准入的 Redis 库存代次；NULL 表示尚未准入或历史请求",
        ),
    )
    options = dict(
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci"
    )
    op.create_table(
        "tb_seckill_gate",
        sa.Column("voucher_id", mysql.BIGINT(unsigned=True), primary_key=True),
        sa.Column("generation", sa.String(32), nullable=False),
        sa.Column(
            "ready", mysql.TINYINT(unsigned=True), nullable=False, server_default=sa.text("0")
        ),
        *timestamps(),
        sa.ForeignKeyConstraint(["voucher_id"], ["tb_voucher.id"], name="fk_gate_voucher"),
        sa.CheckConstraint("ready IN (0,1)", name="ck_gate_ready"),
        comment="Redis 库存初始化标记；丢 Key 不自动重置",
        **options,
    )
    op.create_table(
        "tb_seckill_effect",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("voucher_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("request_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("generation", sa.String(32), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column(
            "applied", mysql.TINYINT(unsigned=True), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "attempts", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("next_attempt_at", mysql.DATETIME(fsp=6), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["voucher_id"], ["tb_voucher.id"], name="fk_effect_voucher"),
        sa.UniqueConstraint("action", "request_id", name="uk_effect_action_request"),
        sa.CheckConstraint("action IN ('confirm','release','cancel')", name="ck_effect_action"),
        sa.CheckConstraint("applied IN (0,1)", name="ck_effect_applied"),
        comment="与订单终态/取消一起提交的 Redis 同步待办；保留历史",
        **options,
    )
    op.create_index("idx_effect_due", "tb_seckill_effect", ["applied", "next_attempt_at"])


def downgrade() -> None:
    # 2. 即使同步待办已执行也不能静默删除购买代次；只允许未使用的新表回退。
    connection = op.get_bind()
    if (
        connection.scalar(sa.text("SELECT COUNT(*) FROM tb_order_request"))
        or connection.scalar(sa.text("SELECT COUNT(*) FROM tb_seckill_gate"))
        or connection.scalar(sa.text("SELECT COUNT(*) FROM tb_seckill_effect"))
        or connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM tb_order_request WHERE reservation_generation IS NOT NULL"
            )
        )
    ):
        raise RuntimeError("已有预扣/补偿历史，不能无损回退至 0003")
    op.drop_table("tb_seckill_effect")
    op.drop_table("tb_seckill_gate")
    op.drop_column("tb_order_request", "reservation_generation")
