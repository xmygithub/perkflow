"""测试运行器也要过滤 SDK 回溯中的凭据，不使用真实配置或真实子进程。"""

from urllib.parse import quote

from backend.app.core.config import Settings
from scripts import run_backend_tests as runner


def test_runner_redacts_raw_and_url_encoded_passwords(monkeypatch, capsys):
    fake_password = "synthetic-mq:a@b+/"
    fake_model_key = "synthetic-model:key/private"
    config = Settings(_env_file=None, rabbitmq_password=fake_password, llm_api_key=fake_model_key)
    environment = {}

    class FakeProcess:
        stdout = iter(
            [
                f"url amqp://user:{fake_password}@localhost\n",
                f"url encoded={quote(fake_password, safe='')}\n",
                f"model key={fake_model_key} encoded={quote(fake_model_key, safe='')}\n",
                "test failed\n",
            ]
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self):
            return 1

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    assert runner.run_tests_safely(["-q"], environment, config) == 1
    output = capsys.readouterr().out
    assert fake_password not in output and quote(fake_password, safe="") not in output
    assert fake_model_key not in output and quote(fake_model_key, safe="") not in output
    assert output.count("[REDACTED]") == 4 and "test failed" in output
    assert environment["PYTHONIOENCODING"] == "utf-8"
