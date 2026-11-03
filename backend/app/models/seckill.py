"""Redis 准入代次和确认/释放待办；业务事实仍保留在 MySQL。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, INTEGER, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.entities import TABLE_OPTIONS, Base, Timestamps


class SeckillGate(Timestamps, Base):
    __tablename__ = "tb_seckill_gate"
    __table_args__ = (
        CheckConstraint("ready IN (0,1)", name="ck_gate_ready"),
        {**TABLE_OPTIONS, "comment": "Redis 库存初始化标记；丢 Key 不自动重置"},
    )
    voucher_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_voucher.id", name="fk_gate_voucher"), primary_key=True
    )
    generation: Mapped[str] = mapped_column(String(32))
    ready: Mapped[int] = mapped_column(TINYINT(unsigned=True), server_default=text("0"))


class SeckillEffect(Timestamps, Base):
    __tablename__ = "tb_seckill_effect"
    __table_args__ = (
        UniqueConstraint("action", "request_id", name="uk_effect_action_request"),
        CheckConstraint("action IN ('confirm','release','cancel')", name="ck_effect_action"),
        CheckConstraint("applied IN (0,1)", name="ck_effect_applied"),
        Index("idx_effect_due", "applied", "next_attempt_at"),
        {**TABLE_OPTIONS, "comment": "与订单终态/取消一起提交的 Redis 同步待办；保留历史"},
    )
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    voucher_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_voucher.id", name="fk_effect_voucher")
    )
    request_id: Mapped[int] = mapped_column(BIGINT(unsigned=True))
    user_id: Mapped[int] = mapped_column(BIGINT(unsigned=True))
    generation: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    applied: Mapped[int] = mapped_column(TINYINT(unsigned=True), server_default=text("0"))
    attempts: Mapped[int] = mapped_column(INTEGER(unsigned=True), server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
