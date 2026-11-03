"""业务观察的行动边界；只提供事实与可选方向，不代替模型选择下一步。"""


def purchase_feedback(items):
    # 1. 账号任一未决意图都优先保留，旧失败不能覆盖正在处理的新请求。
    unresolved = [row.get("id") for row in items if row.get("status") in {"submitting", "pending"}]
    records = []
    for row in items:
        status, error = row.get("status"), row.get("error_code")
        if status in {"submitting", "pending"}:
            code, options = "WAIT_OR_CHECK_ORIGINAL", ["回查原编号", "说明当前仍未决"]
        elif status == "succeeded":
            code, options = "ORDER_CREATED", ["报告实际订单状态，不宣称支付或核销"]
        elif status == "failed":
            if error == "SECKILL_NOT_READY":
                code, options = (
                    "PREPARATION_REQUIRED",
                    ["说明购买服务未准备", "准备后重新核对并授权"],
                )
            elif error == "OUT_OF_STOCK":
                code, options = (
                    "ALTERNATIVE_SEARCH_ALLOWED",
                    ["查询仍满足硬条件的替代券", "说明无替代结果"],
                )
            elif error in {"PURCHASE_TERMS_CHANGED", "VOUCHER_OFF_SHELF", "SECKILL_ENDED"}:
                code, options = (
                    "RECHECK_OR_ALTERNATIVE",
                    ["重查当前商品", "探索仍满足硬条件的替代券"],
                )
            else:
                code, options = "FAILURE_REVIEW", ["解释实际失败原因", "按需要查询当前商品或备选"]
        else:
            code, options = "STATUS_UNKNOWN", ["核对原记录，不推断失败"]
        records.append({"id": row.get("id"), "code": code, "options": options})
    return {
        "can_prepare_new_purchase": not unresolved,
        "unresolved_ids": unresolved,
        "records": records,
        "boundary": "选项不是执行指令；新购买均需新报价和页面授权，未决结果不重新购买",
    }


def assessment(result):
    # 2. 保留具体原因与来源，不把unknown加工成已验证，也不把一页结论当全库结论。
    return {
        "voucher_id": result.voucher.voucher_id,
        "verdict": result.verdict,
        "issues": [issue.model_dump() for issue in result.issues],
        "total_cost_fen": result.total_cost_fen,
    }


def decision_feedback(results, search_feedback=None):
    # 3. 明确本次解决的适用性问题及未知来源；可选方向不是必须依次执行的流程。
    groups = {"eligible": [], "unknown": [], "ineligible": []}
    fields = {"user": set(), "merchant": set(), "conflict": set(), "verification": set()}
    for result in results:
        groups[result.verdict].append(result.voucher.voucher_id)
        for issue in result.issues:
            fields[issue.source].update(issue.fields)
    code = search_feedback.code if search_feedback else "ASSESSED"
    choices = bool(groups["eligible"] or groups["unknown"])
    options = []
    if code == "EMPTY_PAGE":
        options.append("调整分页或用已有候选，不把越页当全局无结果")
    elif code == "NO_MATCH":
        options += ["结合单项诊断调整探索词", "说明冲突，用户明确修改后才改硬条件"]
    elif choices:
        options += ["用已有事实展示候选或比较取舍", "已有核验足够时结束，无需重复evaluate"]
    elif groups["ineligible"]:
        options.append("探索其他仍满足硬条件的候选，或说明无法满足")
    if fields["merchant"]:
        options.append("商家缺资料不能靠追问用户补齐；保留未知或看其他候选")
    if fields["user"]:
        options.append("仅当未知会影响下一步选择时合并追问，也可先展示候选")
    if fields["verification"]:
        options.append("说明核验精度边界，不能将未知宣称适用")
    return {
        "code": code,
        "resolved": {
            "NO_MATCH": "完整过滤条件下目录无匹配，诊断没有放宽原约束",
            "EMPTY_PAGE": "本页为空，不能据此判断整个目录无结果",
        }.get(code, "已按当前硬条件判定本次读取商品，未知不等于适用"),
        "eligible_ids": groups["eligible"],
        "candidate_ids": groups["unknown"],
        "conflicting_ids": groups["ineligible"],
        "remaining_fields_by_source": {key: sorted(value) for key, value in fields.items()},
        "can_finish_with_candidates": choices,
        "options": options,
        "scope": "仅本次资料与当前目标，不证明全库最优或未来库存；最终返回与报价仍复核",
    }
