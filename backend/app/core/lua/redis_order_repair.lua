-- 1. 辅助索引以券 Hash 的完整载荷为依据；检查和回填不能拆成 Python 读后覆盖。
local raw = redis.call('HGET', KEYS[1], 'p:' .. ARGV[1])
if not raw then return 0 end
local source = cjson.decode(raw)
if source.purchase.generation ~= redis.call('HGET', KEYS[1], 'generation') then return 7 end
local current_raw = redis.call('GET', KEYS[2])
if current_raw then
    local current = cjson.decode(current_raw)
    if current.result.status ~= 'pending' then return 0 end
end
-- 2. 不覆盖领取租约；已提交终态会由效果投影删除 p:，不会被旧扫描重新变 pending。
redis.call('SET', KEYS[2], raw)
redis.call('SET', KEYS[3], cjson.encode({id=ARGV[1], voucher_id=source.purchase.voucher_id}), 'NX')
redis.call('ZADD', KEYS[4], 'NX', 0, source.purchase.voucher_id .. ':' .. ARGV[1])
redis.call('ZADD', KEYS[5], source.created_ms, ARGV[1])
return 1
