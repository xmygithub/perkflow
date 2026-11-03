-- 1. 领取 Redis 到期待办；租约只协调投递，不是订单的最终互斥保证。
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now_ms, 'LIMIT', 0, 1)
if #due == 0 then return {} end
local member = due[1]
redis.call('ZADD', KEYS[1], now_ms + tonumber(ARGV[2]), member)
redis.call('HSET', KEYS[2], member, ARGV[1])
local attempts = redis.call('HINCRBY', KEYS[3], member, 1)
return {member, ARGV[1], tostring(attempts)}
