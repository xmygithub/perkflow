"""初始化被忽略的 Compose 本地配置；生成随机密码，不覆盖既有文件或打印凭据。"""

import secrets
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("RABBITMQ_USER", "RABBITMQ_PASSWORD", "RABBITMQ_VHOST")


def main() -> int:
    path = ROOT / ".env"
    # 1. 既有配置只校验，不重生成密码；避免数据卷保留旧用户时连接凭据被误改。
    if path.exists():
        values = dotenv_values(path)
        missing = [key for key in REQUIRED if not values.get(key)]
        if missing:
            print("Root .env exists; fill these missing fields locally: " + ", ".join(missing))
            return 1
        print("Root .env already contains RabbitMQ settings; no changes made.")
        return 0
    # 2. 首次独占创建，随机值只写本地文件；不在终端、参数或日志输出密码。
    content = (
        "# Local Docker Compose credentials. Never commit or share this file.\n"
        "RABBITMQ_USER=perkflow_dev\n"
        f"RABBITMQ_PASSWORD={secrets.token_urlsafe(32)}\n"
        "RABBITMQ_VHOST=perkflow\n"
    )
    try:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
    except FileExistsError:
        print("Root .env was created concurrently; run this command again to validate it.")
        return 1
    print("Created root .env with a random RabbitMQ password; credentials were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
