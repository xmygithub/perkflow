"""离线验证评测资产；不调用模型，不创建数据库或交易资源。"""

import json
import shutil
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from backend.app.schemas.group_buys import GroupBuy, Requirements, UsageRule
from backend.app.services.group_buys import evaluate
from scripts.agent_eval_dataset import (
    DEFAULT_DATASET,
    ROOT,
    Event,
    SelectionCheck,
    StateCheck,
    digest,
    load_dataset,
    render,
)


def test_prepared_dataset_has_expected_splits_and_real_failure_regressions():
    manifest, tasks = load_dataset()
    assert manifest["status"] == "prepared_not_run"
    assert len(tasks) == 30 and sum(len(task.turns) for task in tasks) == 69
    assert sum(task.split == "development" for task in tasks) == 22
    assert sum(task.split == "reserved" for task in tasks) == 8
    tea = next(task for task in tasks if task.id == "DEV-008")
    assert tea.origin == "observed_failure"
    assert any(
        check.path == "requirements.people" and check.value == 2 for check in tea.turns[1].state
    )
    assert tea.turns[2].proposal.voucher_id == 102
    qualification = next(task for task in tasks if task.id == "DEV-013")
    assert qualification.turns[0].events_before[0].value["status"] == "succeeded"
    assert all(turn.proposal.mode == "absent" for turn in qualification.turns)


def test_dates_render_from_same_anchor_and_saturday_is_always_future():
    result = render(
        {"message": "{{SATURDAY}}去", "dates": ["{{MONDAY}}", "{{TOMORROW}}"]}, date(2026, 10, 19)
    )
    assert result == {"message": "2026-10-24去", "dates": ["2026-10-26", "2026-10-20"]}
    assert render("{{SATURDAY}}", date(2026, 10, 24)) == "2026-10-31"
    with pytest.raises(ValueError, match="日期变量"):
        render("{{NEXT_FRIDAY}}", date(2026, 10, 19))


@pytest.mark.parametrize(
    "check",
    [
        {"path": "requirements.fake", "value": 2},
        {"path": "requirements.people", "op": "one_of", "value": []},
        {"path": "requirements.people", "op": "preserved", "value": 2},
    ],
)
def test_invalid_state_check_contract_is_rejected(check):
    with pytest.raises(ValidationError):
        StateCheck.model_validate(check)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("set_stock", -1),
        ("set_stock", True),
        ("set_price", 0),
        ("patch_usage", {"min_people": 3, "max_people": 1}),
        ("patch_usage", {"invented_rule": True}),
        ("purchase_history", {"status": "maybe_failed"}),
    ],
)
def test_event_contract_rejects_undefined_or_invalid_fixture_changes(kind, value):
    with pytest.raises(ValidationError):
        Event(kind=kind, voucher_id=3001, value=value)


def test_eligible_cannot_be_excluded_or_unknown():
    with pytest.raises(ValidationError):
        SelectionCheck(eligible=[3001], not_eligible=[3001])


@pytest.fixture
def editable_dataset(tmp_path):
    directory = tmp_path / "dataset"
    shutil.copytree(DEFAULT_DATASET, directory)
    return directory


