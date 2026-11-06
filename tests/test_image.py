"""Step 15：生图客户端（R10、R12）。BananaRouter 用 responses 模拟。

tests/fixtures/image/generate_b64.json 的结构来自第 1 轮接口检查的真实响应（图片换成 8×8 小图）。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import requests
import responses

from app.clients import image as image_mod
from app.clients.http import ApiTimeout, BadRequestError, CallCounter, ServerError
from app.clients.image import ImageClient, build_prompt
from app.config import load_config

FIX = json.loads((Path(__file__).parent / "fixtures" / "image" / "generate_b64.json").read_text(encoding="utf-8"))
BASE = "https://img.example.com/v1"
URL = BASE + image_mod.GENERATE_PATH
ENV = {"IMAGE_BASE_URL": BASE, "IMAGE_API_KEY": "img-KEY-1234567890", "IMAGE_MODEL": "gpt-image-2"}
PROMPT = "一只透明圆柱形酸奶杯，白色背景"


@pytest.fixture()
def client():
    return ImageClient(load_config(env=ENV), sleep=lambda s: None, counter=CallCounter())


@responses.activate
def test_b64_json(client):
    responses.add(responses.POST, URL, json=FIX)
    img = client.generate(PROMPT)
    assert img.content.startswith(b"\x89PNG") and img.ext == "png" and img.source == "b64_json"
    body = json.loads(responses.calls[0].request.body)
    assert body["model"] == "gpt-image-2" and body["size"] == "1024x1024" and body["n"] == 1
    assert responses.calls[0].request.headers["Authorization"] == "Bearer img-KEY-1234567890"


@responses.activate
def test_url_download(client):
    png = copy.deepcopy(FIX)
    raw = image_mod.base64.b64decode(png["data"][0].pop("b64_json"))
    png["data"][0]["url"] = "https://cdn.example.com/a.png"
    responses.add(responses.POST, URL, json=png)
    responses.add(responses.GET, "https://cdn.example.com/a.png", body=raw, content_type="image/png")
    img = client.generate(PROMPT)
    assert img.content == raw and img.source == "url"
    assert "Authorization" not in responses.calls[1].request.headers  # 下载图片不带密钥


@responses.activate
def test_timeout(client):
    responses.add(responses.POST, URL, body=requests.exceptions.ReadTimeout("slow"))
    with pytest.raises(ApiTimeout):
        client.generate(PROMPT)
    assert len(responses.calls) == 1  # 超时不重试，避免等待成倍拉长
    assert client.http.timeout == 180  # 默认 180 秒（config/app.yaml）


@responses.activate
def test_400_no_retry(client):
    responses.add(responses.POST, URL, json={"error": {"message": "bad prompt"}}, status=400)
    with pytest.raises(BadRequestError):
        client.generate(PROMPT)
    assert len(responses.calls) == 1


@responses.activate
def test_5xx_retry_max_2(client):
    responses.add(responses.POST, URL, json={"error": "busy"}, status=503)
    with pytest.raises(ServerError) as ei:
        client.generate(PROMPT)
    assert len(responses.calls) == 3  # 1 次 + 重试 2 次
    assert ei.value.attempts == 3
    # 第 2 次成功
    responses.reset()
    responses.add(responses.POST, URL, json={"error": "busy"}, status=502)
    responses.add(responses.POST, URL, json=FIX)
    assert client.generate(PROMPT).ext == "png"


@responses.activate
def test_prompt_forbids_text_and_logo(client):
    p = build_prompt(PROMPT)
    assert p.startswith(PROMPT)
    assert "文字" in p and "logo" in p and "no text" in p
    assert build_prompt(p) == p  # 已有约束不重复添加
    responses.add(responses.POST, URL, json=FIX)
    client.generate(PROMPT)
    assert json.loads(responses.calls[0].request.body)["prompt"] == p
    # R12：提示词里不让模型画光明 logo
    assert "光明" not in image_mod.NO_TEXT_RULE
