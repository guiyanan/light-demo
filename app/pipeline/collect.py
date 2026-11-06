"""数据抓取流程（R4–R7，需求 v1.2）：搜索 40 篇候选 → 按评论数挑 10 篇 → 每篇取 1 页评论（按点赞排序）
→ 去掉水评论后全部保留。

只负责业务逻辑；接口调用经 app.clients.xhs（重试、日志、打码在 http 层）。
"""

from __future__ import annotations

import logging
import re
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

# ---------------- 水评论（R6 v1.2） ----------------
JUNK_MIN_CHARS = 3  # 去掉表情代码、话题标签、标点后少于 3 个字
JUNK_WORDS = ("代购", "建联", "私信", "私我", "互关", "互粉", "求互", "加v", "加微", "vx", "滴滴我", "dd我", "达人机构")
_EMOJI_CODE_RE = re.compile(r"\[[^\[\]]{1,10}\]")  # [笑哭R]、[goods]
_TOPIC_RE = re.compile(r"#[^#\s]+#?")
_NON_WORD_RE = re.compile(r"[\W_]+")  # 标点、空格、emoji
_PM_RE = re.compile(r"(?<![a-z])(pm|dd)(?![a-z])")


def is_junk(text: str) -> bool:
    """引流 / 代购 / 纯表情 / 纯话题标签 / 太短的评论。提问和吐槽不算水评论。"""
    low = (text or "").lower()
    if any(w in low for w in JUNK_WORDS) or _PM_RE.search(low):
        return True
    core = _NON_WORD_RE.sub("", _TOPIC_RE.sub("", _EMOJI_CODE_RE.sub("", low)))
    return len(core) < JUNK_MIN_CHARS


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


def pick_notes(candidates: list[dict[str, Any]], *, max_notes: int, min_comments: int) -> list[dict[str, Any]]:
    """R4 v1.2：去掉评论数 < min_comments 的笔记，按评论数从多到少取 max_notes 篇（同数保持搜索顺序）。
    返回新记录：rank 重新编号，search_rank 记录在搜索结果中的位置。"""
    kept = [n for n in candidates if n["comments"] >= min_comments]
    kept = sorted(kept, key=lambda n: n["comments"], reverse=True)[: max(max_notes, 0)]
    return [{**n, "search_rank": n["rank"], "rank": i} for i, n in enumerate(kept, start=1)]


def collect(client: XhsClient, keywords: Iterable[str], cfg: CollectConfig) -> CollectResult:
    calls_before = client.http.counter.get(client.service)
    with log_step(logger, "collect_search", keywords=list(keywords)) as step:
        sr = search_top_notes(client, keywords, max_notes=max(cfg.candidate_notes, cfg.max_notes))
        notes = pick_notes(sr.notes, max_notes=cfg.max_notes, min_comments=cfg.min_note_comments)
        step.count = len(notes)
    logger.info(
        f"候选 {len(sr.notes)} 篇，按评论数挑出 {len(notes)} 篇",
        extra={"event": "notes_picked", "candidates": len(sr.notes), "count": len(notes)},
    )

    res = CollectResult(keywords=sr.keywords, notes=notes, comments=[])
    res.raw = [{"kind": "search", **p} for p in sr.raw_pages]
    points = sr.points
    errors: list[dict[str, str]] = []
    junk = 0

    with log_step(logger, "collect_comments", notes=len(notes)) as step:
        for note in notes:
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
            useful = [c for c in parsed if not is_junk(c["content"])]
            junk += len(parsed) - len(useful)
            res.comments.extend(top_comments(useful, min(cfg.comments_per_note, remaining)))
        step.count = len(res.comments)

    res.stats = {
        "keywords": res.keywords,
        "candidates": len(sr.notes),
        "notes": len(res.notes),
        "ads": sum(1 for n in res.notes if n["is_ad"]),
        "comments": len(res.comments),
        "junk_comments": junk,
        "api_calls": {client.service: client.http.counter.get(client.service) - calls_before},
        "points": points,
        "comment_errors": errors,
    }
    logger.info("数据抓取完成", extra={"event": "collect_done", **res.stats})
    return res
