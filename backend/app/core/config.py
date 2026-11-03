from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, Request
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR.parent / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "PerkFlow"
    # 模型配置仅由本地环境提供；不接受浏览器传入URL、Key或模型名。
    llm_base_url: str = ""
    # 公网HTTP默认拒绝；仅由本地配置显式放行已确认的精确origin，不接受请求参数覆盖。
    llm_http_allowed_origins: list[str] = Field(default_factory=list)
    llm_model: str = ""
    llm_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    llm_timeout: int = Field(default=25, ge=1, le=60)
    agent_timeout: int = Field(default=90, ge=5, le=180)
    agent_max_rounds: int = Field(default=8, ge=1, le=12)
    llm_extra_body: dict = Field(default_factory=dict)
    # 异步版是当前默认；sync 仅供隔离环境复习旧流程，不在同一活动混跑两条入口。
    order_mode: Literal["async", "sync"] = "async"
    # 默认入口只访问 Redis；redis/database 保留前两阶段的独立学习版本，不同券模式不混跑。
    order_admission_mode: Literal["redis_first", "redis", "database"] = "redis_first"
    seckill_key_prefix: str = Field(
        default="perkflow:seckill:", min_length=1, max_length=128, pattern=r"^[\w:-]+:$"
    )
    rabbitmq_host: str = "127.0.0.1"
    rabbitmq_port: int = Field(default=5672, ge=1, le=65535)
    rabbitmq_user: str = "perkflow_dev"
    rabbitmq_password: SecretStr = Field(default=SecretStr(""), repr=False)
    rabbitmq_vhost: str = "perkflow"
    rabbitmq_prefix: str = Field(default="perkflow.seckill", pattern=r"^[\w.-]{1,100}$")
    rabbitmq_timeout: int = Field(default=5, ge=1, le=30)
    order_worker_concurrency: int = Field(default=5, ge=1, le=20)
    order_retry_limit: int = Field(default=5, ge=1, le=10)
    order_delivery_lease: int = Field(default=30, ge=10, le=300)
    order_redelivery_seconds: int = Field(default=60, ge=10, le=3600)
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = "perkflow"
    mysql_user: str = "root"
    mysql_password: SecretStr = Field(default=SecretStr(""), repr=False)
    mysql_pool_size: int = Field(default=5, ge=1, le=50)
    mysql_max_overflow: int = Field(default=5, ge=0, le=50)
    mysql_connect_timeout: int = Field(default=5, ge=1, le=60)
    mysql_query_timeout: int = Field(default=10, ge=1, le=120)
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    redis_host: str = "127.0.0.1"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0, le=15)
    redis_password: SecretStr = Field(default=SecretStr(""), repr=False)
    redis_timeout: int = Field(default=2, ge=1, le=30)
    redis_key_prefix: str = Field(default="perkflow:auth:", min_length=1, max_length=128)
    cache_key_prefix: str = Field(default="perkflow:cache:", min_length=1, max_length=128)
    cache_lock_key_prefix: str = Field(default="perkflow:lock:", min_length=1, max_length=128)
    order_id_key_prefix: str = Field(
        default="perkflow:id:", min_length=1, max_length=128, pattern=r"^[\w:-]+:$"
    )
    order_lock_key_prefix: str = Field(
        default="perkflow:lock:order:", min_length=1, max_length=128, pattern=r"^[\w:-]+:$"
    )
    # 基础订单锁固定租约，单位秒；不等待、不续期，不能把租约当作业务超时保证。
    order_lock_ttl: int = Field(default=30, ge=1, le=300)
    # 重建租约与等待预算独立；租约过期后不允许旧持有者回填，等待耗尽不直接冲击数据库。
    shop_cache_lock_ttl_ms: int = Field(default=15000, ge=100, le=600000)
    shop_cache_lock_wait_ms: int = Field(default=1000, ge=1, le=10000)
    shop_cache_lock_retry_ms: int = Field(default=50, ge=1, le=1000)
    # 查询缓存按回填时确定的期限过期，命中不续期；正结果叠加随机秒数分散到期。
    shop_cache_ttl: int = Field(default=1800, ge=1, le=86400)
    shop_cache_ttl_jitter: int = Field(default=300, ge=0, le=3600)
    # 不存在的商户只短暂缓存，减少重复回源，也允许新数据较快被重新确认。
    shop_null_cache_ttl: int = Field(default=120, ge=1, le=86400)
    shop_type_cache_ttl: int = Field(default=1800, ge=1, le=86400)
    shop_type_cache_ttl_jitter: int = Field(default=300, ge=0, le=3600)
    session_cookie_name: str = "perkflow_session"
    session_cookie_secure: bool = False
    anonymous_session_ttl: int = Field(default=600, ge=60, le=3600)
    # 登录空闲期限：成功访问业务 API 后，Redis 和 Cookie 重新获得此有效期。
    login_session_ttl: int = Field(default=3600, ge=60, le=604800)
    sms_code_ttl: int = Field(default=300, ge=1, le=600)
    sms_send_interval: int = Field(default=60, ge=1, le=300)
    sms_max_attempts: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def validate_cache_lock_namespace(self) -> "Settings":
        # 两种 Key 都以 shop:{id} 结尾，前缀相同会让缓存内容和租约互相覆盖。
        if self.cache_lock_key_prefix == self.cache_key_prefix:
            raise ValueError("CACHE_LOCK_KEY_PREFIX 必须与 CACHE_KEY_PREFIX 不同")
        if self.order_id_key_prefix in {
            self.redis_key_prefix,
            self.cache_key_prefix,
            self.cache_lock_key_prefix,
        }:
            raise ValueError("ORDER_ID_KEY_PREFIX 必须独立于认证、缓存和锁前缀")
        if self.order_lock_key_prefix in {
            self.redis_key_prefix,
            self.cache_key_prefix,
            self.cache_lock_key_prefix,
            self.order_id_key_prefix,
        }:
            raise ValueError("ORDER_LOCK_KEY_PREFIX 必须独立于认证、缓存、商户锁和编号前缀")
        if self.seckill_key_prefix in {
            self.redis_key_prefix,
            self.cache_key_prefix,
            self.cache_lock_key_prefix,
            self.order_id_key_prefix,
            self.order_lock_key_prefix,
        }:
            raise ValueError("SECKILL_KEY_PREFIX 必须独立于其他 Redis 命名空间")
        return self

    @property
    def database_url(self) -> URL:
        # URL.create handles @, :, /, # etc. in passwords without string interpolation.
        return URL.create(
            "mysql+aiomysql",
            username=self.mysql_user,
            password=self.mysql_password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
            query={"charset": "utf8mb4"},
        )


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
