// 商户数据只来自后端；认证 Cookie 随同源请求携带，不在这里创建或伪造身份。
export class ShopApiError extends Error {
  constructor(message, status = 0) { super(message); this.status = status; }
}

export function assetPath(value) {
  // 静态资源使用本站明确目录，数据库文本不直接成为任意外部 URL。
  return typeof value === 'string' && /^public\/assets\/(photos|categories)\/[\w-]+\.(jpg|jpeg|png|webp|svg)$/i.test(value)
    ? value : 'public/assets/photos/01.jpg';
}

export function adaptShop(value) {
  // 接口金额单位是分；只在展示模型转换成元，不改数据库或交易金额。
  if (!Number.isSafeInteger(value?.id) || !Array.isArray(value.images) || !Array.isArray(value.tags)
      || !Number.isFinite(value.score) || !Number.isInteger(value.avg_price)) {
    throw new ShopApiError('商户数据格式不正确，请稍后重试');
  }
  return {
    ...value, categoryId: value.type_id, averagePrice: value.avg_price / 100,
    hours: value.open_hours, images: value.images.map(assetPath),
  };
}

export function shopQuery(params = new URLSearchParams()) {
  // 页面沿用 type 参数，向后端转换成 category_id；只传后端支持的筛选项。
  const query = new URLSearchParams({ city: '天津', page: params.get('page') || '1', page_size: '4' });
  for (const [source, target] of [['type', 'category_id'], ['area', 'area'], ['q', 'q'], ['sort', 'sort']]) {
    if (params.get(source)) query.set(target, params.get(source));
  }
  if (params.get('kind') === 'tea') query.set('kind', '茶馆');
  return query;
}

export function createShopClient(fetcher = globalThis.fetch) {
  async function request(path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      // 每次导航真正请求后端；no-store 只控制浏览器缓存，不禁用服务端 Redis。
      const response = await fetcher(`/api/${path}`, {
        credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json' },
        signal: controller.signal,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new ShopApiError(data?.error?.message || '商户查询失败，请稍后重试', response.status);
      if (!data) throw new ShopApiError('商户服务返回了无效数据');
      return data;
    } catch (error) {
      if (error instanceof ShopApiError) throw error;
      throw new ShopApiError('商户服务暂时无法连接，请确认后端已启动后重试');
    } finally { clearTimeout(timer); }
  }
  return {
    async categories() {
      const data = await request('shop-types');
      if (!Array.isArray(data.items)) throw new ShopApiError('分类数据格式不正确');
      return data.items.map(item => ({ ...item, icon: assetPath(item.icon) }));
    },
    async list(query) {
      const data = await request(`shops?${query}`);
      if (!Array.isArray(data.items) || !Number.isInteger(data.total) || !Number.isInteger(data.page)
          || !Number.isInteger(data.page_size) || data.page_size < 1) {
        throw new ShopApiError('商户列表数据格式不正确');
      }
      return { ...data, items: data.items.map(adaptShop) };
    },
    async detail(id) {
      if (!Number.isSafeInteger(Number(id)) || Number(id) < 1) throw new ShopApiError('商户编号不正确', 422);
      return adaptShop(await request(`shops/${Number(id)}`));
    },
    async saved(ids) {
      // 收藏只存 ID；逐个查真实详情，已删除店铺跳过，连接错误不能当作店铺不存在。
      const results = await Promise.all(ids.map(async id => {
        try { return await this.detail(id); }
        catch (error) { if (error.status === 404) return null; throw error; }
      }));
      return results.filter(Boolean);
    },
  };
}
