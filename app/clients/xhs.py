"""聚道云（小红书数据）客户端。

第 1 轮只提供最小调用（接口检查用）；解析、分页、广告标注在第 2 轮按真实响应结构补充。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.clients.http import CallCounter, HttpClient, HttpResult
from app.config import AppConfig

SEARCH_PATH = "/p6/xhs_v2/search_notes"
COMMENTS_PATH = "/p6/xhs_v2/get_note_comments"
SUCCESS_CODES = {0, 200, "0", "200"}


class XhsClient:
    service = "xhs"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        http: HttpClient | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        counter: CallCounter | None = None,
    ):
        self.http = http or HttpClient(
            self.service,
            cfg.xhs.base_url,
            cfg.xhs.api_key,
            timeout=cfg.xhs.timeout_seconds,
            max_retries=cfg.xhs.max_retries,
            backoff_base=cfg.http.backoff_base_seconds,
            sleep=sleep,
            counter=counter,
        )

    def search_notes_raw(self, keyword: str, page: int = 1, sort_type: str | None = None) -> HttpResult:
        params: dict[str, Any] = {"keyword": keyword, "page": page}
        if sort_type:
            params["sort_type"] = sort_type
        return self.http.get(SEARCH_PATH, params=params)

    def get_note_comments_raw(
        self, note_id: str, cursor: str | None = None, xsec_token: str | None = None
    ) -> HttpResult:
        params: dict[str, Any] = {"note_id": note_id}
        if cursor:
            params["cursor"] = cursor
        if xsec_token:
            params["xsec_token"] = xsec_token
        return self.http.get(COMMENTS_PATH, params=params)


def business_error(data: Any) -> str | None:
    """接口 HTTP 200 但业务码表示失败时，返回错误描述；否则返回 None。"""
    if not isinstance(data, dict) or "code" not in data:
        return None
    code = data.get("code")
    if code in SUCCESS_CODES or data.get("success") is True:
        return None
    msg = data.get("msg") or data.get("message") or ""
    return f"接口返回业务错误码 {code}：{msg}".strip()


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def find_first_note(data: Any) -> dict[str, str] | None:
    """在未知结构的搜索结果里找到第一篇笔记的 note_id（及 xsec_token），供接口检查继续查评论。"""
    for d in _walk(data):
        for key in ("note_id", "noteId"):
            if isinstance(d.get(key), str) and d[key]:
                return {"note_id": d[key], "xsec_token": str(d.get("xsec_token") or "")}
    for d in _walk(data):
        if isinstance(d.get("id"), str) and d["id"] and ("xsec_token" in d or "note_card" in d):
            return {"note_id": d["id"], "xsec_token": str(d.get("xsec_token") or "")}
    return None
