import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import * as views from '../../frontend/src/assistant-view.js';

// 执行真实页面事件处理器，仅替换DOM与API；验证用户点击后的请求、互斥和可见反馈。
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.events = {}; this.dataset = {}; this.value = ''; this.textContent = ''; this.disabled = false; this.scrolls = 0; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, handler) { this.events[name] = handler; }
  querySelectorAll(tag) { return this.children.flatMap(child => [...(child.tagName === tag ? [child] : []), ...child.querySelectorAll(tag)]); }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolls++; }
  async click() { if (!this.disabled) return this.events.click?.({ preventDefault() {} }); }
}

const failed = { id: 'old', status: 'failed', lookup_available: true, message: '购买服务暂未准备就绪，本次未创建订单', proposal: { evaluation: { voucher: { title: '暖锅里工作日双人套餐' } } } };
const state = () => ({ messages: [], goal: '', requirements: { city: '天津' }, preferences: {}, events: [], cards: [], comparisons: [], questions: [], purchase_proposal: null });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

async function page({ initialState = state(), initialPurchases = [failed], initialAccountItems = [], initialSelected = null, hidden = false } = {}) {
  const elements = new Map();
  const element = selector => { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); };
  const conversation = { id: 'one', version: 1, state: initialState };
  const calls = { purchases: 0, purchaseArguments: [], accounts: [], gets: [], turns: [], confirms: 0 };
  const stored = new Map([['perkflow:assistant:1', 'one']]);
  if (initialSelected) stored.set('perkflow:assistant:1:purchase', initialSelected);
  const accountResult = { items: initialAccountItems, total: initialAccountItems.length, unresolved_count: initialAccountItems.filter(row => ['submitting', 'pending'].includes(row.status)).length, page: 1, page_size: 5 };
  const api = {
    status: async () => ({ configured: true }),
    create: async () => ({ ...conversation, id: 'new-chat', state: state() }),
    get: async id => { calls.gets.push(id); return { ...conversation, id }; },
    purchases: async (...args) => { calls.purchases++; calls.purchaseArguments.push(args); return initialPurchases; },
    accountPurchases: async (...args) => { calls.accounts.push(args); return accountResult; },
    turn: async (_conversation, message) => { calls.turns.push(message); return { conversation: { ...conversation, version: 2, state: state() } }; },
    confirm: async () => { calls.confirms++; throw new Error('不应隐式购买'); },
  };
  const auth = { initialize: async () => {}, currentUser: () => ({ id: 1 }) };
  const source = (await readFile(new URL('../../frontend/src/assistant-page.js', import.meta.url), 'utf8'))
    .replace(/^import .*;\r?\n/gm, '')
    .replace('initialize().catch(', 'globalThis.pageReady = initialize().catch(');
  const context = vm.createContext({ ...views, createAuthClient: () => auth, createAssistantClient: () => api,
    document: { hidden, addEventListener() {}, querySelector: element, createElement: tag => new Element(tag) },
    window: { addEventListener() {} }, sessionStorage: { getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key) },
    AbortController, Date, setTimeout, clearTimeout,
  });
  vm.runInContext(source, context);
  await context.pageReady;
  return { element, api, calls, conversation, stored };
}

test('failed purchase review executes one read-only turn, displays new quote and requires separate confirmation', async () => {
  const p = await page(), pending = deferred();
  p.api.turn = async (_conversation, message) => { p.calls.turns.push(message); return pending.promise; };
  const retry = p.element('#purchase-progress').querySelectorAll('button')[0];
  assert.equal(retry.textContent, '重新核对并生成确认');
  const first = retry.click();
  await retry.click();
  assert.equal(p.calls.turns.length, 1);
  assert.match(p.calls.turns[0], /暖锅里工作日双人套餐.*先不要提交购买/);
  assert.match(p.element('#purchase-feedback').textContent, /正在重新核对/);
  assert.equal(retry.disabled, true);
  const voucher = { voucher_id: 3002, shop_name: '暖锅里', title: '工作日双人套餐', area: '南开区', pay_value: 15800, rules_text: [], usage: { package_items: [] } };
  const proposal = { id: 'new', expires_at: '2099-01-01T00:00:00Z', requirements: { city: '天津' }, evaluation: { voucher, verdict: 'unknown', reasons: [], unknowns: ['使用日未确认'] } };
  pending.resolve({ conversation: { ...p.conversation, version: 2, state: { ...state(), purchase_proposal: proposal } } });
  await first;
  assert.equal(p.element('#purchase-panel').hidden, false);
  assert.equal(p.element('#purchase-panel').scrolls, 1);
  assert.equal(p.element('#unknown-consent-row').hidden, false);
  assert.match(p.element('#purchase-feedback').textContent, /已生成新的确认内容.*尚未重新购买/);
  assert.equal(p.calls.confirms, 0);
});

