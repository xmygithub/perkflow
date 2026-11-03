// 两类券共用购买条件；浏览器时间只帮助展示，最终资格由 Redis/SQL 校验。
export function voucherAvailability(voucher, now = Date.now()) {
  const labels = { upcoming: '尚未开始', ended: '已结束', sold_out: '已抢完',
    off_shelf: '已下架', unavailable: '暂不可用' };
  const begin = Date.parse(voucher.begin_time);
  const end = Date.parse(voucher.end_time);
  let state = voucher.state;
  // 1. 后端阻止状态保持阻止；本地时钟只收紧 active，不擅自把 upcoming 改成可购买。
  if (state === 'active') {
    if (!Number.isFinite(begin) || !Number.isFinite(end) || begin >= end) state = 'unavailable';
    else if (now < begin) state = 'upcoming';
    else if (now >= end) state = 'ended';
  }
  return { purchasable: state === 'active', label: labels[state] || '立即下单' };
}

export function voucherActivityWindow(voucher) {
  const begin = Date.parse(voucher.begin_time), end = Date.parse(voucher.end_time);
  if (!Number.isFinite(begin) || !Number.isFinite(end) || begin >= end) return '购买时间：待配置';
  // 2. API 为 UTC，用户看到北京时间；活动购买时间与创建订单后的有效期分别展示。
  const formatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  return `购买时间：${formatter.format(begin)} 至 ${formatter.format(end)}（北京时间）`;
}
