"""持久化异步下单请求和投递恢复状态，保留既有订单/库存。

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 只新增请求表；订单表继续只表达 created/cancelled，库存不会被迁移重置。
    op.create_table(
        "tb_order_request",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("voucher_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column(
            "request_key", sa.String(36), nullable=False, comment="客户端每次购买意图的 UUID"
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "pending_voucher_id",
            mysql.BIGINT(unsigned=True),
            sa.Computed(
                "CASE WHEN status = 'pending' THEN voucher_id ELSE NULL END", persisted=True
            ),
        ),
        sa.Column("order_id", mysql.BIGINT(unsigned=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "attempts", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "deliveries", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("next_delivery_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("delivery_token", sa.String(32)),
        sa.Column("lease_until", mysql.DATETIME(fsp=6)),
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
        sa.ForeignKeyConstraint(["user_id"], ["tb_user.id"], name="fk_request_user"),
        sa.ForeignKeyConstraint(["voucher_id"], ["tb_voucher.id"], name="fk_request_voucher"),
        sa.ForeignKeyConstraint(["order_id"], ["tb_voucher_order.id"], name="fk_request_order"),
        sa.UniqueConstraint("user_id", "request_key", name="uk_request_user_key"),
        sa.UniqueConstraint("user_id", "pending_voucher_id", name="uk_request_user_pending"),
        sa.CheckConstraint("status IN ('pending','succeeded','failed')", name="ck_request_status"),
        sa.CheckConstraint(
            "(status = 'succeeded' AND order_id IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'pending' AND order_id IS NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND order_id IS NULL AND error_code IS NOT NULL)",
            name="ck_request_result",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        comment="异步购买请求、幂等终态和待投递记录；不自动过期",
    )
    op.create_index("idx_request_dispatch", "tb_order_request", ["status", "next_delivery_at"])
    op.create_index("idx_request_user_created", "tb_order_request", ["user_id", "created_at", "id"])


def downgrade() -> None:
    # 2. 请求包含待办或幂等历史，拒绝通过回退静默丢失；仅空表允许移除。
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM tb_order_request")):
        raise RuntimeError("已有异步请求，不能无损回退至 0002；请保留请求及幂等历史")
    op.drop_table("tb_order_request")
