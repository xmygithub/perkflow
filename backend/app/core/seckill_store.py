"""秒杀准入投影：单 Hash 保存库存、资格和购买代次；没有自动 TTL。"""

from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.app.core.errors import ApiError

LUA_DIR = Path(__file__).with_name("lua")


class SeckillStore:
    def __init__(self, redis: Redis, prefix: str):
        self.redis = redis
        self.prefix = prefix
        self.reserve_script = redis.register_script(
            (LUA_DIR / "seckill_reserve.lua").read_text(encoding="utf-8")
        )
        self.effect_script = redis.register_script(
            (LUA_DIR / "seckill_effect.lua").read_text(encoding="utf-8")
        )

    def key(self, voucher_id: int) -> str:
        # 券 ID 对应同一竞争库存；用户和购买编号是 Hash 字段，不用手机号或 Session。
        return f"{self.prefix}voucher:{{{voucher_id}}}"

    async def replace(self, voucher_id: int, mapping: dict[str, str | int]) -> None:
        try:
            # 初始化/离线重建专用；事务内替换单 Key，中途不暴露半份快照。
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.delete(self.key(voucher_id))
                pipe.hset(self.key(voucher_id), mapping=mapping)
                await pipe.execute()
        except RedisError as exc:
            raise self.unavailable() from exc

    @staticmethod
    def unavailable() -> ApiError:
        return ApiError(
            503, "SECKILL_GATE_UNAVAILABLE", "秒杀准入暂不可用，请先查看购买进度，稍后重试", 2
        )

    async def reserve(self, voucher_id: int, generation: str, user_id: int, request_id: int) -> int:
        try:
            result = int(
                await self.reserve_script(
                    keys=[self.key(voucher_id)], args=[generation, str(user_id), str(request_id)]
                )
            )
        except RedisError as exc:
            # 超时可能已预扣，不能立即返还；持久化请求会用同编号重试脚本。
            raise self.unavailable() from exc
        if result == 7:
            raise self.unavailable()
        return result

    async def effect(
        self, voucher_id: int, generation: str, user_id: int, request_id: int, action: str
    ) -> None:
        try:
            result = int(
                await self.effect_script(
                    keys=[self.key(voucher_id)],
                    args=[generation, str(user_id), str(request_id), action],
                )
            )
        except RedisError as exc:
            raise self.unavailable() from exc
        if result != 0:
            raise self.unavailable()
