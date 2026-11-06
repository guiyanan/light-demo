"""BananaRouter 生图客户端（OpenAI 兼容 POST /images/generations）。

- generate_raw / extract_image：第 1 轮接口检查用
- generate(prompt)：第 5 轮（R10）。提示词末尾自动加「不要文字和 logo」的约束；
  真实响应（2026-11-04）为 data[0].b64_json，也兼容 data[0].url（下载时不带密钥）；
  超时（默认 180 秒）不重试，5xx 最多重试 2 次，400 不重试——均由 HttpClient 统一处理
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.clients.http import CallCounter, HttpClient, HttpResult, InvalidResponseError
from app.config import AppConfig

logger = logging.getLogger(__name__)

GENERATE_PATH = "/images/generations"
NO_TEXT_RULE = "包装上不要出现任何文字、字母、数字和 logo（no text, no letters, no logo）。"


def build_prompt(prompt: str) -> str:
    """在画图描述后加上「不要文字和 logo」的约束（已加过则不重复）。"""
    p = (prompt or "").strip()
    return p if NO_TEXT_RULE in p else f"{p}\n{NO_TEXT_RULE}"


@dataclass
class GeneratedImage:
    content: bytes
    ext: str  # png / jpg / webp
    source: str  # b64_json / url
    elapsed_ms: int


class ImageClient:
    service = "image"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        http: HttpClient | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        counter: CallCounter | None = None,
    ):
        self.cfg = cfg.image
        self.http = http or HttpClient(
            self.service,
            cfg.image.base_url,
            cfg.image.api_key,
            timeout=cfg.image.timeout_seconds,
            max_retries=cfg.image.max_retries,
            backoff_base=cfg.http.backoff_base_seconds,
            sleep=sleep,
            counter=counter,
        )

    def generate_raw(self, prompt: str, size: str | None = None) -> HttpResult:
        body = {"model": self.cfg.model, "prompt": prompt, "size": size or self.cfg.size, "n": 1}
        return self.http.post(GENERATE_PATH, json_body=body)

    def generate(self, prompt: str, size: str | None = None) -> GeneratedImage:
        start = time.perf_counter()
        res = self.generate_raw(build_prompt(prompt), size)
        source, content = self.extract_image(res.data)
        elapsed = round((time.perf_counter() - start) * 1000)
        logger.info("生图完成", extra={"event": "image_generated", "source": source, "bytes": len(content),
                                     "elapsed_ms": elapsed})
        return GeneratedImage(content, image_ext(content), source, elapsed)

    def extract_image(self, data: Any) -> tuple[str, bytes]:
        """从响应中取出图片：返回 (格式, 图片字节)。格式为 b64_json 或 url。"""
        items = data.get("data") if isinstance(data, dict) else None
        if not items or not isinstance(items, list) or not isinstance(items[0], dict):
            raise InvalidResponseError("响应中没有 data[0]（可能是异步接口）", service=self.service, body=str(data)[:500])
        item = items[0]
        if item.get("b64_json"):
            try:
                return "b64_json", base64.b64decode(item["b64_json"])
            except (binascii.Error, ValueError) as e:
                raise InvalidResponseError("b64_json 解码失败", service=self.service) from e
        if item.get("url"):
            return "url", self.http.get_bytes(item["url"]).content
        raise InvalidResponseError("data[0] 中既没有 b64_json 也没有 url", service=self.service, body=str(item)[:500])


def image_ext(content: bytes) -> str:
    if content.startswith(b"\x89PNG"):
        return "png"
    if content.startswith(b"\xff\xd8"):
        return "jpg"
    if content[8:12] == b"WEBP":
        return "webp"
    return "png"
