"""单 Redis 实例的缓存重建租约：只处理所属检查、回填和释放，不涉及业务数据库。"""

import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger("uvicorn.error")

# 所属检查和删除必须在同一脚本内，避免 GET 后锁到期、再 DEL 时删除另一请求的锁。
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# 所属检查和 SET EX 也必须原子执行；租约已失效的旧查询不能覆盖新持有者回填的内容。
_FILL = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
    return 1
end
return 0
"""


async def fill_cache_if_owner(
    cache: Redis, lock_key: str, token: str, key: str, value: str, ttl: int
) -> bool:
    # 参数通过 ARGV 传入，不拼接 Lua；仅有效持有者可写，0 表示本次已失去租约。
    return bool(await cache.eval(_FILL, 2, lock_key, key, token, value, ttl))


async def release_cache_lock(cache: Redis, lock_key: str, token: str) -> None:
    try:
        await cache.eval(_RELEASE, 1, lock_key, token)
    except RedisError as exc:
        # 不覆盖原有数据库错误或成功结果；释放响应可能丢失，剩余租约仍可自然过期。
        logger.warning("商户重建锁释放未确认：%s", type(exc).__name__)