def rewrite_task(directory, change):
    # 仅改临时副本并更新摘要，以测试语义校验而不是停在摘要不符。
    path = directory / "development.json"
    tasks = json.loads(path.read_text(encoding="utf-8"))
    change(tasks)
    path.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["splits"]["development"]["sha256"] = digest(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_silent_dataset_edit_requires_explicit_manifest_update(editable_dataset):
    with (editable_dataset / "development.json").open("a", encoding="utf-8") as file:
        file.write(" ")
    with pytest.raises(ValueError, match="摘要"):
        load_dataset(editable_dataset)


def test_rubric_changes_also_require_version_review(editable_dataset):
    (editable_dataset / "rubrics.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="评分标准摘要"):
        load_dataset(editable_dataset)


def test_unknown_voucher_cannot_enter_gold(editable_dataset):
    rewrite_task(
        editable_dataset, lambda tasks: tasks[0]["turns"][0]["selection"].update(include=[999999])
    )
    with pytest.raises(ValueError, match="未知优惠券"):
        load_dataset(editable_dataset)


def test_duplicate_task_id_is_rejected_across_dataset(editable_dataset):
    rewrite_task(editable_dataset, lambda tasks: tasks[1].update(id=tasks[0]["id"]))
    with pytest.raises(ValueError, match="任务ID重复"):
        load_dataset(editable_dataset)


def test_invalid_gold_type_or_range_is_rejected(editable_dataset):
    rewrite_task(
        editable_dataset,
        lambda tasks: tasks[0]["turns"][0]["state"].append(
            {"path": "requirements.people", "op": "eq", "value": 0}
        ),
    )
    with pytest.raises(ValidationError):
        load_dataset(editable_dataset)


def test_unregistered_manual_rubric_is_rejected(editable_dataset):
    rewrite_task(
        editable_dataset,
        lambda tasks: tasks[0]["turns"][0]["review"].append("everything_looks_good"),
    )
    with pytest.raises(ValueError, match="评分项"):
        load_dataset(editable_dataset)


def test_preservation_needs_previous_turn_or_explicit_initial_state(editable_dataset):
    rewrite_task(
        editable_dataset,
        lambda tasks: tasks[0]["turns"][0]["state"].append(
            {"path": "requirements.people", "op": "preserved", "value": None}
        ),
    )
    with pytest.raises(ValueError, match="显式初始状态"):
        load_dataset(editable_dataset)


@pytest.mark.parametrize(
    ("identifier", "day", "price", "reservation", "verdict"),
    [
        (3001, "2026-10-24", None, None, "eligible"),
        (3002, "2026-10-24", None, None, "ineligible"),
        (3002, "2026-10-26", None, None, "eligible"),
        (3003, "2026-10-24", None, None, "ineligible"),
        (3004, "2026-10-24", None, None, "ineligible"),
        (3005, "2026-10-24", None, None, "unknown"),
        (3001, "2026-10-24", 18800, None, "ineligible"),
        (3001, "2026-10-24", None, True, "ineligible"),
    ],
)
def test_gold_rule_facts_match_existing_business_service(
    identifier, day, price, reservation, verdict
):
    # 使用真实种子规则与现有纯核验Service核对gold，不依赖模型，也不造新业务规则。
    entries = json.loads((ROOT / "scripts/data/group_buy_rules.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "scripts/data/tianjin_catalog.json").read_text(encoding="utf-8"))
    entry = next(row for row in entries if row["voucher"]["id"] == identifier)
    voucher = entry["voucher"]
    shop = next(row for row in catalog["shops"] if row["id"] == voucher["shop_id"])
    usage = dict(entry["usage"])
    if reservation is not None:
        usage["reservation_required"] = reservation
    now = datetime(2026, 10, 19, 2, tzinfo=UTC)
    item = GroupBuy(
        voucher_id=identifier,
        shop_id=shop["id"],
        title=voucher["title"],
        shop_name=shop["name"],
        city=shop["city"],
        area=shop["area"],
        category=shop["kind"],
        image=shop["images"][0],
        pay_value=price or voucher["pay_value"],
        face_value=voucher["actual_value"],
        stock_snapshot=voucher["stock"],
        sale_state="active",
        validity_days=voucher["validity_days"],
        rules_text=voucher["rules"],
        usage=UsageRule.model_validate(usage),
        checked_at=now.isoformat(),
        score=shop["score"],
    )
    need = Requirements(
        category="火锅",
        area="南开区",
        people=2,
        budget_fen=17000 if price else 20000,
        use_date=day,
        use_time="18:00",
        allow_reservation=False,
    )
    assert evaluate(item, need, now).verdict == verdict
