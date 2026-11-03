-- KEYS：券投影、全局意图、结果文档、投递索引、当前用户进度索引。
local gate, intent, document, outbox, user_index = unpack(KEYS)
local expected = {'hash', 'string', 'string', 'zset', 'zset'}
-- 1. 先校验类型与已有意图，避免已知错误发生在扣减之后；Lua 不提供运行时错误回滚。
for i, key in ipairs(KEYS) do
    local kind = redis.call('TYPE', key).ok
    if kind ~= 'none' and kind ~= expected[i] then return {7, ''} end
    if kind ~= 'none' and redis.call('TTL', key) ~= -1 then return {7, ''} end
end
local prior = redis.call('GET', intent)
if prior then
    local old = cjson.decode(prior)
    if old.voucher_id ~= ARGV[2] then return {8, ''} end
    return {1, old.id}
end
if redis.call('HGET', gate, 'generation') ~= ARGV[1]
    or redis.call('HGET', gate, 'ingress_version') ~= '1' then return {7, ''} end
local stock = tonumber(redis.call('HGET', gate, 'available'))
local begin_ms = tonumber(redis.call('HGET', gate, 'begin_ms'))
local end_ms = tonumber(redis.call('HGET', gate, 'end_ms'))
local enabled = redis.call('HGET', gate, 'enabled')
if not stock or stock < 0 or stock % 1 ~= 0 or not begin_ms or not end_ms
    or (enabled ~= '0' and enabled ~= '1') then return {7, ''} end
-- 2. 同券意图字段是恢复依据，辅助全局索引发生局部丢失时不重新预扣。
local local_id = redis.call('HGET', gate, 'i:' .. ARGV[3] .. ':' .. ARGV[4])
if local_id then return {1, local_id} end
if redis.call('HGET', gate, 'u:' .. ARGV[3]) then return {2, ''} end
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if enabled ~= '1' then return {5, ''} end
if now_ms < begin_ms then return {3, ''} end
if now_ms >= end_ms then return {4, ''} end
if stock <= 0 then return {6, ''} end
if redis.call('EXISTS', document) ~= 0 then return {7, ''} end
local body = cjson.decode(ARGV[6])
if body.purchase.request_id ~= ARGV[5] or body.purchase.generation ~= ARGV[1]
    or body.purchase.user_id ~= ARGV[3] or body.purchase.voucher_id ~= ARGV[2]
    or body.purchase.request_key ~= ARGV[4] then return {7, ''} end
-- 3. 单次 HSET 同时记录库存、资格、意图和完整待投递载荷；随后建立可重建的查询/调度索引。
redis.call('HSET', gate, 'available', stock - 1, 'u:' .. ARGV[3], ARGV[5],
    'r:' .. ARGV[5], 'held', 'p:' .. ARGV[5], ARGV[6],
    'i:' .. ARGV[3] .. ':' .. ARGV[4], ARGV[5])
redis.call('SET', intent, ARGV[7])
redis.call('SET', document, ARGV[6])
redis.call('ZADD', outbox, now_ms, ARGV[2] .. ':' .. ARGV[5])
redis.call('ZADD', user_index, body.created_ms, ARGV[5])
return {0, ARGV[5]}
