-- 1. 当前结果和新结果在一次脚本内比较；后台预热/旧确认不能覆盖终态或取消状态。
local incoming = cjson.decode(ARGV[1])
local raw = redis.call('GET', KEYS[2])
if raw then
    local current = cjson.decode(raw)
    local old_result, new_result = current.result, incoming.result
    if (old_result.status ~= 'pending' and new_result.status == 'pending')
        or (type(old_result.order) == 'table' and old_result.order.status == 'cancelled'
            and type(new_result.order) == 'table' and new_result.order.status == 'created') then
        incoming = current
    end
end
local result, purchase = incoming.result, incoming.purchase
local identifier = purchase.request_id
local member = purchase.voucher_id .. ':' .. identifier
redis.call('SET', KEYS[2], cjson.encode(incoming))
redis.call('SET', KEYS[3], cjson.encode({id=identifier, voucher_id=purchase.voucher_id}))
-- 2. SQL 终态只清理这一购买的待办；旧取消不涉及同券新购买编号。
if result.status ~= 'pending' then
    redis.call('HDEL', KEYS[1], 'p:' .. identifier)
    redis.call('ZREM', KEYS[4], member)
    redis.call('HDEL', KEYS[6], member)
    redis.call('HDEL', KEYS[7], member)
end
if result.status == 'succeeded' then
    redis.call('ZREM', KEYS[5], identifier)
else
    redis.call('ZADD', KEYS[5], incoming.created_ms, identifier)
end
return 1
