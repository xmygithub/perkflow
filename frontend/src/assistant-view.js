// 展示事实、硬条件和软偏好；不把空字段渲染成需要用户逐项填写的表单。
const money = value => `¥${(value / 100).toFixed(2).replace(/\.00$/, '')}`;
export function requirementRows(state) {
  const need = state.requirements;
  const rows = [['当前城市', `${need.city}${state.condition_sources?.city === '平台当前城市' ? '（平台城市）' : ''}`]];
  if (need.area) rows.push(['限定区域', need.area]);
  if (need.category) rows.push(['品类', need.category]);
  if (need.category_matches?.length) rows.push(['相关类目探索', `${need.category_matches.join('、')}（符合原意仍待核验）`]);
  if (need.people) rows.push(['人数', `${need.people}人`]);
  if (need.budget_fen != null) rows.push(['整单上限', money(need.budget_fen)]);
  if (need.use_date) rows.push(['使用日期', need.use_date]);
  if (need.use_weekdays?.length) rows.push(['星期范围', need.use_weekdays.map(day => ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][day]).join('、')]);
  if (need.use_time) rows.push(['北京时间', need.use_time]);
  if (need.allow_reservation != null) rows.push(['预约要求', need.allow_reservation ? '接受预约' : '不接受预约']);
  return rows;
}
export function preferenceRows(state) {
  const prefs = state.preferences || {};
  const rows = [];
  if (prefs.area) rows.push(['区域优先', prefs.area]);
  if (prefs.category) rows.push(['品类优先', prefs.category]);
  if (prefs.low_price) rows.push(['价格偏好', '便宜一些（没有擅设预算）']);
  if (prefs.high_rating) rows.push(['评分偏好', '评分较高']);
  if (prefs.avoid_reservation) rows.push(['预约偏好', '尽量无需预约']);
  for (const note of prefs.notes || []) rows.push(['其他偏好', note]);
  return rows;
}
export function cardPresentation(result) {
  const exploratory = result.stage === 'candidate';
  return {
    className: exploratory ? 'candidate' : result.verdict,
    label: exploratory
      ? result.verdict === 'eligible' ? '探索候选 · 当前条件已核对' : '探索候选 · 使用条件待确认'
      : { eligible: '当前条件通过', unknown: '尚不能确认适用', ineligible: '与当前条件冲突' }[result.verdict],
    notes: [...result.reasons, ...result.unknowns, ...(result.preference_notes || [])],
  };
}

// 比较只使用服务端的当前校验快照，不把模型文案或代金面额当作整餐花费。
export function comparisonRows(results) {
  const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  return results.map(result => {
    const voucher = result.voucher, usage = voucher.usage;
    const people = usage.min_people != null && usage.max_people != null
      ? usage.min_people === usage.max_people ? `${usage.min_people}人` : `${usage.min_people}–${usage.max_people}人`
      : '人数规则未知';
    const days = usage.allowed_weekdays?.length
      ? usage.allowed_weekdays.map(day => weekdays[day]).join('、') : '可用星期未知';
    const hours = usage.daily_start && usage.daily_end
      ? `${usage.daily_start.slice(0, 5)}–${usage.daily_end.slice(0, 5)}` : '时段未知';
    return {
      voucherId: voucher.voucher_id,
      title: `${voucher.shop_name} · ${voucher.title}`,
      values: [
        ['券实付', `${money(voucher.pay_value)} / 张`],
        ['列明套餐金额', result.total_cost_fen == null ? '整餐金额不能确定' : `${money(result.total_cost_fen)}（不含额外消费）`],
        ['适用人数', people],
        ['可用时间', `${days}；${hours}`],
        ['预约', usage.reservation_required == null ? '规则未知' : usage.reservation_required ? '需要预约' : '无需预约'],
        ['套餐内容', usage.package_items?.length ? usage.package_items.join('、') : '未提供套餐明细'],
        ['当前校验', { eligible: '当前已建模条件通过', unknown: '存在待确认项', ineligible: '与当前条件冲突' }[result.verdict]],
        ['冲突与待确认', [...result.reasons, ...result.unknowns].join('；') || '当前未发现'],
        ['偏好取舍', result.preference_notes?.join('；') || '暂未指定'],
        ['其他规则', usage.note || '未补全'],
        ['查询快照', `${voucher.stock_snapshot}份 · ${voucher.checked_at}`],
      ],
    };
  });
}

export function choiceMessage(position, title) {
  return `请帮我看看上一轮方案${position}「${title}」，有什么需要注意的？`;
}

export function purchasePresentation(receipt) {
  const labels = { submitting: '提交结果待确认', pending: '已受理 · 订单处理中', succeeded: '订单已创建', failed: '本次购买失败' };
  const cancelled = receipt.request?.order?.status === 'cancelled';
  return {
    label: cancelled ? '订单已取消（历史购买结果）' : labels[receipt.status] || '状态未知，请回查',
    waiting: ['submitting', 'pending'].includes(receipt.status),
    canReplan: receipt.status === 'failed',
    message: cancelled ? '保留取消记录，未自动重新购买。' : receipt.message,
  };
}
