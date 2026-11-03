"""只读观察 Redis 会话 TTL，不输出 Cookie、CSRF、验证码或用户资料。

在项目根目录运行：python -m scripts.watch_session_ttl
"""

import argparse
import asyncio
import json
from datetime import datetime

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.app.core.config import Settings


async def watch(interval: float, samples: int) -> None:
    # 1. 复用后端本地配置，只连接 Redis，不连接 MySQL，也不创建会话。
    settings = Settings()
    if any(char in settings.redis_key_prefix for char in "*?[]"):
        raise ValueError("REDIS_KEY_PREFIX 不应包含通配符")
    client = Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password.get_secret_value() or None,
        decode_responses=True,
        protocol=2,
        socket_connect_timeout=settings.redis_timeout,
        socket_timeout=settings.redis_timeout,
    )
    previous: dict[str, int] = {}
    print(f"只读观察会话 TTL，登录空闲期限 {settings.login_session_ttl} 秒；Ctrl+C 停止。")
    try:
        count = 0
        while samples == 0 or count < samples:
            current: dict[str, int] = {}
            # 2. SCAN 只遍历配置前缀下的会话 Key；GET/TTL 都不会续期。
            async for key in client.scan_iter(match=settings.redis_key_prefix + "session:*"):
                raw = await client.get(key)
                ttl = await client.ttl(key)
                if raw is None or ttl < 0:
                    continue  # 扫描和读取之间，Key 可能已经退出或自然过期。
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                # 3. 只输出会话类型、摘要 Key 尾部和 TTL，不输出完整会话内容。
                kind = "已登录" if data.get("user") else "匿名"
                renewed = key in previous and ttl > previous[key] + 2
                note = f"  [续期：{previous[key]} → {ttl} 秒]" if renewed else ""
                print(
                    f"[{datetime.now():%H:%M:%S}] {kind}  Key尾部={key[-12:]}"
                    f"  TTL={ttl} 秒{note}",
                    flush=True,
                )
                current[key] = ttl
            if not current:
                print("没有有效会话，请在浏览器中打开登录页并完成登录。", flush=True)
            for key in previous.keys() - current.keys():
                print(f"Key尾部={key[-12:]} 已消失（退出或到期）。", flush=True)
            previous = current
            count += 1
            # 4. 轮询只观察倒计时，不发业务请求，避免脚本自己触发续期。
            if samples == 0 or count < samples:
                await asyncio.sleep(interval)
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="只读观察会话 TTL 和续期变化")
    parser.add_argument("--interval", type=float, default=1, help="采样间隔秒数，至少 0.5")
    parser.add_argument("--samples", type=int, default=0, help="采样次数；0 表示持续观察")
    args = parser.parse_args()
    if args.interval < 0.5 or args.samples < 0:
        parser.error("interval 至少为 0.5，samples 不能为负数")
    try:
        asyncio.run(watch(args.interval, args.samples))
    except KeyboardInterrupt:
        print("已停止观察。")
    except (RedisError, ValidationError, ValueError):
        parser.exit(1, "无法读取会话，请检查本机 Redis 服务及 backend/.env 的配置。\n")


if __name__ == "__main__":
    main()
