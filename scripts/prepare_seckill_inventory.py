"""后台预热 Redis 优先秒杀入口，保持已就绪库存，不自动回填丢失的库存 Key。"""

import asyncio

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.core.redis_orders import RedisOrderIngress
from backend.app.core.seckill_store import SeckillStore
from backend.app.services.redis_orders import warm


async def main() -> int:
    settings = Settings()
    database = Database(settings)
    redis = create_redis_client(settings)
    try:
        # 1. 只在准备阶段访问 SQL；HTTP 遇到未预热活动不会代替此脚本查询数据库。
        count = await warm(
            database, RedisOrderIngress(SeckillStore(redis, settings.seckill_key_prefix))
        )
        print(f"Redis-first activities prepared: {count}. Existing stock was not reset.")
        return 0
    except Exception as exc:
        print(
            f"Preparation failed ({type(exc).__name__}); "
            "inspect local services, no credentials printed."
        )
        return 1
    finally:
        await redis.aclose()
        await database.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
