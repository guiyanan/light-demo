"""Kimi（月之暗面，OpenAI 兼容协议）客户端。

第 1 轮只提供基础对话调用；JSON 输出、Schema 校验、缓存在第 3 轮（Step 10）补充。
重试、日志、计数复用 app.clients.http 中的逻辑。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import openai

from app.clients.http import (
    GLOBAL_COUNTER,
    ApiTimeout,
    CallCounter,
    InvalidResponseError,
    NetworkError,
    error_for_status,
    run_with_retry,
)
from app.config import AppConfig
from app.logging_setup import register_secrets

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    content: str
    model: str
    raw: dict[str, Any]
    elapsed_ms: int
    attempts: int


class LlmClient:
    service = "llm"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        client: Any = None,
        sleep: Callable[[float], Any] = time.sleep,
        counter: CallCounter | None = None,
    ):
        self.cfg = cfg.llm
        self.backoff_base = cfg.http.backoff_base_seconds
        self.sleep = sleep
        self.counter = counter if counter is not None else GLOBAL_COUNTER
        register_secrets([self.cfg.api_key])
        self.client = client or openai.OpenAI(
            api_key=self.cfg.api_key,
            base_url=self.cfg.base_url,
            timeout=self.cfg.timeout_seconds,
            max_retries=0,  # 重试由 run_with_retry 统一处理
        )
        self.url = f"{self.cfg.base_url}/chat/completions"

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        operation: str = "chat",
        **extra: Any,
    ) -> ChatResult:
        kwargs: dict[str, Any] = {"model": self.cfg.model, "messages": messages, **extra}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature

        def once(attempt: int) -> ChatResult:
            self.counter.add(self.service)
            start = time.perf_counter()
            common = {"service": self.service, "method": "POST", "url": self.url, "attempt": attempt + 1}
            status: int | None = None
            try:
                resp = self.client.chat.completions.create(**kwargs)
                status = 200
            except openai.APITimeoutError as e:
                raise ApiTimeout(f"{self.cfg.timeout_seconds:g} 秒内无响应", service=self.service) from e
            except openai.APIConnectionError as e:
                raise NetworkError(f"网络错误：{e}", service=self.service) from e
            except openai.APIStatusError as e:
                status = e.status_code
                text = getattr(e.response, "text", "") or str(e)
                retry_after = None
                try:
                    retry_after = float(e.response.headers.get("retry-after"))  # type: ignore[arg-type]
                except (TypeError, ValueError, AttributeError):
                    pass
                raise error_for_status(
                    e.status_code, service=self.service, body=text, retry_after=retry_after,
                    message=f"HTTP {e.status_code}：{text[:200]}",
                ) from e
            finally:
                elapsed = round((time.perf_counter() - start) * 1000)
                logger.log(
                    logging.INFO if status == 200 else logging.WARNING,
                    f"外部调用 POST llm → {status}",
                    extra={"event": "http_call", "status_code": status, "elapsed_ms": elapsed, **common},
                )
            try:
                content = resp.choices[0].message.content or ""
                raw = resp.model_dump()
            except (AttributeError, IndexError, TypeError) as e:
                raise InvalidResponseError("返回结构不符合 OpenAI 格式", service=self.service, body=str(resp)) from e
            return ChatResult(content, getattr(resp, "model", self.cfg.model), raw, elapsed, attempt + 1)

        return run_with_retry(
            once,
            service=self.service,
            operation=operation,
            max_retries=self.cfg.max_retries,
            backoff_base=self.backoff_base,
            sleep=self.sleep,
        )
