import test from 'node:test';
import assert from 'node:assert/strict';
import { createAuthClient } from '../../frontend/src/auth-client.js';
import { createDemoStore } from '../../frontend/src/demo-store.js';

const response = (status, data) => ({ ok: status < 400, status, headers: new Headers(), json: async () => data });
test('backend login carries cookies and CSRF, then refreshes user and rotated token', async () => {
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push([url, options]);
    if (url.endsWith('/session')) return response(200, { csrf_token: 'anonymous', user: null });
    if (url.endsWith('/login')) return response(200, { csrf_token: 'rotated', user: { id: 1, nick_name: '真实测试用户' } });
    return response(200, {});
  };
  const auth = createAuthClient(fetcher);
  await auth.sendCode('13800138000');
  assert.equal(auth.currentUser(), null);
  await auth.login('13800138000', '000007');
  assert.equal(auth.currentUser().id, '1');
  await auth.logout();
  assert.equal(auth.currentUser(), null);
  assert.equal(calls[1][1].headers['X-CSRF-Token'], 'anonymous');
  assert.equal(calls.at(-1)[1].headers['X-CSRF-Token'], 'rotated');
  assert.ok(calls.every(([, options]) => options.credentials === 'include'));
});

test('failed backend login never creates a local user; throttle and expiry remain errors', async () => {
  const auth = createAuthClient(async url => url.endsWith('/session')
    ? response(200, { csrf_token: 'csrf', user: null })
    : response(429, { error: { message: '请稍后重试', retry_after: 60 } }));
  await assert.rejects(auth.sendCode('13800138000'), error => error.retryAfter === 60);
  await assert.rejects(auth.login('13800138000', '000007'));
  assert.equal(auth.currentUser(), null);
  const offline = createAuthClient(async () => { throw new Error('offline'); });
  await assert.rejects(offline.login('13800138000', '000007'), /无法连接/);
  assert.equal(offline.currentUser(), null);
});

test('legacy browser user cannot authenticate when backend user provider is used', () => {
  const map = new Map([['perkflow.demo.user.v1', JSON.stringify({ id: 'fake', name: 'fake' })]]);
  const storage = { getItem: k => map.get(k), setItem: (k,v) => map.set(k,v), removeItem: k => map.delete(k) };
  let realUser = null;
  const store = createDemoStore(storage,storage,() => realUser);
  assert.equal(store.currentUser(), null);
  realUser = { id: '5', name: '服务端用户' };
  assert.equal(store.currentUser().id, '5');
  realUser = null;
  assert.equal(store.currentUser(), null);
});

test('static-server 404 gives restart guidance and cannot send a fake successful code', async () => {
  let calls = 0;
  const auth = createAuthClient(async () => {
    calls += 1;
    return { ok: false, status: 404, headers: new Headers(), json: async () => { throw new Error('HTML, not JSON'); } };
  });
  await assert.rejects(auth.sendCode('13800138000'), error =>
    error.code === 'AUTH_ROUTE_NOT_FOUND' && /重新启动/.test(error.message));
  assert.equal(calls, 1);
  assert.equal(auth.currentUser(), null);
});
