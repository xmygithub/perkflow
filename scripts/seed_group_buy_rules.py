"""只插入自有演示套餐和使用规则；默认预览，不覆盖旧券、库存或规则。"""

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import Field, model_validator

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.models import Shop, Voucher, VoucherUsage
from backend.app.schemas.group_buys import Contract, UsageRule
from backend.app.services.voucher_activities import prepare_normal_activities
from scripts.seed_demo_catalog import VoucherSeed

DATA = Path(__file__).resolve().parent / "data/group_buy_rules.json"


class Entry(Contract):
    shop_id: int = Field(ge=1)
    shop_name: str
    voucher: VoucherSeed
    usage: UsageRule

    @model_validator(mode="after")
    def same_shop(self):
        if self.voucher.shop_id != self.shop_id:
            raise ValueError("券与商户归属不一致")
        return self


def load_entries() -> list[Entry]:
    entries = [Entry.model_validate(item) for item in json.loads(DATA.read_text(encoding="utf-8"))]
    if len({item.voucher.id for item in entries}) != len(entries):
        raise ValueError("演示券ID重复")
    return entries


async def seed(database, entries: list[Entry], *, apply: bool = False) -> dict:
    report = {"mode": "applied" if apply else "preview", "new_vouchers": 0, "new_rules": 0}
    # 1. 先校验整包身份，再执行插入；重复运行只跳过，不覆盖人工修改或已消耗库存。
    async with database.sessions() as session, session.begin():
        pending = []
        for item in entries:
            shop = await session.get(Shop, item.shop_id)
            if shop is None or shop.name != item.shop_name or shop.city != "天津":
                raise ValueError("演示商户不存在或归属冲突，请先导入天津增量商户")
            old = await session.get(Voucher, item.voucher.id)
            if old and (old.shop_id, old.title, old.type) != (
                item.shop_id,
                item.voucher.title,
                item.voucher.type,
            ):
                raise ValueError("演示券ID冲突，未执行导入")
            usage = await session.get(VoucherUsage, item.voucher.id)
            report["new_vouchers"] += old is None
            report["new_rules"] += usage is None
            pending.append((item, old, usage))
        if apply:
            # 2. 券、规则和首次活动同事务；旧活动不延期。读取不使用缓存，无新增Redis键。
            for item, old, usage in pending:
                if old is None:
                    session.add(Voucher(**item.voucher.model_dump()))
                    await session.flush()
                if usage is None:
                    session.add(
                        VoucherUsage(
                            voucher_id=item.voucher.id, spec=item.usage.model_dump(mode="json")
                        )
                    )
            await session.flush()
            await prepare_normal_activities(session, [item.voucher.id for item in entries])
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings)
    try:
        async with asyncio.timeout(settings.mysql_query_timeout):
            report = await seed(database, load_entries(), apply=args.apply)
        print(json.dumps(report, ensure_ascii=False))
        if args.apply:
            print(
                "导入不等于准入准备。可运行 python -m scripts.check_purchase_readiness 只读检查；"
                "首次准备沿用Worker启动预热，不在购买请求中重建库存。"
            )
        return 0
    except Exception as error:
        print(f"导入未完成（{type(error).__name__}），请核对数据库和迁移；未输出连接信息")
        return 1
    finally:
        await database.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
