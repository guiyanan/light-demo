"""BananaRouter 生图客户端（OpenAI 兼容 POST /images/generations）。

第 1 轮只提供最小调用（接口检查用）；完整的生图逻辑在第 5 轮（Step 15）按真实响应补充。
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from typing import Any

from app.clients.http import CallCounter, HttpClient, HttpResult, InvalidResponseError
from app.config import AppConfig

GENERATE_PATH = "/images/generations"
NO_TEXT_RULE = "包装上不要出现任何文字、字母、数字和 logo（no text, no letters, no logo）。"


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
