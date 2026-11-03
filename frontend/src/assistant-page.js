import { createAuthClient } from './auth-client.js';
import { createAssistantClient } from './assistant-client.js';
import { requirementRows, preferenceRows, cardPresentation, comparisonRows, choiceMessage, purchasePresentation } from './assistant-view.js';

const auth = createAuthClient();
const api = createAssistantClient(auth);
const $ = selector => document.querySelector(selector);
let conversation = null;
let storageKey = null;
let busy = false;
let controller = null;
let purchasing = false;
let receipts = [];
let pollTimer = null;
let pollCount = 0;
let refreshing = false;
let selectedPurchaseId = null;
let purchaseRevision = 0;
let accountPage = 1;
let accountScope = 'all';
let accountLoadedScope = 'all';
let accountData = null;
let accountLoading = false;
let accountPollTimer = null;
let accountPollCount = 0;
const money = value => `¥${(value / 100).toFixed(2).replace(/\.00$/, '')}`;

function node(tag, text, className = '') {
  const element = document.createElement(tag);
  element.textContent = text; element.className = className; return element;
}
function notice(text) { $('#assistant-notice').textContent = text; }
function purchaseNotice(text) { $('#purchase-feedback').textContent = text; }
function showPurchase() {
  if ($('#purchase-panel').hidden) return;
  // 定位与焦点落在标题，不将焦点置于提交按钮，避免回车被误当作购买确认。
  $('#purchase-panel').scrollIntoView({ behavior: 'smooth', block: 'center' });
  $('#purchase-title').focus({ preventScroll: true });
}
$('#show-purchase').addEventListener('click', showPurchase);
function draft(message) {
  // 只填入输入框，用户可修改后发送；点击卡片不会隐式调用模型或购买。
  if (busy || purchasing) return;
  $('#assistant-input').value = message;
  $('#assistant-input').focus();
  $('#assistant-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
  notice('已填入需求，请检查后点击“发送需求”；尚未发起查询或购买。');
}
function setBusy(value) {
  busy = value;
  $('#send').disabled = value || purchasing || !conversation;
  $('#new-chat').disabled = value || purchasing || !auth.currentUser();
  $('#confirm-purchase').disabled = value || purchasing;
  $('#stop').hidden = !value;
  $('#assistant-input').disabled = value;
  $('#phase').textContent = value ? '正在执行本轮查询…' : '等待输入';
  for (const button of $('#purchase-progress').querySelectorAll('button')) button.disabled = value || purchasing;
  for (const button of $('#account-purchases').querySelectorAll('button')) button.disabled = value || purchasing;
}
function render(state) {
  const messages = $('#messages'); messages.replaceChildren();
  for (const item of state.messages) {
    messages.append(node('div', item.content, `assistant-bubble ${item.role}`));
  }
  if (!state.messages.length) messages.append(node('p', '说说你想吃什么、玩什么，或直接让我先找找。还没想好预算和时间也可以。', 'assistant-welcome'));
  $('#goal').textContent = state.goal || '可以先探索，再一起收敛选择。';
  const requirements = $('#requirements'); requirements.replaceChildren();
  for (const [label, value] of requirementRows(state)) requirements.append(node('dt', label), node('dd', value));
  const preferences = $('#preferences'); preferences.replaceChildren();
  for (const [label, value] of preferenceRows(state)) preferences.append(node('dt', label), node('dd', value));
  $('#preferences-empty').hidden = preferences.children.length > 0;
  const questions = $('#questions'); questions.replaceChildren();
  for (const question of state.questions || []) questions.append(node('li', question));
  $('#questions-panel').hidden = !questions.children.length;
  $('#timeline').replaceChildren();
  for (const event of state.events) progress(event);
  const cards = $('#cards'); cards.replaceChildren();
  for (const [index, result] of state.cards.entries()) {
    const voucher = result.voucher;
    const view = cardPresentation(result);
    const card = node('article', '', `panel assistant-card ${view.className}`);
    card.append(node('span', `方案${index + 1}${state.focused_id === voucher.voucher_id ? ' · 正在了解' : ''}`, 'assistant-choice-number'), node('span', view.label, 'assistant-verdict'),
      node('h3', voucher.title), node('p', `${voucher.shop_name} · ${voucher.area}`),
      node('strong', `${money(voucher.pay_value)} / 张`, 'assistant-price'),
      node('p', result.total_cost_fen == null ? '消费总金额尚不能确定' : `列明套餐金额：${money(result.total_cost_fen)}，不含额外消费`));
    const list = document.createElement('ul');
    for (const text of view.notes) list.append(node('li', text));
    if (!list.children.length) list.append(node('li', '所选日期、人数、时段和预算已通过规则校验'));
    const items = voucher.usage.package_items;
    if (voucher.score != null) card.append(node('p', `店铺评分：${voucher.score}`));
    if (items.length) card.append(node('p', `套餐内容：${items.join('、')}`));
    const saleLabel = { active: '活动进行中', upcoming: '活动未开始', ended: '活动已结束', sold_out: '已售罄', off_shelf: '已下架', unavailable: '暂不可购买' }[voucher.sale_state] || '状态待确认';
    card.append(list, node('p', voucher.usage.note, 'assistant-footnote'),
      node('p', `查询时剩余 ${voucher.stock_snapshot} 份 · ${saleLabel}`, 'assistant-footnote'),
      node('p', `查询时间：${new Date(voucher.checked_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}（北京时间）`, 'assistant-footnote'));
    const link = node('a', '查看商户与优惠券', 'button outline');
    link.href = `shop-detail.html?id=${encodeURIComponent(voucher.shop_id)}`;
    const inspect = node('button', '继续了解这个方案', 'button outline');
    inspect.type = 'button';
    inspect.addEventListener('click', () => draft(choiceMessage(index + 1, voucher.title)));
    card.append(inspect, link); cards.append(card);
  }
  // 用确定性字段形成横向对比；冲突方案保留原因，不能用绿色推荐卡片掩盖。
  const comparisons = comparisonRows(state.comparisons || []);
  $('#comparison-panel').hidden = comparisons.length < 2;
  const table = $('#comparison-table'); table.replaceChildren();
  if (comparisons.length >= 2) {
    const head = document.createElement('thead'), header = document.createElement('tr');
    header.append(node('th', '比较项目'));
    for (const column of comparisons) header.append(node('th', column.title));
    head.append(header); table.append(head);
    const body = document.createElement('tbody');
    for (const [row, [label]] of comparisons[0].values.entries()) {
      const tr = document.createElement('tr'), th = node('th', label); th.scope = 'row'; tr.append(th);
      for (const column of comparisons) tr.append(node('td', column.values[row][1]));
      body.append(tr);
    }
    table.append(body);
  }
  const comparable = state.cards.slice(0, 3).filter(result => result.verdict !== 'ineligible');
  $('#compare-choices').hidden = comparable.length < 2;
  $('#compare-choices').onclick = () => {
    const positions = comparable.map(result => state.cards.indexOf(result) + 1);
    draft(`请比较上一轮方案${positions.join('、')}，哪个更适合当前需求？请说明价格和使用限制的取舍。`);
  };
  renderPurchase();
}

function renderPurchase() {
  const proposal = conversation?.state.purchase_proposal;
  const already = proposal && receipts.some(row => row.id === proposal.id);
  $('#purchase-panel').hidden = !proposal || already;
  // 入口只由真实服务端报价控制，不根据助手文字猜测是否存在确认卡。
  $('#purchase-entry').hidden = !proposal || already;
  $('#purchase-entry-summary').textContent = proposal && !already
    ? `待你确认：${proposal.evaluation.voucher.title} · 1张 · ${money(proposal.evaluation.voucher.pay_value)}` : '';
  const summary = $('#purchase-summary'); summary.replaceChildren();
  if (proposal && !already) {
    const evaluation = proposal.evaluation, voucher = evaluation.voucher;
    summary.append(node('h3', `${voucher.shop_name} · ${voucher.title}`),
      node('strong', `1张 · 合计${money(voucher.pay_value)}`, 'assistant-price'),
      node('p', `区域：${voucher.area}；${voucher.usage.note}`));
    if (voucher.usage.package_items.length) summary.append(node('p', `内容：${voucher.usage.package_items.join('、')}`));
    const details = node('dl', '', 'assistant-requirements');
    for (const [label, value] of comparisonRows([evaluation])[0].values.filter(([label]) =>
      ['列明套餐金额', '适用人数', '可用时间', '预约'].includes(label))) {
      details.append(node('dt', label), node('dd', value));
    }
    summary.append(details);
    const known = requirementRows({requirements: proposal.requirements});
    summary.append(node('p', `本次消费条件：${known.map(([label, value]) => `${label}：${value}`).join('；')}`));
    summary.append(node('p', `券的使用规则：${voucher.rules_text.join('；') || '以列明资料为准'}`));
    const list = node('ul', '');
    for (const unknown of evaluation.unknowns) list.append(node('li', unknown));
    summary.append(list, node('p', `确认截止：${new Date(proposal.expires_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}（北京时间）`));
    $('#unknown-consent-row').hidden = !evaluation.unknowns.length;
    $('#confirm-purchase').textContent = `确认创建订单 · ${money(voucher.pay_value)}`;
  }
  $('#purchase-progress-panel').hidden = !receipts.length;
  const progress = $('#purchase-progress'); progress.replaceChildren();
  for (const receipt of receipts) {
    const view = purchasePresentation(receipt), article = node('article', '');
    article.append(node('h3', view.label), node('p', receipt.proposal.evaluation.voucher.title),
      node('p', view.message));
    if (receipt.request) article.append(node('p', `原请求编号：${receipt.request.id}`));
    if (receipt.request?.order) article.append(node('p', `订单编号：${receipt.request.order.id} · ${money(receipt.request.order.pay_value)}`));
    if (receipt.request?.error_message && receipt.request.error_message !== view.message) article.append(node('p', receipt.request.error_message));
    if (!receipt.lookup_available) article.append(node('p', '本次查询暂不可用，以上为上次保存状态；不会重新提交。'));
    if (receipt.recovery_message) article.append(node('p', receipt.recovery_message));
    if (receipt.needs_manual_review) article.append(node('p', `供维护者核对的授权编号：${receipt.id}；来源会话：${receipt.conversation_id}；原请求：${receipt.request_id || '尚未找到'}。不要清除记录或重新购买。`, 'assistant-footnote'));
    const orders = node('a', '查看我的订单', 'button outline'); orders.href = 'orders.html'; article.append(orders);
    if (view.canReplan) {
      const retry = node('button', '重新核对并生成确认', 'button outline'); retry.type = 'button';
      retry.disabled = busy || purchasing;
      // 明确失败后，点击只执行一轮重新核对；新报价仍由用户单独确认，不重放旧授权。
      retry.addEventListener('click', () => sendMessage(`我仍想买「${receipt.proposal.evaluation.voucher.title}」，请重新核对并生成新的确认内容，先不要提交购买。`, true));
      article.append(retry);
      const alternative = node('button', '找替代方案，先不购买', 'button outline'); alternative.type = 'button';
      alternative.disabled = busy || purchasing;
      alternative.addEventListener('click', () => draft('本次购买已经明确失败，请按原需求找替代方案，先展示给我，不要直接购买。'));
      article.append(alternative);
    }
    progress.append(article);
  }
}

async function loadPurchases(reset = false) {
  clearTimeout(pollTimer);
  if (reset) pollCount = 0;
  if (!conversation) return;
  const id = conversation.id;
  const revision = ++purchaseRevision;
  const items = await api.purchases(id, selectedPurchaseId || undefined);
  if (conversation?.id !== id || revision !== purchaseRevision) return;
  receipts = items; renderPurchase();
  // 只轮询GET，最多10次；不会因超时重发确认或重新购买，用户仍可手动回查。
  if (receipts.some(row => purchasePresentation(row).waiting) && pollCount++ < 10 && !document.hidden) {
    pollTimer = setTimeout(() => loadPurchases().catch(error => notice(error.message)), 2000);
  }
}

function renderAccountPurchases() {
  const list = $('#account-purchases'); list.replaceChildren();
  if (accountData) {
    $('#account-purchase-summary').textContent = `本次范围共 ${accountData.total} 条；账号当前仍有 ${accountData.unresolved_count} 条未决购买。`;
    for (const receipt of accountData.items) {
      const view = purchasePresentation(receipt), article = node('article', '');
      article.append(node('h3', `${receipt.proposal.evaluation.voucher.title} · ${view.label}`), node('p', view.message));
      if (receipt.created_at) article.append(node('p', `确认时间：${new Date(receipt.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}（北京时间）`));
      if (receipt.request?.order) article.append(node('p', `订单编号：${receipt.request.order.id}`));
      if (receipt.recovery_message) article.append(node('p', receipt.recovery_message));
      article.append(node('p', `授权编号：${receipt.id}；原请求：${receipt.request_id || receipt.request?.id || '尚未找到'}。`, 'assistant-footnote'));
      if (receipt.needs_manual_review) article.append(node('p', `结果仍不确定，请交由维护者核对原编号；来源会话：${receipt.conversation_id}。不会自动重新购买。`));
      const open = node('button', receipt.conversation_id === conversation?.id ? '定位本会话的这笔购买' : '回到这笔购买的原会话', 'button outline');
      open.type = 'button'; open.disabled = busy || purchasing;
      open.addEventListener('click', () => openPurchase(receipt));
      article.append(open); list.append(article);
    }
    if (!accountData.items.length) list.append(node('p', accountScope === 'unresolved' ? '当前没有未决购买，可切到“全部记录”查看历史结果。' : '这个账号还没有助手购买记录。'));
    $('#account-purchases-page').textContent = `第 ${accountPage} 页`;
  }
  $('#refresh-account-purchases').disabled = accountLoading;
  $('#account-purchase-scope').disabled = accountLoading;
  $('#account-purchases-prev').disabled = accountLoading || accountPage <= 1;
  $('#account-purchases-next').disabled = accountLoading || !accountData || accountPage * accountData.page_size >= accountData.total;
}

async function loadAccountPurchases(reset = false) {
  if (accountLoading || !auth.currentUser()) return;
  clearTimeout(accountPollTimer);
  if (reset) accountPollCount = 0;
  accountLoading = true; renderAccountPurchases();
  $('#account-purchase-feedback').textContent = '正在核对这个账号的购买记录…';
  try {
    accountData = await api.accountPurchases(accountPage, accountScope);
    accountLoadedScope = accountScope;
    const unavailable = accountData.items.some(row => !row.lookup_available);
    $('#account-purchase-feedback').textContent = unavailable
      ? '部分记录暂无法核对，保留已保存状态，请稍后刷新；未重新提交购买。'
      : '购买记录已更新。打开原会话只恢复查看，不会重发购买。';
    // 账号级轮询独立于当前聊天；离开原会话也可看到处理结果，仍限定10次且只GET。
    if (accountData.items.some(row => purchasePresentation(row).waiting) && accountPollCount++ < 10 && !document.hidden) {
      accountPollTimer = setTimeout(() => loadAccountPurchases(), 2000);
    }
  } catch (error) {
    // 翻页/切换范围失败时恢复已显示列表的页码和范围，不能把旧数据标成新页。
    accountPage = accountData?.page || 1; accountScope = accountLoadedScope;
    $('#account-purchase-scope').value = accountScope;
    $('#account-purchase-feedback').textContent = `购买记录核对失败：${error.message || '连接失败'}。保留上次列表，未重新提交购买。`;
  } finally { accountLoading = false; renderAccountPurchases(); }
}

async function openPurchase(receipt) {
  if (busy || purchasing) return;
  setBusy(true);
  try {
    // 先让服务端验证来源会话归属，再切换；跨账号或已失效的ID不会替换当前会话。
    const saved = await api.get(receipt.conversation_id);
    clearTimeout(pollTimer); pollCount = 0; receipts = [];
    conversation = saved; selectedPurchaseId = receipt.id;
    sessionStorage.setItem(storageKey, conversation.id);
    sessionStorage.setItem(storageKey + ':purchase', receipt.id);
    $('#unknown-consent').checked = false; $('#assistant-input').value = '';
    render(conversation.state);
    await loadPurchases(true);
    purchaseNotice('已打开原会话并核对所选购买，没有重新提交。');
    $('#purchase-progress-panel').scrollIntoView({ behavior: 'smooth', block: 'center' });
    $('#purchase-progress-title').focus({ preventScroll: true });
    notice('已回到这笔购买的原会话，可继续追问进度；不会自动重新购买。');
  } catch (error) {
    $('#account-purchase-feedback').textContent = `打开原会话失败：${error.message || '连接失败'}。没有重新购买。`;
  } finally { setBusy(false); renderAccountPurchases(); }
}

$('#refresh-account-purchases').addEventListener('click', () => loadAccountPurchases(true));
$('#account-purchase-scope').addEventListener('change', () => {
  if (accountLoading) return;
  accountScope = $('#account-purchase-scope').value; accountPage = 1;
  return loadAccountPurchases(true);
});
$('#account-purchases-prev').addEventListener('click', () => {
  if (accountLoading || accountPage <= 1) return;
  accountPage--; return loadAccountPurchases(true);
});
$('#account-purchases-next').addEventListener('click', () => {
  if (accountLoading || !accountData || accountPage * accountData.page_size >= accountData.total) return;
  accountPage++; return loadAccountPurchases(true);
});

$('#confirm-purchase').addEventListener('click', async () => {
  const proposal = conversation?.state.purchase_proposal;
  if (!proposal || purchasing || busy) return;
  if (proposal.evaluation.unknowns.length && !$('#unknown-consent').checked) {
    notice('请先阅读并勾选知悉尚未确认条件，或继续询问助手。'); return;
  }
  purchasing = true; setBusy(false);
  try {
    const result = await api.confirm(conversation, proposal.id, $('#unknown-consent').checked);
    receipts = [result, ...receipts.filter(row => row.id !== result.id)]; renderPurchase();
    notice(result.message);
    await loadPurchases(true);
    await loadAccountPurchases(true);
  } catch (error) {
    notice(`${error.message || '确认响应未收到'}。请回查原购买结果，不要重新选择另一张券购买。`);
    await loadPurchases(true).catch(() => {});
    await loadAccountPurchases(true);
  } finally { purchasing = false; setBusy(false); }
});
$('#refresh-purchases').addEventListener('click', async () => {
  if (refreshing || !conversation) return;
  // 手动回查有独立反馈和互斥；状态未变也明确告知已查完，绝不重新POST购买。
  const id = conversation.id, button = $('#refresh-purchases');
  refreshing = true; button.disabled = true; button.textContent = '正在回查…';
  purchaseNotice('正在回查原购买记录…');
  try {
    await loadPurchases(true);
    if (conversation?.id !== id) return;
    const labels = receipts.map(row => purchasePresentation(row).label);
    purchaseNotice(receipts.some(row => !row.lookup_available)
      ? '回查暂不可用，当前显示上次保存的状态；未重新提交购买。'
      : `回查完成（北京时间 ${new Date().toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai' })}）：${labels.join('；') || '本会话暂无购买记录'}。${receipts.some(row => row.status === 'failed') ? '失败记录保留历史；如仍想购买，请点击“重新核对并生成确认”。' : '未重新提交购买。'}`);
  } catch (error) {
    if (conversation?.id === id) purchaseNotice(`回查失败：${error.message || '连接失败'}。当前保留上次状态，未重新提交购买。`);
  } finally {
    refreshing = false; button.disabled = false; button.textContent = '回查原购买结果';
  }
});
window.addEventListener('pagehide', () => { clearTimeout(pollTimer); clearTimeout(accountPollTimer); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(pollTimer); clearTimeout(accountPollTimer); }
});
function progress(event) {
  if (event.kind === 'answer') return;
  const item = node('li', event.text, `event-${event.kind}`);
  if (event.tool) item.dataset.tool = event.tool;
  $('#timeline').append(item);
  if (busy) $('#phase').textContent = event.text;
}
async function newChat() {
  setBusy(true);
  try {
    clearTimeout(pollTimer); receipts = []; pollCount = 0; purchaseNotice('');
    conversation = await api.create();
    selectedPurchaseId = null; sessionStorage.removeItem(storageKey + ':purchase');
    sessionStorage.setItem(storageKey, conversation.id);
    render(conversation.state);
    await loadPurchases();
    await loadAccountPurchases(true);
  } finally { setBusy(false); }
}

