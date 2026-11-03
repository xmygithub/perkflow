from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import (
    BIGINT,
    DATETIME,
    DECIMAL,
    INTEGER,
    JSON,
    SMALLINT,
    TINYINT,
    VARCHAR,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


class Base(DeclarativeBase):
    pass


class Timestamps:
    # DATETIME has no timezone metadata; all database connections use UTC.
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
    )


class User(Timestamps, Base):
    __tablename__ = "tb_user"
    __table_args__ = (
        UniqueConstraint("phone", name="uk_user_phone"),
        CheckConstraint("CHAR_LENGTH(phone) = 11", name="ck_user_phone_length"),
        {**TABLE_OPTIONS, "comment": "用户；验证码与登录态由后续认证模块管理"},
    )
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(VARCHAR(11, charset="ascii", collation="ascii_bin"))
    nick_name: Mapped[str] = mapped_column(String(32))
    icon: Mapped[str | None] = mapped_column(String(255))


class ShopType(Timestamps, Base):
    __tablename__ = "tb_shop_type"
    __table_args__ = (
        UniqueConstraint("name", name="uk_shop_type_name"),
        {**TABLE_OPTIONS, "comment": "商户分类"},
    )
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32))
    icon: Mapped[str] = mapped_column(String(255), comment="静态资源相对路径")
    sort: Mapped[int] = mapped_column(INTEGER(unsigned=True), server_default=text("0"))


class Shop(Timestamps, Base):
    __tablename__ = "tb_shop"
    __table_args__ = (
        Index("idx_shop_type_city_area", "type_id", "city", "area"),
        CheckConstraint("score BETWEEN 0.0 AND 5.0", name="ck_shop_score"),
        CheckConstraint(
            "JSON_TYPE(images) = 'ARRAY' AND JSON_LENGTH(images) > 0", name="ck_shop_images"
        ),
        CheckConstraint("JSON_TYPE(tags) = 'ARRAY'", name="ck_shop_tags"),
        {**TABLE_OPTIONS, "comment": "商户；当前不录入虚构经纬度"},
    )
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    type_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_shop_type.id", name="fk_shop_type")
    )
    name: Mapped[str] = mapped_column(String(128))
    subtitle: Mapped[str] = mapped_column(String(255), server_default=text("''"))
    kind: Mapped[str] = mapped_column(String(64), comment="细分类展示文案，如茶馆、西餐")
    city: Mapped[str] = mapped_column(String(32))
    area: Mapped[str] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(String(255))
    images: Mapped[list[str]] = mapped_column(JSON, comment="有序图片路径数组，不存逗号拼接文本")
    tags: Mapped[list[str]] = mapped_column(JSON, comment="展示标签数组")
    score: Mapped[Decimal] = mapped_column(DECIMAL(2, 1), server_default=text("0.0"))
    avg_price: Mapped[int] = mapped_column(INTEGER(unsigned=True), comment="人均金额，单位：分")
    open_hours: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)


class Voucher(Timestamps, Base):
    __tablename__ = "tb_voucher"
    __table_args__ = (
        Index("idx_voucher_shop_status", "shop_id", "status"),
        CheckConstraint("type IN (0, 1)", name="ck_voucher_type"),
        CheckConstraint("status IN (0, 1)", name="ck_voucher_status"),
        CheckConstraint("pay_value > 0 AND actual_value >= pay_value", name="ck_voucher_values"),
        CheckConstraint("stock <= total_stock", name="ck_voucher_stock"),
        CheckConstraint("validity_days > 0", name="ck_voucher_validity"),
        CheckConstraint("JSON_TYPE(rules) = 'ARRAY'", name="ck_voucher_rules"),
        {**TABLE_OPTIONS, "comment": "优惠券和唯一的数据库库存来源"},
    )
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_shop.id", name="fk_voucher_shop")
    )
    title: Mapped[str] = mapped_column(String(128))
    sub_title: Mapped[str] = mapped_column(String(255), server_default=text("''"))
    rules: Mapped[list[str]] = mapped_column(
        JSON, comment="规则文案数组；确定性业务规则由后端单独校验"
    )
    pay_value: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), comment="下单金额，单位：分；不代表已支付"
    )
    actual_value: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), comment="抵扣或套餐标示金额，单位：分"
    )
    type: Mapped[int] = mapped_column(TINYINT(unsigned=True), comment="0：普通券；1：秒杀券")
    status: Mapped[int] = mapped_column(
        TINYINT(unsigned=True),
        server_default=text("1"),
        comment="0：下架；1：上架；时间状态另行计算",
    )
    total_stock: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), comment="初始总库存；当前不实现补货"
    )
    stock: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), comment="MySQL 剩余库存，普通券和秒杀券统一在此维护"
    )
    validity_days: Mapped[int] = mapped_column(
        SMALLINT(unsigned=True),
        server_default=text("30"),
        comment="订单创建后有效天数；尚未实现核销",
    )


class SeckillVoucher(Timestamps, Base):
    __tablename__ = "tb_seckill_voucher"
    __table_args__ = (
        Index("idx_seckill_time", "begin_time", "end_time"),
        CheckConstraint("end_time > begin_time", name="ck_seckill_time"),
        {**TABLE_OPTIONS, "comment": "优惠券购买活动时间；普通券与秒杀券共用，保留历史表名"},
    )
    voucher_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("tb_voucher.id", name="fk_seckill_voucher"),
        primary_key=True,
        autoincrement=False,
    )
    begin_time: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), comment="购买开始时间，UTC，含此时间点"
    )
    end_time: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), comment="购买结束时间，UTC，不含此时间点"
    )


class VoucherOrder(Timestamps, Base):
    __tablename__ = "tb_voucher_order"
    __table_args__ = (
        UniqueConstraint("user_id", "active_voucher_id", name="uk_order_user_active_voucher"),
        Index("idx_order_user_created", "user_id", "created_at", "id"),
        Index("idx_order_voucher", "voucher_id"),
        CheckConstraint("pay_value > 0", name="ck_order_value"),
        CheckConstraint("status IN ('created', 'cancelled')", name="ck_order_status"),
        CheckConstraint(
            "(status = 'created' AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL)",
            name="ck_order_cancellation",
        ),
        CheckConstraint("expires_at > created_at", name="ck_order_expiry"),
        {**TABLE_OPTIONS, "comment": "已成功创建的演示订单，不存受理中请求或失败订单"},
    )
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_user.id", name="fk_order_user")
    )
    voucher_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tb_voucher.id", name="fk_order_voucher")
    )
    pay_value: Mapped[int] = mapped_column(
        INTEGER(unsigned=True), comment="创建订单时的金额快照，单位：分"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        server_default=text("'created'"),
        comment="created 或 cancelled，不等同于支付状态",
    )
    # 取消记录保留，但不再占用唯一购买资格；MySQL 唯一索引允许多条 NULL。
    active_voucher_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        # MySQL 反射会补字符集前缀，显式声明避免迁移比较误报生成表达式变化。
        Computed(
            "CASE WHEN status = _utf8mb4'created' THEN voucher_id ELSE NULL END", persisted=True
        ),
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=6), comment="用户主动取消时间，UTC；未取消为 NULL"
    )
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), comment="订单对应的券有效期，UTC")
