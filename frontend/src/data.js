// Curated UI fixtures; not a database export or real merchant information.
export const photo = n => `public/assets/photos/${String(n).padStart(2, '0')}.jpg`;
export const city = { name: '天津', english: 'TIANJIN' };
export const categories = [
  { id: 1, name: '美食', icon: 'ms.png' },
  { id: 2, name: 'KTV', icon: 'KTV.png' },
  { id: 3, name: '理发造型', icon: 'lrmf.png' },
  { id: 10, name: '美甲美睫', icon: 'mjmj.png' },
  { id: 5, name: '按摩·足疗', icon: 'amzl.png' },
  { id: 6, name: '美容 SPA', icon: 'spa.png' },
  { id: 7, name: '亲子游乐', icon: 'qzyl.png' },
  { id: 8, name: '酒吧', icon: 'jiuba.png' },
  { id: 9, name: '轰趴馆', icon: 'hpg.png' },
  { id: 4, name: '健身运动', icon: 'jsyd.png' },
];
export const shops = [
  { id: 1, name: '津巷泰食', subtitle: '约个饭局，换换口味', categoryId: 1, kind: '泰国菜', area: '南开街区', address: '天津 · 南开街区（演示地址）', score: 4.8, averagePrice: 88, hours: '11:00–21:30', tags: ['朋友聚餐', '东南亚风味'], images: [2, 1, 10], description: '把新鲜香草、酸辣滋味和热闹的一餐放在一起。适合约上朋友，慢慢分享一桌好菜。', offer: true },
  { id: 2, name: '海河茶集', subtitle: '逛完河边，再坐一会儿', categoryId: 1, kind: '茶馆', area: '海河沿线', address: '天津 · 海河沿线（演示地址）', score: 4.9, averagePrice: 68, hours: '10:00–20:00', tags: ['庭院茶室', '安静聊天'], images: [5, 14, 12], description: '树影、木色和一杯热茶，给忙碌的日常留一点空白。坐进庭院，感受时间慢下来的样子。', offer: true },
  { id: 3, name: '五大道·花间', subtitle: '散步之后，认真吃顿晚饭', categoryId: 1, kind: '西餐', area: '五大道', address: '天津 · 五大道（演示地址）', score: 4.7, averagePrice: 128, hours: '11:30–22:00', tags: ['双人约会', '氛围西餐'], images: [6, 3, 13], description: '暖灯下的鲜花、刚出炉的焗饭与适合分享的餐盘。为一次认真相聚，准备一顿有仪式感的晚餐。', offer: true },
  { id: 4, name: '南开港味', subtitle: '午饭有着落，下午有精神', categoryId: 1, kind: '港式茶餐厅', area: '南开街区', address: '天津 · 南开街区（演示地址）', score: 4.6, averagePrice: 42, hours: '09:00–21:00', tags: ['日常午餐', '港式风味'], images: [7, 15, 7], description: '烧味、滑蛋和一杯热饮，组成一份踏实的日常。随时走进来，给自己补充一点好心情。', offer: true },
  { id: 5, name: '津城小院', subtitle: '今天的安排，是不赶时间', categoryId: 1, kind: '茶馆', area: '海河沿线', address: '天津 · 海河沿线（演示地址）', score: 4.8, averagePrice: 98, hours: '10:00–19:00', tags: ['周末放松', '庭院茶室'], images: [9, 11, 16], description: '藏在木廊和绿意之间的安静角落。适合独处，也适合和朋友坐一下午，聊聊最近的生活。', offer: false },
  { id: 6, name: '河岸餐桌', subtitle: '把老朋友，约到同一桌', categoryId: 1, kind: '西餐', area: '意式风情区', address: '天津 · 意式风情区（演示地址）', score: 4.7, averagePrice: 108, hours: '11:00–22:00', tags: ['朋友聚餐', '双人约会'], images: [13, 18, 6], description: '一桌丰盛的餐盘，几段轻松的对话。让相聚有滋有味，让每一口都值得慢慢品尝。', offer: true },
];
export const areas = [...new Set(shops.map(shop => shop.area))];
export const stories = [
  { id: 1, shopId: 2, image: 5, label: '松弛时刻', title: '海河边的下午，留给一壶茶', note: '走走停停，找一处能慢慢聊天的地方。', group: 'relax' },
  { id: 2, shopId: 1, image: 2, label: '好好吃饭', title: '周末组个饭局，试试这一桌酸辣', note: '不想重复上次的菜单？来点东南亚风味。', group: 'food' },
  { id: 3, shopId: 3, image: 3, label: '约会灵感', title: '五大道散步之后，坐下来吃顿西餐', note: '两个人的出门计划，从散步延续到晚饭。', group: 'food' },
  { id: 4, shopId: 4, image: 7, label: '日常好味', title: '南开街头的午饭，不用想得太复杂', note: '烧味、滑蛋和热饮，一份简单的满足。', group: 'food' },
  { id: 5, shopId: 5, image: 12, label: '松弛时刻', title: '不赶路的周末，去小院坐一会儿', note: '带本书，把“没安排”也当成一种安排。', group: 'relax' },
  { id: 6, shopId: 6, image: 13, label: '朋友相聚', title: '把聊天的时间，留给这顿晚饭', note: '老朋友好久不见，这次就约在河岸餐桌。', group: 'food' },
];
export const vouchers = [
  { id: 101, shopId: 1, title: '100 元店铺代金券', subtitle: '朋友聚餐，先把优惠备好', payValue: 8800, actualValue: 10000, type: 'normal', state: 'active', stock: 30, rules: ['满 100 元可用，每次消费限用一张', '不可与店内其他优惠同时使用', '演示有效期：创建后 30 天；本页面不发放真实券'] },
  { id: 102, shopId: 2, title: '双人下午茶体验券', subtitle: '散步之后，一起喝杯茶', payValue: 5900, actualValue: 8800, type: 'seckill', state: 'active', stock: 12, rules: ['双人茶饮套餐，演示时段为全天', '每位演示用户限创建一笔该券订单', '仅演示领取流程，不代表真实秒杀或购买'] },
  { id: 103, shopId: 3, title: '200 元店铺代金券', subtitle: '把仪式感，留给下一顿晚餐', payValue: 16800, actualValue: 20000, type: 'normal', state: 'active', stock: 20, rules: ['满 200 元可用，每次消费限用一张', '演示有效期：创建后 30 天', '本页面不包含支付和真实券核销'] },
  { id: 104, shopId: 4, title: '50 元店铺代金券', subtitle: '这一顿午饭，吃得实在些', payValue: 3900, actualValue: 5000, type: 'seckill', state: 'active', stock: 8, rules: ['满 50 元可用', '每位演示用户限创建一笔该券订单', '仅演示限量和重复请求提示'] },
  { id: 105, shopId: 6, title: '双人分享餐体验券', subtitle: '下一次相聚，已经安排好了', payValue: 15800, actualValue: 19800, type: 'seckill', state: 'upcoming', stock: 15, rules: ['当前演示状态为尚未开始', '上线后的真实起止时间由后端返回'] },
  { id: 106, shopId: 1, title: '周末限定尝鲜券', subtitle: '这一场优惠，暂时告一段落', payValue: 6900, actualValue: 10000, type: 'seckill', state: 'ended', stock: 0, rules: ['当前演示状态为已结束，不可创建订单'] },
];
