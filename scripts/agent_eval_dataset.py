"""团购Agent评测集离线校验；不读.env，不访问模型、数据库、Redis或MQ。"""

import argparse
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from agent.schemas import ConversationState, Preferences
from backend.app.schemas.group_buys import Requirements, UsageRule

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "scripts/data/agent_eval/v1.1"
TOOLS = {
    "update_goal",
    "discover_options",
    "search_group_buys",
    "get_group_buy_detail",
    "evaluate_plan",
    "compare_group_buys",
    "read_previous_choices",
    "prepare_purchase",
    "get_purchase_status",
    "present_plan",
    "finish",
}
STATE_PATHS = {f"requirements.{k}" for k in Requirements.model_fields} | {
    f"preferences.{k}" for k in Preferences.model_fields
}
VARIABLES = {"TODAY", "TOMORROW", "SATURDAY", "MONDAY"}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StateCheck(Contract):
    path: str
    op: Literal["eq", "one_of", "preserved"] = "eq"
    value: JsonValue = None

    @model_validator(mode="after")
    def validate_check(self):
        if self.path not in STATE_PATHS:
            raise ValueError("状态检查路径不属于现有Requirements/Preferences")
        if self.op == "one_of" and (not isinstance(self.value, list) or not self.value):
            raise ValueError("one_of需要非空备选值数组")
        if self.op == "preserved" and self.value is not None:
            raise ValueError("preserved比较上轮值，不填写固定value")
        return self


class SelectionCheck(Contract):
    minimum_count: int = Field(default=0, ge=0, le=8)
    maximum_count: int = Field(default=8, ge=0, le=8)
    include: list[int] = Field(default_factory=list)
    exclude: list[int] = Field(default_factory=list)
    not_eligible: list[int] = Field(default_factory=list)
    eligible: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_range(self):
        if self.minimum_count > self.maximum_count or set(self.include) & set(self.exclude):
            raise ValueError("候选范围或包含/排除条件冲突")
        if len(self.include) > self.maximum_count:
            raise ValueError("要求的候选数超过上限")
        if set(self.eligible) & (set(self.exclude) | set(self.not_eligible)):
            raise ValueError("已核验适用不能同时排除或要求未知")
        return self


class ProposalCheck(Contract):
    mode: Literal["absent", "present", "optional"] = "absent"
    voucher_id: int | None = None
    price_fen: int | None = Field(default=None, gt=0)
    verdict: Literal["eligible", "unknown"] | None = None

    @model_validator(mode="after")
    def validate_proposal(self):
        if self.mode != "present" and any(
            x is not None for x in (self.voucher_id, self.price_fen, self.verdict)
        ):
            raise ValueError("固定报价断言仅用于present")
        return self


class Event(Contract):
    kind: Literal["set_stock", "set_price", "patch_usage", "purchase_history"]
    voucher_id: int
    value: JsonValue

    @model_validator(mode="after")
    def validate_value(self):
        if self.kind in {"set_stock", "set_price"}:
            if type(self.value) is not int or self.value < (1 if self.kind == "set_price" else 0):
                raise ValueError("库存为非负整数，价格为正整数分")
        elif self.kind == "patch_usage":
            if not isinstance(self.value, dict) or not self.value:
                raise ValueError("规则补丁需要非空对象")
            UsageRule.model_validate(self.value)
        elif not isinstance(self.value, dict) or self.value.get("status") not in {
            "pending",
            "submitting",
            "failed",
            "succeeded",
        }:
            raise ValueError("购买历史必须明确合成状态")
        return self


class Turn(Contract):
    message: str = Field(min_length=1, max_length=2000)
    events_before: list[Event] = Field(default_factory=list)
    state: list[StateCheck] = Field(default_factory=list)
    selection: SelectionCheck = Field(default_factory=SelectionCheck)
    proposal: ProposalCheck = Field(default_factory=ProposalCheck)
    observe_any: list[str] = Field(default_factory=list)
    max_questions: int = Field(default=1, ge=0, le=2)
    review: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)


class Task(Contract):
    id: str = Field(pattern=r"^(DEV|RES)-\d{3}$")
    title: str
    split: Literal["development", "reserved"]
    family: str
    origin: Literal["observed_failure", "existing_regression", "authored"]
    source_note: str
    initial_state: dict[str, JsonValue] = Field(default_factory=dict)
    turns: list[Turn] = Field(min_length=1, max_length=6)


