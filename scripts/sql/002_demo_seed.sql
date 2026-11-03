-- PerkFlow curated demo seed, aligned with frontend/src/data.js on 2026-10-13.
-- Fictional Tianjin merchants; images are illustrative, not real merchant records.
-- Run once on the empty schema created by 001_schema.sql.
-- Stop on the first SQL error; do not use mysql --force.
SET NAMES utf8mb4;
SET SESSION time_zone = '+00:00';
USE perkflow;
START TRANSACTION;
SET @seed_now = UTC_TIMESTAMP(6);

INSERT INTO tb_user (id, phone, nick_name) VALUES
  (1, '13800138000', '演示用户8000');

INSERT INTO tb_shop_type (id, name, icon, sort) VALUES
  (1, '美食', 'public/assets/categories/ms.png', 1),
  (2, 'KTV', 'public/assets/categories/KTV.png', 2),
  (3, '理发造型', 'public/assets/categories/lrmf.png', 3),
  (10, '美甲美睫', 'public/assets/categories/mjmj.png', 4),
  (5, '按摩·足疗', 'public/assets/categories/amzl.png', 5),
  (6, '美容 SPA', 'public/assets/categories/spa.png', 6),
  (7, '亲子游乐', 'public/assets/categories/qzyl.png', 7),
  (8, '酒吧', 'public/assets/categories/jiuba.png', 8),
  (9, '轰趴馆', 'public/assets/categories/hpg.png', 9),
  (4, '健身运动', 'public/assets/categories/jsyd.png', 10);

INSERT INTO tb_shop (id, type_id, name, subtitle, kind, city, area, address, images, tags, score, avg_price, open_hours, description) VALUES
  (1, 1, '津巷泰食', '约个饭局，换换口味', '泰国菜', '天津', '南开街区', '天津 · 南开街区（演示地址）', JSON_ARRAY('public/assets/photos/02.jpg', 'public/assets/photos/01.jpg', 'public/assets/photos/10.jpg'), JSON_ARRAY('朋友聚餐', '东南亚风味'), 4.8, 8800, '11:00–21:30', '把新鲜香草、酸辣滋味和热闹的一餐放在一起。适合约上朋友，慢慢分享一桌好菜。'),
  (2, 1, '海河茶集', '逛完河边，再坐一会儿', '茶馆', '天津', '海河沿线', '天津 · 海河沿线（演示地址）', JSON_ARRAY('public/assets/photos/05.jpg', 'public/assets/photos/14.jpg', 'public/assets/photos/12.jpg'), JSON_ARRAY('庭院茶室', '安静聊天'), 4.9, 6800, '10:00–20:00', '树影、木色和一杯热茶，给忙碌的日常留一点空白。坐进庭院，感受时间慢下来的样子。'),
  (3, 1, '五大道·花间', '散步之后，认真吃顿晚饭', '西餐', '天津', '五大道', '天津 · 五大道（演示地址）', JSON_ARRAY('public/assets/photos/06.jpg', 'public/assets/photos/03.jpg', 'public/assets/photos/13.jpg'), JSON_ARRAY('双人约会', '氛围西餐'), 4.7, 12800, '11:30–22:00', '暖灯下的鲜花、刚出炉的焗饭与适合分享的餐盘。为一次认真相聚，准备一顿有仪式感的晚餐。'),
  (4, 1, '南开港味', '午饭有着落，下午有精神', '港式茶餐厅', '天津', '南开街区', '天津 · 南开街区（演示地址）', JSON_ARRAY('public/assets/photos/07.jpg', 'public/assets/photos/15.jpg', 'public/assets/photos/07.jpg'), JSON_ARRAY('日常午餐', '港式风味'), 4.6, 4200, '09:00–21:00', '烧味、滑蛋和一杯热饮，组成一份踏实的日常。随时走进来，给自己补充一点好心情。'),
  (5, 1, '津城小院', '今天的安排，是不赶时间', '茶馆', '天津', '海河沿线', '天津 · 海河沿线（演示地址）', JSON_ARRAY('public/assets/photos/09.jpg', 'public/assets/photos/11.jpg', 'public/assets/photos/16.jpg'), JSON_ARRAY('周末放松', '庭院茶室'), 4.8, 9800, '10:00–19:00', '藏在木廊和绿意之间的安静角落。适合独处，也适合和朋友坐一下午，聊聊最近的生活。'),
  (6, 1, '河岸餐桌', '把老朋友，约到同一桌', '西餐', '天津', '意式风情区', '天津 · 意式风情区（演示地址）', JSON_ARRAY('public/assets/photos/13.jpg', 'public/assets/photos/18.jpg', 'public/assets/photos/06.jpg'), JSON_ARRAY('朋友聚餐', '双人约会'), 4.7, 10800, '11:00–22:00', '一桌丰盛的餐盘，几段轻松的对话。让相聚有滋有味，让每一口都值得慢慢品尝。');

INSERT INTO tb_voucher (id, shop_id, title, sub_title, rules, pay_value, actual_value, type, status, total_stock, stock, validity_days) VALUES
  (101, 1, '100 元店铺代金券', '朋友聚餐，先把优惠备好', JSON_ARRAY('满 100 元可用，每次消费限用一张', '不可与店内其他优惠同时使用', '演示有效期：创建后 30 天；本页面不发放真实券'), 8800, 10000, 0, 1, 30, 30, 30),
  (102, 2, '双人下午茶体验券', '散步之后，一起喝杯茶', JSON_ARRAY('双人茶饮套餐，演示时段为全天', '每位演示用户限创建一笔该券订单', '仅演示领取流程，不代表真实秒杀或购买'), 5900, 8800, 1, 1, 12, 12, 30),
  (103, 3, '200 元店铺代金券', '把仪式感，留给下一顿晚餐', JSON_ARRAY('满 200 元可用，每次消费限用一张', '演示有效期：创建后 30 天', '本页面不包含支付和真实券核销'), 16800, 20000, 0, 1, 20, 20, 30),
  (104, 4, '50 元店铺代金券', '这一顿午饭，吃得实在些', JSON_ARRAY('满 50 元可用', '每位演示用户限创建一笔该券订单', '仅演示限量和重复请求提示'), 3900, 5000, 1, 1, 8, 8, 30),
  (105, 6, '双人分享餐体验券', '下一次相聚，已经安排好了', JSON_ARRAY('活动开始前不可创建订单', '实际起止时间由后端返回'), 15800, 19800, 1, 1, 15, 15, 30),
  (106, 1, '周末限定尝鲜券', '这一场优惠，暂时告一段落', JSON_ARRAY('仅在活动起止时间内可创建订单'), 6900, 10000, 1, 1, 0, 0, 30);

-- Initial time windows: active for 30 days; upcoming tomorrow; ended yesterday.
INSERT INTO tb_seckill_voucher (voucher_id, begin_time, end_time) VALUES
  (102, DATE_SUB(@seed_now, INTERVAL 1 HOUR), DATE_ADD(@seed_now, INTERVAL 30 DAY)),
  (104, DATE_SUB(@seed_now, INTERVAL 1 HOUR), DATE_ADD(@seed_now, INTERVAL 30 DAY)),
  (105, DATE_ADD(@seed_now, INTERVAL 1 DAY), DATE_ADD(@seed_now, INTERVAL 31 DAY)),
  (106, DATE_SUB(@seed_now, INTERVAL 2 DAY), DATE_SUB(@seed_now, INTERVAL 1 DAY));

-- Orders deliberately start empty: no purchases or payments are fabricated.
COMMIT;
