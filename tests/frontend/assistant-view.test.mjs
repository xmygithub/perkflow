import test from 'node:test';
import assert from 'node:assert/strict';
import { requirementRows, preferenceRows, cardPresentation, comparisonRows, choiceMessage, purchasePresentation } from '../../frontend/src/assistant-view.js';

test('unknown fields do not turn the goal panel into an empty form', () => {
  const state = { requirements: { city: '天津', budget_fen: null }, condition_sources: { city: '平台当前城市' }, preferences: { low_price: true, area: '南开区' } };
  assert.deepEqual(requirementRows(state), [['当前城市', '天津（平台城市）']]);
  assert.equal(preferenceRows(state)[0][0], '区域优先');
  assert.match(JSON.stringify(preferenceRows(state)), /没有擅设预算/);
  assert.doesNotMatch(JSON.stringify(requirementRows(state)), /待补充|200/);
});

test('catalog mapping preserves the original expression and exposes verification boundary', () => {
  const rows = requirementRows({ requirements: { city: '天津', category: '炒菜', category_matches: ['天津菜', '家常菜'] } });
  assert.deepEqual(rows.find(([label]) => label === '品类'), ['品类', '炒菜']);
  assert.match(rows.find(([label]) => label === '相关类目探索')[1], /天津菜、家常菜.*待核验/);
});

test('comparison distinguishes cash price from unknown meal cost and keeps rule conflicts', () => {
  const base = { voucher: { voucher_id: 7, shop_name: '<商户>', title: '100元代金券', pay_value: 8500, stock_snapshot: 2, checked_at: '2026-10-17', usage: { package_items: [], min_people: null, max_people: null, reservation_required: null, allowed_weekdays: null } }, verdict: 'unknown', total_cost_fen: null, reasons: [], unknowns: ['规则缺失'] };
  const rows = comparisonRows([base, { ...base, verdict: 'ineligible', reasons: ['超预算'], unknowns: [] }]);
  assert.match(rows[0].values.find(([label]) => label === '券实付')[1], /85/);
  assert.equal(rows[0].values.find(([label]) => label === '列明套餐金额')[1], '整餐金额不能确定');
  assert.equal(rows[0].values.find(([label]) => label === '预约')[1], '规则未知');
  assert.match(JSON.stringify(rows[1]), /超预算|冲突/);
  assert.equal(rows[0].title, '<商户> · 100元代金券'); // 渲染层仍通过textContent插入。
  assert.match(choiceMessage(2, '双人券'), /上一轮方案2「双人券」/);
});
test('exploratory unknown candidate is distinct from checked eligibility', () => {
  const result = { stage: 'candidate', verdict: 'unknown', reasons: [], unknowns: ['日期未确认'], preference_notes: ['其他区域备选'] };
  const view = cardPresentation(result);
  assert.match(view.label, /探索候选.*待确认/);
  assert.deepEqual(view.notes, ['日期未确认', '其他区域备选']);
  assert.equal(cardPresentation({ ...result, stage: 'checked', verdict: 'eligible' }).label, '当前条件通过');
  assert.match(cardPresentation({ ...result, stage: 'checked', verdict: 'ineligible' }).label, /冲突/);
});

test('pending and uncertain cannot be success or trigger alternatives; cancellation is history', () => {
  for (const status of ['submitting', 'pending']) {
    const view = purchasePresentation({status, message:'等待'});
    assert.equal(view.waiting, true); assert.equal(view.canReplan, false);
    assert.doesNotMatch(view.label, /已创建|成功/);
  }
  assert.equal(purchasePresentation({status:'failed'}).canReplan, true);
  const cancelled = purchasePresentation({status:'succeeded',request:{order:{status:'cancelled'}}});
  assert.match(cancelled.label, /已取消/);
  assert.equal(cancelled.canReplan, false);
});
