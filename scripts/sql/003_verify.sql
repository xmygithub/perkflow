-- Read-only verification after importing schema and seed.
SET NAMES utf8mb4;
SET SESSION time_zone = '+00:00';
USE perkflow;

SELECT VERSION() AS server_version, @@session.time_zone AS session_time_zone;
SELECT 'tb_user' AS table_name, COUNT(*) AS row_count FROM tb_user
UNION ALL SELECT 'tb_shop_type', COUNT(*) FROM tb_shop_type
UNION ALL SELECT 'tb_shop', COUNT(*) FROM tb_shop
UNION ALL SELECT 'tb_voucher', COUNT(*) FROM tb_voucher
UNION ALL SELECT 'tb_seckill_voucher', COUNT(*) FROM tb_seckill_voucher
UNION ALL SELECT 'tb_voucher_order', COUNT(*) FROM tb_voucher_order;

SELECT s.id, s.name, t.name AS category, s.city, s.area,
       s.score, s.avg_price / 100 AS average_price_yuan
FROM tb_shop AS s JOIN tb_shop_type AS t ON t.id = s.type_id
ORDER BY s.id;

SELECT v.id, s.name AS shop, v.title, v.pay_value / 100 AS price_yuan,
       v.stock, v.total_stock,
       CASE WHEN v.status = 0 THEN 'disabled'
            WHEN v.type = 0 THEN 'active'
            WHEN sv.voucher_id IS NULL THEN 'invalid_configuration'
            WHEN UTC_TIMESTAMP(6) < sv.begin_time THEN 'upcoming'
            WHEN UTC_TIMESTAMP(6) >= sv.end_time THEN 'ended'
            ELSE 'active' END AS sale_state,
       sv.begin_time AS begin_time_utc, sv.end_time AS end_time_utc
FROM tb_voucher AS v
JOIN tb_shop AS s ON s.id = v.shop_id
LEFT JOIN tb_seckill_voucher AS sv ON sv.voucher_id = v.id
ORDER BY v.id;

-- 初始种子要求秒杀活动存在；0005 后普通券也共用本表，不再把普通券配置视为错误。
SELECT v.id AS invalid_voucher_id
FROM tb_voucher AS v
LEFT JOIN tb_seckill_voucher AS sv ON sv.voucher_id = v.id
WHERE v.type = 1 AND sv.voucher_id IS NULL;

-- Expect zero rows: only active created orders consume stock; cancelled history is retained.
SELECT v.id, v.total_stock, v.stock, COUNT(o.id) AS created_order_count
FROM tb_voucher AS v LEFT JOIN tb_voucher_order AS o
  ON o.voucher_id = v.id AND o.status = 'created'
GROUP BY v.id, v.total_stock, v.stock
HAVING v.total_stock - v.stock <> COUNT(o.id);
