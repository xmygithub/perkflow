"""订单互斥锁：每次获取独立 UUID，Lua 原子校验归属并释放；固定租约不续期。"""

import logging
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.app.core.errors import ApiError

logger = logging.getLogger(__name__)

# 比较和删除必须在一次脚本中执行，不能拆成 GET 后在 Python 判断、再 DEL。
_RELEASE_IF_OWNER = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisOrderLock:
    def __init__(self, redis: Redis, prefix: str, ttl: int):
        self.redis = redis
        self.prefix = prefix
        self.ttl = ttl

    def key(self, user_id: int, voucher_id: int) -> str:
        # 1. 身份由服务端会话确定，券 ID 来自已校验的路径；只互斥同一购买组合。
        return f"{self.prefix}{user_id}:{voucher_id}"

    async def try_acquire(self, user_id: int, voucher_id: int) -> str | None:
        # 2. 每次尝试生成独立 UUID；只把成功的标识交给当前调用方，不放 self 避免协程覆盖。
        token = uuid4().hex
        try:
            acquired = await self.redis.set(
                self.key(user_id, voucher_id), token, nx=True, ex=self.ttl
            )
            return token if acquired else None
        except RedisError as exc:
            # Redis 超时可能已写入锁；不补发 DEL、不降级绕过，等待租约到期。
            raise ApiError(503, "ORDER_LOCK_UNAVAILABLE", "下单锁服务暂不可用，请稍后重试") from exc

    async def release(self, user_id: int, voucher_id: int, token: str) -> bool | None:
        # 3. 事务结束后传回本次标识，Lua 比较归属再删除；旧持有者不能删掉后来者的锁。
        #    True 表示已删除，False 表示已到期/换人，None 表示 Redis 未确认；都不撤销订单。
        try:
            return bool(
                await self.redis.eval(_RELEASE_IF_OWNER, 1, self.key(user_id, voucher_id), token)
            )
        except RedisError as exc:
            # 订单可能已经提交；不覆盖原结果、不自动重试，租约仍可兜底。
            #    仅比较删除已原子化，不代表锁到期后旧请求会自动停止写数据库。
            logger.warning("订单锁释放未确认，依赖租约到期 error=%s", type(exc).__name__)
            return None
