# PerkFlow 前端

使用原生 HTML、CSS 和 JavaScript 实现商户浏览、优惠券购买、订单管理和智能选购页面。Node.js 本地服务器托管静态文件，并将 `/api/` 和 `/health/` 请求代理到 FastAPI。

## 启动

先启动 API 和订单 Worker，再执行：

```powershell
cd frontend
npm run dev
```

打开 <http://127.0.0.1:5173>。开发服务器没有额外 npm 依赖，通过 Node.js 内置模块运行；需要支持 `node --watch` 和 Node Test Runner 的版本，建议 Node.js 20 或更新版本。

端口被占用时，在同一 PowerShell 终端修改：

```powershell
$env:PORT = '5174'
npm run dev
```

如果 API 使用其他本机端口，可设置 `$env:PERKFLOW_API_TARGET = 'http://127.0.0.1:8001'`。改变前端端口后，同步将对应来源加入后端 `CORS_ORIGINS`；代理仅允许本机 HTTP 目标。请通过 HTTP 服务访问页面，不要直接双击 HTML 文件。

## 页面与交互

| 页面 | 功能 |
|---|---|
| `index.html` | 首页、分类及商户入口 |
| `shop-list.html` | 城市、区域、分类与排序筛选 |
| `shop-detail.html` | 商户详情和优惠券 |
| `vouchers.html` | 普通券与秒杀券活动、库存和购买 |
| `orders.html` | 当前用户订单与取消操作 |
| `assistant.html` | 多轮选购、真实工具执行进度、报价确认和购买结果回查 |

页面使用后端返回的活动与订单状态。`pending` 只表示请求已受理，不能显示为已购买；结果未知时回查原请求，不自动重复下单。购买请求通过 `Idempotency-Key` 标识意图，同一页面内结果不明的重试复用原 UUID；一人一单仍由服务端保证。

金额按分转换展示，长订单 ID 始终保留字符串。报价确认提交服务端报价标识，不让前端指定订单价格。操作使用 Cookie 会话，写请求携带 CSRF Token。

## 检查与测试

```powershell
npm run check
npm test
```

## 演示素材

商户和商品为虚构演示数据，图片仅用于页面展示。第三方图片来源、固定版本与校验信息见 [素材清单](assets-manifest.json)，相应许可证保留在 [资源目录](public/assets/)。这些图片不作为本项目原创素材声明。

后端配置和启动见 [后端说明](../backend/README.md)，整体架构及评测入口见 [项目 README](../README.md)。
