"""结构化团购检索与规则判定，可直接供HTTP和Agent工具复用。"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import ApiError
from backend.app.models import Shop, Voucher, VoucherUsage
from backend.app.repositories.vouchers import voucher_query
from backend.app.schemas.group_buys import (
    Evaluation,
    GroupBuy,
    Requirements,
    RuleIssue,
    SearchFeedback,
    SearchInput,
    SearchResult,
    UsageRule,
)
from backend.app.services.vouchers import voucher_out

TIANJIN = ZoneInfo("Asia/Shanghai")


def group_buy(row, now: datetime) -> GroupBuy:
    voucher, shop, activity, usage = row
    out = voucher_out((voucher, shop, activity), now)
    try:
        spec = UsageRule.model_validate(usage.spec) if usage else UsageRule()
    except ValidationError:
        spec = UsageRule(note="结构化规则不合法，需人工核对")
    return GroupBuy(
        voucher_id=voucher.id,
        shop_id=shop.id,
        title=voucher.title,
        shop_name=shop.name,
        city=shop.city,
        area=shop.area,
        category=shop.kind,
        image=shop.images[0],
        pay_value=voucher.pay_value,
        face_value=voucher.actual_value,
        stock_snapshot=voucher.stock,
        sale_state=out.state,
        validity_days=voucher.validity_days,
        rules_text=voucher.rules,
        usage=spec,
        checked_at=now.isoformat(),
        score=float(shop.score),
    )


def query():
    return (
        voucher_query()
        .add_columns(VoucherUsage)
        .outerjoin(VoucherUsage, VoucherUsage.voucher_id == Voucher.id)
    )


async def discover(session: AsyncSession, need: Requirements) -> dict:
    # 只发现当前硬区域/预算内的目录，不假装库存或使用规则已确认，也不修改消费目标。
    conditions = [Shop.city == need.city]
    if need.area:
        conditions.append(Shop.area == {"南开区": "南开街区"}.get(need.area, need.area))
    if need.budget_fen:
        conditions.append(Voucher.pay_value <= need.budget_fen)
    rows = (
        await session.execute(
            select(
                Shop.kind,
                Shop.area,
                func.count(Voucher.id).label("voucher_count"),
                func.count(func.distinct(Shop.id)).label("shop_count"),
            )
            .join(Voucher, Voucher.shop_id == Shop.id)
            .where(*conditions)
            .group_by(Shop.kind, Shop.area)
            .order_by(Shop.kind, Shop.area)
            .limit(41)
        )
    ).all()
    # 类目解释必须知道原词是否本来就有字面匹配；此处不按预算/区域过滤，防止用映射绕开约束。
    literal_match = None
    if need.category:
        literal_match = (
            await session.scalar(
                select(Voucher.id)
                .join(Shop, Voucher.shop_id == Shop.id)
                .where(
                    or_(
                        Shop.kind.contains(need.category, autoescape=True),
                        Shop.name.contains(need.category, autoescape=True),
                        Voucher.title.contains(need.category, autoescape=True),
                    )
                )
                .limit(1)
            )
            is not None
        )
    return {
        "options": [
            dict(
                category=row.kind,
                area=row.area,
                voucher_count=row.voucher_count,
                shop_count=row.shop_count,
            )
            for row in rows[:40]
        ],
        "truncated": len(rows) > 40,
        "category_expression": need.category,
        "literal_catalog_match": literal_match,
        "note": (
            "当前城市、硬区域和预算内的目录发现，未按品类过滤；"
            "voucher_count是券数量，shop_count是不同门店数，不能混淆；"
            "不是适用性或实时库存证明。无定位能力。"
            "原词无字面匹配时可从本目录选择相关类目探索，原消费意图保持，相关不等于已核实。"
        ),
    }


async def search(session: AsyncSession, args: SearchInput) -> SearchResult:
    # 1. 使用参数化查询；字符串按字面包含匹配，不把用户输入当SQL或LIKE通配符。
    need = args.requirements
    filters = {}
    if need.area:
        area = {"南开区": "南开街区"}.get(need.area, need.area)
        filters["area"] = Shop.area == area
    if need.category:
        filters["category"] = (
            Shop.kind == need.category
            if need.category_matching == "literal"
            else Shop.kind.in_(need.category_matches)
            if need.category_matches
            else or_(
                Shop.kind.contains(need.category, autoescape=True),
                Shop.name.contains(need.category, autoescape=True),
                Voucher.title.contains(need.category, autoescape=True),
            )
        )
    if need.budget_fen:
        filters["budget_fen"] = Voucher.pay_value <= need.budget_fen
    # 原品类已由专门条件表达，重复词不再叠加；相关类目探索也不能被旧原词再次挡住。
    keywords = [word for word in args.keywords if word != need.category]
    if keywords:
        # 探索词只追加过滤，不替换已保存硬条件；模型不能通过工具参数扩大用户预算/区域。
        filters["keywords"] = or_(
            *[
                column.contains(word, autoescape=True)
                for word in keywords
                for column in (Shop.kind, Shop.name, Voucher.title)
            ]
        )
    conditions = [Shop.city == need.city, *filters.values()]
    total = await session.scalar(
        select(func.count())
        .select_from(Voucher)
        .join(Shop, Voucher.shop_id == Shop.id)
        .where(*conditions)
    )
    feedback = SearchFeedback(code="MATCHES", message="有目录候选，须按使用条件逐项核验")
    if total == 0:
        # 2. 仅零结果追加一次聚合；每列只移除一个过滤项，其余硬条件及城市保留。
        # 数量只是诊断，不向Agent提供越界候选，不擅自改预算/区域或推断唯一冲突。
        labels = list(filters)
        counts = (
            await session.execute(
                select(
                    func.count(Voucher.id),
                    *[
                        func.count(
                            case(
                                (
                                    and_(
                                        True,
                                        *[
                                            condition
                                            for name, condition in filters.items()
                                            if name != omitted
                                        ],
                                    ),
                                    1,
                                )
                            )
                        )
                        for omitted in labels
                    ],
                )
                .select_from(Voucher)
                .join(Shop, Voucher.shop_id == Shop.id)
                .where(Shop.city == need.city)
            )
        ).one()
        feedback = SearchFeedback(
            code="NO_MATCH",
            message=(
                "原查询没有目录匹配；counts_if_omitted仅为单项诊断，多个条件可能共同冲突。"
                "额外探索词可调整，用户硬条件只能经用户明确修改；日期/人数未参与本次目录过滤。"
            ),
            city_catalog_count=counts[0],
            counts_if_omitted=dict(zip(labels, counts[1:], strict=True)),
            requires_user_permission=[name for name in labels if name != "keywords"],
        )
    # 软偏好只改变排序，候选不足时仍能看到其他区域；不把“优先”当作硬过滤。
    ordering = []
    if args.preferred_area:
        area = {"南开区": "南开街区"}.get(args.preferred_area, args.preferred_area)
        ordering.append(case((Shop.area == area, 0), else_=1))
    if args.preferred_category:
        ordering.append(
            case((Shop.kind.contains(args.preferred_category, autoescape=True), 0), else_=1)
        )
    ordering.extend(
        [Shop.score.desc(), Voucher.pay_value, Voucher.id]
        if args.sort == "score"
        else [Voucher.pay_value, Voucher.id]
    )
    rows = (
        await session.execute(
            query()
            .where(*conditions)
            .order_by(*ordering)
            .offset((args.page - 1) * args.page_size)
            .limit(args.page_size)
        )
    ).all()
    now = datetime.now(UTC)
    if total and not rows:
        feedback = SearchFeedback(
            code="EMPTY_PAGE", message="本页超出结果范围，不能解释为全局无结果"
        )
    # 2. 不把检索命中当成使用资格通过；库存和活动只是本次快照。
    return SearchResult(
        items=[group_buy(row, now) for row in rows],
        total=int(total or 0),
        page=args.page,
        page_size=args.page_size,
        feedback=feedback,
    )


async def detail(session: AsyncSession, voucher_id: int) -> GroupBuy:
    row = (await session.execute(query().where(Voucher.id == voucher_id))).first()
    if row is None:
        raise ApiError(404, "VOUCHER_NOT_FOUND", "优惠券不存在")
    return group_buy(row, datetime.now(UTC))


def evaluate(item: GroupBuy, need: Requirements, now: datetime | None = None) -> Evaluation:
    # 1. 所有金额按整数分计算；先验证销售快照和本次明确条件。
    now = now or datetime.now(UTC)
    local = now.astimezone(TIANJIN)
    rule = item.usage
    reasons, unknowns, issues = [], [], []

    def issue(code, message, source, *fields):
        # 机器码与中文理由同源生成；区分用户未决定和商家缺资料，避免向用户追问不存在的规则。
        (reasons if source == "conflict" else unknowns).append(message)
        issues.append(RuleIssue(code=code, message=message, source=source, fields=list(fields)))

    if item.sale_state != "active":
        issue(
            "SALE_UNAVAILABLE",
            f"当前销售状态为{item.sale_state}，不能保证现在可买",
            "conflict",
            "sale_state",
        )
    if item.city != need.city:
        issue("CITY_CONFLICT", "城市不符合要求", "conflict", "city")
    area = {"南开区": "南开街区"}.get(need.area, need.area)
    if area and item.area != area:
        issue("AREA_CONFLICT", "区域不符合要求", "conflict", "area")
    category_satisfied = not need.category or any(
        need.category in value for value in (item.category, item.shop_name, item.title)
    )
    if need.category_matching == "literal":
        category_satisfied = item.category == need.category
    if need.category and not category_satisfied:
        if need.category_matches and item.category in need.category_matches:
            issue(
                "CATEGORY_RELATED_UNVERIFIED",
                f"按相关类目“{item.category}”探索；尚无菜品资料证实符合“{need.category}”",
                "verification",
                "category",
            )
        else:
            issue("CATEGORY_CONFLICT", "品类不符合要求", "conflict", "category")
    if need.people is None:
        issue("PEOPLE_UNSPECIFIED", "使用人数尚未确定，可先作为候选", "user", "people")
    elif rule.min_people is None or rule.max_people is None:
        issue("PEOPLE_RULE_MISSING", "适用人数未知", "merchant", "min_people", "max_people")
    elif not rule.min_people <= need.people <= rule.max_people:
        issue("PEOPLE_CONFLICT", "套餐适用人数不符合要求", "conflict", "people")
    # 2. 购买窗口与使用日期分开；有效期按当前假定下单时刻估算，不承诺未来仍可买。
    if need.use_date is None:
        issue("DATE_UNSPECIFIED", "具体使用日期尚未确定", "user", "use_date")
    else:
        if need.use_date < local.date():
            issue("DATE_PAST", "使用日期已经过去", "conflict", "use_date")
        expiry = (now + timedelta(days=item.validity_days)).astimezone(TIANJIN)
        if need.use_date > expiry.date():
            issue("EXPIRY_CONFLICT", "使用日期超过假定现在下单的有效期", "conflict", "use_date")
        elif need.use_date == expiry.date():
            issue(
                "EXPIRY_PRECISION_UNKNOWN",
                "使用日期位于有效期末日，需核对精确到期时刻",
                "verification",
                "use_date",
            )
        if rule.allowed_weekdays is None:
            issue("WEEKDAY_RULE_MISSING", "可使用星期未知", "merchant", "allowed_weekdays")
        elif need.use_date.weekday() not in rule.allowed_weekdays:
            issue("WEEKDAY_CONFLICT", "指定星期不可使用", "conflict", "use_date")
    if need.use_weekdays and rule.allowed_weekdays is None and need.use_date is None:
        issue("WEEKDAY_RULE_MISSING", "可使用星期未知", "merchant", "allowed_weekdays")
    if need.use_weekdays and rule.allowed_weekdays is not None:
        if not set(need.use_weekdays).intersection(rule.allowed_weekdays):
            issue("WEEKDAY_CONFLICT", "用户要求的星期范围均不可使用", "conflict", "use_weekdays")
    if rule.holiday_policy == "unknown":
        issue(
            "HOLIDAY_RULE_MISSING",
            "节假日适用规则未明确，不能保证指定日期可用",
            "merchant",
            "holiday_policy",
        )
    if rule.daily_start is None:
        issue("TIME_RULE_MISSING", "每日使用时段未知", "merchant", "daily_start", "daily_end")
    elif need.use_time is None:
        issue(
            "TIME_UNSPECIFIED",
            f"请确认使用时间在{rule.daily_start:%H:%M}至{rule.daily_end:%H:%M}内",
            "user",
            "use_time",
        )
    elif need.use_time.tzinfo:
        issue("TIME_ZONE_CONFLICT", "使用时间应为北京时间，不附时区", "conflict", "use_time")
    elif not rule.daily_start <= need.use_time < rule.daily_end:
        issue("TIME_CONFLICT", "使用时间不在允许时段内", "conflict", "use_time")
    if need.use_date == local.date() and need.use_time and not need.use_time.tzinfo:
        if need.use_time <= local.time().replace(tzinfo=None):
            issue("TIME_PAST", "今天指定的使用时间已经过去", "conflict", "use_time")
    # 3. 代金券面额不是整餐价格；无法确定补差价时保持unknown，不能拿人均估算冒充报价。
    total = None
    if rule.benefit_kind != "package" or rule.covers_meal is not True:
        issue(
            "COVERAGE_RULE_MISSING",
            "套餐覆盖范围或消费总金额尚未明确",
            "merchant",
            "benefit_kind",
            "covers_meal",
        )
    elif rule.extra_cost_fen is None:
        issue("EXTRA_COST_MISSING", "额外费用未知", "merchant", "extra_cost_fen")
    else:
        total = item.pay_value + rule.extra_cost_fen
    if need.budget_fen is None:
        issue("BUDGET_UNSPECIFIED", "整单预算上限尚未确定，可先比较价格", "user", "budget_fen")
    elif item.pay_value > need.budget_fen or (total is not None and total > need.budget_fen):
        issue("BUDGET_CONFLICT", "金额超过整单预算", "conflict", "budget_fen")
    if rule.reservation_required is None:
        issue("RESERVATION_RULE_MISSING", "是否需要预约未知", "merchant", "reservation_required")
    elif rule.reservation_required:
        if need.allow_reservation is False:
            issue(
                "RESERVATION_CONFLICT", "需要预约，与用户要求冲突", "conflict", "allow_reservation"
            )
        elif need.allow_reservation is None:
            issue(
                "RESERVATION_UNSPECIFIED", "需要预约，请确认是否接受", "user", "allow_reservation"
            )
    verdict = "ineligible" if reasons else "unknown" if unknowns else "eligible"
    return Evaluation(
        voucher=item,
        verdict=verdict,
        reasons=reasons,
        unknowns=unknowns,
        total_cost_fen=total,
        issues=issues,
    )
