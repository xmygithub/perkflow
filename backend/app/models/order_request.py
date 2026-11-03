"""下单请求兼作持久化待投递记录；终态保留，阻止旧消息复活已取消的购买。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, Computed, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.entities import TABLE_OPTIONS, Base, Timestamps


class OrderRequest(Timestamps, Base):
    __tablename__ = "tb_order_request"
    __table_args__ = (
        UniqueConstraint("user_id", "request_key", name="uk_request_user_key"),
        UniqueConstraint("user_id", "pending_voucher_id", name="uk_request_user_pending"),
        Index("idx_request_dispatch", "status", "next_delivery_at"),
        Index("idx_request_user_created", "user_id", "created_at", "id"),
        CheckConstraint("status IN ('pending','succeeded','failed')", name="ck_request_status"),
        CheckConstraint(
            "(status = 'succeeded' AND order_id IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'pending' AND order_id IS NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND order_id IS NULL AND error_code IS NOT NULL)",
            name="ck_request_result",
        ),
        {**TABLE_OPTIONS, "comment": "异步购买请求、幂等终态和待投递记录；不自动过期"},
    )
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_user.id", name="fk_request_user")
    )
    voucher_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_voucher.id", name="fk_request_voucher")
    )
    request_key: Mapped[str] = mapped_column(String(36), comment="客户端每次购买意图的 UUID")
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    pending_voucher_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed(
            "CASE WHEN status = _utf8mb4'pending' THEN voucher_id ELSE NULL END", persisted=True
        ),
    )
    order_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_voucher_order.id", name="fk_request_order")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(INTEGER(unsigned=True), server_default=text("0"))
    deliveries: Mapped[int] = mapped_column(INTEGER(unsigned=True), server_default=text("0"))
    next_delivery_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    delivery_token: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    reservation_generation: Mapped[str | None] = mapped_column(
        String(32), comment="成功准入的 Redis 库存代次；NULL 表示尚未准入或历史请求"
    )
