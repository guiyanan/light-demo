"""Kimi（月之暗面，OpenAI 兼容协议）客户端。

- chat()：基础对话调用（接口检查用）
- chat_json()：要求输出 JSON → 解析 → JSON Schema 校验；不合格把错误告诉模型再试 1 次，仍不合格抛 LlmOutputError；
  合格结果按「模型 + 消息 + Schema」的哈希缓存到文件，同样的输入不重复调用（R8a）
重试、日志、计数复用 app.clients.http 中的逻辑。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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
from app.pipeline.schemas import validate

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    content: str
    model: str
    raw: dict[str, Any]
    elapsed_ms: int
    attempts: int


@dataclass
class JsonResult:
    data: dict[str, Any]
    cached: bool
    attempts: int  # 调用大模型的次数（缓存命中为 0）
    content: str  # 最后一次的原始回复文字


class LlmOutputError(InvalidResponseError):
    """大模型的回复不是合格的 JSON（已重试 1 次）。"""

    error_type = "invalid_output"

    def summary(self) -> str:
        return f"大模型返回的内容格式不对，已重试 1 次仍不合格，请点重试：{self}"


DEFAULT_JSON_MAX_TOKENS = 16000  # Kimi 的思考过程也占 token，留足余量


def parse_json(text: str) -> dict[str, Any]:
    """从回复中取出 JSON 对象：允许外面包着 ```json 代码块或前后夹带说明文字。"""
    s = (text or "").strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end < start:
        raise ValueError("回复中没有 JSON 对象")
    try:
        data = json.loads(s[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 格式错误：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


def cache_key(model: str, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
    raw = json.dumps({"model": model, "messages": messages, "schema": schema}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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

    # ---------------- JSON 输出 ----------------
    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        cache_dir: Path | None = None,
        operation: str = "chat_json",
        max_tokens: int = DEFAULT_JSON_MAX_TOKENS,
    ) -> JsonResult:
        key = cache_key(self.cfg.model, messages, schema)
        path = Path(cache_dir) / f"{key}.json" if cache_dir else None
        cached = self._read_cache(path, schema)
        if cached is not None:
            logger.info("大模型结果命中缓存", extra={"event": "llm_cache_hit", "operation": operation, "cache_key": key[:12]})
            return JsonResult(cached, True, 0, "")

        msgs = list(messages)
        problem = ""
        for attempt in (1, 2):
            res = self.chat(msgs, max_tokens=max_tokens, operation=operation)
            try:
                data = parse_json(res.content)
                errors = validate(data, schema)
            except ValueError as e:
                errors = [str(e)]
            if not errors:
                self._write_cache(path, data, key)
                return JsonResult(data, False, attempt, res.content)
            problem = "；".join(errors[:5])
            logger.warning(
                f"大模型输出不合格（第 {attempt} 次）：{problem}",
                extra={"event": "llm_output_invalid", "operation": operation, "attempt": attempt,
                       "error_type": "invalid_output", "body_excerpt": res.content[:300]},
            )
            msgs = [
                *messages,
                {"role": "assistant", "content": res.content},
                {"role": "user", "content": f"上面的输出不合格：{problem}。请重新输出，只输出一个符合要求的 JSON 对象，不要任何其他文字。"},
            ]
        err = LlmOutputError(problem, service=self.service, status_code=200, body=res.content, attempts=2)
        logger.error(
            f"大模型输出两次都不合格：{operation}",
            exc_info=(type(err), err, None),
            extra={"event": "llm_output_failed", "operation": operation, "error_type": err.error_type},
        )
        raise err

    def _read_cache(self, path: Path | None, schema: dict[str, Any]) -> dict[str, Any] | None:
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"大模型缓存无法读取，重新调用：{e}",
                           extra={"event": "llm_cache_bad", "error_type": type(e).__name__, "path": str(path)})
            return None
        if not isinstance(data, dict) or validate(data, schema):
            logger.warning("大模型缓存内容不合格，重新调用", extra={"event": "llm_cache_bad", "path": str(path)})
            return None
        return data

    def _write_cache(self, path: Path | None, data: dict[str, Any], key: str) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:  # 缓存写不进去不影响本次结果
            logger.warning(f"大模型缓存写入失败：{e}",
                           extra={"event": "llm_cache_write_failed", "error_type": type(e).__name__, "path": str(path)})
            return
        logger.info("大模型结果已缓存", extra={"event": "llm_cache_saved", "cache_key": key[:12]})
