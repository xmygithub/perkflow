"""增加团购使用规则与只读助手会话，不变更原库存、订单或活动。

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新增独立资料表，历史券无结构化规则时保持未知，不能自动猜测。
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

    options = dict(
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci"
    )
    op.create_table(
        "tb_voucher_usage",
        sa.Column(
            "voucher_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("tb_voucher.id", name="fk_usage_voucher"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column(
            "spec", mysql.JSON(), nullable=False, comment="UsageRule v1，写入和读取均由Pydantic校验"
        ),
        *timestamps(),
        sa.CheckConstraint("JSON_TYPE(spec) = 'OBJECT'", name="ck_usage_spec_object"),
        comment="结构化使用规则；缺资料表示未知，不从营销文案推断",
        **options,
    )
    # 2. 会话按用户隔离，版本用于完成时CAS，模型调用期间不持有SQL锁或事务。
    op.create_table(
        "tb_agent_conversation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("tb_user.id", name="fk_agent_user"),
            nullable=False,
        ),
        sa.Column(
            "version", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("state", mysql.JSON(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("JSON_TYPE(state) = 'OBJECT'", name="ck_agent_state_object"),
        comment="团购只读助手会话，完成一轮后保存业务状态，不保存内部思维链",
        **options,
    )
    op.create_index("idx_agent_user_updated", "tb_agent_conversation", ["user_id", "updated_at"])


def downgrade() -> None:
    # 3. 有规则或对话时拒绝丢数据回退；只允许空学习库删除新增表。
    for table in ("tb_agent_conversation", "tb_voucher_usage"):
        if op.get_bind().scalar(sa.text(f"SELECT COUNT(*) FROM {table}")):
            raise RuntimeError("助手已有会话或使用规则，不能无损回退至0005")
    op.drop_table("tb_agent_conversation")
    op.drop_table("tb_voucher_usage")
