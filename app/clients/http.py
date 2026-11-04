"""HTTP 公共层（R2、R3）。

所有外部调用都经过这里：
- 鉴权：Bearer {api_key}
- 容错：400/401/402/403/404 等不重试，直接抛出带类型的错误；
  429 / 5xx / 网络断开按指数退避重试（默认最多 3 次，生图可设为 2 次）；
  超时抛出 ApiTimeout（不重试，避免生图等长请求被成倍拉长）
- 日志：每次请求记录 service、method、url（不含密钥）、status_code、elapsed_ms、attempt
- 计数：每一次真实发出的请求都计入 CallCounter（R15 运行记录会用到）
- `run_with_retry` 可被 openai SDK 等非 requests 的调用复用
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import requests

from app.logging_setup import get_request_id, redact, register_secrets

logger = logging.getLogger(__name__)

T = TypeVar("T")
SENSITIVE_PARAMS = {"key", "apikey", "api_key", "token", "access_token", "secret", "sign", "signature", "password", "auth"}
SENSITIVE_SUFFIXES = ("_key", "_token", "_secret", "_sign")
MAX_RETRY_AFTER = 30.0
BODY_EXCERPT = 500


# ---------------- 错误类型 ----------------
class ApiError(Exception):
    error_type = "api_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        service: str,
        status_code: int | None = None,
        body: str = "",
        retry_after: float | None = None,
        attempts: int = 1,
        request_id: str | None = None,
    ):
        self.service = service
        self.status_code = status_code
        self.body = redact(body or "")[:BODY_EXCERPT]
        self.retry_after = retry_after
        self.attempts = attempts
        self.request_id = request_id or get_request_id()
        self.message = redact(message)
        super().__init__(self.message)

    def __str__(self) -> str:
        code = f" HTTP {self.status_code}" if self.status_code is not None else ""
        return f"[{self.service}{code} {self.error_type}] {self.message}"

    def summary(self) -> str:
        """给网页展示的一句话错误摘要。"""
        return ERROR_HINTS.get(self.error_type, "接口调用失败") + f"：{self}"


class AuthError(ApiError):
    error_type = "auth_failed"


class QuotaError(ApiError):
    error_type = "quota_exceeded"


class BadRequestError(ApiError):
    error_type = "bad_request"


class RateLimitError(ApiError):
    error_type = "rate_limited"
    retryable = True


class ServerError(ApiError):
    error_type = "server_error"
    retryable = True


class NetworkError(ApiError):
    error_type = "network_error"
    retryable = True


class ApiTimeout(ApiError):
    error_type = "timeout"


class InvalidResponseError(ApiError):
    error_type = "invalid_response"


ERROR_HINTS = {
    "auth_failed": "密钥无效或无权限，请检查 .env 中的密钥",
    "quota_exceeded": "接口额度不足，请充值后重试",
    "bad_request": "请求参数或地址有误",
    "rate_limited": "调用太频繁，已重试仍失败，请稍后再试",
    "server_error": "接口服务端出错，已重试仍失败",
    "network_error": "网络连接失败，请检查网络或接口地址",
    "timeout": "接口响应超时",
    "invalid_response": "接口返回的内容无法解析",
}


def error_for_status(status: int, **kw: Any) -> ApiError:
    """把 HTTP 状态码映射为错误类型。"""
    msg = kw.pop("message", f"HTTP {status}")
    if status in (401, 403):
        cls: type[ApiError] = AuthError
    elif status == 402:
        cls = QuotaError
    elif status == 429:
        cls = RateLimitError
    elif status >= 500:
        cls = ServerError
    else:
        cls = BadRequestError
    return cls(msg, status_code=status, **kw)


# ---------------- 调用计数 ----------------
class CallCounter:
    """线程安全的外部调用计数器（按服务分别计数，含重试）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def add(self, service: str, n: int = 1) -> None:
        with self._lock:
            self._counts[service] = self._counts.get(service, 0) + n

    def get(self, service: str) -> int:
        with self._lock:
            return self._counts.get(service, 0)

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


GLOBAL_COUNTER = CallCounter()


# ---------------- 通用重试 ----------------
def run_with_retry(
    fn: Callable[[int], T],
    *,
    service: str,
    operation: str,
    max_retries: int,
    backoff_base: float,
    sleep: Callable[[float], Any] = time.sleep,
    logger: logging.Logger = logger,
) -> T:
    """调用 fn(attempt)；遇到可重试的 ApiError 按指数退避重试，其余错误直接抛出。"""
    attempt = 0
    while True:
        try:
            return fn(attempt)
        except ApiError as err:
            err.attempts = attempt + 1
            if not err.retryable or attempt >= max_retries:
                logger.error(
                    f"外部调用失败：{service} {operation}：{err}",
                    exc_info=(type(err), err, err.__traceback__),
                    extra={
                        "event": "http_fail",
                        "service": service,
                        "operation": operation,
                        "status_code": err.status_code,
                        "attempts": err.attempts,
                        "body_excerpt": err.body,
                    },
                )
                raise
            delay = backoff_base * (2**attempt)
            if err.retry_after is not None:
                delay = min(err.retry_after, MAX_RETRY_AFTER)
            logger.warning(
                f"外部调用重试：{service} {operation}，{delay:g} 秒后第 {attempt + 1} 次重试",
                extra={
                    "event": "http_retry",
                    "service": service,
                    "operation": operation,
                    "status_code": err.status_code,
                    "error_type": err.error_type,
                    "attempt": attempt + 1,
                    "delay_seconds": delay,
                },
            )
            sleep(delay)
            attempt += 1


