"""数据抓取流程（R4–R7）：搜索前 N 篇笔记 → 每篇取 1 页评论（按点赞排序）→ 每篇留点赞最高的 2 条。

只负责业务逻辑；接口调用经 app.clients.xhs（重试、日志、打码在 http 层）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.clients.http import ApiError, AuthError, QuotaError
from app.clients.xhs import (
    COMMENT_SORT_BY_LIKES,
    COMMENT_SORT_DEFAULT,
    XhsClient,
    comment_count_l1,
    consumed_points,
    parse_comments,
    scrub_identity,
    search_top_notes,
)
from app.config import CollectConfig
from app.logging_setup import log_step

logger = logging.getLogger(__name__)

# 这些错误换一篇笔记也不会好，直接停止整个抓取
FATAL_ERRORS = (AuthError, QuotaError)


@dataclass
class CollectResult:
    keywords: list[str]
    notes: list[dict[str, Any]]
    comments: list[dict[str, Any]]
    raw: list[dict[str, Any]] = field(default_factory=list)  # 已去除身份信息的原始响应
    stats: dict[str, Any] = field(default_factory=dict)


def top_comments(comments: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """按点赞从高到低取前 n 条；点赞相同保持接口顺序（sorted 是稳定排序）。"""
    return sorted(comments, key=lambda c: c["likes"], reverse=True)[: max(n, 0)]


def collect(client: XhsClient, keywords: Iterable[str], cfg: CollectConfig) -> CollectResult:
    calls_before = client.http.counter.get(client.service)
    with log_step(logger, "collect_search", keywords=list(keywords)) as step:
        sr = search_top_notes(client, keywords, max_notes=cfg.max_notes)
        step.count = len(sr.notes)

    res = CollectResult(keywords=sr.keywords, notes=sr.notes, comments=[])
    res.raw = [{"kind": "search", **p} for p in sr.raw_pages]
    points = sr.points
    errors: list[dict[str, str]] = []

    with log_step(logger, "collect_comments", notes=len(sr.notes)) as step:
        for note in sr.notes:
            remaining = cfg.max_comments - len(res.comments)
            if remaining <= 0:
                break
            pages: list[Any] = []
            try:
                for sort in (COMMENT_SORT_BY_LIKES, COMMENT_SORT_DEFAULT):
                    data = client.get_note_comments_raw(
                        note["note_id"], xsec_token=note.get("xsec_token") or None, sort_strategy=sort
                    ).data
                    pages.append(data)
                    parsed = parse_comments(data, note["note_id"])
                    if parsed or sort != COMMENT_SORT_BY_LIKES or comment_count_l1(data) <= 0:
                        break
                    # BUG-003：按点赞排序返回空页但笔记有评论 → 改用默认排序，再自己按点赞取前几条
                    logger.warning(
                        f"笔记 {note['note_id']} 按点赞排序返回空页（实际有 {comment_count_l1(data)} 条评论），改用默认排序",
                        extra={"event": "comment_sort_fallback", "note_id": note["note_id"]},
                    )
            except FATAL_ERRORS:
                raise
            except ApiError as err:
                logger.warning(
                    f"笔记 {note['note_id']} 的评论获取失败，跳过这一篇：{err}",
                    exc_info=True,
                    extra={"event": "comment_fetch_failed", "note_id": note["note_id"],
                           "status_code": err.status_code, "error_type": err.error_type},
                )
                errors.append({"note_id": note["note_id"], "error": str(err)})
                continue
            for data in pages:
                res.raw.append({"kind": "comments", "note_id": note["note_id"], "response": scrub_identity(data)})
                points += consumed_points(data)
            res.comments.extend(top_comments(parsed, min(cfg.comments_per_note, remaining)))
        step.count = len(res.comments)

    res.stats = {
        "keywords": res.keywords,
        "notes": len(res.notes),
        "ads": sum(1 for n in res.notes if n["is_ad"]),
        "comments": len(res.comments),
        "api_calls": {client.service: client.http.counter.get(client.service) - calls_before},
        "points": points,
        "comment_errors": errors,
    }
    logger.info("数据抓取完成", extra={"event": "collect_done", **res.stats})
    return res
