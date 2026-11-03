"""只读检查新券是否完成准入准备，不自动初始化、恢复或修改任何库存。"""

import argparse
import asyncio
import json

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.core.seckill_store import SeckillStore
from backend.app.services.purchase_readiness import inspect


async def check(voucher_ids):
    settings = Settings()
    database = Database(settings)
    redis = create_redis_client(settings)
    try:
        async with asyncio.timeout(settings.mysql_query_timeout):
            result = await inspect(
                database, SeckillStore(redis, settings.seckill_key_prefix), voucher_ids
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(
            bool(
                result["truncated"]
                or result["not_found"]
                or any(item["code"] != "PREPARED_SNAPSHOT" for item in result["items"])
            )
        )
    finally:
        await redis.aclose()
        await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--voucher-id", type=int, action="append", help="可重复；未指定时只检查前200张"
    )
    args = parser.parse_args()
    if args.voucher_id and (
        len(args.voucher_id) > 200 or any(not 0 < i < 2**64 for i in args.voucher_id)
    ):
        parser.error("最多200个正整数券ID，必须小于2**64")
    try:
        return asyncio.run(check(args.voucher_id))
    except Exception as error:
        print("只读检查未完成：", type(error).__name__, "；未输出连接配置，未自动准备或重建。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
