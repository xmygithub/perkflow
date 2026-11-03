"""保留取消历史，按有效订单限制一人一单。

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 同一 ALTER 内增加状态/时间/生成列，并替换唯一索引；不先移除旧防线再分步补回。
    #    已有 created 订单生成原 voucher_id，不删除、重编号或改写库存。
    op.execute("""
        ALTER TABLE tb_voucher_order
        DROP CHECK ck_order_status,
        ADD COLUMN cancelled_at DATETIME(6) NULL COMMENT '用户主动取消时间，UTC；未取消为 NULL',
        ADD COLUMN active_voucher_id BIGINT UNSIGNED
          GENERATED ALWAYS AS (CASE WHEN status = 'created' THEN voucher_id ELSE NULL END) STORED,
        DROP INDEX uk_order_user_voucher,
        ADD CONSTRAINT uk_order_user_active_voucher UNIQUE (user_id, active_voucher_id),
        ADD CONSTRAINT ck_order_status CHECK (status IN ('created', 'cancelled')),
        ADD CONSTRAINT ck_order_cancellation CHECK (
          (status = 'created' AND cancelled_at IS NULL) OR
          (status = 'cancelled' AND cancelled_at IS NOT NULL)
        ),
        MODIFY COLUMN status VARCHAR(16) NOT NULL DEFAULT 'created'
          COMMENT 'created 或 cancelled，不等同于支付状态'
    """)


def downgrade() -> None:
    # 2. 旧版本无法表达取消历史和重买记录，不能为了回退偷偷删除历史订单。
    if op.get_bind().scalar(text("SELECT COUNT(*) FROM tb_voucher_order WHERE status='cancelled'")):
        raise RuntimeError("已有取消历史，不能无损回退至 0001；请保留数据并使用 0002")
    op.execute("""
        ALTER TABLE tb_voucher_order
        DROP CHECK ck_order_cancellation,
        DROP CHECK ck_order_status,
        DROP INDEX uk_order_user_active_voucher,
        DROP COLUMN active_voucher_id,
        DROP COLUMN cancelled_at,
        ADD CONSTRAINT uk_order_user_voucher UNIQUE (user_id, voucher_id),
        ADD CONSTRAINT ck_order_status CHECK (status = 'created'),
        MODIFY COLUMN status VARCHAR(16) NOT NULL DEFAULT 'created'
          COMMENT '当前仅支持 created，不等同于支付成功'
    """)
