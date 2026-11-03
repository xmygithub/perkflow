-- PerkFlow initial schema, independently designed for the current project.
-- Requires MySQL >= 8.0.16 (enforced CHECK constraints).
-- One-time bootstrap for a NEW, EMPTY perkflow database.
-- No DROP, TRUNCATE, or replacement of existing tables.
-- This file is a bootstrap snapshot, not an Alembic migration.
SET NAMES utf8mb4;
SET SESSION time_zone = '+00:00';

CREATE DATABASE IF NOT EXISTS perkflow
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE perkflow;

CREATE TABLE tb_user (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  phone VARCHAR(11) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  nick_name VARCHAR(32) NOT NULL,
  icon VARCHAR(255) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_phone (phone),
  CONSTRAINT ck_user_phone_length CHECK (CHAR_LENGTH(phone) = 11)
) ENGINE=InnoDB COMMENT='用户；验证码与登录态由后续认证模块管理';

CREATE TABLE tb_shop_type (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name VARCHAR(32) NOT NULL,
  icon VARCHAR(255) NOT NULL COMMENT '静态资源相对路径',
  sort INT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_shop_type_name (name)
) ENGINE=InnoDB COMMENT='商户分类';

CREATE TABLE tb_shop (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  type_id BIGINT UNSIGNED NOT NULL,
  name VARCHAR(128) NOT NULL,
  subtitle VARCHAR(255) NOT NULL DEFAULT '',
  kind VARCHAR(64) NOT NULL COMMENT '细分类展示文案，如茶馆、西餐',
  city VARCHAR(32) NOT NULL,
  area VARCHAR(64) NOT NULL,
  address VARCHAR(255) NOT NULL,
  images JSON NOT NULL COMMENT '有序图片路径数组，不存逗号拼接文本',
  tags JSON NOT NULL COMMENT '展示标签数组',
  score DECIMAL(2,1) NOT NULL DEFAULT 0.0,
  avg_price INT UNSIGNED NOT NULL COMMENT '人均金额，单位：分',
  open_hours VARCHAR(32) NOT NULL,
  description TEXT NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY idx_shop_type_city_area (type_id, city, area),
  CONSTRAINT fk_shop_type FOREIGN KEY (type_id) REFERENCES tb_shop_type (id),
  CONSTRAINT ck_shop_score CHECK (score BETWEEN 0.0 AND 5.0),
  CONSTRAINT ck_shop_images CHECK (JSON_TYPE(images) = 'ARRAY' AND JSON_LENGTH(images) > 0),
  CONSTRAINT ck_shop_tags CHECK (JSON_TYPE(tags) = 'ARRAY')
) ENGINE=InnoDB COMMENT='商户；当前不录入虚构经纬度';

CREATE TABLE tb_voucher (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  shop_id BIGINT UNSIGNED NOT NULL,
  title VARCHAR(128) NOT NULL,
  sub_title VARCHAR(255) NOT NULL DEFAULT '',
  rules JSON NOT NULL COMMENT '规则文案数组；确定性业务规则由后端单独校验',
  pay_value INT UNSIGNED NOT NULL COMMENT '下单金额，单位：分；不代表已支付',
  actual_value INT UNSIGNED NOT NULL COMMENT '抵扣或套餐标示金额，单位：分',
  type TINYINT UNSIGNED NOT NULL COMMENT '0：普通券；1：秒杀券',
  status TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '0：下架；1：上架；时间状态另行计算',
  total_stock INT UNSIGNED NOT NULL COMMENT '初始总库存；当前不实现补货',
  stock INT UNSIGNED NOT NULL COMMENT 'MySQL 剩余库存，普通券和秒杀券统一在此维护',
  validity_days SMALLINT UNSIGNED NOT NULL DEFAULT 30 COMMENT '订单创建后有效天数；尚未实现核销',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY idx_voucher_shop_status (shop_id, status),
  CONSTRAINT fk_voucher_shop FOREIGN KEY (shop_id) REFERENCES tb_shop (id),
  CONSTRAINT ck_voucher_type CHECK (type IN (0, 1)),
  CONSTRAINT ck_voucher_status CHECK (status IN (0, 1)),
  CONSTRAINT ck_voucher_values CHECK (pay_value > 0 AND actual_value >= pay_value),
  CONSTRAINT ck_voucher_stock CHECK (stock <= total_stock),
  CONSTRAINT ck_voucher_validity CHECK (validity_days > 0),
  CONSTRAINT ck_voucher_rules CHECK (JSON_TYPE(rules) = 'ARRAY')
) ENGINE=InnoDB COMMENT='优惠券和唯一的数据库库存来源';

CREATE TABLE tb_seckill_voucher (
  voucher_id BIGINT UNSIGNED NOT NULL,
  begin_time DATETIME(6) NOT NULL COMMENT '秒杀开始时间，UTC，含此时间点',
  end_time DATETIME(6) NOT NULL COMMENT '秒杀结束时间，UTC，不含此时间点',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (voucher_id),
  KEY idx_seckill_time (begin_time, end_time),
  CONSTRAINT fk_seckill_voucher FOREIGN KEY (voucher_id) REFERENCES tb_voucher (id),
  CONSTRAINT ck_seckill_time CHECK (end_time > begin_time)
) ENGINE=InnoDB COMMENT='秒杀时间配置；仅用于 type=1 的券，由 Service 保证';

CREATE TABLE tb_voucher_order (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  voucher_id BIGINT UNSIGNED NOT NULL,
  pay_value INT UNSIGNED NOT NULL COMMENT '创建订单时的金额快照，单位：分',
  status VARCHAR(16) NOT NULL DEFAULT 'created' COMMENT '当前仅支持 created，不等同于支付成功',
  expires_at DATETIME(6) NOT NULL COMMENT '订单对应的券有效期，UTC',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_order_user_voucher (user_id, voucher_id),
  KEY idx_order_user_created (user_id, created_at, id),
  KEY idx_order_voucher (voucher_id),
  CONSTRAINT fk_order_user FOREIGN KEY (user_id) REFERENCES tb_user (id),
  CONSTRAINT fk_order_voucher FOREIGN KEY (voucher_id) REFERENCES tb_voucher (id),
  CONSTRAINT ck_order_value CHECK (pay_value > 0),
  CONSTRAINT ck_order_status CHECK (status = 'created'),
  CONSTRAINT ck_order_expiry CHECK (expires_at > created_at)
) ENGINE=InnoDB COMMENT='已成功创建的演示订单，不存受理中请求或失败订单';
