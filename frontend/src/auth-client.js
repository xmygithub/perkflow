// Authentication is always provided by the backend. No local login fallback.
export class AuthError extends Error {
  constructor(message, status = 0, code = 'NETWORK_ERROR', retryAfter = 0) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

export function createAuthClient(fetcher = globalThis.fetch) {
  let user = null;
  let csrf = null;
  let initializing = null;
  async function request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetcher(`/api/auth/${path}`, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'include',
        headers: {
          Accept: 'application/json',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 401) { user = null; csrf = null; }
        if (response.status === 404) {
          throw new AuthError('登录服务尚未接入，请重新启动前后端服务并刷新页面', 404, 'AUTH_ROUTE_NOT_FOUND');
        }
        throw new AuthError(data.error?.message || '登录请求失败，请稍后重试', response.status,
          data.error?.code, data.error?.retry_after || Number(response.headers.get('Retry-After')) || 0);
      }
      return data;
    } catch (error) {
      if (error instanceof AuthError) throw error;
      throw new AuthError('登录服务暂时无法连接，请确认后端和 Redis 已启动');
    } finally { clearTimeout(timer); }
  }
  function remember(data) {
    csrf = data.csrf_token;
    user = data.user ? { id: String(data.user.id), name: data.user.nick_name, phoneMasked: data.user.phone_masked } : null;
  }
  async function initialize() {
    if (initializing) return initializing;
    initializing = (async () => {
      try { const data = await request('session'); remember(data); return data; }
      catch (error) { user = null; csrf = null; throw error; }
    })();
    try { return await initializing; } finally { initializing = null; }
  }
  async function ensureSession() { if (!csrf) await initialize(); }
  return {
    initialize,
    currentUser: () => user,
    // 业务写接口复用当前会话的防护值；Cookie 仍由浏览器携带，不读取 HttpOnly SID。
    async csrfToken() { await ensureSession(); return csrf; },
    async sendCode(phone) { await ensureSession(); return request('code', { phone }); },
    async login(phone, code) {
      await ensureSession();
      const data = await request('login', { phone, code });
      remember(data);
      return user;
    },
    async logout() { await ensureSession(); await request('logout', {}); user = null; csrf = null; },
  };
}
