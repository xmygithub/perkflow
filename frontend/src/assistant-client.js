export class AssistantError extends Error {
  constructor(message, code = 'ASSISTANT_ERROR', status = 0) {
    super(message); this.code = code; this.status = status;
  }
}

// 只解析服务端逐行事件；没有done不能当作整轮成功，不自动重发请求。
export async function consumeEvents(body, onEvent) {
  if (!body) throw new AssistantError('服务没有返回执行进度');
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let pending = ''; let result = null;
  function accept(line) {
    if (!line.trim()) return;
    let event;
    try { event = JSON.parse(line); } catch { throw new AssistantError('执行进度格式不正确'); }
    if (event.type === 'error') throw new AssistantError(event.message, event.code);
    if (event.type === 'done') {
      if (!event.result?.conversation || typeof event.result.answer !== 'string') {
        throw new AssistantError('助手结果不完整');
      }
      result = event.result;
    } else if (event.type === 'progress' && typeof event.event?.text === 'string') {
      onEvent(event.event);
    }
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (pending.length > 200000) throw new AssistantError('执行进度超过允许大小');
      const lines = pending.split('\n'); pending = lines.pop();
      for (const line of lines) accept(line);
      if (done) break;
    }
    if (pending) accept(pending);
    if (!result) throw new AssistantError('连接已中断，请刷新读取已保存对话；已确认的购买请回查原请求进度');
    return result;
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

export function createAssistantClient(auth, fetcher = globalThis.fetch) {
  async function request(path, body, signal) {
    const response = await fetcher(`/api/assistant/${path}`, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'include', signal,
      headers: { Accept: 'application/json, application/x-ndjson',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json',
          'X-CSRF-Token': await auth.csrfToken() }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new AssistantError(data.error?.message || '助手请求失败', data.error?.code, response.status);
    }
    return response;
  }
  async function lookup(path) {
    // 同一等待预算覆盖响应正文；超时只报告查验失败，绝不重试购买。
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try { return await (await request(path, undefined, controller.signal)).json(); }
    catch (error) {
      if (error.name === 'AbortError') throw new AssistantError('回查超时，请稍后重试', 'PURCHASE_LOOKUP_TIMEOUT');
      throw error;
    } finally { clearTimeout(timer); }
  }
  return {
    status: async () => (await request('status')).json(),
    create: async () => (await request('conversations', {})).json(),
    get: async id => (await request(`conversations/${encodeURIComponent(id)}`)).json(),
    purchases(id, purchaseId) {
      const query = purchaseId ? `?purchase_id=${encodeURIComponent(purchaseId)}` : '';
      return lookup(`conversations/${encodeURIComponent(id)}/purchases${query}`);
    },
    accountPurchases(page = 1, scope = 'all') {
      return lookup(`purchases?${new URLSearchParams({ page, page_size: 5, scope })}`);
    },
    async confirm(conversation, proposalId, acknowledgeUnknowns) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      try {
        return await (await request(`conversations/${encodeURIComponent(conversation.id)}/purchases/confirm`, {
          proposal_id: proposalId, expected_version: conversation.version,
          acknowledge_unknowns: acknowledgeUnknowns,
        }, controller.signal)).json();
      } finally { clearTimeout(timer); }
    },
    async turn(conversation, message, onEvent, signal) {
      const controller = new AbortController();
      const abort = () => controller.abort();
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) controller.abort();
      const timer = setTimeout(abort, 210000);
      try {
        const response = await request(`conversations/${encodeURIComponent(conversation.id)}/turns`,
          { message, expected_version: conversation.version }, controller.signal);
        return await consumeEvents(response.body, onEvent);
      } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
    },
  };
}
