"""停写后重建单张券的 Redis 准入投影；默认只读预览，不清空数据库或其他 Redis Key。"""

import argparse
import asyncio

from sqlalchemy import func, select

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.core.redis_orders import RedisOrderIngress
from backend.app.core.seckill_store import SeckillStore
from backend.app.models import OrderRequest, SeckillGate, Voucher, VoucherOrder
from backend.app.services.redis_orders import rebuild as rebuild_fast
from backend.app.services.seckill import rebuild


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voucher-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--writers-stopped",
        action="store_true",
        help="操作者确认此环境 API、全部 Worker 和其他库存写入者已停止",
    )
    args = parser.parse_args()
    if args.voucher_id < 1 or (args.apply and not args.writers_stopped):
        parser.error("应用前必须停止所有写入者并传入 --writers-stopped；该参数不会自动停止进程")
    settings = Settings()
    database = Database(settings)
    redis = create_redis_client(settings)
    store = SeckillStore(redis, settings.seckill_key_prefix)
    try:
        if args.apply:
            # 1. 明确停写后才重新分配代次；不能把 SQL 行锁当成跨 Redis 的维护屏障。
            result = (
                await rebuild_fast(database, RedisOrderIngress(store), args.voucher_id)
                if settings.order_admission_mode == "redis_first"
                else await rebuild(database, store, args.voucher_id)
            )
            print(
                f"Rebuilt voucher={result['voucher_id']} available={result['available']} "
                f"pending_reserved={result['pending_reserved']} "
                f"pending_failed={result['pending_failed']}"
            )
            return 0
        async with database.sessions() as session:
            voucher = await session.get(Voucher, args.voucher_id)
            if voucher is None:
                print("Voucher not found. No writes performed.")
                return 1
            gate = await session.get(SeckillGate, args.voucher_id)
            pending = await session.scalar(
                select(func.count())
                .select_from(OrderRequest)
                .where(OrderRequest.voucher_id == args.voucher_id, OrderRequest.status == "pending")
            )
            active = await session.scalar(
                select(func.count())
                .select_from(VoucherOrder)
                .where(VoucherOrder.voucher_id == args.voucher_id, VoucherOrder.status == "created")
            )
        # 2. 只打印计数和代次是否匹配，不打印配置、密码、用户标识或原始消息。
        kind = await redis.type(store.key(args.voucher_id))
        matching = (
            await redis.hget(store.key(args.voucher_id), "generation") == gate.generation
            if gate and kind == "hash"
            else False
        )
        available = (
            await redis.hget(store.key(args.voucher_id), "available") if kind == "hash" else None
        )
        print(
            f"Preview voucher={args.voucher_id} mysql_stock={voucher.stock} created={active} "
            f"pending={pending} redis_type={kind} available={available} "
            f"generation_matches={matching}"
        )
        print(
            "Read-only preview. Apply requires all writers stopped and --apply --writers-stopped."
        )
        return 0
    except Exception as exc:
        print(
            f"Inventory operation failed ({type(exc).__name__}); no credentials printed. "
            "If applying failed, keep writers stopped, inspect and rerun before resuming."
        )
        return 1
    finally:
        await redis.aclose()
        await database.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
