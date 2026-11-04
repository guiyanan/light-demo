"""聚道云（小红书数据）客户端。

- 第 1 轮：最小调用（接口检查用）
- 第 2 轮（Step 6）：搜索结果解析、多关键词轮流取前 N 篇、广告标注、去除用户身份信息

真实响应结构（2026-11-04 接口检查确认）：
    data.result.list.items[] → {model_type: "note", note: {id, title, desc, liked_count, ...}}
    每页 20 条；没有 cursor / has_more，下一页为空即表示没有更多结果。
广告标注：搜索结果没有单独的广告字段；笔记的 widgets_context（JSON 字符串）中
order_cooperate / commercial_bridge_note 为 true 表示「商业合作」笔记，按广告帖标注。
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.clients.http import ApiError, CallCounter, HttpClient, HttpResult, InvalidResponseError
from app.config import AppConfig

logger = logging.getLogger(__name__)

MAX_KEYWORDS = 3
MAX_PAGES_PER_KEYWORD = 5  # 防止接口一直返回重复内容时无限翻页
AD_FLAGS = ("order_cooperate", "commercial_bridge_note")
# 评论排序：like_count = 按点赞从高到低（2026-11-04 实测确认；另有 default、latest_v2）
COMMENT_SORT_BY_LIKES = "like_count"
# 不保存的用户身份字段（R7）
IDENTITY_KEYS = frozenset(
    {"user", "user_info", "nickname", "original_name", "userid", "user_id", "red_id", "images", "avatar",
     "source_avatars", "ip_location", "author_id", "author_name", "at_users"}
)
_TAG_RE = re.compile(r"#([^#\s\[\]]+)")
_HIGHLIGHT_RE = re.compile(r"\[(搜索高亮|话题)\]")


class KeywordError(ValueError):
    """关键词数量或内容不合法。"""


class XhsBusinessError(ApiError):
    """HTTP 200 但聚道云业务码表示失败。"""

    error_type = "business_error"

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
        self,
        note_id: str,
        cursor: str | None = None,
        xsec_token: str | None = None,
        sort_strategy: str | None = None,
    ) -> HttpResult:
        params: dict[str, Any] = {"note_id": note_id}
        if cursor:
            params["cursor"] = cursor
        if xsec_token:
            params["xsec_token"] = xsec_token
        if sort_strategy:
            params["sort_strategy"] = sort_strategy
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


# ---------------- 身份信息清理（R7） ----------------
def strip_mentions(content: str, at_users: Iterable[Any] | None) -> str:
    """去掉评论里的 @昵称（昵称可能带空格，按 at_users 精确去除）。"""
    for u in at_users or []:
        name = u.get("nickname") if isinstance(u, dict) else None
        if name:
            content = content.replace("@" + name, "")
    return re.sub(r"\s{2,}", " ", content).strip()


def scrub_identity(obj: Any) -> Any:
    """返回去掉用户身份信息后的副本：删除昵称、用户 ID、头像、IP 属地等字段，
    清理 widgets_context 里的作者信息和评论里的 @昵称。原对象不变。"""
    if isinstance(obj, list):
        return [scrub_identity(v) for v in obj]
    if not isinstance(obj, dict):
        return copy.deepcopy(obj)
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if k in IDENTITY_KEYS:
            continue
        if k == "widgets_context" and isinstance(v, str):
            try:
                ctx = json.loads(v)
            except ValueError:
                logger.warning("widgets_context 不是合法 JSON，已整段丢弃", extra={"event": "scrub_drop"})
                continue  # 解析不了的整段丢弃，宁可少存
            out[k] = json.dumps(scrub_identity(ctx), ensure_ascii=False) if isinstance(ctx, dict) else v
        elif k == "content" and isinstance(v, str) and obj.get("at_users"):
            out[k] = strip_mentions(v, obj.get("at_users"))
        else:
            out[k] = scrub_identity(v)
    return out


# ---------------- 搜索结果解析（R4、R5） ----------------
def _check_business(data: Any) -> None:
    err = business_error(data)
    if err:
        raise XhsBusinessError(err, service=XhsClient.service, status_code=200, body=json.dumps(data, ensure_ascii=False))


def parse_search_page(data: Any) -> tuple[list[dict[str, Any]], bool | None]:
    """返回 (含 note 的条目列表, has_more)。结构不对时抛 InvalidResponseError。"""
    _check_business(data)
    try:
        result = data["data"]["result"]
        items = result["list"]["items"]
    except (KeyError, TypeError) as e:
        raise InvalidResponseError(
            f"搜索结果结构不符合预期（缺少 {e}）", service=XhsClient.service, status_code=200
        ) from e
    if not isinstance(items, list):
        raise InvalidResponseError("搜索结果 items 不是列表", service=XhsClient.service, status_code=200)
    notes = [it for it in items if isinstance(it, dict) and isinstance(it.get("note"), dict) and it["note"].get("id")]
    has_more = result.get("has_more")
    return notes, (bool(has_more) if has_more is not None else None)


def detect_ad(item: dict[str, Any]) -> bool | None:
    """True/False：能判断是否广告；None：没有可用的广告字段。"""
    if "ad" in str(item.get("model_type", "")).lower():
        return True
    note = item.get("note") or {}
    if note.get("is_ad") is True or note.get("ads_info"):
        return True
    ctx = note.get("widgets_context")
    if not isinstance(ctx, str):
        return None
    try:
        parsed = json.loads(ctx)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return any(parsed.get(f) is True for f in AD_FLAGS)


def parse_tags(text: str) -> list[str]:
    tags: list[str] = []
    for t in _TAG_RE.findall(_HIGHLIGHT_RE.sub("", text or "")):
        if t not in tags:
            tags.append(t)
    return tags


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def normalize_note(item: dict[str, Any], keyword: str) -> dict[str, Any]:
    """把一条搜索结果标准化为笔记记录（不含任何用户身份字段；rank 由调用方填写）。"""
    note = item["note"]
    is_ad = detect_ad(item)
    if is_ad is None:
        logger.warning(
            "笔记没有可用的广告字段，按非广告处理",
            extra={"event": "ad_field_missing", "note_id": note.get("id")},
        )
        is_ad = False
    desc = note.get("desc") or note.get("abstract_show") or ""
    return {
        "note_id": str(note["id"]),
        "keyword": keyword,
        "rank": 0,
        "title": note.get("title") or "",
        "desc": desc,
        "liked": _int(note.get("liked_count")),
        "collected": _int(note.get("collected_count")),
        "comments": _int(note.get("comments_count")),
        "shared": _int(note.get("shared_count")),
        "note_type": note.get("type") or "",
        "tags": parse_tags(desc),
        "is_ad": is_ad,
        "xsec_token": note.get("xsec_token") or "",
    }


def clean_keywords(keywords: Iterable[str]) -> list[str]:
    out: list[str] = []
    for k in keywords:
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    if not out:
        raise KeywordError("请至少输入 1 个关键词")
    if len(out) > MAX_KEYWORDS:
        raise KeywordError(f"关键词最多 {MAX_KEYWORDS} 个（当前 {len(out)} 个）")
    return out


@dataclass
class SearchResult:
    keywords: list[str]
    notes: list[dict[str, Any]]
    raw_pages: list[dict[str, Any]] = field(default_factory=list)  # 已去除身份信息
    points: int = 0  # 聚道云消耗点数


def consumed_points(data: Any) -> int:
    """本次调用消耗的聚道云点数（取不到时为 0）。"""
    try:
        return _int(data["data"].get("consumedPoints"))
    except (KeyError, TypeError, AttributeError):
        return 0


def search_top_notes(client: XhsClient, keywords: Iterable[str], *, max_notes: int) -> SearchResult:
    """按关键词轮流取搜索结果，保持接口顺序、按 note_id 去重，凑满 max_notes 篇或没有更多结果为止。"""
    kws = clean_keywords(keywords)
    queues: dict[str, list[dict[str, Any]]] = {k: [] for k in kws}
    next_page = dict.fromkeys(kws, 1)
    exhausted = dict.fromkeys(kws, False)
    res = SearchResult(keywords=kws, notes=[])
    seen: set[str] = set()

    def refill(kw: str) -> None:
        while not queues[kw] and not exhausted[kw]:
            page = next_page[kw]
            if page > MAX_PAGES_PER_KEYWORD:
                exhausted[kw] = True
                return
            data = client.search_notes_raw(kw, page=page).data
            res.raw_pages.append({"keyword": kw, "page": page, "response": scrub_identity(data)})
            res.points += consumed_points(data)
            items, has_more = parse_search_page(data)
            logger.info(
                f"搜索「{kw}」第 {page} 页：{len(items)} 篇",
                extra={"event": "xhs_search_page", "keyword": kw, "page": page, "count": len(items)},
            )
            next_page[kw] = page + 1
            queues[kw].extend(items)
            if not items or has_more is False:
                exhausted[kw] = True

    while len(res.notes) < max_notes:
        progressed = False
        for kw in kws:
            if len(res.notes) >= max_notes:
                break
            while True:
                refill(kw)
                if not queues[kw]:
                    break
                item = queues[kw].pop(0)
                nid = str(item["note"]["id"])
                if nid in seen:
                    continue
                seen.add(nid)
                note = normalize_note(item, kw)
                note["rank"] = len(res.notes) + 1
                res.notes.append(note)
                progressed = True
                break
        if not progressed:
            break
    return res


# ---------------- 评论解析（R6、R7） ----------------
def clean_text(text: str) -> str:
    """去掉搜索高亮等标记。"""
    return _HIGHLIGHT_RE.sub("", text or "").strip()


def parse_comments(data: Any, note_id: str) -> list[dict[str, Any]]:
    """把一页评论标准化为 {comment_id, note_id, content, likes}（保持接口顺序）。
    跳过隐藏 / 失效 / 去掉 @昵称后没有内容的评论；不含任何用户身份字段。"""
    _check_business(data)
    try:
        lst = data["data"]["result"]["list"]
        raw = lst.get("comments", []) if isinstance(lst, dict) else lst
    except (KeyError, TypeError) as e:
        raise InvalidResponseError(
            f"评论结构不符合预期（缺少 {e}）", service=XhsClient.service, status_code=200
        ) from e
    if not isinstance(raw, list):
        raise InvalidResponseError("评论列表不是数组", service=XhsClient.service, status_code=200)
    out: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict) or c.get("hidden") or c.get("invalid"):
            continue
        content = clean_text(strip_mentions(str(c.get("content") or ""), c.get("at_users")))
        if not content:
            continue
        out.append(
            {"comment_id": str(c.get("id") or ""), "note_id": note_id, "content": content, "likes": _int(c.get("like_count"))}
        )
    return out
