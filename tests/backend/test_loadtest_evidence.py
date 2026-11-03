"""Web尚未退出时的日志恢复，以及半行/冲突/重复证据不得冒充完成。"""

import json

import pytest

from scripts.loadtest_contract import read_attempts, write_json


@pytest.fixture
def evidence(tmp_path):
    manifest = {"run_id": "a" * 12, "attempts": 2}
    rows = [
        {"ticket": 0, "outcome": "succeeded", "order_id": "100"},
        {"ticket": 1, "outcome": "rejected", "order_id": None},
    ]
    return tmp_path, manifest, rows


def journal(folder, rows):
    (folder / "attempts.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_complete_journal_without_process_exit(evidence):
    folder, manifest, rows = evidence
    journal(folder, rows)
    actual, diagnostic = read_attempts(folder, manifest)
    assert actual == rows
    assert diagnostic == {"source": "attempts.jsonl", "valid": True, "issues": []}
    assert not (folder / "observations.json").exists()


def test_newer_journal_can_extend_stale_snapshot(evidence):
    folder, manifest, rows = evidence
    write_json(folder / "observations.json", {"run_id": manifest["run_id"], "attempts": rows[:1]})
    journal(folder, rows)
    actual, diagnostic = read_attempts(folder, manifest)
    assert actual == rows and diagnostic["valid"]


def test_partial_trailing_line_is_not_a_completed_attempt(evidence):
    folder, manifest, rows = evidence
    journal(folder, rows[:1])
    with (folder / "attempts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"ticket":1')
    actual, diagnostic = read_attempts(folder, manifest)
    assert actual == rows[:1]
    assert diagnostic["issues"] == ["INCOMPLETE_JOURNAL_TAIL"]
    assert not diagnostic["valid"]


@pytest.mark.parametrize("kind", ["duplicate", "out_of_range", "unknown_outcome", "wrong_run"])
def test_invalid_evidence_is_never_counted_as_complete(evidence, kind):
    folder, manifest, rows = evidence
    if kind == "duplicate":
        rows[1]["ticket"] = 0
    elif kind == "out_of_range":
        rows[1]["ticket"] = 2
    elif kind == "unknown_outcome":
        rows[1]["outcome"] = "invented_success"
    write_json(
        folder / "observations.json",
        {
            "run_id": "b" * 12 if kind == "wrong_run" else manifest["run_id"],
            "attempts": rows,
        },
    )
    actual, diagnostic = read_attempts(folder, manifest)
    assert actual == [] and not diagnostic["valid"]


def test_conflicting_journal_cannot_silently_replace_snapshot(evidence):
    folder, manifest, rows = evidence
    write_json(folder / "observations.json", {"run_id": manifest["run_id"], "attempts": rows})
    journal(folder, [dict(rows[0], order_id="999"), rows[1]])
    _, diagnostic = read_attempts(folder, manifest)
    assert not diagnostic["valid"]
    assert "CONFLICTING_OBSERVATION_SOURCES" in diagnostic["issues"]
