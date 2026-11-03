"""统一连接参数；认证和查询缓存使用各自的客户端，不混用存储职责。"""

from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis

from backend.app.core.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password.get_secret_value() or None,
        decode_responses=True,
        protocol=2,
        socket_connect_timeout=settings.redis_timeout,
        socket_timeout=settings.redis_timeout,
        max_connections=20,
    )


def get_cache_redis(request: Request) -> Redis:
    return request.app.state.cache_redis


CacheRedisDep = Annotated[Redis, Depends(get_cache_redis)]
