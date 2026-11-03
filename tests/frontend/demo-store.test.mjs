import test from 'node:test';
import assert from 'node:assert/strict';
import { createDemoStore, safeReturn } from '../../frontend/src/demo-store.js';

function storage() {
  const data = new Map();
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) };
}
const coupon = { id: 101, state: 'active', stock: 1 };

test('login is required and duplicate claims do not consume more demo stock', () => {
  const store = createDemoStore(storage(), storage());
  assert.throws(() => store.createOrder(coupon), /演示账户/);
  store.signIn('13800138000');
  store.createOrder(coupon);
  assert.throws(() => store.createOrder(coupon), /已经创建/);
  assert.equal(store.orders().length, 1);
  assert.equal(store.remaining(coupon), 0);
});

test('other demo users cannot see the first user orders or claim exhausted stock', () => {
  const persistent = storage();
  const first = createDemoStore(persistent, storage());
  const second = createDemoStore(persistent, storage());
  first.signIn('13800138000');
  first.createOrder(coupon);
  second.signIn('13900139000');
  assert.deepEqual(second.orders(), []);
  assert.throws(() => second.createOrder(coupon), /库存已用完/);
  assert.equal(first.orders().length, 1);
});

test('upcoming and ended coupons cannot create demo orders', () => {
  const store = createDemoStore(storage(), storage());
  store.signIn('13800138000');
  assert.throws(() => store.createOrder({ ...coupon, state: 'upcoming' }), /尚未开始/);
  assert.throws(() => store.createOrder({ ...coupon, state: 'ended' }), /已结束/);
  assert.deepEqual(store.orders(), []);
});

test('reloading the adapter preserves records; reset restores fixture stock', () => {
  const persistent = storage(), session = storage();
  const first = createDemoStore(persistent, session);
  first.signIn('13800138000');
  first.createOrder(coupon);
  const reloaded = createDemoStore(persistent, session);
  assert.equal(reloaded.orders().length, 1);
  reloaded.resetOrders();
  assert.equal(reloaded.remaining(coupon), 1);
  assert.equal(reloaded.currentUser().name, '体验用户 8000');
});

test('invalid stored data does not crash rendering or accept a fake order shape', () => {
  const persistent = storage();
  persistent.setItem('perkflow.demo.orders.v1', '{broken JSON');
  const store = createDemoStore(persistent, storage());
  store.signIn('13800138000');
  assert.deepEqual(store.orders(), []);
  persistent.setItem('perkflow.demo.orders.v1', JSON.stringify([null, { voucherId: 101 }]));
  assert.deepEqual(store.orders(), []);
  assert.equal(store.remaining(coupon), 1);
});

test('login return path allows local pages and rejects external redirects', () => {
  assert.equal(safeReturn('shop-detail.html?id=2'), 'shop-detail.html?id=2');
  for (const path of ['https://example.com/', '//example.com/', 'javascript:alert(1)', 'login.html']) assert.equal(safeReturn(path), 'index.html');
});
