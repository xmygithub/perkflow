# PerkFlow 后端

FastAPI 提供登录、商户、优惠券、订单与选购助手 API；独立订单 Worker 负责消息投递、消费、重试和补偿。MySQL 保存业务事实，Redis 提供会话、缓存及购买准入，RabbitMQ 传递异步建单任务。

## 环境配置

以下命令均从项目根目录运行：

```powershell
conda activate perkflow
python -m pip install -r requirements.lock
```

首次配置将 `backend/.env.example` 复制为 `backend/.env`，不要覆盖已有本地配置。主要配置项：

| 配置 | 用途 |
|---|---|
| `MYSQL_*` | MySQL 连接和连接池，默认数据库名 `perkflow` |
| `REDIS_*` | Redis 连接；会话、缓存与订单使用各自前缀 |
| `RABBITMQ_*` | RabbitMQ 连接，初始化脚本将本机凭据写入根目录 `.env` |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | 选购助手使用的兼容模型接口 |
| `ORDER_MODE=async` | 默认异步购买；`sync` 保留作隔离比较 |
| `ORDER_ADMISSION_MODE=redis_first` | HTTP 入口只进行 Redis 准入，SQL 检查点始于消费者 |
| `ORDER_WORKER_CONCURRENCY=5` | RabbitMQ Prefetch 上限，不等于线程数或实际事务并发数 |

真实凭据只保存在本地。开发环境允许本机 HTTP Cookie，部署 HTTPS 时应配置安全 Cookie 和准确的前端来源。远程 HTTP 模型服务需显式配置允许来源，建议使用 HTTPS。

## 首次初始化数据库

1. 在 MySQL 8 中创建空的 `perkflow` 数据库，字符集 `utf8mb4`，将连接信息写入 `backend/.env`。
2. 执行迁移创建当前表结构：

```powershell
python -m alembic -c backend/alembic.ini upgrade head
```

3. 仅在新库中导入 `scripts/sql/002_demo_seed.sql`，使用遇错停止的客户端并完整执行事务。文件固定选择 `perkflow` 数据库，不要对已有业务库重复导入。
4. 追加演示商户、优惠券与结构化使用规则：

```powershell
python -m scripts.seed_demo_catalog
python -m scripts.seed_demo_catalog --apply
python -m scripts.seed_group_buy_rules
python -m scripts.seed_group_buy_rules --apply
```

不带 `--apply` 为预览；增量脚本不覆盖已有库存或订单。已有数据库只执行适用的迁移，不重新导入种子。通过初始 SQL 快照建过表、但未记录迁移版本的旧库，应先用 `python -m backend.app.core.schema_check --initial` 核对初始结构，再按真实版本登记和迁移，不能直接标记为最新版本。

## 启动

先保证 MySQL、Redis 和 RabbitMQ 可连接，再在两个终端分别执行：

```powershell
# API
python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000

# 独立订单 Worker
python -m backend.app.workers.orders
```

API 文档：<http://127.0.0.1:8000/docs>。Worker 首次启动准备活动和 Redis 入口，未预热的券不会由 HTTP 请求临时回源建库存。活动时间以数据库实际配置为准，历史演示活动不会自动无限延期。

## 接口与状态

- 登录：`/api/auth/*`，Cookie + Redis Session，写请求校验 CSRF。
- 商户：`GET /api/shop-types`、`GET /api/shops`、`GET /api/shops/{id}`。
- 优惠券：`GET /api/vouchers`、`GET /api/vouchers/{id}`。
- 购买：`POST /api/vouchers/{id}/orders`，携带 `Idempotency-Key`，返回购买请求及复用标识；待处理为 `pending`，不代表成功。
- 进度：`GET /api/order-requests`、`GET /api/order-requests/{id}`。
- 订单：`GET /api/orders`、`GET /api/orders/{id}`、`POST /api/orders/{id}/cancel`，仅操作当前用户的记录。
- 助手：`/api/assistant/*`，对话事件流、服务端报价、页面确认与购买结果回查；具体参数见 API 文档。

金额统一使用整数分，订单和请求 ID 使用字符串。订单创建、库存扣减和请求终态在 MySQL 事务中提交；取消保留历史，同事务返库并登记 Redis 补偿待办。业务不包含真实支付或退款。

## 测试

```powershell
python -m scripts.run_backend_tests
```

脚本使用本地连接配置创建随机 `perkflow_test_` 测试库，执行迁移和测试后清理该库；测试账号需要相应建库权限。Redis 与 MQ 集成用例按测试配置使用隔离命名空间，未配置外部服务的用例会跳过。不要把开发库传给集成测试。

评测和压测入口、当前能力与边界见 [项目 README](../README.md)。本地学习笔记、验证报告和私有运行数据不在发布仓库中。
