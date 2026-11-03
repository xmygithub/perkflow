-- 专项候选：一次往返领取最多16项，每项仍有自己的持有者、尝试次数和同长度租约。
local count = tonumber(ARGV[1])
local lease = tonumber(ARGV[2])
if not count or count % 1 ~= 0 or count < 1 or count > 16
    or not lease or lease <= 0 or lease % 1 ~= 0 or #ARGV ~= count + 2 then
    return redis.error_reply('invalid batch claim')
end
for i = 1, 3 do
    local kind = redis.call('TYPE', KEYS[i]).ok
    local expected = i == 1 and 'zset' or 'hash'
    if kind ~= 'none' and kind ~= expected then return redis.error_reply('invalid key type') end
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local members = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, count)
-- 写之前核对所有counter/token，避免中途字段损坏造成部分领取后脚本报错。
for i, member in ipairs(members) do
    local old = redis.call('HGET', KEYS[3], member)
    if old and (not string.match(old, '^%d+$') or tonumber(old) >= 2147483647) then
        return redis.error_reply('invalid attempt counter')
    end
    if string.len(ARGV[i + 2]) ~= 32 or not string.match(ARGV[i + 2], '^%x+$') then
        return redis.error_reply('invalid owner token')
    end
end
local result = {}
for i, member in ipairs(members) do
    redis.call('ZADD', KEYS[1], now + lease, member)
    redis.call('HSET', KEYS[2], member, ARGV[i + 2])
    local attempt = redis.call('HINCRBY', KEYS[3], member, 1)
    result[i] = {member, ARGV[i + 2], tostring(attempt)}
end
return result