def digest(path: Path) -> str:
    # 1. 统一换行后计算摘要，Windows检出换行不应被误判为业务数据变化。
    return hashlib.sha256(path.read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()


def render(value, anchor: date):
    # 2. 所有任务共享同一运行锚点；周六始终取未来一天，周一为随后周一。
    saturday = anchor + timedelta(days=(5 - anchor.weekday()) % 7 or 7)
    dates = {
        "TODAY": anchor,
        "TOMORROW": anchor + timedelta(days=1),
        "SATURDAY": saturday,
        "MONDAY": saturday + timedelta(days=2),
    }
    if isinstance(value, str):
        names = re.findall(r"\{\{([A-Z_]+)\}\}", value)
        if set(names) - VARIABLES:
            raise ValueError("存在未定义的日期变量")
        for name in names:
            value = value.replace("{{" + name + "}}", dates[name].isoformat())
    elif isinstance(value, list):
        value = [render(item, anchor) for item in value]
    elif isinstance(value, dict):
        value = {key: render(item, anchor) for key, item in value.items()}
    return value


def load_dataset(directory: Path = DEFAULT_DATASET, root: Path = ROOT):
    # 3. manifest只引用项目内显式文件；校验摘要，不执行数据里描述的事件。
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1 or manifest["status"] != "prepared_not_run":
        raise ValueError("未知数据版本或状态")
    for relative, checksum in manifest["source_sha256"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path) != checksum:
            raise ValueError("业务种子来源摘要不匹配；需审查后版本化更新")
    if digest(directory / "rubrics.json") != manifest["rubrics_sha256"]:
        raise ValueError("人工评分标准摘要不匹配")
    rubrics = json.loads((directory / "rubrics.json").read_text(encoding="utf-8"))
    voucher_ids = set(manifest["voucher_ids"])
    tasks, seen = [], set()
    for split, descriptor in manifest["splits"].items():
        path = (directory / descriptor["file"]).resolve()
        if not path.is_relative_to(directory.resolve()) or digest(path) != descriptor["sha256"]:
            raise ValueError("任务文件摘要不匹配")
        rows = json.loads(path.read_text(encoding="utf-8"))
        if len(rows) != descriptor["tasks"]:
            raise ValueError("任务数量与manifest不符")
        if sum(len(row["turns"]) for row in rows) != descriptor["turns"]:
            raise ValueError("轮次数量与manifest不符")
        for row in rows:
            task = Task.model_validate(row)
            if task.id in seen or task.split != split:
                raise ValueError("任务ID重复或分组不符")
            seen.add(task.id)
            # 4. 仅用固定离线日期展开以检查合同；不代表实际运行时冻结了系统时间。
            ConversationState.model_validate(render(task.initial_state, date(2026, 10, 19)))
            for index, turn in enumerate(task.turns):
                rendered = render(turn.model_dump(), date(2026, 10, 19))
                if set(turn.observe_any) - TOOLS or set(turn.review) - rubrics.keys():
                    raise ValueError("工具或人工评分项未注册")
                ids = (
                    turn.selection.include
                    + turn.selection.exclude
                    + turn.selection.not_eligible
                    + turn.selection.eligible
                )
                ids += [event.voucher_id for event in turn.events_before]
                if turn.proposal.voucher_id is not None:
                    ids.append(turn.proposal.voucher_id)
                if set(ids) - voucher_ids or any(type(v) is not int for v in ids):
                    raise ValueError("引用了未知优惠券ID")
                for check in rendered["state"]:
                    if check["op"] == "preserved" and index == 0 and not task.initial_state:
                        raise ValueError("首轮preserved需要显式初始状态")
                    if check["op"] != "preserved":
                        group, field = check["path"].split(".")
                        contract = Requirements if group == "requirements" else Preferences
                        values = check["value"] if check["op"] == "one_of" else [check["value"]]
                        for value in values:
                            TypeAdapter(
                                contract.model_fields[field].rebuild_annotation()
                            ).validate_python(value)
            tasks.append(task)
    if len(tasks) != manifest["task_count"]:
        raise ValueError("总任务数不符")
    if sum(len(task.turns) for task in tasks) != manifest["turn_count"]:
        raise ValueError("总轮次数不符")
    return manifest, tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--split", choices=["development", "reserved", "all"], default="development"
    )
    args = parser.parse_args()
    manifest, tasks = load_dataset()
    selected = [t for t in tasks if args.split == "all" or t.split == args.split]
    print(
        json.dumps(
            {
                "dataset": manifest["dataset_id"],
                "status": "validated_not_executed",
                "split": args.split,
                "tasks": len(selected),
                "turns": sum(len(t.turns) for t in selected),
                "families": sorted({t.family for t in selected}),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
