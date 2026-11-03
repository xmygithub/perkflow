-- 1. 只操作持久化待办指定的库存代次，不能用旧事件修改重建后的新库存。
local key = KEYS[1]
if redis.call('TYPE', key).ok ~= 'hash' then return 7 end
if redis.call('HGET', key, 'generation') ~= ARGV[1] then return 7 end
local stock = tonumber(redis.call('HGET', key, 'available'))
if not stock or stock < 0 or stock % 1 ~= 0 or redis.call('TTL', key) ~= -1 then return 7 end
local user_field = 'u:' .. ARGV[2]
local request_field = 'r:' .. ARGV[3]
local state = redis.call('HGET', key, request_field)
local owner = redis.call('HGET', key, user_field)
local action = ARGV[4]
if action ~= 'confirm' and action ~= 'release' and action ~= 'cancel' then return 7 end
-- 2. 取消/释放是终态；晚到的确认和重复释放都不能重新占位或多加库存。
if state == 'released' then return 0 end
if action == 'confirm' then
    if owner ~= ARGV[3] or (state ~= 'held' and state ~= 'committed') then return 7 end
    redis.call('HSET', key, request_field, 'committed')
    return 0
end
if not state then
    -- 没有实际扣过名额时只留终态标记，绝不凭补偿消息直接加库存。
    redis.call('HSET', key, request_field, 'released')
    return 0
end
if owner ~= ARGV[3] or (state ~= 'held' and state ~= 'committed') then return 7 end
-- 3. 必须属于这个购买编号才返一份；旧取消不会删除同用户重买后的新资格。
redis.call('HSET', key, 'available', stock + 1, request_field, 'released')
redis.call('HDEL', key, user_field)
return 0