test('manual lookup shows loading then unchanged failed result, prevents duplicate clicks and never purchases', async () => {
  const p = await page(), pending = deferred();
  p.api.purchases = async () => { p.calls.purchases++; return pending.promise; };
  const button = p.element('#refresh-purchases');
  const first = button.click();
  await button.click();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, '正在回查…');
  assert.equal(p.calls.purchases, 2); // 初始化一次，用户点击一次。
  pending.resolve([failed]); await first;
  assert.equal(button.disabled, false);
  assert.match(p.element('#purchase-feedback').textContent, /回查完成.*本次购买失败.*失败记录保留历史/);
  assert.equal(p.calls.confirms, 0);
  assert.equal(p.calls.turns.length, 0);
});

test('lookup and review failures are visible beside purchase controls and leave purchase untouched', async () => {
  const p = await page();
  p.api.purchases = async () => { throw new Error('回查超时'); };
  await p.element('#refresh-purchases').click();
  assert.match(p.element('#purchase-feedback').textContent, /回查失败：回查超时.*未重新提交购买/);
  assert.equal(p.element('#refresh-purchases').disabled, false);
  p.api.turn = async () => { throw new Error('模型暂不可用'); };
  await p.element('#purchase-progress').querySelectorAll('button')[0].click();
  assert.match(p.element('#purchase-feedback').textContent, /重新核对未完成：模型暂不可用/);
  assert.equal(p.calls.confirms, 0);
});

test('draft-only alternative action moves to input and explicitly requests sending', async () => {
  const p = await page();
  await p.element('#purchase-progress').querySelectorAll('button')[1].click();
  assert.match(p.element('#assistant-input').value, /找替代方案/);
  assert.equal(p.element('#assistant-form').scrolls, 1);
  assert.match(p.element('#assistant-notice').textContent, /点击“发送需求”/);
  assert.equal(p.calls.turns.length, 0);
  assert.equal(p.calls.confirms, 0);
});

test('ordinary chat purchase shows a real confirmation entry and automatically locates the quote', async () => {
  const p = await page();
  assert.equal(p.element('#purchase-entry').hidden, true);
  const voucher = { voucher_id: 3002, shop_name: '暖锅里', title: '工作日双人套餐', area: '南开区', pay_value: 13800, rules_text: [], usage: { package_items: [] } };
  const proposal = { id: 'new', expires_at: '2099-01-01T00:00:00Z', requirements: { city: '天津' }, evaluation: { voucher, verdict: 'unknown', reasons: [], unknowns: ['使用日未确认'] } };
  p.api.turn = async () => ({ conversation: { ...p.conversation, state: { ...state(), purchase_proposal: proposal } } });
  p.element('#assistant-input').value = '帮我购买';
  await p.element('#assistant-form').events.submit({ preventDefault() {} });
  assert.equal(p.element('#purchase-entry').hidden, false);
  assert.match(p.element('#purchase-entry-summary').textContent, /工作日双人套餐.*138/);
  assert.equal(p.element('#purchase-panel').scrolls, 1);
  assert.equal(p.element('#purchase-title').focused, true);
  await p.element('#show-purchase').click();
  assert.equal(p.element('#purchase-panel').scrolls, 2);
  assert.equal(p.calls.confirms, 0);
  p.api.turn = async () => ({ conversation: { ...p.conversation, state: state() } });
  p.element('#assistant-input').value = '先不买了';
  await p.element('#assistant-form').events.submit({ preventDefault() {} });
  assert.equal(p.element('#purchase-entry').hidden, true);
  assert.doesNotMatch(p.element('#assistant-notice').textContent, /再点击确认/);
});

test('restored quote exposes a jump entry, while already submitted quote never offers reconfirmation', async () => {
  const voucher = { title: '双人套餐', shop_name: '虚构商户', area: '南开区', pay_value: 13800, rules_text: [], usage: { package_items: [] } };
  const proposal = { id: 'old', expires_at: '2099-01-01T00:00:00Z', requirements: { city: '天津' }, evaluation: { voucher, verdict: 'unknown', reasons: [], unknowns: [] } };
  const restored = await page({ initialState: { ...state(), purchase_proposal: proposal }, initialPurchases: [] });
  assert.equal(restored.element('#purchase-entry').hidden, false);
  await restored.element('#show-purchase').click();
  assert.equal(restored.element('#purchase-panel').scrolls, 1);
  const submitted = await page({ initialState: { ...state(), purchase_proposal: proposal } });
  assert.equal(submitted.element('#purchase-entry').hidden, true);
  assert.equal(submitted.element('#purchase-panel').hidden, true);
});

