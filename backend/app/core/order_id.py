"""订单编号生成器；计数器是编号分配状态，不是可随意删除的缓存。"""

from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.app.core.errors import ApiError

BEGIN_TIMESTAMP = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
COUNT_BITS = 32


class RedisOrderId:
    def __init__(self, redis: Redis, prefix: str):
        self.redis = redis
        self.prefix = prefix

    async def next_id(self) -> int:
        # 1. 同一次调用的日期和秒数都取自 UTC；不在共享实例保存可变的“当前日期”。
        now = datetime.now(UTC)
        timestamp = int(now.timestamp()) - BEGIN_TIMESTAMP
        if not 0 <= timestamp < 2**31:
            raise ApiError(503, "ORDER_ID_CLOCK_INVALID", "订单编号时间超出允许范围")
        key = f"{self.prefix}order:{now:%Y%m%d}"
        # 2. 所有后端共享同一计数器；不设 TTL，不因订单失败而减回或重用编号。
        try:
            sequence = await self.redis.incr(key)
        except RedisError as exc:
            raise ApiError(503, "ORDER_ID_UNAVAILABLE", "订单编号服务暂不可用，请稍后重试") from exc
        if not 1 <= sequence < 2**COUNT_BITS:
            raise ApiError(503, "ORDER_ID_EXHAUSTED", "当天订单编号序列不可用")
        # 3. 高位存相对秒数，低位存当天序列；接口会把最终 ID 转成字符串。
        return (timestamp << COUNT_BITS) | sequence