# ---------------- requests 客户端 ----------------
@dataclass
class HttpResult:
    status_code: int
    data: Any
    text: str
    elapsed_ms: int
    attempts: int
    url: str
    content: bytes = b""


def safe_url(url: str, params: dict[str, Any] | None = None) -> str:
    """拼出可写入日志的 URL：敏感参数打码。"""
    if not params:
        return url
    def sensitive(name: str) -> bool:
        n = name.lower()
        return n in SENSITIVE_PARAMS or n.endswith(SENSITIVE_SUFFIXES)

    shown = "&".join(f"{k}={'****' if sensitive(k) else v}" for k, v in params.items() if v is not None)
    return f"{url}?{shown}"


def _retry_after(resp: requests.Response) -> float | None:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None


class HttpClient:
    def __init__(
        self,
        service: str,
        base_url: str,
        api_key: str,
        *,
        timeout: float,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        sleep: Callable[[float], Any] = time.sleep,
        session: requests.Session | None = None,
        counter: CallCounter | None = None,
    ):
        self.service = service
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.sleep = sleep
        self.session = session or requests.Session()
        self.counter = counter if counter is not None else GLOBAL_COUNTER
        register_secrets([self.api_key])

    # 公共方法 -------------------------------------------------
    def get(self, path: str, *, params: dict[str, Any] | None = None, **kw: Any) -> HttpResult:
        return self.request("GET", path, params=params, **kw)

    def post(self, path: str, *, json_body: Any = None, **kw: Any) -> HttpResult:
        return self.request("POST", path, json_body=json_body, **kw)

    def get_bytes(self, url: str, **kw: Any) -> HttpResult:
        """下载绝对地址的文件（如生图返回的图片 url），不带鉴权头。"""
        return self.request("GET", url, expect="bytes", auth=False, **kw)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        expect: str = "json",
        auth: bool = True,
    ) -> HttpResult:
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}/{path.lstrip('/')}"
        log_url = safe_url(url, params)
        headers = {"Accept": "application/json" if expect == "json" else "*/*"}
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        tmo = timeout if timeout is not None else self.timeout
        operation = f"{method} {path if not path.startswith('http') else 'download'}"

        def attempt_once(attempt: int) -> HttpResult:
            self.counter.add(self.service)
            start = time.perf_counter()
            common = {"service": self.service, "method": method, "url": log_url, "attempt": attempt + 1}
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, headers=headers, timeout=tmo
                )
            except requests.exceptions.Timeout as e:
                elapsed = round((time.perf_counter() - start) * 1000)
                logger.warning("外部调用超时", extra={"event": "http_call", "elapsed_ms": elapsed, **common})
                raise ApiTimeout(f"{tmo:g} 秒内无响应：{e}", service=self.service) from e
            except requests.exceptions.RequestException as e:
                elapsed = round((time.perf_counter() - start) * 1000)
                logger.warning("外部调用网络错误", extra={"event": "http_call", "elapsed_ms": elapsed, **common})
                raise NetworkError(f"网络错误：{e}", service=self.service) from e

            elapsed = round((time.perf_counter() - start) * 1000)
            level = logging.INFO if resp.status_code < 400 else logging.WARNING
            logger.log(
                level,
                f"外部调用 {method} {self.service} → {resp.status_code}",
                extra={"event": "http_call", "status_code": resp.status_code, "elapsed_ms": elapsed, **common},
            )
            if resp.status_code >= 400:
                raise error_for_status(
                    resp.status_code,
                    service=self.service,
                    body=resp.text,
                    retry_after=_retry_after(resp),
                    message=f"HTTP {resp.status_code}：{resp.text[:200]}",
                )
            data = None
            if expect == "json":
                try:
                    data = resp.json()
                except ValueError as e:
                    raise InvalidResponseError(
                        "返回内容不是 JSON", service=self.service, status_code=resp.status_code, body=resp.text
                    ) from e
            return HttpResult(
                status_code=resp.status_code,
                data=data,
                text=resp.text if expect == "json" else "",
                elapsed_ms=elapsed,
                attempts=attempt + 1,
                url=log_url,
                content=resp.content if expect == "bytes" else b"",
            )

        return run_with_retry(
            attempt_once,
            service=self.service,
            operation=operation,
            max_retries=self.max_retries if max_retries is None else max_retries,
            backoff_base=self.backoff_base,
            sleep=self.sleep,
        )