async function sendMessage(message, reviewPurchase = false) {
  const input = $('#assistant-input');
  if (!message || busy || purchasing || !conversation) return;
  controller = new AbortController(); setBusy(true);
  if (reviewPurchase) {
    purchaseNotice('正在重新核对商品、价格和规则，完成后展示新的确认内容；尚未提交购买。');
    $('#assistant-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  $('#timeline').replaceChildren();
  // 等待本轮期间保留上轮编号，便于理解指代；完成后由服务端新状态整体替换。
  $('#messages').append(node('div', message, 'assistant-bubble user'));
  try {
    const result = await api.turn(conversation, message, progress, controller.signal);
    conversation = result.conversation; $('#unknown-consent').checked = false; render(conversation.state); input.value = '';
    await loadPurchases(true).catch(error => purchaseNotice(`购买记录回查失败：${error.message}。本轮对话已保存。`));
    const proposal = conversation.state.purchase_proposal;
    const hasConfirmation = proposal && !receipts.some(row => row.id === proposal.id);
    notice(hasConfirmation
      ? '已生成购买确认卡，可通过对话旁“查看购买确认”定位；核对商品、金额和未知项后再确认。'
      : '本轮已完成，可以继续了解或比较方案。');
    if (hasConfirmation) showPurchase();
    if (reviewPurchase) {
      purchaseNotice(hasConfirmation
        ? '重新核对完成，已生成新的确认内容；请核对后点击“确认创建订单”。旧失败记录保留，尚未重新购买。'
        : '重新核对完成，本轮未生成新的确认内容，请查看助手回复和待确认问题；尚未重新购买。');
      if (!hasConfirmation) $('#assistant-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  } catch (error) {
    notice(error.name === 'AbortError' ? '本轮对话已停止，将回查已保存会话；不会自动提交新的购买。' : error.message || '连接失败，请稍后重试');
    // 连接回执可能丢失：先读取服务端版本，避免旧版本继续覆盖；不自动重发模型请求。
    try { conversation = await api.get(conversation.id); render(conversation.state); }
    catch { conversation = null; }
    if (reviewPurchase) purchaseNotice(`重新核对未完成：${error.message || '连接失败'}。未自动提交购买，请查看已恢复的会话后再操作。`);
  } finally { setBusy(false); controller = null; }
}
$('#assistant-form').addEventListener('submit', async event => {
  event.preventDefault();
  await sendMessage($('#assistant-input').value.trim());
});
$('#new-chat').addEventListener('click', () => {
  if (!busy) return newChat().then(() => notice('已创建新会话，可以描述这次消费需求。')).catch(error => notice(error.message));
});
$('#stop').addEventListener('click', () => controller?.abort());

async function initialize() {
  await auth.initialize();
  if (!auth.currentUser()) {
    notice('登录后可使用助手，消费需求与会话按账号隔离。');
    const link = node('a', '去登录', 'button primary');
    link.href = 'login.html?next=assistant.html'; $('#messages').append(link); setBusy(false); return;
  }
  storageKey = `perkflow:assistant:${auth.currentUser().id}`;
  $('#account-purchases-panel').hidden = false;
  const status = await api.status();
  if (!status.configured) {
    notice('模型尚未配置：请填写 backend/.env 中的 LLM_BASE_URL、LLM_MODEL、LLM_API_KEY，重启后端后刷新。');
  } else notice('可查询、比较并准备购买；模型不能确认，只有你点击购买确认才会提交。');
  const saved = sessionStorage.getItem(storageKey);
  selectedPurchaseId = sessionStorage.getItem(storageKey + ':purchase');
  if (saved) {
    try { conversation = await api.get(saved); }
    catch (error) { if (error.status !== 404) throw error; }
  }
  if (!conversation) await newChat();
  else { render(conversation.state); setBusy(false); await loadPurchases(true); await loadAccountPurchases(true); }
}
initialize().catch(error => { notice(error.message || '助手初始化失败，请检查后端是否启动并已完成迁移'); setBusy(false); });
