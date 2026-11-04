"""Step 3：结构化日志（R2）。"""

from __future__ import annotations

import logging
import logging.handlers
import threading

import pytest

from app import logging_setup as ls

REQUIRED = {"timestamp", "level", "module", "message", "request_id", "status_code", "error_type", "stack"}


def test_record_has_required_fields(read_logs):
    log = logging.getLogger("app.test")
    with ls.request_context("rid-001"):
        log.info("调用完成", extra={"status_code": 200, "elapsed_ms": 12})
    rec = read_logs("app")[-1]
    assert REQUIRED <= rec.keys()
    assert rec["level"] == "INFO"
    assert rec["module"] == "app.test"
    assert rec["message"] == "调用完成"
    assert rec["request_id"] == "rid-001"
    assert rec["status_code"] == 200
    assert rec["elapsed_ms"] == 12
    assert rec["error_type"] is None and rec["stack"] is None


def test_error_goes_to_error_dir_with_stack(read_logs):
    log = logging.getLogger("app.test")
    try:
        raise ValueError("坏数据")
    except ValueError:
        log.exception("解析失败")
    errs = read_logs("error")
    assert len(errs) == 1
    rec = errs[0]
    assert rec["level"] == "ERROR"
    assert rec["error_type"] == "ValueError"
    assert "坏数据" in rec["stack"] and "test_logging.py" in rec["stack"]
    # 错误同时出现在 app 日志中
    assert any(r["message"] == "解析失败" for r in read_logs("app"))
    # INFO 不进 error 日志
    log.info("普通信息")
    assert len(read_logs("error")) == 1


def test_secret_never_in_log(read_logs):
    secret = "sk-super-secret-0123456789"
    ls.register_secrets([secret])
    log = logging.getLogger("app.test")
    log.info(f"key={secret}", extra={"url": f"https://x.com/?k={secret}"})
    log.info("header", extra={"headers": {"Authorization": "Bearer abcdefghijklmnop"}})
    try:
        raise RuntimeError(f"失败 {secret}")
    except RuntimeError:
        log.exception("带密钥的异常")
    raw = (ls.current_log_dir() / "app" / "app.log").read_text(encoding="utf-8")
    raw += (ls.current_log_dir() / "error" / "error.log").read_text(encoding="utf-8")
    assert secret not in raw
    assert "abcdefghijklmnop" not in raw
    assert "sk-s****" in raw


def test_request_id_propagates(read_logs):
    log = logging.getLogger("app.test")
    with ls.request_context("outer") as rid:
        assert rid == "outer"
        assert ls.get_request_id() == "outer"
        log.info("a")
        with ls.request_context("inner"):
            log.info("b")
        log.info("c")
    log.info("d")
    recs = read_logs("app")[-4:]
    assert [r["request_id"] for r in recs] == ["outer", "inner", "outer", None]

    # 线程之间互不干扰
    seen = {}

    def worker(name):
        with ls.request_context(name):
            seen[name] = ls.get_request_id()

    ts = [threading.Thread(target=worker, args=(n,)) for n in ("t1", "t2")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert seen == {"t1": "t1", "t2": "t2"}


def test_new_request_id_generated_when_none():
    with ls.request_context() as rid:
        assert isinstance(rid, str) and len(rid) >= 8
        assert ls.get_request_id() == rid


def test_step_logger_records_start_end_count_elapsed(read_logs):
    log = logging.getLogger("app.pipeline.test")
    ticks = iter([1.0, 1.25])
    with ls.log_step(log, "抓取", clock=lambda: next(ticks), keyword="低温酸奶") as step:
        step.count = 10
    recs = read_logs("app")
    start, end = recs[-2], recs[-1]
    assert start["step"] == "抓取" and start["event"] == "start" and start["keyword"] == "低温酸奶"
    assert end["event"] == "end" and end["count"] == 10 and end["elapsed_ms"] == 250


def test_step_logger_records_failure(read_logs):
    log = logging.getLogger("app.pipeline.test")
    ticks = iter([0.0, 0.5])
    with pytest.raises(KeyError), ls.log_step(log, "洞察", clock=lambda: next(ticks)):
        raise KeyError("x")
    rec = read_logs("error")[-1]
    assert rec["step"] == "洞察" and rec["event"] == "fail"
    assert rec["error_type"] == "KeyError" and rec["elapsed_ms"] == 500
    assert "KeyError" in rec["stack"]


def test_error_type_from_exception_attribute(read_logs):
    class Boom(Exception):
        error_type = "quota_exceeded"

    log = logging.getLogger("app.test")
    try:
        raise Boom("x")
    except Boom:
        log.exception("额度不足")
    assert read_logs("error")[-1]["error_type"] == "quota_exceeded"


def test_setup_is_idempotent(log_dir):
    ls.setup_logging(log_dir)
    ls.setup_logging(log_dir)
    ours = [h for h in logging.getLogger("app").handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(ours) == 2  # app + error（console 已关闭），重复调用不会重复添加