test('account recovery opens an owned source conversation and exact old purchase without buying', async () => {
  const old = { ...failed, id: 'lost', conversation_id: 'older-chat', status: 'submitting', needs_manual_review: true, recovery_message: '尚未找到原请求，交由维护者核对' };
  const p = await page({ initialAccountItems: [old], hidden: true });
  assert.equal(p.element('#account-purchases-panel').hidden, false);
  assert.match(p.element('#account-purchase-summary').textContent, /1 条未决/);
  const open = p.element('#account-purchases').querySelectorAll('button')[0];
  assert.match(open.textContent, /回到.*原会话/);
  p.api.purchases = async (...args) => { p.calls.purchaseArguments.push(args); return [old]; };
  await open.click();
  assert.equal(p.stored.get('perkflow:assistant:1'), 'older-chat');
  assert.equal(p.stored.get('perkflow:assistant:1:purchase'), 'lost');
  assert.deepEqual(p.calls.purchaseArguments.at(-1), ['older-chat', 'lost']);
  assert.equal(p.element('#purchase-progress-title').focused, true);
  assert.equal(p.calls.confirms, 0);
  assert.equal(p.calls.turns.length, 0);
  const restored = await page({ initialSelected: 'lost', hidden: true });
  assert.deepEqual(restored.calls.purchaseArguments[0], ['one', 'lost']);
});

test('account history supports pagination and unresolved filter using only lookups', async () => {
  const p = await page();
  p.api.accountPurchases = async (number, scope) => {
    p.calls.accounts.push([number, scope]);
    return { items: [failed], total: scope === 'all' ? 7 : 1, unresolved_count: 1, page: number, page_size: 5 };
  };
  await p.element('#refresh-account-purchases').click();
  await p.element('#account-purchases-next').click();
  assert.deepEqual(p.calls.accounts.at(-1), [2, 'all']);
  assert.equal(p.element('#account-purchases-next').disabled, true);
  await p.element('#account-purchases-prev').click();
  assert.deepEqual(p.calls.accounts.at(-1), [1, 'all']);
  p.element('#account-purchase-scope').value = 'unresolved';
  await p.element('#account-purchase-scope').events.change();
  assert.deepEqual(p.calls.accounts.at(-1), [1, 'unresolved']);
  assert.equal(p.calls.confirms, 0);
  assert.equal(p.calls.turns.length, 0);
});

test('new chat preserves account history but clears exact selection, failed source lookup keeps current chat', async () => {
  const p = await page({ initialSelected: 'lost', initialAccountItems: [{ ...failed, conversation_id: 'other' }] });
  p.api.get = async () => { throw new Error('会话不存在'); };
  await p.element('#account-purchases').querySelectorAll('button')[0].click();
  assert.equal(p.stored.get('perkflow:assistant:1'), 'one');
  assert.match(p.element('#account-purchase-feedback').textContent, /打开原会话失败/);
  await p.element('#new-chat').click();
  assert.equal(p.stored.get('perkflow:assistant:1'), 'new-chat');
  assert.equal(p.stored.has('perkflow:assistant:1:purchase'), false);
  assert.equal(p.element('#account-purchases').children.length, 1);
  assert.equal(p.calls.confirms, 0);
});

test('late lookup cannot overwrite the exact purchase opened in the same conversation', async () => {
  const old = { ...failed, id: 'lost', conversation_id: 'one' };
  const p = await page({ initialAccountItems: [old] }), pending = deferred();
  let lookups = 0;
  p.api.purchases = async () => ++lookups === 1 ? pending.promise : [{ ...old, status: 'succeeded', message: '订单已创建' }];
  const first = p.element('#refresh-purchases').click();
  await p.element('#account-purchases').querySelectorAll('button')[0].click();
  pending.resolve([failed]); await first;
  const text = p.element('#purchase-progress').children[0].children.map(child => child.textContent).join(' ');
  assert.match(text, /订单已创建/);
  assert.doesNotMatch(text, /本次购买失败/);
  assert.equal(p.calls.confirms, 0);
});

test('failed account pagination keeps the old list under its original page and scope', async () => {
  const p = await page({ initialAccountItems: [failed] });
  p.api.accountPurchases = async (number, scope) => ({ items: [failed], total: 7, page: number, page_size: 5, unresolved_count: 0 });
  await p.element('#refresh-account-purchases').click();
  p.api.accountPurchases = async () => { throw new Error('查询超时'); };
  await p.element('#account-purchases-next').click();
  assert.equal(p.element('#account-purchases-page').textContent, '第 1 页');
  assert.equal(p.element('#account-purchases').children.length, 1);
  p.element('#account-purchase-scope').value = 'unresolved';
  await p.element('#account-purchase-scope').events.change();
  assert.equal(p.element('#account-purchase-scope').value, 'all');
  assert.match(p.element('#account-purchase-feedback').textContent, /核对失败.*保留上次列表/);
  assert.equal(p.calls.confirms, 0);
});
