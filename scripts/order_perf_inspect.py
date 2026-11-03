"""只读导出失败专项轮次的效果待办/预占；可显式调用既有恢复函数，不重发购买。"""

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.core.seckill_store import SeckillStore
from backend.app.models import SeckillEffect
from scripts.loadtest import settings_for
from scripts.loadtest_contract import directory, write_json


async def inspect(run_id, recover=False):
    _, settings = settings_for(run_id)
    database, redis = Database(settings), create_redis_client(settings)
    try:
        if recover:
            from backend.app.services.seckill import sync_effects

            for _ in range(10):
                await sync_effects(database, SeckillStore(redis, settings.seckill_key_prefix))
                await asyncio.sleep(0.5)
        async with database.sessions() as session:
            effects = list(
                await session.scalars(select(SeckillEffect).where(SeckillEffect.applied.is_(False)))
            )
            pending = [
                {
                    "request_id": str(e.request_id),
                    "action": e.action,
                    "attempts": e.attempts,
                    "next_attempt_at": str(e.next_attempt_at),
                }
                for e in effects
            ]
        gate = await redis.hgetall(SeckillStore(redis, settings.seckill_key_prefix).key(1))
        result = {
            "run_id": run_id,
            "recovery_invoked": recover,
            "sampled_at": datetime.now(UTC).isoformat(),
            "pending_effects": pending,
            "held_request_ids": [
                key[2:] for key, value in gate.items() if key.startswith("r:") and value == "held"
            ],
        }
        write_json(
            directory(run_id) / ("effect-recovery.json" if recover else "effect-inspection.json"),
            result,
        )
        print(result)
    finally:
        await database.close()
        await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    asyncio.run(inspect(args.run_id, args.recover))
