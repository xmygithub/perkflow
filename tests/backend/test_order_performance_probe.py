"""观察器只验证计时、上下文隔离、原返回值/异常及证据公式，不发起业务购买。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts.order_perf_probe import CURRENT, Recorder, async_span
from scripts.order_perf_report import distribution


def test_quantiles_are_nearest_rank():
    values = list(range(1, 101))
    result = distribution(values)
    assert (result["p50_ms"], result["p95_ms"], result["p99_ms"]) == (50, 95, 99)
    assert distribution([]) == {"count": 0}


@pytest.mark.asyncio
async def test_scope_and_return_values_are_preserved(tmp_path):
    recorder = Recorder(tmp_path / "events.jsonl", "test")

    async def inner(identifier):
        await asyncio.sleep(0)
        assert CURRENT.get()["request_id"] == identifier
        return {"id": identifier}

    owner = SimpleNamespace(work=inner)
    async_span(recorder, owner, "work", "stage", identifier=lambda a: a[0], scope=True)
    results = await asyncio.gather(owner.work("1"), owner.work("2"))
    assert results == [{"id": "1"}, {"id": "2"}]
    assert CURRENT.get() is None
    recorder.close()
    rows = [json.loads(line) for line in recorder.path.read_text().splitlines()]
    assert {row["request_id"] for row in rows} == {"1", "2"}
    assert all(row["ok"] and row["end_ns"] >= row["start_ns"] for row in rows)
    assert json.loads(recorder.path.with_suffix(".meta.json").read_text())["dropped"] == 0


@pytest.mark.asyncio
async def test_exceptions_are_recorded_and_rethrown(tmp_path):
    recorder = Recorder(tmp_path / "events.jsonl", "test")
    failure = ValueError("private value must not be recorded")

    async def fail():
        raise failure

    owner = SimpleNamespace(work=fail)
    async_span(recorder, owner, "work", "stage", scope=True)
    with pytest.raises(ValueError) as caught:
        await owner.work()
    assert caught.value is failure
    recorder.close()
    row = json.loads(recorder.path.read_text())
    assert row["ok"] is False and row["error"] == "ValueError"
    assert "private value" not in recorder.path.read_text()


def test_default_business_path_does_not_import_probe():
    from scripts.loadtest_contract import ROOT

    for path in (ROOT / "backend/app").rglob("*.py"):
        assert "order_perf_probe" not in path.read_text(encoding="utf-8")


def test_buffer_overflow_is_reported(tmp_path):
    recorder = Recorder(tmp_path / "events.jsonl", "test", capacity=1)
    recorder.emit("one")
    recorder.emit("two")
    recorder.close()
    meta = json.loads(recorder.path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["dropped"] == 1 and meta["written"] == 1


def test_missing_stages_cannot_pass_coverage(tmp_path):
    from scripts.order_perf_report import analyze

    (tmp_path / "manifest.json").write_text('{"attempts":1}', encoding="utf-8")
    (tmp_path / "observations.json").write_text(
        json.dumps({"attempts": [{"request_id": "1", "order_id": "1", "outcome": "succeeded"}]}),
        encoding="utf-8",
    )
    (tmp_path / "queue-series.json").write_text("[]", encoding="utf-8")
    result = analyze(tmp_path)
    assert not result["coverage_pass"]
    assert result["missing_request_ids"] == ["1"]


def test_duplicate_delivery_does_not_mix_commit_spans():
    from scripts.order_perf_report import creation_transactions

    creator = {"start_ns": 0, "end_ns": 100, "publish_id": "a"}
    duplicate = {"start_ns": 10, "end_ns": 90, "publish_id": "b"}
    commit_a = {"start_ns": 70, "end_ns": 80, "publish_id": "a", "ok": True}
    commit_b = {"start_ns": 50, "end_ns": 60, "publish_id": "b", "ok": True}
    grouped = {
        "order_transaction": [duplicate, creator],
        "mysql_commit": [commit_b, commit_a],
        "sql": [
            {"operation": "INSERT", "table": "tb_voucher_order", "start_ns": 40, "publish_id": "a"}
        ],
    }
    assert creation_transactions(grouped) == [(creator, commit_a)]
