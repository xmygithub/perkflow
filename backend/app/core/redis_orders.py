"""Redis 优先入口与待投递索引。此组件没有数据库依赖。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from redis.exceptions import RedisError

from backend.app.core.errors import ApiError
from backend.app.core.order_id import RedisOrderId
from backend.app.core.seckill_store import LUA_DIR, SeckillStore
from backend.app.schemas.order_requests import RequestOut, RequestPage, RequestResult


class RedisOrderIngress:
    def __init__(self, store: SeckillStore):
        self.store, self.redis, self.prefix = store, store.redis, store.prefix
        self.admit_script = self.redis.register_script(
            (LUA_DIR / "redis_order_admit.lua").read_text(encoding="utf-8")
        )
        self.claim_script = self.redis.register_script(
            (LUA_DIR / "redis_order_claim.lua").read_text(encoding="utf-8")
        )
        self.finish_script = self.redis.register_script(
            (LUA_DIR / "redis_order_finish.lua").read_text(encoding="utf-8")
        )
        self.project_script = self.redis.register_script(
            (LUA_DIR / "redis_order_project.lua").read_text(encoding="utf-8")
        )
        self.repair_script = self.redis.register_script(
            (LUA_DIR / "redis_order_repair.lua").read_text(encoding="utf-8")
        )
        self.outbox = self.prefix + "outbox"
        self.owners = self.prefix + "outbox:owners"
        self.attempts = self.prefix + "outbox:attempts"
        self._scan = 0
        self._repair_keys: list[tuple[str, int]] = []

    def document_key(self, identifier):
        return f"{self.prefix}request:{identifier}"

    def intent_key(self, user_id, request_key):
        return f"{self.prefix}intent:{user_id}:{request_key}"

    def user_key(self, user_id):
        return f"{self.prefix}user:{user_id}:requests"

    async def get_document(self, identifier):
        raw = await self.redis.get(self.document_key(identifier))
        return json.loads(raw) if raw else None

    async def submit(
        self, user_id: int, voucher_id: int, request_key: str, ids: RedisOrderId
    ) -> RequestResult:
        try:
            # 1. 已受理意图直接读 Redis；包括失败或取消后的旧结果，不重新扣名额。
            prior = await self.redis.get(self.intent_key(user_id, request_key))
            if prior:
                intent = json.loads(prior)
                if intent["voucher_id"] != str(voucher_id):
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一请求标识不能用于其他优惠券")
                return await self.result(user_id, intent["id"], reused=True, voucher_id=voucher_id)
            generation, meta = await self.redis.hmget(
                self.store.key(voucher_id), ["generation", "meta"]
            )
            if not generation or not meta:
                raise ApiError(
                    503, "SECKILL_NOT_READY", "活动尚未预热或库存投影不可用，请稍后重试", 2
                )
            metadata = json.loads(meta)
            now = datetime.now(UTC)
            identifier = str(await ids.next_id())
            result = RequestOut(
                id=identifier,
                voucher_id=voucher_id,
                title=metadata["title"],
                shop_name=metadata["shop_name"],
                status="pending",
                order=None,
                error_code=None,
                error_message=None,
                created_at=now,
                reservation_acquired=True,
            )
            body = {
                "purchase": {
                    "request_id": identifier,
                    "user_id": str(user_id),
                    "voucher_id": str(voucher_id),
                    "request_key": request_key,
                    "generation": generation,
                    "created_at": now.isoformat(),
                },
                "result": result.model_dump(mode="json"),
                "created_ms": int(now.timestamp() * 1000),
            }
            intent = json.dumps({"id": identifier, "voucher_id": str(voucher_id)})
            # 2. 校验、预扣和完整载荷在一次 Lua 中登记，拒绝请求不保存 SQL 请求记录。
            code, canonical = await self.admit_script(
                keys=[
                    self.store.key(voucher_id),
                    self.intent_key(user_id, request_key),
                    self.document_key(identifier),
                    self.outbox,
                    self.user_key(user_id),
                ],
                args=[
                    generation,
                    str(voucher_id),
                    str(user_id),
                    request_key,
                    identifier,
                    json.dumps(body, ensure_ascii=False),
                    intent,
                ],
            )
            code = int(code)
            if code not in {0, 1}:
                if code == 7:
                    raise self.store.unavailable()
                code_name, message = {
                    2: ("PURCHASE_QUALIFICATION_BUSY", "已有订单或预占名额，请先查看我的订单"),
                    3: ("SECKILL_NOT_STARTED", "活动尚未开始"),
                    4: ("SECKILL_ENDED", "活动已结束"),
                    5: ("VOUCHER_OFF_SHELF", "优惠券已下架"),
                    6: ("OUT_OF_STOCK", "优惠券已抢完"),
                    8: ("IDEMPOTENCY_CONFLICT", "同一请求标识不能用于其他优惠券"),
                }[code]
                raise ApiError(409, code_name, message)
            return await self.result(user_id, canonical, reused=code == 1, voucher_id=voucher_id)
        except (RedisError, ValueError, KeyError, TypeError) as exc:
            # 网络结果未知时不返库、不改走 SQL；同 UUID 重试可找回原载荷。
            raise self.store.unavailable() from exc

    async def result(self, user_id, identifier, *, reused=False, voucher_id=None):
        document = await self.get_document(identifier)
        if document is None and voucher_id is not None:
            raw = await self.redis.hget(self.store.key(voucher_id), f"p:{identifier}")
            if raw:
                document = json.loads(raw)
                await self.repair(document)
        if document is None:
            raise self.store.unavailable()
        if document["purchase"]["user_id"] != str(user_id):
            raise ApiError(404, "ORDER_REQUEST_NOT_FOUND", "购买请求不存在")
        return RequestResult(request=RequestOut.model_validate(document["result"]), reused=reused)

    async def list_requests(self, user_id: int, page: int, page_size: int) -> RequestPage:
        # Redis 维护 pending/failed 索引；成功后移出，订单历史仍从 SQL 查询。
        total = await self.redis.zcard(self.user_key(user_id))
        identifiers = await self.redis.zrevrange(
            self.user_key(user_id), (page - 1) * page_size, page * page_size - 1
        )
        items = [(await self.result(user_id, identifier)).request for identifier in identifiers]
        return RequestPage(items=items, total=total, page=page, page_size=page_size)

    async def claim(self, lease_seconds: int):
        claimed = await self.claim_script(
            keys=[self.outbox, self.owners, self.attempts], args=[uuid4().hex, lease_seconds * 1000]
        )
        return (claimed[0], claimed[1], int(claimed[2])) if claimed else None

    async def finish(self, member, token, *, delay: int | None):
        await self.finish_script(
            keys=[self.outbox, self.owners, self.attempts],
            args=[member, token, -1 if delay is None else delay * 1000],
        )

    async def project(self, document, result: RequestOut):
        # 3. SQL 终态投影到 Redis，失败由同事务的效果待办继续恢复；成功才移除用户进度。
        purchase = document["purchase"]
        identifier, user_id, voucher_id = (
            purchase["request_id"],
            purchase["user_id"],
            purchase["voucher_id"],
        )
        document = {**document, "result": result.model_dump(mode="json")}
        await self.project_script(
            keys=[
                self.store.key(int(voucher_id)),
                self.document_key(identifier),
                self.intent_key(user_id, purchase["request_key"]),
                self.outbox,
                self.user_key(user_id),
                self.owners,
                self.attempts,
            ],
            args=[json.dumps(document, ensure_ascii=False)],
        )

    async def repair(self, document):
        purchase = document["purchase"]
        identifier, voucher_id = purchase["request_id"], purchase["voucher_id"]
        code = await self.repair_script(
            keys=[
                self.store.key(int(voucher_id)),
                self.document_key(identifier),
                self.intent_key(purchase["user_id"], purchase["request_key"]),
                self.outbox,
                self.user_key(purchase["user_id"]),
            ],
            args=[identifier],
        )
        if int(code) == 7:
            raise self.store.unavailable()

    async def repair_some(self):
        # 分段扫描辅助索引；正常投递走 ZSET，不把每次购买变成全量 SCAN。
        if not self._repair_keys:
            self._scan, keys = await self.redis.scan(
                self._scan, match=self.prefix + "voucher:*", count=20
            )
            self._repair_keys.extend((key, 0) for key in keys)
        if not self._repair_keys:
            return
        key, cursor = self._repair_keys.pop(0)
        cursor, values = await self.redis.hscan(key, cursor=cursor, match="p:*", count=100)
        if cursor:
            self._repair_keys.append((key, cursor))
        for raw in values.values():
            await self.repair(json.loads(raw))
