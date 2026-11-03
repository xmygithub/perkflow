import test from 'node:test';
import assert from 'node:assert/strict';
import { createVoucherClient, adaptOrder, adaptRequest } from '../../frontend/src/voucher-client.js';

const order = { id: '377000000000000001', voucher_id: 102, shop_id: 2, shop_name: '测试茶馆',
  shop_image: 'public/assets/photos/01.jpg', title: '测试优惠', pay_value: 5900, status: 'created',
  created_at: '2026-10-14T09:00:00Z', expires_at: '2026-11-13T09:00:00Z' };
const response = (status, data) => ({ ok: status < 400, status, json: async () => data });
test('order POST uses session CSRF and cookie, exact string ID and repeat response', async () => {
  const calls = [];
  const api = createVoucherClient({ csrfToken: async () => 'rotated-test' }, async (url, options) => {
    calls.push([url, options]); return response(200, { order, reused: true });
  });
  const result = await api.createOrder(102);
  assert.equal(result.order.id, order.id); assert.equal(result.reused, true);
  assert.equal(calls[0][0], '/api/vouchers/102/orders');
  assert.equal(calls[0][1].method, 'POST'); assert.equal(calls[0][1].credentials, 'include');
  assert.equal(calls[0][1].headers['X-CSRF-Token'], 'rotated-test');
  assert.equal(calls[0][1].body, undefined);
});
test('order price comes from order snapshot, long IDs reject numeric representation', () => {
  assert.equal(adaptOrder(order).payValue, 5900);
  assert.throws(() => adaptOrder({ ...order, id: Number(order.id) }), /格式/);
});
test('query returns real stock and maps cents, types and safe assets', async () => {
  const api = createVoucherClient({}, async () => response(200, { items: [{ id: 102, shop_id: 2,
    shop_name: '测试', shop_image: 'javascript:bad', type: 1, stock: 3, pay_value: 5900,
    actual_value: 8800, rules: ['规则'], state: 'active' }], total: 1, page: 1, page_size: 20 }));
  const data = await api.list();
  assert.equal(data.items[0].stock, 3); assert.equal(data.items[0].type, 'seckill');
  assert.equal(data.items[0].payValue, 5900); assert.equal(data.items[0].shopImage, 'public/assets/photos/01.jpg');
});
test('stock failures and login expiry are errors, never local successful orders', async () => {
  const auth = { csrfToken: async () => 'test' };
  const api = createVoucherClient(auth, async () => response(409, { error: { code: 'OUT_OF_STOCK', message: '优惠券已抢完' } }));
  await assert.rejects(api.createOrder(102), error => error.status === 409 && error.code === 'OUT_OF_STOCK');
  const expired = createVoucherClient(auth, async () => response(401, { error: { message: '请先登录' } }));
  await assert.rejects(expired.orders(), error => error.status === 401);
});
test('write network failure prompts result lookup and does not automatically retry', async () => {
  let calls = 0;
  const api = createVoucherClient({ csrfToken: async () => 'test' }, async () => { calls++; throw new Error('lost reply'); });
  await assert.rejects(api.createOrder(102), error => error.code === 'ORDER_RESULT_UNKNOWN' && /我的订单/.test(error.message));
  assert.equal(calls, 1);
});

test('cancel uses exact string ID, CSRF and cookies, and accepts retained cancelled history', async () => {
  const calls = [];
  const cancelled = { ...order, status: 'cancelled', cancelled_at: '2026-10-15T09:00:00Z' };
  const api = createVoucherClient({ csrfToken: async () => 'cancel-csrf' }, async (url, options) => {
    calls.push([url, options]); return response(200, { order: cancelled, reused: false });
  });
  const result = await api.cancelOrder(order.id);
  assert.equal(result.order.id, order.id);
  assert.equal(result.order.status, 'cancelled');
  assert.equal(result.order.cancelledAt, cancelled.cancelled_at);
  assert.equal(calls[0][0], `/api/orders/${order.id}/cancel`);
  assert.equal(calls[0][1].method, 'POST');
  assert.equal(calls[0][1].credentials, 'include');
  assert.equal(calls[0][1].headers['X-CSRF-Token'], 'cancel-csrf');
  assert.equal(calls[0][1].body, undefined);
  assert.throws(() => adaptOrder({ ...cancelled, cancelled_at: null }), /格式/);
});

test('cancel rejects unsafe IDs before request and cannot turn a failed response into success', async () => {
  let calls = 0;
  const api = createVoucherClient({ csrfToken: async () => 'test' }, async () => {
    calls++; return response(404, { error: { code: 'ORDER_NOT_FOUND', message: '订单不存在' } });
  });
  for (const id of [Number(order.id), '../102', '0', '9223372036854775808']) {
    await assert.rejects(api.cancelOrder(id), /编号/);
  }
  assert.equal(calls, 0);
  await assert.rejects(api.cancelOrder(order.id), error => error.code === 'ORDER_NOT_FOUND');
  assert.equal(calls, 1);
});

