# PerkFlow

智能团购与异步交易系统，使用 Python 实现本地生活选购、有限库存购买与订单管理，并通过 Agent 将自然语言需求接入真实业务服务。

用户可以浏览商户和优惠券，也可以向助手描述消费需求。助手结合查询结果进行筛选、核验和比较，在用户明确确认后提交购买。交易后端通过 Redis 原子准入、消息待办和 RabbitMQ 异步建单处理库存竞争、重复提交与消息处理异常。

## 核心能力

| 模块 | 实现内容 |
|---|---|
| 智能选购 | 原生 Tool Calling、有限 Agent Loop、模糊需求探索、多轮条件更新、历史方案指代及方案比较 |
| 受控购买 | 服务端报价、用户确认、消费时条款复核、幂等请求关联与异步结果查询 |
| 用户登录 | 短信验证码流程、Cookie + Redis Session、CSRF 校验与活跃续期 |
| 商户缓存 | 详情 String、分类 List、空值缓存、随机 TTL、互斥重建与更新后失效 |
| 购买准入 | Redis Lua 校验活动、库存和购买资格，原子预扣与请求幂等；MySQL 条件更新和唯一约束兜底 |
| 异步订单 | Redis 消息待办、发布确认、租约重投、手动 ACK、消费幂等与持久化补偿 |
| 订单管理 | 本人订单查询、取消返库、保留取消历史及取消后重买 |
| 评测与压测 | 版本化 Agent 数据集、业务状态断言、工具轨迹、模型辅助评分与隔离 Locust 压测 |

## 技术栈

- 后端：Python 3.11、FastAPI、SQLAlchemy Async、Pydantic、Alembic。
- 存储与消息：MySQL 8、Redis、Lua、RabbitMQ。
- Agent：HTTPX 调用兼容 Chat Completions 的原生工具协议，不依赖 Agent 编排框架。
- 前端：HTML、CSS、JavaScript，Node.js 提供本地静态资源和 API 代理。
- 验证与环境：pytest、Node Test Runner、Locust、Docker Compose。

## 购买链路

```text
普通页面 / Agent 报价确认
            ↓
FastAPI：身份、CSRF 与请求参数校验
            ↓
Redis Lua：资格校验 + 库存预扣 + 购买请求与消息待办登记
            ├── 返回已受理及请求编号
            ↓
后台投递程序：领取待办 → RabbitMQ 发布确认 → 失败重试
            ↓
订单消费者：持久化请求 → MySQL 事务扣库存、建单并记录补偿待办
            ↓
更新 Redis 结果 → 清理消息待办 → 手动 ACK
            ↓
前端按原请求编号查询最终结果
```

API 和订单 Worker 是独立进程。Redis 负责入口准入和调度状态，MySQL 保存订单事实，RabbitMQ 缓冲并传递建单任务。消息可能重复交付，消费端通过请求状态、事务与数据库约束避免重复生效。

## 本地运行

前置环境：Python 3.11、Node.js、MySQL 8、Redis；RabbitMQ 可使用 Docker Compose 启动。

```powershell
conda create -n perkflow python=3.11
conda activate perkflow
python -m pip install -r requirements.lock
```

已有 `perkflow` 环境时直接激活，不必重新创建。首次配置将 `backend/.env.example` 复制为 `backend/.env`，填写本地数据库、Redis 与模型配置。初始化数据库及演示数据见 [后端运行说明](backend/README.md)。凭据只保存在本地，不提交仓库。

从项目根目录准备 RabbitMQ：

```powershell
python -m scripts.init_rabbitmq_env
docker compose up -d --wait --wait-timeout 180
```

在三个终端分别启动，Python 终端先执行 `conda activate perkflow`：

```powershell
# 终端一：API（项目根目录）
python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000

# 终端二：订单 Worker（项目根目录）
python -m backend.app.workers.orders

# 终端三：前端
cd frontend
npm run dev
```

- 前端：<http://127.0.0.1:5173>；选购助手：<http://127.0.0.1:5173/assistant.html>。
- API 文档：<http://127.0.0.1:8000/docs>。
- 默认购买模式为 `ORDER_MODE=async`、`ORDER_ADMISSION_MODE=redis_first`；Worker 启动时预热购买入口，仅启动 API 不会完成异步订单。
- 本地验证码通过开发短信发送器输出；接入模型需配置 `LLM_BASE_URL`、`LLM_MODEL` 和 `LLM_API_KEY`。

## 验证与实验

```powershell
# 后端：随机独立测试库；不覆盖开发库
python -m scripts.run_backend_tests

# 前端
cd frontend
npm run check
npm test
```

Agent 数据集预览与实际执行在项目根目录运行：

```powershell
python -m scripts.evaluate_agent_dataset
python -m scripts.evaluate_agent_dataset --live --judge
```

实际评测调用模型并使用独立测试库；`--judge` 是辅助评分，不能覆盖自动断言失败。已保存的一次开发集结果为 22 个任务通过 19 个（86.4%），不代表真实用户场景的总体成功率。

购买压测入口为 `python -m scripts.loadtest --help`，支持库存竞争、同账号购买和同请求重复提交。每轮使用随机 MySQL 测试库、独立 Redis 前缀和 RabbitMQ 资源，并核对订单、库存、未决请求及补偿待办。

投递性能实验比较原串行、扫描分离串行、扫描分离两路和四路，每组重复三轮。20 个虚拟用户、每轮 1000 个不同账号的合法购买请求下，两路方案的平均唯一建单吞吐由 45.42 提升至 50.74 单/秒，请求提交至订单提交 P95 由 17.50 降至 14.71 秒；12 轮订单和库存最终核对一致。该结果是隔离有限批次对照，不是生产容量上限；优化候选通过实验入口安装，日常 Worker 默认仍保留原逻辑。

实验入口：`python -m scripts.order_dispatch_performance --help`。详细运行记录和原始报告由本地私有目录保存，不随仓库发布。

## 目录

```text
agent/          模型协议、目标状态、业务工具与选购循环
backend/        FastAPI、数据模型、业务服务、迁移与订单 Worker
frontend/       业务页面、助手页面与本地代理
scripts/        数据准备、评测、隔离压测和观测工具
tests/          后端与前端测试
```

更多操作说明见 [后端](backend/README.md) 和 [前端](frontend/README.md)。演示图片的来源清单与许可证保存在 [前端资源目录](frontend/public/assets/) 和 [素材清单](frontend/assets-manifest.json)。

## 实现边界

演示商户、券和交易数据用于功能验证；创建订单不代表真实支付完成。当前未接入支付、退款、核销、定位或 Redis 高可用集群。Redis 消息待办的可恢复性依赖 Redis 数据仍然存在，不能据此承诺 Redis 节点数据丢失后零丢单。
