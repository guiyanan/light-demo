"""Step 5：接口检查（R14）。聚道云和生图用 responses 模拟，Kimi 用假的 openai 客户端。"""

from __future__ import annotations

import base64
import datetime as dt
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
import responses

from app import diag
from app.config import load_config

XHS = "https://xhs.example.com"
IMG = "https://img.example.com/v1"
ENV = {
    "XHS_BASE_URL": XHS,
    "XHS_API_KEY": "xhs-KEY-1234567890",
    "LLM_BASE_URL": "https://llm.example.com/v1",
    "LLM_API_KEY": "llm-KEY-1234567890",
    "LLM_MODEL": "kimi-test",
    "IMAGE_BASE_URL": IMG,
    "IMAGE_API_KEY": "img-KEY-1234567890",
    "IMAGE_MODEL": "gpt-image-2",
}
NOW = dt.datetime(2026, 1, 2, 3, 4, 5)
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepng").decode()

SEARCH_OK = {
    "code": 0,
    "data": {"items": [{"note_card": {"note_id": "n001", "title": "好喝", "user": {"nickname": "某人"}}}]},
    "token_echo": "xhs-KEY-1234567890",  # 模拟接口把密钥回显：保存前必须打码
}
COMMENTS_OK = {"code": 0, "data": {"comments": [{"id": "c1", "content": "不错", "like_count": "3"}]}}


class FakeCompletions:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        b = self.behaviour.pop(0) if isinstance(self.behaviour, list) else self.behaviour
        if isinstance(b, Exception):
            raise b
        return SimpleNamespace(
            model_dump=lambda: {"id": "x", "model": kw["model"], "choices": [{"message": {"content": "OK"}}]},
            choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
            model=kw["model"],
        )


def fake_openai(behaviour="ok"):
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(behaviour)))


def status_error(code: int, text: str = '{"error":"bad"}') -> openai.APIStatusError:
    req = httpx.Request("POST", "https://llm.example.com/v1/chat/completions")
    return openai.APIStatusError(text, response=httpx.Response(code, request=req, text=text), body=None)


@pytest.fixture()
def cfg():
    return load_config(env=ENV)


@pytest.fixture()
def run(cfg, log_dir):
    def _run(service, **kw):
        kw.setdefault("diag_dir", log_dir / "diag")
        kw.setdefault("now", NOW)
        kw.setdefault("sleep", lambda s: None)
        return diag.run_check(service, cfg=cfg, **kw)

    return _run


def mock_xhs_ok():
    responses.get(f"{XHS}/p6/xhs_v2/search_notes", json=SEARCH_OK)
    responses.get(f"{XHS}/p6/xhs_v2/get_note_comments", json=COMMENTS_OK)


@responses.activate
def test_all_success(run, log_dir):
    mock_xhs_ok()
    responses.post(f"{IMG}/images/generations", json={"created": 1, "data": [{"b64_json": PNG}]})
    results = [
        run("xhs"),
        run("llm", llm_client=fake_openai()),
        run("image"),
    ]
    assert [r.service for r in results] == ["xhs", "llm", "image"]
    assert all(r.ok for r in results), [r.message for r in results]
    for r in results:
        assert r.status_code == 200
        assert isinstance(r.elapsed_ms, int)
        assert r.request_id
    xhs = results[0]
    assert xhs.details["note_id"] == "n001" and xhs.details["comments_status"] == 200
    assert results[2].details["image_format"] == "b64_json"
    # 生图检查把测试图片存下来，方便人工查看
    assert any(p.endswith(".png") for p in results[2].raw_files)
    # 请求参数正确
    sent = responses.calls[0].request
    assert "keyword=" in sent.url and sent.headers["Authorization"].startswith("Bearer ")
    body = json.loads(responses.calls[-1].request.body)
    assert body["model"] == "gpt-image-2" and body["n"] == 1


@responses.activate
def test_partial_failure_reports_status_and_elapsed(run):
    responses.get(f"{XHS}/p6/xhs_v2/search_notes", status=402, json={"msg": "余额不足"})
    responses.post(f"{IMG}/images/generations", status=503, body="busy")
    xhs = run("xhs")
    assert not xhs.ok and xhs.status_code == 402 and xhs.error_type == "quota_exceeded"
    assert "额度不足" in xhs.message and isinstance(xhs.elapsed_ms, int)
    assert len(responses.calls) == 1  # 402 不重试，也不再查评论

    llm = run("llm", llm_client=fake_openai(status_error(401)))
    assert not llm.ok and llm.status_code == 401 and llm.error_type == "auth_failed"

    img = run("image")
    assert not img.ok and img.status_code == 503 and img.error_type == "server_error"
    assert len(responses.calls) == 1 + 3  # 生图最多重试 2 次


