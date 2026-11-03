"""单券授权与异步请求关联；不修改库存、订单或消息结构。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    # 1. 只有明确确认才写授权收据；状态独立于会话JSON，避免模型保存覆盖异步进度。
    op.create_table(
        "tb_agent_purchase",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("tb_agent_conversation.id", name="fk_purchase_conversation"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("tb_user.id", name="fk_purchase_user"),
            nullable=False,
        ),
        sa.Column(
            "voucher_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("tb_voucher.id", name="fk_purchase_voucher"),
            nullable=False,
        ),
        sa.Column("snapshot", mysql.JSON(), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, comment="submitting/pending/succeeded/failed"
        ),
        sa.Column("request_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
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
        sa.CheckConstraint("JSON_TYPE(snapshot) = 'OBJECT'", name="ck_agent_purchase_snapshot"),
        comment="用户点击确认的单券授权快照与异步请求关联，不替代订单事实",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("idx_agent_purchase_user_status", "tb_agent_purchase", ["user_id", "status"])
    op.create_index(
        "idx_agent_purchase_conversation", "tb_agent_purchase", ["conversation_id", "created_at"]
    )


def downgrade():
    # 2. 已授权记录用于恢复不确定结果，不允许为了回退而丢失关联。
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM tb_agent_purchase")):
        raise RuntimeError("已有购买授权，不能无损回退至0006")
    op.drop_table("tb_agent_purchase")
