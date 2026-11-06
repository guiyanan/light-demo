"""Step 10：大模型客户端 JSON 输出、Schema 校验、重试、缓存（R8a 基础）。Kimi 用假的 openai 客户端。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.clients import llm as llm_mod
from app.clients.http import CallCounter
from app.clients.llm import LlmClient, LlmOutputError, parse_json
from app.config import load_config

ENV = {
    "LLM_BASE_URL": "https://llm.example.com/v1",
    "LLM_API_KEY": "llm-KEY-1234567890",
    "LLM_MODEL": "kimi-test",
}
SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
}
GOOD = {"items": ["a", "b"]}
MSGS = [{"role": "user", "content": "列出两个字母"}]


class FakeCompletions:
    """按顺序返回预设的回复文字。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        content = self.replies.pop(0)
        return SimpleNamespace(
            model_dump=lambda: {"model": kw["model"], "choices": [{"message": {"content": content}}]},
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            model=kw["model"],
        )


def make(replies, env=ENV):
    comp = FakeCompletions(replies)
    counter = CallCounter()
    client = LlmClient(
        load_config(env=env), client=SimpleNamespace(chat=SimpleNamespace(completions=comp)),
        sleep=lambda s: None, counter=counter,
    )
    return client, comp, counter


def test_json_schema_ok(tmp_path):
    client, comp, counter = make([json.dumps(GOOD)])
    res = client.chat_json(MSGS, SCHEMA, cache_dir=tmp_path)
    assert res.data == GOOD
    assert res.cached is False and res.attempts == 1
    assert len(comp.calls) == 1 and counter.get("llm") == 1


def test_invalid_json_retry_once_then_error(tmp_path, read_logs):
    cache = tmp_path / "cache"
    # 两次都不合格（第 1 次不是 JSON，第 2 次不符合 Schema）→ 报错，只调用 2 次
    client, comp, _ = make(["这不是 JSON", json.dumps({"items": []})])
    with pytest.raises(LlmOutputError) as ei:
        client.chat_json(MSGS, SCHEMA, cache_dir=cache)
    assert len(comp.calls) == 2
    assert ei.value.error_type == "invalid_output"
    assert "大模型" in ei.value.summary()
    # 第 2 次调用带上了上次的错误说明，要求只输出合格 JSON
    second = comp.calls[1]["messages"]
    assert second[: len(MSGS)] == MSGS and "JSON" in second[-1]["content"]
    assert not cache.exists()  # 失败结果不写缓存
    assert any(r.get("event") == "llm_output_invalid" for r in read_logs("app"))

    # 第 1 次不合格、第 2 次合格 → 成功
    client, comp, _ = make(["oops", json.dumps(GOOD)])
    res = client.chat_json(MSGS, SCHEMA, cache_dir=cache)
    assert res.data == GOOD and res.attempts == 2 and len(comp.calls) == 2


def test_cache_by_content_hash(tmp_path):
    client, comp, counter = make([json.dumps(GOOD), json.dumps({"items": ["z"]})])
    first = client.chat_json(MSGS, SCHEMA, cache_dir=tmp_path)
    again = client.chat_json(MSGS, SCHEMA, cache_dir=tmp_path)
    assert again.data == first.data and again.cached is True
    assert counter.get("llm") == 1  # 同样的输入不重复调用
    other = client.chat_json([{"role": "user", "content": "换个问题"}], SCHEMA, cache_dir=tmp_path)
    assert other.data == {"items": ["z"]} and other.cached is False
    assert counter.get("llm") == 2
    # 换模型不会用到旧缓存
    client2, comp2, _ = make([json.dumps(GOOD)], env=dict(ENV, LLM_MODEL="kimi-other"))
    assert client2.chat_json(MSGS, SCHEMA, cache_dir=tmp_path).cached is False
    # 缓存文件损坏时重新调用，不报错
    for p in tmp_path.iterdir():
        p.write_text("{坏的", encoding="utf-8")
    client3, comp3, _ = make([json.dumps(GOOD)])
    assert client3.chat_json(MSGS, SCHEMA, cache_dir=tmp_path).cached is False
    # 不给缓存目录时每次都调用
    client4, comp4, _ = make([json.dumps(GOOD), json.dumps(GOOD)])
    client4.chat_json(MSGS, SCHEMA)
    client4.chat_json(MSGS, SCHEMA)
    assert len(comp4.calls) == 2


def test_base_url_model_configurable(monkeypatch):
    seen = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            seen.update(kw)
            self.chat = SimpleNamespace(completions=FakeCompletions([json.dumps(GOOD)]))

    monkeypatch.setattr(llm_mod.openai, "OpenAI", FakeOpenAI)
    env = dict(ENV, LLM_BASE_URL="https://api.moonshot.cn/v1/", LLM_MODEL="kimi-k2.6")
    client = LlmClient(load_config(env=env), sleep=lambda s: None, counter=CallCounter())
    assert seen["base_url"] == "https://api.moonshot.cn/v1"
    assert seen["api_key"] == "llm-KEY-1234567890"
    client.chat_json(MSGS, SCHEMA)
    assert client.client.chat.completions.calls[0]["model"] == "kimi-k2.6"


def test_json_in_code_fence_parsed():
    assert parse_json('```json\n{"items": ["a"]}\n```') == {"items": ["a"]}
    assert parse_json('好的，结果如下：\n{"items": ["a"]}\n以上。') == {"items": ["a"]}
    with pytest.raises(ValueError):
        parse_json("没有 JSON")
    with pytest.raises(ValueError):
        parse_json("[1, 2]")  # 顶层必须是对象
