-- 1. 所有数据位于一个 Hash；代次来自 MySQL，丢失/错代/字段损坏时拒绝回源放量。
local key = KEYS[1]
if redis.call('TYPE', key).ok ~= 'hash' then return 7 end
if redis.call('HGET', key, 'generation') ~= ARGV[1] then return 7 end
local stock = tonumber(redis.call('HGET', key, 'available'))
local begin_ms = tonumber(redis.call('HGET', key, 'begin_ms'))
local end_ms = tonumber(redis.call('HGET', key, 'end_ms'))
local enabled = redis.call('HGET', key, 'enabled')
if not stock or stock < 0 or stock % 1 ~= 0 or not begin_ms or not end_ms
    or (enabled ~= '0' and enabled ~= '1') or redis.call('TTL', key) ~= -1 then return 7 end
local user_field = 'u:' .. ARGV[2]
local request_field = 'r:' .. ARGV[3]
local state = redis.call('HGET', key, request_field)
local owner = redis.call('HGET', key, user_field)
-- 2. 同一请求重复执行不再扣库存；释放过的旧购买代次不能复活。
if state == 'released' then return 6 end
if state == 'held' or state == 'committed' then
    if owner == ARGV[3] then return 0 else return 7 end
end
if state then return 7 end
if owner then return 2 end
-- 3. 使用 Redis 服务端时间检查活动，与资格/库存判断放在同一个原子脚本中。
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if enabled ~= '1' then return 5 end
if now_ms < begin_ms then return 3 end
if now_ms >= end_ms then return 4 end
if stock <= 0 then return 1 end
-- 4. 写入前完成字段校验；预扣和用户归属一起更新，不等于真实订单已经创建。
redis.call('HSET', key, 'available', stock - 1, user_field, ARGV[3], request_field, 'held')
return 0
