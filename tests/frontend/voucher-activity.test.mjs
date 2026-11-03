import test from 'node:test';
import assert from 'node:assert/strict';
import { voucherAvailability, voucherActivityWindow } from '../../frontend/src/voucher-activity.js';

const begin = '2026-10-16T10:00:00Z', end = '2026-10-16T12:00:00Z';
const voucher = { type: 'normal', state: 'active', begin_time: begin, end_time: end };

test('normal and seckill use the same inclusive-start exclusive-end purchase window', () => {
  for (const type of ['normal', 'seckill']) {
    assert.equal(voucherAvailability({ ...voucher, type }, Date.parse(begin)).purchasable, true);
    assert.equal(voucherAvailability({ ...voucher, type }, Date.parse(end)).purchasable, false);
    assert.equal(voucherAvailability({ ...voucher, type }, Date.parse(begin) - 1).label, '尚未开始');
  }
});

test('local clock never promotes server blocked status or absent activity into purchasable', () => {
  for (const state of ['upcoming', 'ended', 'sold_out', 'off_shelf', 'unavailable']) {
    assert.equal(voucherAvailability({ ...voucher, state }, Date.parse(begin)).purchasable, false);
  }
  assert.equal(voucherAvailability({ ...voucher, end_time: null }).purchasable, false);
  assert.equal(voucherAvailability({ ...voucher, end_time: begin }).purchasable, false);
});

test('activity window converts UTC to Beijing time and represents missing configuration honestly', () => {
  const text = voucherActivityWindow(voucher);
  assert.match(text, /2026\/10\/16 18:00/);
  assert.match(text, /2026\/10\/16 20:00/);
  assert.match(text, /北京时间/);
  assert.equal(voucherActivityWindow({ begin_time: null, end_time: null }), '购买时间：待配置');
});
