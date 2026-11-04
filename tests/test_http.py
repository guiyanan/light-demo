"""Step 4：HTTP 公共层（R2、R3）。所有请求由 responses 模拟，退避等待函数注入，不真实等待。"""

from __future__ import annotations

import pytest
import requests
import responses

from app.clients import http as h

BASE = "https://api.example.com"
KEY = "secret-key-ABCDEFGH123456"


def make_client(sleeps, counter=None, **kw):
    return h.HttpClient(
        "xhs",
        BASE + "/",
        KEY,
        timeout=5,
        max_retries=kw.pop("max_retries", 3),
        backoff_base=1.0,
        sleep=sleeps.append,
        counter=counter or h.CallCounter(),
        **kw,
    )


@pytest.mark.parametrize(
    "status,exc,etype",
    [
        (400, h.BadRequestError, "bad_request"),
        (401, h.AuthError, "auth_failed"),
        (402, h.QuotaError, "quota_exceeded"),
        (403, h.AuthError, "auth_failed"),
        (404, h.BadRequestError, "bad_request"),
    ],
)
@responses.activate
def test_400_401_402_no_retry(status, exc, etype, read_logs):
    responses.get(f"{BASE}/p6/x", status=status, json={"msg": "nope"})
    sleeps: list[float] = []
    c = make_client(sleeps)
    with pytest.raises(exc) as ei:
        c.get("/p6/x")
    assert len(responses.calls) == 1
    assert sleeps == []
    assert ei.value.status_code == status
    assert ei.value.error_type == etype
    assert ei.value.retryable is False
    assert "nope" in ei.value.body
    err = read_logs("error")[-1]
    assert err["status_code"] == status and err["error_type"] == etype and err["service"] == "xhs"


@responses.activate
def test_429_5xx_backoff_max_retries(log_dir):
    responses.get(f"{BASE}/a", status=503, body="busy")
    sleeps: list[float] = []
    c = make_client(sleeps)
    with pytest.raises(h.ServerError) as ei:
        c.get("/a")
    assert len(responses.calls) == 4  # 1 次 + 重试 3 次
    assert sleeps == [1.0, 2.0, 4.0]
    assert ei.value.attempts == 4


@responses.activate
def test_429_then_success_honors_retry_after(log_dir):
    responses.get(f"{BASE}/b", status=429, headers={"Retry-After": "7"})
    responses.get(f"{BASE}/b", status=200, json={"ok": True})
    sleeps: list[float] = []
    res = make_client(sleeps).get("/b")
    assert res.data == {"ok": True} and res.status_code == 200 and res.attempts == 2
    assert sleeps == [7.0]


@responses.activate
def test_retry_limit_overridable_for_image(log_dir):
    responses.post(f"{BASE}/img", status=500)
    sleeps: list[float] = []
    c = make_client(sleeps, max_retries=2)
    with pytest.raises(h.ServerError):
        c.post("/img", json_body={"p": 1})
    assert len(responses.calls) == 3
    assert sleeps == [1.0, 2.0]


@responses.activate
def test_timeout_raises_typed_error(read_logs):
    responses.get(f"{BASE}/slow", body=requests.exceptions.ReadTimeout("read timed out"))
    sleeps: list[float] = []
    with pytest.raises(h.ApiTimeout) as ei:
        make_client(sleeps).get("/slow", timeout=3)
    assert ei.value.error_type == "timeout"
    assert len(responses.calls) == 1 and sleeps == []
    assert read_logs("error")[-1]["error_type"] == "timeout"


@responses.activate
def test_network_error_retried(log_dir):
    responses.get(f"{BASE}/n", body=requests.exceptions.ConnectionError("dns"))
    responses.get(f"{BASE}/n", json={"ok": 1})
    sleeps: list[float] = []
    assert make_client(sleeps).get("/n").data == {"ok": 1}
    assert sleeps == [1.0]


@responses.activate
def test_invalid_json_raises(log_dir):
    responses.get(f"{BASE}/html", body="<html>oops</html>", status=200)
    with pytest.raises(h.InvalidResponseError) as ei:
        make_client([]).get("/html")
    assert "oops" in ei.value.body


@responses.activate
def test_log_has_method_url_status_elapsed_without_key(read_logs, log_dir):
    responses.get(f"{BASE}/p6/search", json={"data": []})
    c = make_client([])
    c.get("/p6/search", params={"keyword": "低温酸奶", "page": 1, "token": "tok-should-hide-123"})
    sent = responses.calls[0].request
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    rec = [r for r in read_logs("app") if r.get("event") == "http_call"][-1]
    assert rec["method"] == "GET"
    assert rec["url"].startswith(f"{BASE}/p6/search?")
    assert "tok-should-hide-123" not in rec["url"]
    assert "keyword=低温酸奶" in rec["url"] and "token=****" in rec["url"]
    assert rec["status_code"] == 200
    assert isinstance(rec["elapsed_ms"], int)
    assert rec["service"] == "xhs" and rec["attempt"] == 1
    raw = (log_dir / "app" / "app.log").read_text(encoding="utf-8")
    assert KEY not in raw


@responses.activate
def test_call_counter(log_dir):
    responses.get(f"{BASE}/c", status=502)
    responses.get(f"{BASE}/c", json={})
    counter = h.CallCounter()
    c = make_client([], counter=counter)
    c.get("/c")
    c.get("/c")
    assert counter.get("xhs") == 3  # 第一次调用含 1 次重试
    other = h.HttpClient("image", BASE, KEY, timeout=5, sleep=lambda s: None, counter=counter)
    other.get("/c")
    assert counter.snapshot() == {"xhs": 3, "image": 1}
    assert counter.total() == 4
    counter.reset()
    assert counter.total() == 0


@responses.activate
def test_request_id_attached_to_error(log_dir):
    from app.logging_setup import request_context

    responses.get(f"{BASE}/e", status=401)
    with request_context("rid-x"), pytest.raises(h.AuthError) as ei:
        make_client([]).get("/e")
    assert ei.value.request_id == "rid-x"
    assert "401" in str(ei.value)


@responses.activate
def test_get_bytes_absolute_url_without_auth(log_dir):
    responses.get("https://cdn.example.com/a.png", body=b"\x89PNG", content_type="image/png")
    res = make_client([]).get_bytes("https://cdn.example.com/a.png")
    assert res.content == b"\x89PNG"
    assert "Authorization" not in responses.calls[0].request.headers


def test_run_with_retry_generic(log_dir):
    """供非 requests 的 SDK（如 openai）复用的重试逻辑。"""
    import logging

    attempts = []

    def fn(attempt):
        attempts.append(attempt)
        if attempt < 2:
            raise h.RateLimitError("429", service="llm", status_code=429)
        return "ok"

    sleeps: list[float] = []
    out = h.run_with_retry(
        fn, service="llm", operation="chat", max_retries=3, backoff_base=0.5,
        sleep=sleeps.append, logger=logging.getLogger("app.test"),
    )
    assert out == "ok" and attempts == [0, 1, 2] and sleeps == [0.5, 1.0]
