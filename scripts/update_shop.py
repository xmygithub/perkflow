"""本地商户维护：默认只读预览；--apply 写入，--invalidate-only 只修复缓存。"""

import argparse
import asyncio
import json
import re
import sys
from decimal import Decimal

from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.errors import ApiError
from backend.app.core.redis_connection import create_redis_client
from backend.app.repositories import shops as repository
from backend.app.schemas.shops import ShopOut, ShopUpdate
from backend.app.services import shops as service


def yuan_to_cents(value: str) -> int:
    # 金额直接用十进制转分，不使用浮点数或悄悄四舍五入。
    if len(value) > 16 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise argparse.ArgumentTypeError("金额必须是非负十进制数，最多两位小数")
    cents = int(Decimal(value) * 100)
    if cents > 4294967295:
        raise argparse.ArgumentTypeError("金额超过数据库字段范围")
    return cents


async def maintain(
    settings: Settings,
    shop_id: int,
    changes: ShopUpdate | None,
    apply: bool = False,
    invalidate_only: bool = False,
) -> int:
    if type(shop_id) is not int or shop_id < 1:
        raise ValueError("商户 ID 必须为正整数")
    # 1. 修复只删除明确的商户 Key，不重复执行数据库更新，也不需要数据库连接。
    if invalidate_only:
        if changes is not None or apply:
            raise ValueError("只修复缓存时不能同时提供修改字段或 --apply")
        cache = create_redis_client(settings)
        try:
            invalidated = await service.invalidate_shop_cache(cache, settings, shop_id)
            print("缓存删除已确认。" if invalidated else "缓存删除仍未确认，请恢复 Redis 后重试。")
            return 0 if invalidated else 2
        finally:
            await cache.aclose()
    if changes is None:
        raise ValueError("至少提供一个维护字段")

    database = Database(settings)
    cache = create_redis_client(settings) if apply else None
    try:
        # 2. 默认从数据库读取预览，不使用缓存，不提交修改，不持有会话到下一条命令。
        if not apply:
            async with asyncio.timeout(settings.mysql_query_timeout):
                async with database.sessions() as session:
                    shop = await repository.get_shop(session, shop_id)
                    if shop is None:
                        raise ApiError(404, "SHOP_NOT_FOUND", "商户不存在")
                    before = ShopOut.model_validate(shop)
            print(
                json.dumps(
                    {
                        "database": settings.mysql_database,
                        "shop_id": shop_id,
                        "mode": "preview",
                        "fields": {
                            field: {"before": getattr(before, field), "after": value}
                            for field, value in changes.model_dump(exclude_unset=True).items()
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print("当前仅预览；执行同一条命令并添加 --apply 才会写入。")
            return 0

        # 3. 显式写入使用新 Session；Service 内部提交完成后才删除缓存。
        async with database.sessions() as session:
            result = await service.update_shop(session, shop_id, changes, cache, settings)
        print(
            json.dumps(
                {
                    "database": settings.mysql_database,
                    "shop_id": shop_id,
                    "database_committed": result.database_committed,
                    "changed_fields": result.changed_fields,
                    "cache_invalidated": result.cache_invalidated,
                    "fields": {
                        field: getattr(result.shop, field) for field in changes.model_fields_set
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not result.cache_invalidated:
            print("数据库事务已提交，缓存删除未确认。请仅修复缓存，不要把本次更新当作回滚。")
            print(f"python -m scripts.update_shop --shop-id {shop_id} --invalidate-only")
            return 2
        print("数据库事务已提交，详情缓存删除已确认；下一次查询会按需重建。")
        return 0
    finally:
        try:
            if cache is not None:
                await cache.aclose()
        finally:
            await database.close()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="本地商户维护，默认只预览")
    parser.add_argument("--shop-id", type=int, required=True)
    parser.add_argument("--name", help="商户名称，1–128 字符")
    parser.add_argument("--open-hours", help="营业时间文案，1–32 字符")
    money = parser.add_mutually_exclusive_group()
    money.add_argument(
        "--avg-price-yuan", type=yuan_to_cents, help="人均金额，单位元，最多两位小数"
    )
    money.add_argument("--avg-price-cents", type=int, help="人均金额，整数分")
    parser.add_argument("--apply", action="store_true", help="实际更新数据库并删除缓存")
    parser.add_argument(
        "--invalidate-only", action="store_true", help="只删除该商户缓存，修复删除未确认"
    )
    args = parser.parse_args()
    values = {}
    for field, value in [
        ("name", args.name),
        ("open_hours", args.open_hours),
        (
            "avg_price",
            args.avg_price_yuan if args.avg_price_yuan is not None else args.avg_price_cents,
        ),
    ]:
        if value is not None:
            values[field] = value
    if args.shop_id < 1:
        parser.error("shop-id 必须为正整数")
    if args.invalidate_only and (values or args.apply):
        parser.error("invalidate-only 不能与修改字段或 apply 同时使用")
    try:
        changes = None if args.invalidate_only else ShopUpdate.model_validate(values)
        code = asyncio.run(
            maintain(Settings(), args.shop_id, changes, args.apply, args.invalidate_only)
        )
    except ValidationError as exc:
        details = [
            f"{'.'.join(map(str, item['loc'])) or '参数'}：{item['msg']}"
            for item in exc.errors(include_input=False, include_url=False)
        ]
        parser.exit(1, "参数校验失败：" + "；".join(details) + "\n")
    except ApiError as exc:
        parser.exit(1, f"维护未完成：{exc.message}\n")
    except (SQLAlchemyError, RedisError, TimeoutError, OSError) as exc:
        # 提交丢失响应时结果可能不确定，不打印 SQL/连接凭据，也不承诺已回滚。
        parser.exit(1, f"维护结果未确认（{type(exc).__name__}），请检查连接并先回查数据库结果。\n")
    except KeyboardInterrupt:
        parser.exit(130, "维护已中断，请先回查数据库和缓存状态。\n")
    parser.exit(code)


if __name__ == "__main__":
    main()
