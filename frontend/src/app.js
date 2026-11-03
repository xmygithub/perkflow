import { stories, photo, city, areas } from './data.js';
import { createShopClient, shopQuery, assetPath } from './shop-client.js';
import { createDemoStore, validPhone, safeReturn } from './demo-store.js';
import { createAuthClient } from './auth-client.js';
import { createVoucherClient } from './voucher-client.js';
import { voucherAvailability, voucherActivityWindow } from './voucher-activity.js';

const page = document.body.dataset.page;
const params = new URLSearchParams(location.search);
const main = document.querySelector('#main');
const auth = createAuthClient();
const shopApi = createShopClient();
const merchantPage = ['home', 'shops', 'detail'].includes(page);
const businessPage = merchantPage || ['vouchers', 'orders'].includes(page);
const voucherApi = createVoucherClient(auth);
// 业务页面只展示真实 API 数据；收藏 ID 仍保存在本浏览器。
let shops = [];
let vouchers = [];
let orderResult = { items: [], total: 0, page: 1, page_size: 20 };
let requestResult = { items: [], total: 0, page: 1, page_size: 20 };
let orderPollTimer = null;
let orderPollUntil = 0;
let orderRevision = 0;
let voucherResult = { items: [], total: 0, page: 1, page_size: 20 };
let orderLoading = false;
let orderError = null;
let authError = null;
let categories = [];
let shopResult = { items: [], total: 0, page: 1, page_size: 4 };
let dataState = businessPage ? 'loading' : 'ready';
let dataError = null;
let dataRevision = 0;
const store = createDemoStore(localStorage, sessionStorage, () => auth.currentUser());
const dialog = document.querySelector('#coupon-dialog');
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const money = cents => (cents / 100).toFixed(2).replace(/\.00$/, '');
const shopUrl = id => `shop-detail.html?id=${id}`;
const image = (n, alt, className = '', eager = false) => `<img src="${escape(assetPath(typeof n === 'number' ? photo(n) : n))}" alt="${escape(alt)}" class="${className}" loading="${eager ? 'eager' : 'lazy'}" decoding="async">`;
const icons = {
  pin: '<path d="M18 9c0 5-6 10-6 10S6 14 6 9a6 6 0 1 1 12 0Z"/><circle cx="12" cy="9" r="2"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
  heart: '<path d="M20 5a5 5 0 0 0-8 1 5 5 0 0 0-8-1c-4 4 1 10 8 15 7-5 12-11 8-15Z"/>',
  ticket: '<path d="M3 7h18v4a2 2 0 0 0 0 4v3H3v-3a2 2 0 0 0 0-4V7Z"/><path d="M15 7v2m0 3v2m0 2v2"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  logout: '<path d="M9 4H4v16h5m4-13 5 5-5 5m-5-5h10"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.arrow}</svg>`;
let toastTimer;
let codeTimer;
let action;
let submittingOrder = false;

function toast(message) {
  const el = document.querySelector('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
}
function loginUrl() {
  const destination = page === 'login' ? safeReturn(params.get('next')) : (location.pathname.split('/').pop() || 'index.html') + location.search;
  return `login.html?next=${encodeURIComponent(destination)}`;
}
function header() {
  const user = store.currentUser();
  const active = name => (page === name || (name === 'shops' && page === 'detail')) ? 'class="active" aria-current="page"' : '';
  document.querySelector('#header').innerHTML = `<div class="header-inner">
    <a class="brand" href="index.html" aria-label="PerkFlow 首页"><span class="brand-symbol">P<span></span></span><span>PerkFlow<span class="brand-sub">你的城市出门清单</span></span></a>
    <span class="city">${icon('pin')}天津</span>
    <nav class="main-nav" aria-label="主导航"><a href="index.html" ${active('home')}>发现</a><a href="shop-list.html" ${active('shops')}>好店</a><a href="vouchers.html" ${active('vouchers')}>优惠券</a><a href="assistant.html">选购助手</a><a href="orders.html" ${active('orders')}>我的订单</a></nav>
    <form class="header-search search-form" role="search">${icon('search')}<input name="q" aria-label="搜索商户" placeholder="输入店名，发现好店" maxlength="50" value="${escape(params.get('q') || '')}"><button aria-label="搜索" type="submit">${icon('arrow')}</button></form>
    <a class="saved-link" href="shop-list.html?saved=1" aria-label="查看收藏的好店">${icon('heart')}</a>
    ${user ? `<a class="account" href="orders.html" aria-label="查看${escape(user.name)}的订单">${icon('user')}<span>${escape(user.name)}</span></a><button class="icon-button" data-action="logout" aria-label="退出登录">${icon('logout')}</button>` : `<a class="login-link" href="${loginUrl()}">${icon('user')}<span>登录 / 注册</span></a>`}
  </div>`;
}
function footer() {
  document.querySelector('#footer').innerHTML = `<div class="footer-inner"><a class="footer-brand" href="index.html">PerkFlow<span>逛街有方向，吃饭有着落。</span></a><div class="footer-links"><a href="shop-list.html">发现好店</a><span>·</span><a href="vouchers.html">超值优惠</a><span>·</span><a href="orders.html">我的订单</a></div><span class="footer-city">TIANJIN · LOCAL LIFE NOTES</span></div><div class="demo-note"><span class="demo-dot"></span>登录、商户、优惠券与订单已接入后端 · 数据为虚构种子，下单会扣减开发库库存，不涉及真实支付。</div>`;
}
function empty(title, description, link = 'shop-list.html', label = '发现更多好店') {
  return `<div class="empty-state"><span class="empty-icon">${icon('search')}</span><h2>${escape(title)}</h2><p>${escape(description)}</p><a href="${link}" class="button outline">${label}${icon('arrow')}</a></div>`;
}
function saveButton(shop, small = false) {
  const saved = store.saved().includes(shop.id);
  return `<button class="${small ? 'save-cover' : 'button outline'} ${saved ? 'is-saved' : ''}" data-action="save" data-id="${shop.id}" aria-label="${saved ? '取消收藏' : '收藏'}${escape(shop.name)}" aria-pressed="${saved}">${icon('heart')}${small ? '' : `<span>${saved ? '已收藏' : '收藏好店'}</span>`}</button>`;
}
function shopCard(shop) {
  return `<article class="shop-card"><div class="shop-cover"><a href="${shopUrl(shop.id)}">${image(shop.images[0], shop.name)}</a>${saveButton(shop, true)}<span class="photo-label">${escape(shop.kind)}</span></div><div class="shop-body"><div class="shop-title-row"><a href="${shopUrl(shop.id)}"><h3>${escape(shop.name)}</h3></a><span class="score">★ ${shop.score.toFixed(1)}</span></div><p class="shop-caption">${escape(shop.subtitle)}</p><div class="shop-meta"><span>${icon('pin')}${escape(shop.area)}</span><span>¥${shop.averagePrice} / 人</span></div><div class="shop-tags">${shop.tags.map(tag => `<span>${escape(tag)}</span>`).join('')}${shop.offer ? `<span class="offer-tag">${icon('ticket')}有优惠</span>` : ''}</div></div></article>`;
}
function storyCard(story) {
  const shop = shops.find(x => x.id === story.shopId);
  if (!shop) return '';
  return `<article class="story-card"><a class="story-cover" href="${shopUrl(story.shopId)}">${image(story.image, story.title)}<span class="photo-label">${story.label}</span></a><div class="story-body"><a href="${shopUrl(story.shopId)}"><h3>${story.title}</h3></a><p>${story.note}</p><a class="story-place" href="${shopUrl(story.shopId)}">${icon('pin')}${escape(shop.name)}<span>${icon('arrow')}</span></a></div></article>`;
}
function home() {
  const featured = shops[0];
  const featuredLink = featured ? shopUrl(featured.id) : 'shop-list.html';
  main.innerHTML = `<section class="hero city-cover" aria-labelledby="hero-title">
    <div class="hero-copy"><div class="cover-meta"><span>${city.english} / CITY NOTES</span><span>城市清单 · 第 01 期</span></div><span class="eyebrow">${icon('pin')}${city.name}，今天去哪儿？</span><h1 id="hero-title">在津城，<br>找到你的<span>好去处。</span></h1><p>沿海河走走，在街巷里吃顿饭。<br>把想去的地方，变成今天的安排。</p><div class="cover-actions"><a class="button primary" href="shop-list.html">打开城市清单${icon('arrow')}</a><a class="cover-secondary" href="vouchers.html">先看看优惠${icon('chevron')}</a></div><div class="cover-foot"><svg viewBox="0 0 200 36" fill="none" aria-hidden="true"><path d="M0 29h200M25 27V15h18v12M35 15V8h4m24 19V6h22v21m-17-15h12m-12 7h12m34 7c8-27 34-27 45 0m-45 0h45m-39-11v12m8-21v21m8-25v25m8-19v19m7-10v10m16 0V12h16v15" stroke="currentColor" stroke-width="1.2"/><path d="M0 34c20-5 30 4 50 0s30 4 50 0 30 4 50 0 30 4 50 0" stroke="currentColor"/></svg><span>走街串巷 / 吃点好的 / 不赶时间</span></div></div>
    <div class="hero-visual">${image(14, '庭院茶室的空间示意照片', 'hero-photo', true)}<span class="cover-label">WEEKEND PICK <span>周末歇脚处</span></span><a class="cover-caption" href="${featuredLink}"><span><small>${escape(featured ? featured.area + ' · ' + featured.kind : '天津城市清单')}</small><strong>${escape(featured?.name || '发现更多好店')}</strong></span><span class="caption-arrow">${icon('arrow')}</span></a><span class="city-seal" aria-hidden="true">津<small>CITY WALK</small></span></div>
  </section>
  <nav class="category-strip category-pills" aria-label="商户分类">${categories.map(cat => `<a href="shop-list.html?type=${cat.id}"><span class="category-image"><img src="${escape(cat.icon)}" alt="" width="38" height="38"></span><span>${escape(cat.name)}</span></a>`).join('')}</nav>
  <section class="scene-grid" aria-label="按出门场景发现商户"><a href="shop-list.html?area=${encodeURIComponent('五大道')}"><span class="scene-number">01</span><div><h2>散步之后，吃点好的</h2><p>五大道 · 西餐与朋友小聚</p></div>${icon('arrow')}</a><a href="shop-list.html?kind=tea"><span class="scene-number">02</span><div><h2>找张桌子，坐一下午</h2><p>茶馆清单 · 给周末留点余地</p></div>${icon('arrow')}</a><a href="vouchers.html?type=seckill"><span class="scene-number">03</span><div><h2>出门之前，备点优惠</h2><p>限量体验 · 先看规则再安排</p></div>${icon('arrow')}</a></section>
  <section class="city-discover" aria-labelledby="discover-title"><div class="section-heading"><div><span class="section-eyebrow">THE LOCAL LIST / 天津城市清单</span><h2 id="discover-title">这周，去点新地方<span class="heading-dot"></span></h2></div><div class="tabs" role="group" aria-label="发现内容分类"><button class="active" aria-pressed="true" data-action="story-tab" data-group="all">全部清单</button><button aria-pressed="false" data-action="story-tab" data-group="food">饭局灵感</button><button aria-pressed="false" data-action="story-tab" data-group="relax">喝茶歇脚</button></div></div><div class="story-grid" id="story-grid">${stories.map(storyCard).join('')}</div><div class="more-row"><a class="button outline rounded" href="shop-list.html">翻翻更多好店${icon('arrow')}</a></div></section>
  <section class="local-highlights"><div class="panel"><div class="panel-heading"><div><span class="section-eyebrow">PERKFLOW PICKS</span><h2>先收藏，下次就去</h2></div><a href="shop-list.html?sort=score">查看好店${icon('chevron')}</a></div><div class="highlight-shops">${shops.slice(0, 3).map((shop, i) => `<a class="mini-shop" href="${shopUrl(shop.id)}"><div class="mini-cover">${image(shop.images[0], shop.name)}<span>0${i + 1}</span></div><div><h3>${escape(shop.name)}</h3><span class="mini-score">★ <strong>${shop.score}</strong><small>店铺评分</small></span><p>${escape(shop.kind)} · ${escape(shop.area)}</p></div></a>`).join('')}</div></div><div class="deal-panel"><span class="section-eyebrow">A LITTLE PERK</span><div class="panel-heading"><h2>${icon('ticket')}给散步加个喝茶计划</h2><span class="deal-badge">限量示例</span></div><h3>双人下午茶体验券</h3><p>海河茶集 · 逛完河边，再坐一会儿</p><div class="deal-bottom"><span class="price">¥<strong>59</strong><del>¥88</del></span><a class="button primary small" href="vouchers.html?type=seckill">查看优惠${icon('arrow')}</a></div></div></section>
  <section class="weekend-banner"><div><span class="eyebrow">YOUR CITY, YOUR PACE</span><h2>不必远行，津城就有下一站。</h2><p>换条街走走，也许就找到新的常去之处。</p></div><a class="button outline" href="shop-list.html?area=${encodeURIComponent('海河沿线')}">逛逛海河沿线${icon('arrow')}</a></section>`;
}
function catalog() {
  const list = shopResult.items;
  const type = Number(params.get('type'));
  const currentType = categories.find(x => x.id === type);
  const q = params.get('q');
  const title = params.has('saved') ? '喜欢的好店，留给下一次。' : q ? `关于“${q}”的好店` : currentType ? `${currentType.name}，总有新发现。` : params.get('kind') === 'tea' ? '不赶时间，找个地方喝茶。' : '天津好店，按你的心意挑。';
  const totalPages = Math.max(1, Math.ceil(shopResult.total / shopResult.page_size));
  const currentPage = shopResult.page;
  const href = n => { const next = new URLSearchParams(params); next.set('page', n); return `shop-list.html?${next}`; };
  main.innerHTML = `<div class="page-intro"><div><span class="eyebrow">TIANJIN · 街区好店</span><h1>${escape(title)}</h1><p>选个街区，看看人均，再决定今天的安排。</p></div><span class="result-count">找到 <strong>${shopResult.total}</strong> 家店铺</span></div>
  <section class="filter-panel" aria-label="筛选商户"><div class="filter-row"><span class="filter-label">分类</span><div class="filter-options"><button data-filter="type" data-value="" class="chip ${!type ? 'active' : ''}" aria-pressed="${!type}">全部</button>${categories.map(cat => `<button data-filter="type" data-value="${cat.id}" class="chip ${type === cat.id ? 'active' : ''}" aria-pressed="${type === cat.id}">${escape(cat.name)}</button>`).join('')}</div></div><div class="filter-row"><span class="filter-label">区域</span><div class="filter-options">${['', ...areas].map(area => `<button data-filter="area" data-value="${area}" class="chip ${(params.get('area') || '') === area ? 'active' : ''}" aria-pressed="${(params.get('area') || '') === area}">${area || '全部区域'}</button>`).join('')}</div></div></section>
  <div class="catalog-toolbar"><div><span class="muted">${icon('pin')}天津</span>${params.has('saved') || params.has('kind') || params.has('area') || q ? '<a href="shop-list.html" class="text-link">清除筛选</a>' : ''}</div><div class="sort-controls"><label for="sort-select" class="sr-only">商户排序</label><select id="sort-select"><option value="default" ${!params.get('sort') ? 'selected' : ''}>综合推荐</option><option value="score" ${params.get('sort') === 'score' ? 'selected' : ''}>评分优先</option><option value="price" ${params.get('sort') === 'price' ? 'selected' : ''}>人均从低到高</option></select></div></div>
  ${list.length ? `<div class="catalog-grid">${list.map(shopCard).join('')}</div><nav class="pagination" aria-label="店铺分页">${currentPage > 1 ? `<a class="button outline small" href="${href(currentPage - 1)}">上一页</a>` : ''}<span>第 ${currentPage} / ${totalPages} 页</span>${currentPage < totalPages ? `<a class="button outline small" href="${href(currentPage + 1)}">下一页${icon('arrow')}</a>` : ''}</nav>` : empty(params.has('saved') ? '还没有收藏的好店' : '暂时没有匹配的好店', '可以试试其他店名、区域或分类，也可以清除筛选。', 'shop-list.html', '查看全部好店')}`;
}
function voucherCard(voucher, compact = false) {
  const claimed = orderResult.items.some(x => x.voucherId === voucher.id && x.status === 'created');
  const pending = requestResult.items.some(x => x.voucherId === voucher.id && x.status === 'pending');
  const availability = voucherAvailability(voucher);
  const disabled = claimed || pending || !availability.purchasable;
  const label = claimed ? '已创建订单' : pending ? '购买处理中' : availability.label;
  return `<article class="voucher-card ${compact ? 'compact' : ''}">${compact ? '' : `<a class="voucher-cover" href="${shopUrl(voucher.shopId)}">${image(voucher.shopImage, voucher.shopName)}<span class="photo-label">${voucher.type === 'seckill' ? '限量优惠' : '店铺代金券'}</span></a>`}<div class="voucher-body"><a class="voucher-shop" href="${shopUrl(voucher.shopId)}">${icon('pin')}${escape(voucher.shopName)}${icon('chevron')}</a><h3>${escape(voucher.title)}</h3><p>${escape(voucher.subtitle)}</p><div class="voucher-stats"><span>${voucher.type === 'seckill' ? '限量优惠' : '普通券'} · 剩余 ${voucher.stock} 份</span><button class="text-link" data-action="rules" data-id="${voucher.id}">使用规则</button></div><p class="voucher-window">${escape(voucherActivityWindow(voucher))}<br>订单创建后 ${voucher.validity_days} 天有效</p><div class="voucher-bottom"><span class="price">¥<strong>${money(voucher.payValue)}</strong><del>¥${money(voucher.actualValue)}</del></span><button class="button ${disabled ? 'soft' : 'primary'} small" data-action="claim" data-id="${voucher.id}" ${disabled ? 'disabled' : ''}>${label}</button></div></div></article>`;
}
function pagination(result, file, pageKey = 'page') {
  const pages = Math.max(1, Math.ceil(result.total / result.page_size));
  const href = n => { const next = new URLSearchParams(params); next.set(pageKey, n); return `${file}?${next}`; };
  return `<nav class="pagination" aria-label="分页">${result.page > 1 ? `<a class="button outline small" href="${href(result.page - 1)}">上一页</a>` : ''}<span>第 ${result.page} / ${pages} 页</span>${result.page < pages ? `<a class="button outline small" href="${href(result.page + 1)}">下一页</a>` : ''}</nav>`;
}
function voucherPage() {
  const type = params.get('type') || 'all';
  main.innerHTML = `<div class="page-intro"><div><span class="eyebrow">GOOD PLACES, BETTER PRICES</span><h1>喜欢的店，顺手省一点。</h1><p>先读使用规则，再把优惠放进你的出门计划。</p></div><span class="intro-icon">${icon('ticket')}</span></div><div class="offer-banner"><div>${icon('ticket')}<strong>吃得合心意，花得有分寸</strong><span>普通优惠与秒杀优惠均可下单，请留意活动时间与使用规则。</span></div><span class="demo-pill">学习项目</span></div><div class="voucher-toolbar"><div class="tabs" role="group" aria-label="优惠券类型">${[['all', '全部优惠'], ['seckill', '限量优惠'], ['normal', '店铺代金券']].map(([value, name]) => `<button data-filter="type" data-value="${value}" class="${type === value ? 'active' : ''}" aria-pressed="${type === value}">${name}</button>`).join('')}</div><span class="muted">${voucherResult.total} 款优惠</span></div>${vouchers.length ? `<div class="voucher-grid">${vouchers.map(v => voucherCard(v)).join('')}</div>` : empty('暂时没有优惠券', '可以切换优惠类型，或者稍后再来。', 'vouchers.html', '查看全部优惠')}${pagination(voucherResult, 'vouchers.html')}<p class="inline-note">${icon('ticket')}库存与活动状态来自后端。下单会创建数据库订单并扣库存，没有真实支付或核销。</p>`;
}
function detail() {
  const shop = shops.find(x => x.id === Number(params.get('id')));
  if (!shop) { main.innerHTML = empty('这家店暂时没有收录', '请从好店列表选择一个有效的商户。'); return; }
  document.title = `${shop.name} · PerkFlow`;
  const offers = vouchers.filter(x => x.shopId === shop.id);
  main.innerHTML = `<nav class="breadcrumbs" aria-label="面包屑"><a href="index.html">发现</a>${icon('chevron')}<a href="shop-list.html">好店</a>${icon('chevron')}<span>${escape(shop.name)}</span></nav><div class="detail-top"><div><span class="eyebrow">${escape(shop.kind)} · ${escape(shop.area)}</span><h1>${escape(shop.name)}</h1><p>${escape(shop.subtitle)}</p></div>${saveButton(shop)}</div><div class="detail-layout"><div><div class="gallery"><div class="gallery-main">${image(shop.images[0], shop.name + '店铺展示', '', true)}</div><div class="gallery-thumbs">${shop.images.map((n, i) => `<button data-action="gallery" data-photo="${escape(n)}" aria-label="查看第 ${i + 1} 张照片" aria-pressed="${i === 0}" class="${i === 0 ? 'active' : ''}">${image(n, shop.name + '展示照片 ' + (i + 1))}</button>`).join('')}</div></div><section class="detail-section"><span class="section-eyebrow">A PLACE TO ENJOY</span><h2>关于这家好店</h2><p>${escape(shop.description)}</p><div class="shop-tags">${shop.tags.map(t => `<span>${escape(t)}</span>`).join('')}</div></section><section class="detail-section"><div class="panel-heading"><h2>店铺优惠</h2><span class="muted">${offers.length} 款优惠</span></div>${offers.length ? `<div class="detail-offers">${offers.map(v => voucherCard(v, true)).join('')}</div>` : '<p class="muted">这家店铺暂时没有优惠券，去看看其他好店吧。</p>'}</section></div><aside class="detail-aside"><section class="panel shop-info"><div class="detail-rating"><span>★ ${shop.score.toFixed(1)}</span><small>店铺评分</small></div><p class="average-price">人均 <strong>¥${shop.averagePrice}</strong></p><div class="info-row">${icon('pin')}<div><h3>店铺位置</h3><p>${escape(shop.address)}</p></div></div><div class="info-row">${icon('clock')}<div><h3>营业时间</h3><p>${escape(shop.hours)}（示例）</p></div></div><div class="info-note">当前为虚构商户的页面演示，图片仅用于布局展示，请勿据此到店消费。</div></section><div class="city-note"><span>下次相聚，就在这里</span><h3>好地方，<br>值得慢慢发现。</h3><p>收藏起来，把期待留给下一次出门。</p></div></aside></div>`;
}
function orderCard(order) {
  const cancelled = order.status === 'cancelled';
  return `<article class="order-card"><div class="order-top"><span>${escape(order.id)}</span><time datetime="${escape(order.createdAt)}">${new Date(order.createdAt).toLocaleString('zh-CN')}</time></div><div class="order-content"><a href="${shopUrl(order.shopId)}">${image(order.shopImage, order.shopName)}</a><div class="order-copy"><a href="${shopUrl(order.shopId)}"><h3>${escape(order.title)}</h3></a><p>${escape(order.shopName)}</p>${cancelled ? `<p>取消于 ${escape(new Date(order.cancelledAt).toLocaleString('zh-CN'))}</p>` : ''}<a href="${shopUrl(order.shopId)}" class="text-link">查看店铺${icon('arrow')}</a></div><span class="order-price">¥${money(order.payValue)}</span><div class="order-actions"><span class="order-status ${cancelled ? 'is-cancelled' : ''}">${icon(cancelled ? 'close' : 'check')}${cancelled ? '已取消' : '已创建'}</span>${cancelled ? '' : `<button class="button outline small" data-action="cancel-order" data-order-id="${escape(order.id)}">取消订单</button>`}</div></div></article>`;
}
function ordersPage() {
  if (authError) { main.innerHTML = `<div class="empty-state" role="alert"><h2>暂时无法确认登录状态</h2><p>${escape(authError.message)}</p><button class="button primary" data-action="retry-shops">重新查询</button></div>`; return; }
  if (!auth.currentUser()) { main.innerHTML = empty('请先登录', '登录后，可以查看当前账户在后端创建的订单。', loginUrl(), '登录 / 注册'); return; }
  if (orderLoading) { main.innerHTML = '<div class="empty-state" role="status"><h2>正在查询我的订单…</h2></div>'; return; }
  if (orderError) { main.innerHTML = `<div class="empty-state" role="alert"><h2>订单暂时无法加载</h2><p>${escape(orderError.message)}</p><button class="button primary" data-action="retry-shops">重新查询</button></div>`; return; }
  main.innerHTML = `<div class="page-intro"><div><span class="eyebrow">YOUR NEXT GOOD MOMENT</span><h1>我的优惠券订单</h1><p>下单与取消记录保留在后端，换一个浏览器登录也可以查询。</p></div><button class="button outline small" data-action="retry-shops">刷新订单</button></div><div class="notice">“处理中”表示请求已受理，“已创建”表示订单已保存，均不表示已支付。取消后返还库存并保留记录，活动开放且有库存时可以重新下单。当前没有真实支付、退款或核销。</div>${requestCards()}${orderResult.items.length ? `<div class="orders-list">${orderResult.items.map(orderCard).join('')}</div>` : requestResult.items.length ? '' : empty('还没有优惠券订单', '去看看喜欢的限量优惠吧。', 'vouchers.html', '发现优惠券')}${orderResult.total ? pagination(orderResult, 'orders.html') : ''}`;
}

function requestCards() {
  if (!requestResult.items.length) return '';
  return `<section class="request-section" aria-label="购买请求"><h2>购买进度</h2><p class="muted">处理中表示请求已受理，尚未创建订单。页面会短暂自动查询，也可手动刷新。</p><div class="orders-list">${requestResult.items.map(item => `<article class="order-card" role="status"><div class="order-top"><span>购买请求 ${escape(item.id)}</span><time>${new Date(item.createdAt).toLocaleString('zh-CN')}</time></div><div class="request-copy"><h3>${escape(item.title)}</h3><p>${escape(item.shopName)}</p><strong>${item.status === 'pending' ? '处理中，请稍候' : '本次未创建订单'}</strong><p>${escape(item.status === 'failed' ? item.errorMessage : item.reservationAcquired ? '名额已预占，正在等待创建订单；最终结果以查询为准。' : '正在校验购买资格或等待后台恢复，最终结果以查询为准。')}</p></div></article>`).join('')}</div>${pagination(requestResult, 'orders.html', 'request_page')}</section>`;
}
function loginPage() {
  const user = auth.currentUser();
  main.innerHTML = `<section class="login-layout"><div class="login-visual">${image(5, '绿树与木廊间的庭院', '', true)}<div><span class="eyebrow">WELCOME TO YOUR EVERYDAY</span><h1>你的津城清单，<br>从这里接着逛。</h1><p>收藏一家店，安排一顿饭。<br>天津的下一站，按你的节奏来。</p><span class="login-wordmark">PerkFlow</span></div></div><div class="login-panel">${user ? `<span class="login-icon">${icon('user')}</span><h2>欢迎回来</h2><p>${escape(user.name)}，你已登录 PerkFlow。</p><a class="button primary full" href="${escape(safeReturn(params.get('next')))}">继续发现${icon('arrow')}</a><button class="text-link" data-action="logout">退出并切换账户</button>` : `<span class="login-icon">${icon('user')}</span><span class="section-eyebrow">HELLO, PERKFLOW</span><h2>欢迎来到 PerkFlow</h2><p class="login-subtitle">验证码登录，首次使用自动创建账户。</p><form id="login-form"><label for="phone">手机号</label><input id="phone" name="phone" type="tel" inputmode="numeric" autocomplete="tel" maxlength="11" value="13800138000" required aria-describedby="login-demo-note"><label for="code">验证码</label><div class="code-field"><input id="code" name="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="输入 6 位验证码" required><button type="button" id="send-code">获取验证码</button></div><p id="code-status" class="code-status" aria-live="polite"></p><label class="checkbox-label"><input type="checkbox" id="demo-consent" required><span>我了解当前使用模拟短信，不会发送到真实手机</span></label><button type="submit" class="button primary full">登录 / 自动注册${icon('arrow')}</button><p id="login-error" class="form-error" role="alert"></p></form><div class="login-demo-note" id="login-demo-note">开发模式：点击获取验证码后，在运行后端的终端查看随机验证码。账户与订单保存在数据库；下单会扣减开发库库存，没有真实支付。</div>`}</div></section>`;
}

function render() {
  header();
  footer();
  if (dataState === 'loading') {
    main.innerHTML = '<div class="empty-state" role="status" aria-live="polite"><h2>正在加载…</h2><p>正在从后端查询最新数据。</p></div>';
  } else if (dataState === 'error') {
    const missing = dataError?.status === 404;
    main.innerHTML = `<div class="empty-state" role="alert"><h2>${missing ? '这家店暂时没有收录' : '暂时无法加载数据'}</h2><p>${escape(dataError?.message || '请稍后重试')}</p><button class="button primary" data-action="retry-shops">重新加载</button><a class="button outline" href="shop-list.html">查看好店列表</a></div>`;
  } else {
    ({ home, shops: catalog, vouchers: voucherPage, detail, orders: ordersPage, login: loginPage }[page] || home)();
  }
}

async function loadMerchantData() {
  // 1. 每次页面导航/重试发起真实请求，先展示加载状态，不渲染旧示例数据。
  const revision = ++dataRevision;
  dataState = 'loading';
  dataError = null;
  render();
  try {
    if (page === 'detail') {
      // 2. 详情直接请求 ID 接口，触发 String 缓存；404 和连接失败分别展示。
      const [shop, offers] = await Promise.all([shopApi.detail(params.get('id')), voucherApi.list(new URLSearchParams({ shop_id: params.get('id'), page_size: '50' }))]);
      vouchers = offers.items;
      if (revision !== dataRevision) return;
      shops = [shop];
    } else {
      const query = page === 'home'
        ? new URLSearchParams({ city: city.name, sort: 'score', page: '1', page_size: '6' })
        : shopQuery(params);
      // 3. 分类触发 List 缓存，列表由后端筛选排序分页；独立查询并行等待。
      const [types, result] = await Promise.all([
        shopApi.categories(),
        page === 'shops' && params.has('saved') ? loadSavedShops() : shopApi.list(query),
      ]);
      if (revision !== dataRevision) return;
      categories = types;
      shopResult = result;
      if (page === 'shops' && result.page > Math.max(1, Math.ceil(result.total / result.page_size))) {
        // 删除商户后当前页可能越界，回到最后有效页，不能显示错误的页数。
        query.set('page', String(Math.max(1, Math.ceil(result.total / result.page_size))));
        const lastPage = await shopApi.list(query);
        if (revision !== dataRevision) return;
        shopResult = lastPage;
        params.set('page', String(shopResult.page));
        history.replaceState(null, '', `${location.pathname}?${params}`);
      }
      shops = shopResult.items;
    }
    dataState = 'ready';
  } catch (error) {
    // 4. 保留错误和重试入口，不能把接口异常伪装成“没有商户”或静态查询成功。
    if (revision !== dataRevision) return;
    dataError = error;
    dataState = 'error';
  }
  render();
}

async function loadSavedShops() {
  // 收藏没有服务端存储接口；按本浏览器保存的 ID 查详情，不扫描全部商户分页。
  const all = await shopApi.saved([...new Set(store.saved())]);
  const q = (params.get('q') || '').trim().toLowerCase();
  const list = all.filter(shop => (!params.get('type') || shop.categoryId === Number(params.get('type')))
    && (!q || shop.name.toLowerCase().includes(q))
    && (!params.get('area') || shop.area === params.get('area'))
    && (params.get('kind') !== 'tea' || shop.kind === '茶馆'));
  list.sort((a, b) => (params.get('sort') === 'price' ? a.averagePrice - b.averagePrice
    : params.get('sort') === 'score' ? b.score - a.score : 0) || a.id - b.id);
  const requestedPage = Number.parseInt(params.get('page'), 10) || 1;
  const currentPage = Math.min(Math.max(1, Math.ceil(list.length / 4)), Math.max(1, requestedPage));
  return { items: list.slice((currentPage - 1) * 4, currentPage * 4), total: list.length, page: currentPage, page_size: 4 };
}

function filter(key, value) {
  const next = new URLSearchParams(location.search);
  if (value) next.set(key, value); else next.delete(key);
  next.delete('page');
  location.href = `${location.pathname.split('/').pop()}${next.size ? '?' + next : ''}`;
}
function openCoupon(voucher, claim) {
  action = claim ? { kind: 'claim', voucher } : null;
  dialog.innerHTML = `<button class="dialog-close icon-button" data-action="close-dialog" aria-label="关闭">${icon('close')}</button><span class="dialog-icon">${icon('ticket')}</span><span class="section-eyebrow">${claim ? 'YOUR COUPON ORDER' : 'BEFORE YOUR NEXT VISIT'}</span><h2 id="dialog-title">${claim ? '确认创建订单' : '优惠券使用规则'}</h2><p>${escape(voucher.shopName)} · ${escape(voucher.title)}</p><div class="dialog-price">¥${money(voucher.payValue)}</div><ul class="rule-list">${voucher.rules.map(rule => `<li>${escape(rule)}</li>`).join('')}</ul><p class="dialog-note">${escape(voucherActivityWindow(voucher))}。订单创建后 ${voucher.validity_days} 天有效。确认后提交购买请求，处理完成后才会创建订单并扣减库存，没有真实支付。每个账户对同一张券最多一笔未取消订单，取消后可重新参与。</p><div class="dialog-actions"><button class="button outline" data-action="close-dialog">${claim ? '再想想' : '知道了'}</button>${claim ? '<button class="button primary" data-action="confirm-dialog" autofocus>确认创建订单</button>' : ''}</div>`;
  dialog.showModal();
}
function openCancellation(order) {
  action = { kind: 'cancel', order };
  dialog.innerHTML = `<button class="dialog-close icon-button" data-action="close-dialog" aria-label="关闭">${icon('close')}</button><span class="dialog-icon">${icon('ticket')}</span><span class="section-eyebrow">YOUR COUPON ORDER</span><h2 id="dialog-title">确认取消订单</h2><p>${escape(order.shopName)} · ${escape(order.title)}</p><p class="dialog-note">取消后返还一份库存，历史记录保留。活动仍开放且有库存时，可以重新下单。本项目没有真实支付，因此不涉及退款。</p><div class="dialog-actions"><button class="button outline" data-action="close-dialog">保留订单</button><button class="button primary" data-action="confirm-dialog" autofocus>确认取消</button></div>`;
  dialog.showModal();
}

document.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button || button.disabled) return;
  if (button.dataset.filter) { filter(button.dataset.filter, button.dataset.value); return; }
  const id = Number(button.dataset.id);
  try {
    switch (button.dataset.action) {
      case 'retry-shops': {
        orderPollUntil = Date.now() + 30000;
        if (page === 'orders') {
          try { await auth.initialize(); authError = null; }
          catch (error) { authError = error; }
        }
        await loadPageData(); break;
      }
      case 'save': {
        const saved = store.toggleSaved(id);
        const shop = shops.find(x => x.id === id);
        button.classList.toggle('is-saved', saved);
        button.setAttribute('aria-pressed', saved);
        button.setAttribute('aria-label', `${saved ? '取消收藏' : '收藏'}${shop.name}`);
        if (button.querySelector('span')) button.querySelector('span').textContent = saved ? '已收藏' : '收藏好店';
        if (page === 'shops' && params.has('saved')) await loadMerchantData();
        toast(saved ? '好店已收藏，留给下一次出门。' : '已取消收藏');
        break;
      }
      case 'story-tab': {
        document.querySelectorAll('[data-action="story-tab"]').forEach(el => { const selected = el === button; el.classList.toggle('active', selected); el.setAttribute('aria-pressed', selected); });
        document.querySelector('#story-grid').innerHTML = stories.filter(x => button.dataset.group === 'all' || x.group === button.dataset.group).map(storyCard).join('');
        break;
      }
      case 'gallery': {
        const cover = document.querySelector('.gallery-main img');
        cover.src = assetPath(button.dataset.photo);
        document.querySelectorAll('[data-action="gallery"]').forEach(el => { el.classList.toggle('active', el === button); el.setAttribute('aria-pressed', el === button); });
        break;
      }
      case 'rules': openCoupon(vouchers.find(x => x.id === id), false); break;
      case 'claim': {
        await auth.initialize();
        if (!store.currentUser()) { location.href = loginUrl(); return; }
        openCoupon(vouchers.find(x => x.id === id), true);
        break;
      }
      case 'cancel-order': {
        const order = orderResult.items.find(row => row.id === button.dataset.orderId);
        if (order?.status === 'created') openCancellation(order);
        break;
      }
      case 'logout': {
        button.disabled = true;
        try { await auth.logout(); location.href = 'login.html'; }
        finally { button.disabled = false; }
        break;
      }
      case 'close-dialog': dialog.close(); action = null; break;
      case 'confirm-dialog': {
        if (!['claim', 'cancel'].includes(action?.kind) || submittingOrder) break;
        const operation = action;
        submittingOrder = true;
        dialog.querySelectorAll('button').forEach(control => { control.disabled = true; });
        try {
          // 1. 取消使用原始字符串订单号；请求进行中禁止重复确认与关闭弹窗。
          const result = operation.kind === 'cancel'
            ? await voucherApi.cancelOrder(operation.order.id)
            : await voucherApi.createOrder(operation.voucher.id);
          dialog.close(); action = null;
          orderPollUntil = Date.now() + 30000;
          toast(operation.kind === 'cancel' ? '订单已取消，库存已返还。'
            : result.request?.status === 'pending' ? '购买请求已受理，正在处理，可到「我的订单」查看进度。'
              : result.request?.status === 'failed' ? result.request.errorMessage
                : result.order?.status === 'cancelled' ? '该次购买已取消，可重新选择优惠。'
                  : result.reused ? '你已创建过这张券的订单，可在「我的订单」查看。' : '订单已创建，可在「我的订单」查看。');
          await loadOrders();
          await loadPageData();
        } catch (error) {
          if (error.status === 401) { location.href = loginUrl(); return; }
          toast(error.message);
          if (error.status === 409) { dialog.close(); action = null; await loadPageData(); }
        } finally {
          submittingOrder = false;
          dialog.querySelectorAll('button').forEach(control => { control.disabled = false; });
        }
        break;
      }
    }
  } catch (error) { toast(error.message || '暂时无法保存，请检查浏览器存储设置。'); }
});
dialog.addEventListener('cancel', event => { if (submittingOrder) event.preventDefault(); else action = null; });
dialog.addEventListener('click', event => {
  if (event.target !== dialog || submittingOrder) return;
  const rect = dialog.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) { dialog.close(); action = null; }
});
document.addEventListener('submit', async event => {
  if (event.target.matches('.search-form')) {
    event.preventDefault();
    const query = new FormData(event.target).get('q').trim();
    location.href = `shop-list.html${query ? '?q=' + encodeURIComponent(query) : ''}`;
  }
  if (event.target.id === 'login-form') {
    event.preventDefault();
    const form = event.target;
    const error = document.querySelector('#login-error');
    const phone = form.elements.phone.value.trim();
    const code = form.elements.code.value.trim();
    error.textContent = '';
    if (!validPhone(phone)) { error.textContent = '请输入有效的 11 位手机号'; return; }
    if (!/^[0-9]{6}$/.test(code)) { error.textContent = '请输入 6 位验证码'; return; }
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try { await auth.login(phone, code); location.href = safeReturn(params.get('next')); }
    catch (failure) { error.textContent = failure.message; }
    finally { submit.disabled = false; }
  }
});

document.addEventListener('change', event => {
  if (event.target.id === 'sort-select') filter('sort', event.target.value === 'default' ? '' : event.target.value);
});
function startCodeTimer(button, seconds) {
  button.disabled = true;
  button.textContent = `${seconds}s 后重试`;
  clearInterval(codeTimer);
  codeTimer = setInterval(() => { seconds -= 1; button.textContent = seconds > 0 ? `${seconds}s 后重试` : '获取验证码'; if (seconds <= 0) { clearInterval(codeTimer); button.disabled = false; } }, 1000);
}
document.addEventListener('click', async event => {
  if (event.target.id !== 'send-code' || event.target.disabled) return;
  const phone = document.querySelector('#phone').value.trim();
  const error = document.querySelector('#login-error');
  if (!validPhone(phone)) { error.textContent = '请输入有效的 11 位手机号'; return; }
  error.textContent = '';
  const button = event.target;
  button.disabled = true;
  try {
    const result = await auth.sendCode(phone);
    document.querySelector('#code-status').textContent = `模拟验证码已发送，请查看后端终端（${result.expires_in} 秒内有效）`;
    startCodeTimer(button, result.retry_after);
  } catch (failure) {
    error.textContent = failure.message;
    if (failure.retryAfter > 0) startCodeTimer(button, failure.retryAfter);
    else button.disabled = false;
  }
});
async function loadOrders(background = false) {
  // 1. 清除上一账户的页面快照；不把接口失败当成零订单。
  if (!background) {
    orderResult = { items: [], total: 0, page: 1, page_size: 20 };
    requestResult = { items: [], total: 0, page: 1, page_size: 20 };
  }
  clearTimeout(orderPollTimer);
  const revision = ++orderRevision;
  orderError = null;
  if (!auth.currentUser()) return;
  orderLoading = !background;
  if (page === 'orders' && !background) render();
  try {
    const query = new URLSearchParams({ page: page === 'orders' ? params.get('page') || '1' : '1', page_size: '20' });
    // 取消历史不会占用券页的已购提示快照；订单页仍展示完整历史。
    if (page !== 'orders') query.set('status', 'created');
    const requestsQuery = new URLSearchParams({ page: page === 'orders' ? params.get('request_page') || '1' : '1', page_size: '20' });
    const [ordersData, requestsData] = await Promise.all([voucherApi.orders(query), voucherApi.requests(requestsQuery)]);
    if (revision !== orderRevision) return;
    orderResult = ordersData; requestResult = requestsData;
    if (requestResult.items.some(item => item.status === 'pending')) {
      if (!orderPollUntil) orderPollUntil = Date.now() + 30000;
      if (Date.now() < orderPollUntil) orderPollTimer = setTimeout(async () => {
        if (document.hidden || !auth.currentUser()) return;
        // 进度没有变化时保留 DOM，避免每两秒重建页面、打断正在点击或输入的控件。
        const before = JSON.stringify([orderResult, requestResult, orderError?.code]);
        await loadOrders(true);
        if (before !== JSON.stringify([orderResult, requestResult, orderError?.code])) {
          if (!requestResult.items.some(item => item.status === 'pending') && page !== 'orders') await loadPageData();
          else render();
        }
      }, 2000);
    } else orderPollUntil = 0;
  } catch (error) {
    orderError = error;
    if (error.status === 401) await auth.initialize().catch(() => {});
  } finally { orderLoading = false; }
}
async function loadPageData() {
  if (page === 'orders') {
    await loadOrders(); dataState = 'ready'; render(); return;
  }
  if (merchantPage) { await loadMerchantData(); return; }
  if (page !== 'vouchers') return;
  const revision = ++dataRevision;
  dataState = 'loading'; dataError = null; render();
  try {
    const query = new URLSearchParams({ page: params.get('page') || '1', page_size: '20' });
    if (params.get('type') === 'seckill') query.set('type', '1');
    if (params.get('type') === 'normal') query.set('type', '0');
    const result = await voucherApi.list(query);
    if (revision !== dataRevision) return;
    voucherResult = result; vouchers = result.items; dataState = 'ready';
  } catch (error) {
    if (revision !== dataRevision) return;
    dataError = error; dataState = 'error';
  }
  render();
}
window.addEventListener('storage', () => {
  if (dialog.open || page === 'login') return;
  if (page === 'shops' && params.has('saved')) loadMerchantData(); else render();
});
window.addEventListener('focus', async () => {
  try { await auth.initialize(); authError = null; if (page !== 'orders') await loadOrders(); if (businessPage) await loadPageData(); else render(); }
  catch (error) { authError = error; header(); if (page === 'orders') { orderError = error; dataState = 'ready'; render(); } }
});
render();
(async () => {
  try { await auth.initialize(); authError = null; if (page !== 'orders') await loadOrders(); }
  catch (error) { authError = error; orderError = error; }
  await loadPageData();
  header();
  if (!businessPage) render();
  const loginError = document.querySelector('#login-error');
  if (loginError && authError) loginError.textContent = authError.message;
})();
