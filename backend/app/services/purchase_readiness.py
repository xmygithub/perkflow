"""只读检查首次购买准备；不预热、不重建、不把SQL库存覆盖Redis预占。"""

import json

from sqlalchemy import select

from backend.app.models import SeckillGate, SeckillVoucher, Voucher

FIELDS = ("generation", "ingress_version", "meta", "available", "begin_ms", "end_ms", "enabled")


def classify(gate, activity, values, kind, ttl):
    if activity is None:
        return "ACTIVITY_MISSING"
    if gate is None or not gate.ready:
        return "INITIAL_PREPARATION_REQUIRED"
    if kind == "none":
        return "PROJECTION_MISSING_DO_NOT_RESET"
    if kind != "hash" or ttl != -1:
        return "PROJECTION_INVALID"
    data = dict(zip(FIELDS, values, strict=True))
    if data["generation"] != gate.generation:
        return "GENERATION_MISMATCH"
    try:
        metadata = json.loads(data["meta"])
        if data["ingress_version"] != "1" or not isinstance(metadata, dict):
            return "INGRESS_METADATA_MISSING"
        if not all(
            isinstance(metadata.get(key), str) and metadata[key] for key in ("title", "shop_name")
        ):
            return "INGRESS_METADATA_MISSING"
        if int(data["available"]) < 0 or data["enabled"] not in {"0", "1"}:
            return "PROJECTION_INVALID"
        if int(data["begin_ms"]) >= int(data["end_ms"]):
            return "PROJECTION_INVALID"
    except (ValueError, TypeError):
        return "PROJECTION_INVALID"
    return "PREPARED_SNAPSHOT"


async def inspect(database, store, voucher_ids=None):
    # 1. SQL只读取目录/活动/初始化标记，最多200张；不加写锁，不修改活动或授权。
    statement = (
        select(Voucher, SeckillGate, SeckillVoucher)
        .outerjoin(
            SeckillGate,
            SeckillGate.voucher_id == Voucher.id,
        )
        .outerjoin(SeckillVoucher, SeckillVoucher.voucher_id == Voucher.id)
        .order_by(Voucher.id)
        .limit(201)
    )
    if voucher_ids:
        statement = statement.where(Voucher.id.in_(voucher_ids))
    async with database.sessions() as session:
        rows = (await session.execute(statement)).all()
    items = []
    for voucher, gate, activity in rows[:200]:
        # 2. 只检查固定字段，不能HGETALL读取/输出资格、用户和订单载荷。
        key = store.key(voucher.id)
        kind = await store.redis.type(key)
        ttl = await store.redis.ttl(key)
        values = await store.redis.hmget(key, FIELDS) if kind == "hash" else [None] * len(FIELDS)
        items.append(
            {
                "voucher_id": voucher.id,
                "title": voucher.title,
                "code": classify(gate, activity, values, kind, ttl),
            }
        )
    found = {item["voucher_id"] for item in items}
    return {
        "items": items,
        "truncated": len(rows) > 200,
        "not_found": sorted(set(voucher_ids or []) - found),
        "scope": "只读准备快照，不证明实时可买、库存对账、Worker或MQ健康；读取期间可能变化",
        "remedy": "首次准备检查现有Worker启动预热；已准备投影丢失需停写核对，禁止直接重置库存",
    }
