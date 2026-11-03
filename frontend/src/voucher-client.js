import { assetPath } from './shop-client.js';

export class VoucherApiError extends Error {
  constructor(message, status = 0, code = 'NETWORK_ERROR') {
    super(message); this.status = status; this.code = code;
  }
}

export function adaptVoucher(value) {
  if (!Number.isSafeInteger(value?.id) || !Number.isSafeInteger(value.shop_id)
      || ![0, 1].includes(value.type) || !Array.isArray(value.rules)
      || !Number.isInteger(value.stock) || value.stock < 0
      || !Number.isInteger(value.pay_value) || !Number.isInteger(value.actual_value)
      || !['active', 'upcoming', 'ended', 'sold_out', 'off_shelf', 'unavailable'].includes(value.state)) {
    throw new VoucherApiError('优惠券数据格式不正确，请稍后重试');
  }
  return { ...value, shopId: value.shop_id, shopName: value.shop_name,
    shopImage: assetPath(value.shop_image), subtitle: value.sub_title,
    payValue: value.pay_value, actualValue: value.actual_value,
    type: value.type === 1 ? 'seckill' : 'normal' };
}

export function adaptOrder(value) {
  // 订单 ID 始终按字符串传递，不能 Number() 转换导致长整数精度丢失。
  if (typeof value?.id !== 'string' || !/^[1-9]\d{0,18}$/.test(value.id)
      || !Number.isSafeInteger(value.voucher_id) || !Number.isInteger(value.pay_value)
      || !['created', 'cancelled'].includes(value.status) || Number.isNaN(Date.parse(value.created_at))
      || Number.isNaN(Date.parse(value.expires_at))
      || (value.status === 'cancelled' && (!value.cancelled_at || Number.isNaN(Date.parse(value.cancelled_at))))) {
    throw new VoucherApiError('订单数据格式不正确，请稍后重试');
  }
  return { ...value, voucherId: value.voucher_id, shopId: value.shop_id,
    shopName: value.shop_name, shopImage: assetPath(value.shop_image),
    payValue: value.pay_value, createdAt: value.created_at, cancelledAt: value.cancelled_at || null };
}

export function adaptRequest(value) {
  // 受理编号不是成功订单；只有 succeeded 且有真实订单时才显示已创建。
  if (typeof value?.id !== 'string' || !/^[1-9]\d{0,18}$/.test(value.id)
      || !Number.isSafeInteger(value.voucher_id)
      || !['pending', 'succeeded', 'failed'].includes(value.status)
      || (value.reservation_acquired !== undefined && typeof value.reservation_acquired !== 'boolean')
      || Number.isNaN(Date.parse(value.created_at))
      || (value.status === 'succeeded' && !value.order)
      || (value.status === 'failed' && (typeof value.error_code !== 'string' || typeof value.error_message !== 'string'))) {
    throw new VoucherApiError('购买请求数据格式不正确，请刷新查询');
  }
  return { ...value, voucherId: value.voucher_id, shopName: value.shop_name,
    createdAt: value.created_at, errorMessage: value.error_message,
    reservationAcquired: value.reservation_acquired === true,
    order: value.order ? adaptOrder(value.order) : null };
}

export function createVoucherClient(auth, fetcher = globalThis.fetch) {
  const intentKeys = new Map();
  async function request(path, write = false, cancelling = false, requestKey = null) {
    // 1. 写请求取得当前会话的 CSRF，身份编号不放入客户端请求参数。
    const csrf = write ? await auth.csrfToken() : null;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetcher(`/api/${path}`, {
        method: write ? 'POST' : 'GET', credentials: 'include', cache: 'no-store',
        headers: { Accept: 'application/json', ...(write ? { 'X-CSRF-Token': csrf } : {}),
          ...(requestKey ? { 'Idempotency-Key': requestKey } : {}) },
        signal: controller.signal,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new VoucherApiError(data?.error?.message || '优惠券服务请求失败',
        response.status, data?.error?.code);
      if (!data) throw new VoucherApiError('服务返回了无效数据');
      return data;
    } catch (error) {
      if (error instanceof VoucherApiError) throw error;
      // 2. 写请求超时不代表事务回滚：提示回查，不自动重发或伪造本地成功记录。
      throw new VoucherApiError(write
        ? cancelling ? '未能确认取消结果，请刷新「我的订单」查看状态后再操作。'
          : '未能确认下单结果，请先到「我的订单」查看；再次提交同一张券会回查已有订单。'
        : '优惠券服务暂时无法连接，请确认后端已启动后重试', 0,
      write ? cancelling ? 'ORDER_CANCEL_RESULT_UNKNOWN' : 'ORDER_RESULT_UNKNOWN' : 'NETWORK_ERROR');
    } finally { clearTimeout(timer); }
  }
  function page(data, adapt) {
    if (!Array.isArray(data.items) || !Number.isInteger(data.total) || data.total < 0
        || !Number.isInteger(data.page) || data.page < 1
        || !Number.isInteger(data.page_size) || data.page_size < 1) {
      throw new VoucherApiError('分页数据格式不正确');
    }
    return { ...data, items: data.items.map(adapt) };
  }
  return {
    async list(query = new URLSearchParams()) { return page(await request(`vouchers?${query}`), adaptVoucher); },
    async orders(query = new URLSearchParams()) { return page(await request(`orders?${query}`), adaptOrder); },
    async requests(query = new URLSearchParams()) { return page(await request(`order-requests?${query}`), adaptRequest); },
    async orderRequest(id) {
      if (typeof id !== 'string' || !/^[1-9]\d{0,18}$/.test(id)) throw new VoucherApiError('请求编号不正确');
      return adaptRequest(await request(`order-requests/${id}`));
    },
    async createOrder(id) {
      if (!Number.isSafeInteger(id) || id < 1) throw new VoucherApiError('优惠券编号不正确');
      // 同一页面内结果不明的重试保留意图 UUID；响应已确认后，下次购买生成新 UUID。
      const slot = `${auth.currentUser?.()?.id || 'current'}:${id}`;
      if (!intentKeys.has(slot)) intentKeys.set(slot, globalThis.crypto.randomUUID());
      try {
        const data = await request(`vouchers/${id}/orders`, true, false, intentKeys.get(slot));
        if (typeof data.reused !== 'boolean') throw new VoucherApiError('下单响应格式不正确');
        const result = data.request
          ? { request: adaptRequest(data.request), order: data.request.order ? adaptOrder(data.request.order) : null, reused: data.reused }
          : { order: adaptOrder(data.order), reused: data.reused };
        intentKeys.delete(slot);
        return result;
      } catch (error) {
        if (error.status >= 400 && error.status < 500) intentKeys.delete(slot);
        throw error;
      }
    },
    async cancelOrder(id) {
      // 长订单号必须保持字符串，不能 Number() 后丢失末位；不接受用户或库存参数。
      if (typeof id !== 'string' || !/^[1-9]\d{0,18}$/.test(id) || BigInt(id) > 9223372036854775807n) {
        throw new VoucherApiError('订单编号不正确');
      }
      const data = await request(`orders/${id}/cancel`, true, true);
      if (typeof data.reused !== 'boolean' || data.order?.status !== 'cancelled') {
        throw new VoucherApiError('取消订单响应格式不正确');
      }
      return { order: adaptOrder(data.order), reused: data.reused };
    },
  };
}
