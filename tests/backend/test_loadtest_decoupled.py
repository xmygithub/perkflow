"""受控HTTP反馈验证发压与观察确实分离；不把伪服务计时当生产性能。"""

import asyncio
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from scripts.loadtest import start_locust
from scripts.loadtest_contract import ROOT, directory, identity, read_attempts, write_json


@pytest.mark.parametrize("execution", [None, "closed-loop", "decoupled"])
def test_launcher_keeps_old_default_and_selects_new_script(monkeypatch, execution):
    import scripts.loadtest as module

    manifest = {"port": 8095}
    monkeypatch.setattr(module, "settings_for", lambda _: (manifest, None))
    args = SimpleNamespace(
        wait=3, poll=0.1, headless=True, users=2, spawn_rate=20, seconds=10, observers=2
    )
    if execution:
        args.execution = execution
    calls = []
    monkeypatch.setattr(
        module.subprocess, "call", lambda command, **kwargs: calls.append((command, kwargs)) or 0
    )
    assert start_locust("a" * 12, args) == 0
    command, options = calls[0]
    expected = "locust_purchase_decoupled.py" if execution == "decoupled" else "locust_purchase.py"
    assert command[command.index("-f") + 1] == str(ROOT / "scripts" / expected)
    assert options["env"]["PERKFLOW_LOADTEST_RUN"] == "a" * 12
    if execution == "decoupled":
        assert options["env"]["PERKFLOW_LOADTEST_OBSERVERS"] == "2"


@pytest.mark.parametrize(
    "behavior", ["success", "lookup-retry", "timeout", "interrupted", "post-interrupted"]
)
async def test_decoupled_pending_does_not_block_next_purchase(behavior):
    if importlib.util.find_spec("locust") is None:
        pytest.skip("需要安装requirements-loadtest.lock")
    run_id = secrets.token_hex(6)
    folder = directory(run_id)
    folder.mkdir(parents=True)
    manifest = {
        "run_id": run_id,
        "resources": identity(run_id),
        "status": "ready",
        "mode": "async",
        "scenario": "distinct",
        "accounts": 6,
        "attempts": 6,
        "stock": 6,
        "port": 8095,
    }
    write_json(folder / "manifest.json", manifest)
    write_json(
        folder / "credentials.json",
        [{"user_id": i, "sid": f"fake-sid-{i}", "csrf": f"fake-csrf-{i}"} for i in range(1, 7)],
    )
    lock = threading.Lock()
    state = {
        "posts": [],
        "gets": 0,
        "in_get": 0,
        "max_in_get": 0,
        "first_success_posts": None,
        "auth_errors": 0,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-PerkFlow-Loadtest", run_id)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            uid = self.headers.get("Cookie", "").split("fake-sid-")[-1]
            valid = (
                uid in {str(i) for i in range(1, 7)}
                and self.headers.get("X-CSRF-Token") == f"fake-csrf-{uid}"
                and self.path == "/api/vouchers/1/orders"
            )
            with lock:
                state["auth_errors"] += not valid
                state["posts"].append((uid, self.headers.get("Idempotency-Key")))
            if behavior == "post-interrupted":
                time.sleep(3)
            self.reply(202, {"request": {"id": f"request-{uid}", "status": "pending"}})

        def do_GET(self):
            if self.path == "/_loadtest":
                self.reply(
                    200,
                    {
                        "run_id": run_id,
                        "mode": "async",
                        "database": identity(run_id)["mysql_database"],
                    },
                )
                return
            if self.path == "/_loadtest/ready":
                self.reply(200, {"ready": True})
                return
            uid = self.path.rsplit("-", 1)[-1]
            with lock:
                state["auth_errors"] += (
                    self.headers.get("Cookie") != f"perkflow_lt_{run_id}=fake-sid-{uid}"
                )
                state["gets"] += 1
                state["in_get"] += 1
                state["max_in_get"] = max(state["max_in_get"], state["in_get"])
                retry = behavior == "lookup-retry" and state["gets"] <= 2
                success = len(state["posts"]) == 6 and behavior in {"success", "lookup-retry"}
                if success and not retry and state["first_success_posts"] is None:
                    state["first_success_posts"] = len(state["posts"])
            try:
                time.sleep(0.02)
                if retry:
                    self.reply(503, {"error": {"code": "TEST_UNAVAILABLE"}})
                else:
                    self.reply(
                        200,
                        {
                            "id": f"request-{uid}",
                            "status": "succeeded" if success else "pending",
                            "order": {"id": uid, "status": "created"} if success else None,
                        },
                    )
            finally:
                with lock:
                    state["in_get"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 8095), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.update(
        PERKFLOW_LOADTEST_RUN=run_id,
        PERKFLOW_LOADTEST_WAIT="5" if behavior == "interrupted" else "1",
        PERKFLOW_LOADTEST_POLL="0.1",
        PERKFLOW_LOADTEST_OBSERVERS="2",
        PYTHONIOENCODING="utf-8",
    )
    try:
        # 只有2个购买用户，却必须先发完6次POST才可能收到成功；旧闭环会卡在前2次。
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "locust",
                "-f",
                str(ROOT / "scripts/locust_purchase_decoupled.py"),
                "--host",
                "http://127.0.0.1:8095",
                "--headless",
                "-u",
                "2",
                "-r",
                "20",
                "--run-time",
                "1s" if behavior in {"interrupted", "post-interrupted"} else "10s",
                "--stop-timeout",
                "0" if behavior == "post-interrupted" else "1",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
        )
        assert (folder / "observations.json").exists(), result.stdout + result.stderr
        report = json.loads((folder / "observations.json").read_text(encoding="utf-8"))
        submissions = [
            json.loads(line)
            for line in (folder / "submissions.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        if behavior == "post-interrupted":
            assert 1 <= len(state["posts"]) <= 2
            assert report["submission_phase"]["completed_posts"] == 0
            assert report["submission_phase"]["window_seconds"] is None
            assert report["completed_attempts"] == len(state["posts"])
            assert all(row["code"] == "SUBMISSION_INTERRUPTED" for row in report["attempts"])
            assert result.returncode != 0
            return
        assert len(state["posts"]) == 6
        assert len({key for _, key in state["posts"]}) == 6
        assert state["auth_errors"] == 0
        assert 1 <= state["max_in_get"] <= 2
        assert len(submissions) == 6 and all(row["outcome"] == "pending" for row in submissions)
        assert report["completed_attempts"] == 6
        assert report["submission_phase"]["completed_posts"] == 6
        rows, evidence = read_attempts(folder, manifest)
        assert evidence["valid"] and len(rows) == 6
        assert "fake-sid" not in (folder / "observations.json").read_text(encoding="utf-8")
        if behavior in {"success", "lookup-retry"}:
            assert state["first_success_posts"] == 6
            assert report["outcomes"] == {"succeeded": 6}
            assert result.returncode == (1 if behavior == "lookup-retry" else 0), (
                result.stdout + result.stderr
            )
        else:
            assert result.returncode != 0
            assert report["outcomes"] == {"unresolved": 6}
            expected = (
                "OBSERVATION_INTERRUPTED" if behavior == "interrupted" else "OBSERVATION_TIMEOUT"
            )
            assert all(row["code"] == expected for row in rows)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        # 只删除本测试已知的几个文件，不递归清理，也不接触真实环境资源。
        for name in (
            "manifest.json",
            "credentials.json",
            "started.json",
            "attempts.jsonl",
            "submissions.jsonl",
            "observations.json",
        ):
            (folder / name).unlink(missing_ok=True)
        folder.rmdir()
