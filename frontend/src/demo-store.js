// Browser-local UI simulation. These checks are not backend security guarantees.
const ORDER_KEY = 'perkflow.demo.orders.v1';
const USER_KEY = 'perkflow.demo.user.v1';
const SAVE_KEY = 'perkflow.demo.saved.v1';

function read(storage, key, fallback) {
  try { return JSON.parse(storage.getItem(key)) ?? fallback; } catch { return fallback; }
}
export const validPhone = phone => /^1[3-9]\d{9}$/.test(phone);
export function safeReturn(value) {
  try {
    const url = new URL(value || 'index.html', 'https://perkflow.invalid/');
    if (url.origin !== 'https://perkflow.invalid' || !['/index.html', '/shop-list.html', '/shop-detail.html', '/vouchers.html', '/orders.html'].includes(url.pathname)) return 'index.html';
    return url.pathname.slice(1) + url.search;
  } catch { return 'index.html'; }
}
export function createDemoStore(persistent, session, getAuthenticatedUser = null) {
  const allOrders = () => {
    const value = read(persistent, ORDER_KEY, []);
    return Array.isArray(value) ? value.filter(x => x && typeof x.id === 'string' && typeof x.userId === 'string' && Number.isInteger(x.voucherId) && typeof x.createdAt === 'string' && !Number.isNaN(Date.parse(x.createdAt))) : [];
  };
  const currentUser = () => {
    if (getAuthenticatedUser) return getAuthenticatedUser();
    const value = read(session, USER_KEY, null);
    return value && typeof value.id === 'string' && typeof value.name === 'string' ? value : null;
  };
  return {
    currentUser,
    signIn(phone) {
      if (!validPhone(phone)) throw new Error('请输入有效的 11 位演示手机号');
      const user = { id: `demo-${phone}`, name: `体验用户 ${phone.slice(-4)}` };
      session.setItem(USER_KEY, JSON.stringify(user));
      return user;
    },
    signOut() { session.removeItem(USER_KEY); },
    orders() { const user = currentUser(); return user ? allOrders().filter(x => x.userId === user.id) : []; },
    remaining(voucher) { return Math.max(0, voucher.stock - allOrders().filter(x => x.voucherId === voucher.id).length); },
    createOrder(voucher) {
      const user = currentUser();
      if (!user) throw new Error(getAuthenticatedUser ? '请先登录' : '请先进入演示账户');
      if (voucher.state !== 'active') throw new Error(voucher.state === 'ended' ? '这场活动已结束' : '活动尚未开始');
      const orders = allOrders();
      if (orders.some(x => x.userId === user.id && x.voucherId === voucher.id)) throw new Error('你已经创建过这张券的演示订单');
      if (this.remaining(voucher) === 0) throw new Error('演示库存已用完');
      const order = { id: `DEMO-${crypto.randomUUID()}`, userId: user.id, voucherId: voucher.id, createdAt: new Date().toISOString() };
      persistent.setItem(ORDER_KEY, JSON.stringify([order, ...orders]));
      return order;
    },
    saved() { const ids = read(persistent, SAVE_KEY, []); return Array.isArray(ids) ? ids.filter(Number.isInteger) : []; },
    toggleSaved(id) {
      const ids = this.saved();
      persistent.setItem(SAVE_KEY, JSON.stringify(ids.includes(id) ? ids.filter(x => x !== id) : [...ids, id]));
      return !ids.includes(id);
    },
    resetOrders() { persistent.removeItem(ORDER_KEY); },
  };
}
