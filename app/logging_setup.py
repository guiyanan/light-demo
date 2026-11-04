"""结构化日志（R2）。

- 每条日志是一行 JSON，固定包含：timestamp、level、module、message、request_id、
  status_code、error_type、stack；调用方通过 `extra={...}` 附加其他字段
- 目录：logs/app（全部 INFO 以上）、logs/error（ERROR 以上，含堆栈）、
  logs/diag（接口检查原始响应，由 diag 写入）、logs/test、logs/build（测试 / 构建输出）
- 写入前统一打码：已登记的密钥、Bearer token、sk- 开头的 key
- request_id 通过 contextvars 传递，线程、协程互不干扰
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import logging.handlers
import re
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import mask_secret

ROOT_LOGGER = "app"
LOG_CATEGORIES = ("app", "error", "test", "build", "diag")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_secrets: set[str] = set()
_secrets_lock = threading.Lock()
_state: dict[str, Any] = {"dir": None, "handlers": []}

_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}
_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1****"),
    (re.compile(r"\b(sk-[A-Za-z0-9]{4})[A-Za-z0-9_\-]{8,}"), r"\1****"),
]


# ---------------- request_id ----------------
def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def get_request_id() -> str | None:
    return _request_id.get()


@contextlib.contextmanager
def request_context(request_id: str | None = None) -> Iterator[str]:
    """在 with 块内设置 request_id；不传则自动生成。"""
    rid = request_id or new_request_id()
    token = _request_id.set(rid)
    try:
        yield rid
    finally:
        _request_id.reset(token)


# ---------------- 打码 ----------------
def register_secrets(values: Iterable[str | None]) -> None:
    """登记需要打码的密钥（太短的值忽略，避免误伤普通文字）。"""
    with _secrets_lock:
        for v in values:
            if v and len(v) >= 6:
                _secrets.add(v)


def redact(text: str) -> str:
    """把文本中的密钥替换为只显示前 4 位的形式。"""
    if not text:
        return text
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, mask_secret(s))
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


# ---------------- 格式化 ----------------
def _error_type(record: logging.LogRecord) -> str | None:
    explicit = getattr(record, "error_type", None)
    if explicit:
        return str(explicit)
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        return str(getattr(exc, "error_type", None) or type(exc).__name__)
    return None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stack = None
        if record.exc_info:
            stack = "".join(traceback.format_exception(*record.exc_info))
        elif getattr(record, "stack", None):
            stack = str(record.stack)
        payload: dict[str, Any] = {
            "timestamp": _dt.datetime.fromtimestamp(record.created, _dt.UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or _request_id.get(),
            "status_code": getattr(record, "status_code", None),
            "error_type": _error_type(record),
            "stack": stack,
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key not in payload:
                payload[key] = value
        payload["source"] = f"{record.pathname}:{record.lineno}"
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


# ---------------- 初始化 ----------------
def current_log_dir() -> Path | None:
    return _state["dir"]


def _file_handler(path: Path, level: int) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(JsonFormatter())
    return h


def setup_logging(
    logs_dir: Path | str = "logs", *, level: int = logging.INFO, force: bool = False, console: bool = True
) -> Path:
    """初始化项目日志（可重复调用；Streamlit 每次刷新都会调用）。"""
    logs_dir = Path(logs_dir)
    logger = logging.getLogger(ROOT_LOGGER)
    if not force and _state["dir"] == logs_dir and _state["handlers"]:
        return logs_dir
    reset_logging()
    for cat in LOG_CATEGORIES:
        (logs_dir / cat).mkdir(parents=True, exist_ok=True)
    logger.setLevel(level)
    logger.propagate = False
    handlers = [
        _file_handler(logs_dir / "app" / "app.log", level),
        _file_handler(logs_dir / "error" / "error.log", logging.ERROR),
    ]
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(level)
        sh.setFormatter(JsonFormatter())
        handlers.append(sh)
    for h in handlers:
        logger.addHandler(h)
    _state["dir"] = logs_dir
    _state["handlers"] = handlers
    return logs_dir


def reset_logging() -> None:
    """移除本模块添加的日志处理器（不影响其他处理器）。"""
    logger = logging.getLogger(ROOT_LOGGER)
    for h in _state["handlers"]:
        logger.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    _state["dir"] = None
    _state["handlers"] = []


# ---------------- 步骤日志 ----------------
@dataclass
class StepInfo:
    name: str
    count: int | None = None


@contextlib.contextmanager
def log_step(
    logger: logging.Logger, step: str, *, clock: Callable[[], float] = time.perf_counter, **fields: Any
) -> Iterator[StepInfo]:
    """记录流程步骤的开始、结束（条数、耗时）；出错时记录错误和堆栈后继续抛出。"""
    info = StepInfo(step)
    start = clock()
    logger.info(f"步骤开始：{step}", extra={"step": step, "event": "start", **fields})
    try:
        yield info
    except BaseException:
        elapsed = round((clock() - start) * 1000)
        logger.exception(
            f"步骤失败：{step}", extra={"step": step, "event": "fail", "elapsed_ms": elapsed, **fields}
        )
        raise
    elapsed = round((clock() - start) * 1000)
    logger.info(
        f"步骤结束：{step}",
        extra={"step": step, "event": "end", "count": info.count, "elapsed_ms": elapsed, **fields},
    )
