// Local preview with same-origin API proxy. Backend owns authentication and database access.
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PORT || 5173);
const apiTarget = new URL(process.env.PERKFLOW_API_TARGET || 'http://127.0.0.1:8000');
if (apiTarget.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(apiTarget.hostname)) {
  throw new Error('本地 API 代理仅支持 http://127.0.0.1 或 http://localhost');
}
function proxyApi(request, response) {
  const upstream = http.request({
    hostname: apiTarget.hostname, port: apiTarget.port || 80,
    method: request.method, path: request.url,
    headers: { ...request.headers, host: apiTarget.host },
  }, backendResponse => {
    response.writeHead(backendResponse.statusCode, backendResponse.headers);
    backendResponse.pipe(response);
  });
  // 模型单次等待可超过普通API的15秒；仅助手对话流使用客户端同级上限，交易接口不变。
  const assistantTurn = /^\/api\/assistant\/conversations\/[^/]+\/turns(?:\?|$)/.test(request.url);
  upstream.setTimeout(assistantTurn ? 210000 : 15000, () => upstream.destroy(new Error('API timeout')));
  upstream.on('error', () => {
    if (response.headersSent) { response.destroy(); return; }
    response.writeHead(503, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    response.end(JSON.stringify({ error: { code: 'BACKEND_UNAVAILABLE', message: '后端尚未连接，请启动 FastAPI 服务' } }));
  });
  request.on('aborted', () => upstream.destroy());
  request.pipe(upstream);
}
const types = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.txt': 'text/plain; charset=utf-8' };
const server = http.createServer(async (request, response) => {
  if (request.url.startsWith('/api/') || request.url.startsWith('/health/')) {
    proxyApi(request, response);
    return;
  }
  if (!['GET', 'HEAD'].includes(request.method)) {
    response.writeHead(405, { Allow: 'GET, HEAD' }).end();
    return;
  }
  try {
    const url = new URL(request.url, 'http://localhost');
    const name = decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname);
    const file = path.resolve(root, `.${name}`);
    if (!file.startsWith(root + path.sep)) {
      response.writeHead(403).end();
      return;
    }
    const contents = await readFile(file);
    response.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
    response.end(request.method === 'HEAD' ? undefined : contents);
  } catch {
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('页面不存在');
  }
});
server.on('error', error => {
  console.error(error.code === 'EADDRINUSE' ? `端口 ${port} 已被占用，请设置 PORT 后重试。` : error.message);
  process.exitCode = 1;
});
server.listen(port, '127.0.0.1', () => {
  console.log(`PerkFlow preview: http://127.0.0.1:${port}`);
  console.log(`API proxy: /api and /health -> ${apiTarget.origin}`);
});
