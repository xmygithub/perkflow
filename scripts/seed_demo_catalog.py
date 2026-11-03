"""增量补充天津虚构商户和普通展示券；默认预览，--apply 只插入缺失记录。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.redis_connection import create_redis_client
from backend.app.models import Shop, ShopType, Voucher
from backend.app.schemas.shops import ShopOut
from backend.app.services.voucher_activities import prepare_normal_activities

ROOT = Path(__file__).resolve().parents[1]
CATALOG_FILE = ROOT / "scripts/data/tianjin_catalog.json"
CATEGORIES = {
    1: "美食",
    2: "KTV",
    3: "理发造型",
    4: "健身运动",
    5: "按摩·足疗",
    6: "美容 SPA",
    7: "亲子游乐",
    8: "酒吧",
    9: "轰趴馆",
    10: "美甲美睫",
}


class ShopSeed(ShopOut):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: int = Field(ge=1, le=2**64 - 1)
    type_id: int = Field(ge=1, le=10)
    name: str = Field(min_length=1, max_length=128)
    subtitle: str = Field(max_length=255)
    kind: str = Field(min_length=1, max_length=64)
    city: Literal["天津"]
    area: str = Field(min_length=1, max_length=64)
    address: str = Field(min_length=1, max_length=255)
    images: list[str] = Field(min_length=1)
    score: float = Field(ge=0, le=5, allow_inf_nan=False)
    avg_price: int = Field(ge=0, le=4294967295)
    open_hours: str = Field(min_length=1, max_length=32)


class VoucherSeed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: int = Field(ge=1, le=2**64 - 1)
    shop_id: int = Field(ge=1, le=2**64 - 1)
    title: str = Field(min_length=1, max_length=128)
    sub_title: str = Field(max_length=255)
    rules: list[str] = Field(min_length=1)
    pay_value: int = Field(gt=0, le=4294967295)
    actual_value: int = Field(gt=0, le=4294967295)
    type: Literal[0]
    status: Literal[1]
    total_stock: int = Field(ge=0, le=4294967295)
    stock: int = Field(ge=0, le=4294967295)
    validity_days: int = Field(gt=0, le=65535)

    @model_validator(mode="after")
    def check_values(self) -> Self:
        if self.stock > self.total_stock or self.pay_value > self.actual_value:
            raise ValueError("库存或金额关系不合法")
        return self


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shops: list[ShopSeed] = Field(min_length=1)
    vouchers: list[VoucherSeed]

    @model_validator(mode="after")
    def check_relations_and_assets(self) -> Self:
        # 1. 在访问数据库之前校验整份数据和关联，图片只允许使用现有本地照片。
        shop_ids = {shop.id for shop in self.shops}
        if len(shop_ids) != len(self.shops):
            raise ValueError("商户 ID 重复")
        if len({voucher.id for voucher in self.vouchers}) != len(self.vouchers):
            raise ValueError("优惠券 ID 重复")
        if any(voucher.shop_id not in shop_ids for voucher in self.vouchers):
            raise ValueError("普通券必须关联本增量包的商户")
        assets = (ROOT / "frontend/public/assets/photos").resolve()
        for shop in self.shops:
            if "虚构" not in shop.address or "虚构" not in shop.description:
                raise ValueError("演示地址和介绍必须注明虚构")
            for image in shop.images:
                file = (ROOT / "frontend" / image).resolve()
                if not file.is_relative_to(assets) or not file.is_file():
                    raise ValueError("图片必须是项目现有照片")
        return self


class ImportReport(BaseModel):
    mode: Literal["preview", "applied"]
    new_shops: int
    existing_shops: int
    new_vouchers: int
    existing_vouchers: int
    cache_invalidated: bool | None = None


def load_catalog() -> Catalog:
    return Catalog.model_validate_json(CATALOG_FILE.read_text(encoding="utf-8"))


async def seed(database: Database, catalog: Catalog, *, apply: bool = False) -> ImportReport:
    # 2. 先完整检查已有 ID 归属；同 ID/身份跳过，其他数据冲突则拒绝整包，不能覆盖旧店或券。
    async with database.sessions() as session, session.begin():
        types = dict((await session.execute(select(ShopType.id, ShopType.name))).all())
        if any(types.get(shop.type_id) != CATEGORIES[shop.type_id] for shop in catalog.shops):
            raise ValueError("分类缺失或与初始分类不一致，请核对初始化数据")
        shops = {
            shop.id: shop
            for shop in await session.scalars(
                select(Shop).where(Shop.id.in_([item.id for item in catalog.shops]))
            )
        }
        vouchers = {
            voucher.id: voucher
            for voucher in await session.scalars(
                select(Voucher).where(Voucher.id.in_([item.id for item in catalog.vouchers]))
            )
        }
        for item in catalog.shops:
            old = shops.get(item.id)
            if old and (old.name, old.type_id, old.city) != (item.name, item.type_id, item.city):
                raise ValueError(f"商户 ID {item.id} 已被不同资料占用，未执行导入")
        for item in catalog.vouchers:
            old = vouchers.get(item.id)
            if old and (old.shop_id, old.title, old.type) != (item.shop_id, item.title, item.type):
                raise ValueError(f"优惠券 ID {item.id} 已被不同资料占用，未执行导入")
        report = ImportReport(
            mode="applied" if apply else "preview",
            new_shops=len(catalog.shops) - len(shops),
            existing_shops=len(shops),
            new_vouchers=len(catalog.vouchers) - len(vouchers),
            existing_vouchers=len(vouchers),
        )
        if apply:
            # 3. 商户与普通券同事务插入；不执行 UPDATE/DELETE/重置库存，也不改原初始种子。
            session.add_all(
                Shop(**item.model_dump()) for item in catalog.shops if item.id not in shops
            )
            await session.flush()  # 外键依赖的商户先落在本事务内，尚未提交。
            session.add_all(
                Voucher(**item.model_dump()) for item in catalog.vouchers if item.id not in vouchers
            )
            await session.flush()
            # 普通券也参与统一购买；首次活动与数据同事务保存，重跑不续展时间。
            await prepare_normal_activities(session, [item.id for item in catalog.vouchers])
    # 退出事务后才返回；异常整体回滚，提交响应未知时通过重复执行回查，不盲目覆盖。
    return report


async def run_import(settings: Settings, *, apply: bool = False) -> ImportReport:
    catalog = load_catalog()
    database = Database(settings)
    try:
        async with asyncio.timeout(settings.mysql_query_timeout):
            report = await seed(database, catalog, apply=apply)
    finally:
        await database.close()
    if apply:
        # 4. 提交后清理这些商户可能存在的空值/旧详情缓存；重跑也清理，支持删除未确认后的恢复。
        # 分类未变化，列表直接查 SQL；不删除分类、Session、秒杀或其他商户 Key。
        cache = create_redis_client(settings)
        try:
            await cache.delete(
                *(f"{settings.cache_key_prefix}shop:{shop.id}" for shop in catalog.shops)
            )
            report.cache_invalidated = True
        except RedisError:
            report.cache_invalidated = False  # 数据已经提交，不能误报为数据库回滚。
        finally:
            await cache.aclose()
    return report


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="确认增量插入缺失数据，并清理对应详情缓存"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(run_import(Settings(), apply=args.apply))
    except ValidationError:
        parser.exit(1, "数据包校验失败，请检查固定演示数据文件；未打印原始数据或配置。\n")
    except ValueError as exc:
        parser.exit(1, f"导入拒绝：{exc}\n")
    except (SQLAlchemyError, RedisError, TimeoutError, OSError) as exc:
        parser.exit(
            1, f"导入结果未确认（{type(exc).__name__}）；请回查后重跑，不清库或覆盖库存。\n"
        )
    print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
    if not args.apply:
        print("当前只读预览，添加 --apply 才实际插入。")
    elif not report.cache_invalidated:
        print(
            "数据库已提交，缓存清理未确认；恢复 Redis 后重跑 --apply，只跳过已有记录并重试清缓存。"
        )
        parser.exit(2)
    else:
        print("增量导入完成；现有记录未覆盖，刷新页面即可查询。")


if __name__ == "__main__":
    main()
