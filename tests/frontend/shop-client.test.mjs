import test from 'node:test';
import assert from 'node:assert/strict';
import { adaptShop, assetPath, createShopClient, shopQuery } from '../../frontend/src/shop-client.js';

const shop = {
  id: 1, type_id: 2, name: '数据库店名', score: 4.8, avg_price: 8850,
  open_hours: '11:00–21:00', images: ['public/assets/photos/02.jpg'], tags: ['茶馆'],
};
const response = (status, data) => ({ ok: status < 400, status, json: async () => data });

test('API fields preserve image paths, convert cents once, and keep database identity', () => {
  const result = adaptShop(shop);
  assert.equal(result.categoryId, 2);
  assert.equal(result.averagePrice, 88.5);
  assert.equal(result.avg_price, 8850);
  assert.equal(result.name, '数据库店名');
  assert.equal(result.hours, shop.open_hours);
  assert.deepEqual(result.images, shop.images);
  assert.equal(assetPath('https://other.example/image.jpg'), 'public/assets/photos/01.jpg');
  assert.equal(assetPath('public/assets/photos/../secret.png'), 'public/assets/photos/01.jpg');
});

test('page filters become supported backend parameters and preserve server pagination', async () => {
  const query = shopQuery(new URLSearchParams('type=3&area=海河沿线&q=茶&sort=price&page=2&kind=tea&offer=1'));
  assert.equal(query.get('category_id'), '3');
  assert.equal(query.get('city'), '天津');
  assert.equal(query.get('q'), '茶');
  assert.equal(query.get('kind'), '茶馆');
  assert.equal(query.get('page'), '2');
  assert.equal(query.get('page_size'), '4');
  assert.equal(query.has('offer'), false);
  let call;
  const api = createShopClient(async (url, options) => {
    call = [url, options];
    return response(200, { items: [shop], total: 19, page: 2, page_size: 4 });
  });
  const result = await api.list(query);
  assert.equal(result.total, 19);
  assert.equal(result.page, 2);
  assert.equal(result.items.length, 1);
  assert.equal(call[1].credentials, 'include');
  assert.equal(call[1].cache, 'no-store');
  assert.equal(call[1].signal instanceof AbortSignal, true);
});

test('category list and details use exact cache-backed routes', async () => {
  const calls = [];
  const api = createShopClient(async url => {
    calls.push(url);
    return response(200, url.endsWith('shop-types') ? {
      items: [{ id: 10, name: '美甲', icon: 'public/assets/categories/mjmj.png', sort: 1 }],
    } : shop);
  });
  assert.equal((await api.categories())[0].icon, 'public/assets/categories/mjmj.png');
  assert.equal((await api.detail('1')).name, '数据库店名');
  assert.deepEqual(calls, ['/api/shop-types', '/api/shops/1']);
  await assert.rejects(api.detail('bad'), error => error.status === 422);
  assert.equal(calls.length, 2);
});

test('missing shops stay 404; failed requests cannot produce static successful data', async () => {
  const missing = createShopClient(async () => response(404, { error: { message: '商户不存在' } }));
  await assert.rejects(missing.detail(999999), error => error.status === 404);
  const offline = createShopClient(async () => { throw new Error('offline'); });
  await assert.rejects(offline.categories(), /无法连接/);
  const unavailable = createShopClient(async () => response(503, { error: { message: '数据库暂不可用' } }));
  await assert.rejects(unavailable.list(shopQuery()), error => error.status === 503);
});

test('invalid or non-JSON response is an error, distinct from a legitimate empty list', async () => {
  const empty = createShopClient(async () => response(200, { items: [], total: 0, page: 1, page_size: 4 }));
  assert.deepEqual((await empty.list(shopQuery())).items, []);
  const broken = createShopClient(async () => ({ ok: true, json: async () => { throw new Error('HTML'); } }));
  await assert.rejects(broken.categories(), /无效数据/);
  const invalid = createShopClient(async () => response(200, { items: [shop], total: 1 }));
  await assert.rejects(invalid.list(shopQuery()), /格式不正确/);
});

test('saved IDs fetch real cached details; deleted shops are skipped but outages remain errors', async () => {
  const api = createShopClient(async url => url.endsWith('/2')
    ? response(404, { error: { message: '商户不存在' } }) : response(200, shop));
  assert.deepEqual((await api.saved([1, 2])).map(item => item.id), [1]);
  const failed = createShopClient(async () => response(503, { error: { message: '数据库暂不可用' } }));
  await assert.rejects(failed.saved([1]), error => error.status === 503);
});