test('unknown cancellation result asks to refresh and never automatically resubmits', async () => {
  let calls = 0;
  const api = createVoucherClient({ csrfToken: async () => 'test' }, async () => {
    calls++; throw new Error('lost cancellation reply');
  });
  await assert.rejects(api.cancelOrder(order.id), error => error.code === 'ORDER_CANCEL_RESULT_UNKNOWN'
    && /取消结果/.test(error.message) && /刷新/.test(error.message));
  assert.equal(calls, 1);
});

const pending = { id: '377000000000000002', voucher_id: 102, shop_name: '测试茶馆',
  title: '测试优惠', status: 'pending', order: null, error_code: null, error_message: null,
  created_at: '2026-10-14T09:00:00Z' };

test('202 acceptance remains pending and sends one UUID intent header', async () => {
  const calls = [];
  const api = createVoucherClient({ csrfToken: async () => 'test' }, async (url, options) => {
    calls.push([url, options]); return response(202, { request: pending, reused: false });
  });
  const result = await api.createOrder(102);
  assert.equal(result.request.status, 'pending');
  assert.equal(result.order, null);
  assert.equal(result.request.id, pending.id);
  assert.match(calls[0][1].headers['Idempotency-Key'], /^[0-9a-f-]{36}$/);
  assert.equal(calls.length, 1);
});

test('uncertain response retains UUID while a confirmed new intent uses a fresh UUID', async () => {
  const keys = [];
  const api = createVoucherClient({ csrfToken: async () => 'test' }, async (url, options) => {
    keys.push(options.headers['Idempotency-Key']);
    if (keys.length === 1) throw new Error('network reply lost');
    return response(202, { request: pending, reused: true });
  });
  await assert.rejects(api.createOrder(102), error => error.code === 'ORDER_RESULT_UNKNOWN');
  await api.createOrder(102);
  await api.createOrder(102);
  assert.equal(keys[0], keys[1]);
  assert.notEqual(keys[1], keys[2]);
});

test('same UUID is never reused across user accounts', async () => {
  let user = 'first';
  const keys = [];
  const api = createVoucherClient({ currentUser: () => ({ id: user }), csrfToken: async () => 'test' }, async (url, options) => {
    keys.push(options.headers['Idempotency-Key']); throw new Error('lost');
  });
  await assert.rejects(api.createOrder(102));
  user = 'second';
  await assert.rejects(api.createOrder(102));
  assert.notEqual(keys[0], keys[1]);
});

test('request states keep failure and cancelled success distinct from pending', () => {
  const failed = adaptRequest({ ...pending, status: 'failed', error_code: 'OUT_OF_STOCK', error_message: '已抢完' });
  assert.equal(failed.errorMessage, '已抢完'); assert.equal(failed.order, null);
  const cancelled = { ...order, status: 'cancelled', cancelled_at: '2026-10-15T09:00:00Z' };
  assert.equal(adaptRequest({ ...pending, status: 'succeeded', order: cancelled }).order.status, 'cancelled');
  assert.throws(() => adaptRequest({ ...pending, status: 'succeeded' }), /格式/);
  assert.throws(() => adaptRequest({ ...pending, id: Number(pending.id) }), /格式/);
});

test('request lookup is an authenticated read and supports reload from server history', async () => {
  const calls = [];
  const api = createVoucherClient({}, async (url, options) => {
    calls.push([url, options]);
    return response(200, url.includes('?') ? { items: [pending], total: 1, page: 1, page_size: 20 } : pending);
  });
  assert.equal((await api.requests()).items[0].status, 'pending');
  assert.equal((await api.orderRequest(pending.id)).id, pending.id);
  assert.equal(calls[0][1].method, 'GET');
  assert.equal(calls[0][1].credentials, 'include');
  assert.equal(calls[1][0], `/api/order-requests/${pending.id}`);
  assert.equal(calls[0][1].headers['Idempotency-Key'], undefined);
});

test('Redis reservation is progress rather than a successful order', async () => {
  assert.equal(adaptRequest(pending).reservationAcquired, false);
  const reserved = { ...pending, reservation_acquired: true };
  const api = createVoucherClient({ csrfToken: async () => 'test' }, async () =>
    response(202, { request: reserved, reused: false }));
  const result = await api.createOrder(102);
  assert.equal(result.request.reservationAcquired, true);
  assert.equal(result.request.status, 'pending');
  assert.equal(result.order, null);
  assert.throws(() => adaptRequest({ ...pending, reservation_acquired: 'true' }), /格式/);
});

test('Lua admission rejection returns a persisted failure without a fake order', async () => {
  const failed = { ...pending, status: 'failed', reservation_acquired: false,
    error_code: 'PURCHASE_QUALIFICATION_BUSY', error_message: '购买资格正在释放，请稍后重试' };
  const api = createVoucherClient({ csrfToken: async () => 'test' }, async () =>
    response(200, { request: failed, reused: false }));
  const result = await api.createOrder(102);
  assert.equal(result.request.status, 'failed');
  assert.equal(result.order, null);
  assert.match(result.request.errorMessage, /资格/);
});
