-- 1. 旧投递者不能覆盖新领取者；结果未知时安排重投，不立即释放购买名额。
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('HDEL', KEYS[2], ARGV[1])
if ARGV[3] == '-1' then
    redis.call('ZREM', KEYS[1], ARGV[1])
    redis.call('HDEL', KEYS[3], ARGV[1])
else
    local clock = redis.call('TIME')
    local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
    redis.call('ZADD', KEYS[1], now_ms + tonumber(ARGV[3]), ARGV[1])
end
return 1