def test_missing_config_reports_field_without_calling(log_dir):
    env = dict(ENV, LLM_API_KEY="")
    cfg = load_config(env=env)
    client = fake_openai()
    r = diag.run_check("llm", cfg=cfg, llm_client=client, diag_dir=log_dir / "diag", now=NOW)
    assert not r.ok and "LLM_API_KEY" in r.message and r.error_type == "config_missing"
    assert client.chat.completions.calls == []


def test_llm_retries_rate_limit_then_ok(run):
    client = fake_openai([status_error(429), "ok"])
    r = run("llm", llm_client=client)
    assert r.ok and r.details["attempts"] == 2


def test_llm_timeout_typed(run):
    req = httpx.Request("POST", "https://llm.example.com/v1/chat/completions")
    r = run("llm", llm_client=fake_openai(openai.APITimeoutError(request=req)))
    assert not r.ok and r.error_type == "timeout"


@responses.activate
def test_xhs_business_error_code_is_failure(run):
    responses.get(f"{XHS}/p6/xhs_v2/search_notes", json={"code": 1001, "msg": "keyword 不能为空"})
    r = run("xhs")
    assert not r.ok and "1001" in r.message and "keyword 不能为空" in r.message


@responses.activate
def test_image_url_response_downloaded(run):
    responses.post(f"{IMG}/images/generations", json={"data": [{"url": "https://cdn.example.com/a.png"}]})
    responses.get("https://cdn.example.com/a.png", body=b"\x89PNGdata", content_type="image/png")
    r = run("image")
    assert r.ok and r.details["image_format"] == "url"
    assert "Authorization" not in responses.calls[1].request.headers


@responses.activate
def test_raw_saved_masked(run, log_dir):
    mock_xhs_ok()
    responses.post(f"{IMG}/images/generations", json={"data": [{"b64_json": PNG * 50}]})
    xhs = run("xhs")
    img = run("image")
    files = sorted((log_dir / "diag").glob("*.json"))
    assert len(files) == 3  # 搜索、评论、生图
    assert all(f.name.startswith("20260102-030405-") for f in files)
    search = json.loads(next(f for f in files if "xhs-search" in f.name).read_text(encoding="utf-8"))
    assert search["response"]["data"]["items"][0]["note_card"]["note_id"] == "n001"
    assert search["response"]["token_echo"] == "xhs-****"
    assert search["request"]["url"].startswith(f"{XHS}/p6/xhs_v2/search_notes?keyword=低温酸奶")
    assert search["request_id"] == xhs.request_id
    image_raw = json.loads(next(f for f in files if "image" in f.name).read_text(encoding="utf-8"))
    assert image_raw["response"]["data"][0]["b64_json"].startswith("<base64")
    assert img.raw_files


@responses.activate
def test_no_secret_in_output(run, log_dir, capsys):
    mock_xhs_ok()
    responses.post(f"{IMG}/images/generations", status=401, json={"error": "invalid key img-KEY-1234567890"})
    outs = [run("xhs"), run("llm", llm_client=fake_openai(status_error(403, "bad key llm-KEY-1234567890"))), run("image")]
    text = "".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in outs)
    for p in log_dir.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".log"):
            text += p.read_text(encoding="utf-8")
    text += capsys.readouterr().out
    for k in ("XHS_API_KEY", "LLM_API_KEY", "IMAGE_API_KEY"):
        assert ENV[k] not in text


def test_cli_prints_table_and_exit_code(monkeypatch, capsys, log_dir):
    def fake(service, **kw):
        return diag.CheckResult(service=service, label=diag.SERVICES[service], ok=service != "image",
                                status_code=200 if service != "image" else 500, elapsed_ms=10,
                                message="ok" if service != "image" else "失败", request_id="r1")

    monkeypatch.setattr(diag, "run_check", fake)
    code = diag.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert out.index("聚道云") < out.index("Kimi") < out.index("生图")
    assert "500" in out and "失败" in out
    assert diag.main(["--only", "xhs"]) == 0


def test_bad_base_url_fails_without_calling(log_dir):
    """BUG-002：地址填成文档网页时，接口检查直接提示地址错误，不发请求。"""
    cfg = load_config(env=dict(ENV, IMAGE_BASE_URL="https://bananarouter.com/docs/models"))
    with responses.RequestsMock() as rsps:
        r = diag.run_check("image", cfg=cfg, diag_dir=log_dir / "diag", now=NOW, sleep=lambda s: None)
        assert len(rsps.calls) == 0
    assert not r.ok and r.error_type == "config_invalid"
    assert "IMAGE_BASE_URL" in r.message and "https://api.bananarouter.com/v1" in r.message
