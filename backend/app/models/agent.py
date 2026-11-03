"""只读助手的业务资料与会话；不引入通用运行时或逐步检查点。"""

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.mysql import BIGINT, INTEGER, JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.entities import TABLE_OPTIONS, Base, Timestamps


class VoucherUsage(Timestamps, Base):
    __tablename__ = "tb_voucher_usage"
    __table_args__ = (
        CheckConstraint("JSON_TYPE(spec) = 'OBJECT'", name="ck_usage_spec_object"),
        {**TABLE_OPTIONS, "comment": "结构化使用规则；缺资料表示未知，不从营销文案推断"},
    )
    voucher_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("tb_voucher.id", name="fk_usage_voucher"),
        primary_key=True,
        autoincrement=False,
    )
    spec: Mapped[dict] = mapped_column(JSON, comment="UsageRule v1，写入和读取均由Pydantic校验")


class AgentConversation(Timestamps, Base):
    __tablename__ = "tb_agent_conversation"
    __table_args__ = (
        Index("idx_agent_user_updated", "user_id", "updated_at"),
        CheckConstraint("JSON_TYPE(state) = 'OBJECT'", name="ck_agent_state_object"),
        {**TABLE_OPTIONS, "comment": "团购只读助手会话，完成一轮后保存业务状态，不保存内部思维链"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_user.id", name="fk_agent_user")
    )
    version: Mapped[int] = mapped_column(INTEGER(unsigned=True), server_default=text("0"))
    state: Mapped[dict] = mapped_column(JSON)


class AgentPurchase(Timestamps, Base):
    __tablename__ = "tb_agent_purchase"
    __table_args__ = (
        Index("idx_agent_purchase_user_status", "user_id", "status"),
        Index("idx_agent_purchase_conversation", "conversation_id", "created_at"),
        CheckConstraint("JSON_TYPE(snapshot) = 'OBJECT'", name="ck_agent_purchase_snapshot"),
        {**TABLE_OPTIONS, "comment": "用户点击确认的单券授权快照与异步请求关联，不替代订单事实"},
    )
    # UUID5专用于本模块意图，原交易UUID合同不变；不是身份凭据。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tb_agent_conversation.id", name="fk_purchase_conversation")
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_user.id", name="fk_purchase_user")
    )
    voucher_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_voucher.id", name="fk_purchase_voucher")
    )
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), comment="submitting/pending/succeeded/failed")
    request_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
