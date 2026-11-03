"""使用HTTPX调用兼容Chat Completions的原生工具协议，无Agent框架。"""

import json
import logging
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from agent.schemas import ModelMessage, ModelUsage
from backend.app.core.config import Settings
from backend.app.core.errors import ApiError

logger = logging.getLogger("perkflow.agent")


class NativeToolModel:
    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_configured(self) -> None:
        settings = self.settings
        parsed = urlsplit(settings.llm_base_url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        allowed_http = (
            parsed.scheme == "http"
            and f"http://{parsed.netloc.lower()}" in settings.llm_http_allowed_origins
        )
        if not (
            settings.llm_model
            and settings.llm_api_key.get_secret_value()
            and parsed.hostname
            and (parsed.scheme == "https" or (local and parsed.scheme == "http") or allowed_http)
        ):
            raise ApiError(
                503,
                "AGENT_NOT_CONFIGURED",
                "请在backend/.env配置LLM_BASE_URL、LLM_MODEL和LLM_API_KEY后重启后端",
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ApiError(503, "AGENT_NOT_CONFIGURED", "模型服务地址格式不合法")
        if set(settings.llm_extra_body) & {"messages", "model", "tools", "stream", "tool_choice"}:
            raise ApiError(503, "AGENT_NOT_CONFIGURED", "模型附加参数不能覆盖消息或工具协议")

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelMessage:
        self.ensure_configured()
        settings = self.settings
        payload = {
            "model": settings.llm_model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "max_tokens": 1600,
            **settings.llm_extra_body,
        }
        # 1. 服务地址取受信任配置，禁止跟随重定向；凭据只发给此模型服务。
        try:
            async with httpx.AsyncClient(
                timeout=settings.llm_timeout, follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream(
                    "POST",
                    settings.llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.llm_api_key.get_secret_value()},
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 200000:
                            raise ValueError("模型响应过大")
                        chunks.append(chunk)
            raw = json.loads(b"".join(chunks))
            source = raw["choices"][0]["message"]
            # 供应商未提供或格式不合法时记录unknown，不能用0冒充免费/未消耗。
            usage = raw.get("usage")
            measured = {
                key: value
                for key in ModelUsage.model_fields
                if isinstance(usage, dict) and type(value := usage.get(key)) is int and value >= 0
            }
            # 2. 只保留文本和真实工具调用，不向前端提供或持久化内部推理内容。
            return ModelMessage.model_validate(
                {
                    "content": source.get("content"),
                    "tool_calls": source.get("tool_calls") or [],
                    "usage": measured or None,
                }
            )
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            # 不返回供应商响应、请求体或异常URL，避免凭据和私人对话进入日志。
            logger.warning(
                "模型请求未完成 category=%s status=%s",
                type(error).__name__,
                error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None,
            )
            raise ApiError(
                502, "AGENT_MODEL_UNAVAILABLE", "模型调用失败，请检查服务配置或稍后重试"
            ) from None
