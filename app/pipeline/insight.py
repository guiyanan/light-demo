"""需求洞察（R8a、R8b，需求 v1.3）。

输入：每篇笔记的标题 + 正文开头（搜索接口只给前约 60 字）+ 全部保留的评论；广告帖参与并在材料里标明。
笔记在提示词中用代号 N<rank> 指代，模型返回代号，程序再映射回真实 note_id。
原话必须是对应来源原文的子串（正文 = 标题或正文开头；评论 = 这篇笔记的某条评论），否则丢弃并计数；
某条需求丢光原话时整体重试 1 次，仍不合格则标注「缺少证据」。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.pipeline.schemas import NEEDS_SCHEMA

logger = logging.getLogger(__name__)

QUOTE_MARKS = " \t\r\n\"'“”‘’「」『』"

SYSTEM_PROMPT = """你是乳品行业的消费者洞察分析师。下面是小红书上关于低温酸奶 / 发酵乳的笔记和评论。
请提炼 5 条最有价值的消费者需求，供光明乳业开发新品参考。

材料说明：
- 每篇笔记以 [N编号] 开头，包含标题、正文开头（只有前约 60 字）和评论。
- 标有「（广告帖）」的是品牌商业合作笔记，代表竞品在主推的卖点；可以参考，但需求应主要来自消费者的真实反馈。

要求：
1. 需求要具体、可指导产品开发，5 条之间不要重复。type 只能是 "正面"（满意点）、"负面"（不满 / 痛点）、"期待"（希望有的）。
2. 每条需求给 1–2 句原话证据。原话必须从材料中逐字摘抄（可以只摘一段），不得改写、合并或翻译。
3. source 写 "正文"（出自标题或正文开头）或 "评论"；note 写这句话所在笔记的编号，如 "N3"。
4. 只输出一个 JSON 对象，不要其他文字，格式：
{"needs": [{"title": "需求标题（10 字以内）", "description": "一句话描述", "type": "期待",
  "quotes": [{"text": "逐字摘抄的原话", "source": "评论", "note": "N3"}]}]}"""

RETRY_HINT = (
    "注意：上一次的结果里有需求的原话在材料中找不到。原话必须从材料中逐字摘抄，"
    "source 和 note 必须与原话实际所在的位置一致。请重新输出完整 JSON。"
)


class InsightInputError(ValueError):
    """没有可分析的笔记。"""


@dataclass
class InsightResult:
    needs: list[dict[str, Any]]
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"needs": self.needs, "stats": self.stats}


def _clean(text: str) -> str:
    return (text or "").strip(QUOTE_MARKS)


def build_material(notes: list[dict[str, Any]], comments: list[dict[str, Any]]) -> tuple[str, dict[str, dict]]:
    """拼出给大模型看的材料；返回 (材料文字, 代号 → {note, comments})。"""
    by_note: dict[str, list[dict[str, Any]]] = {}
    for c in comments:
        by_note.setdefault(c["note_id"], []).append(c)
    blocks, refs = [], {}
    for i, n in enumerate(notes, start=1):
        ref = f"N{n.get('rank') or i}"
        cs = by_note.get(n["note_id"], [])
        refs[ref] = {"note": n, "comments": cs}
        lines = [f"[{ref}]{'（广告帖）' if n.get('is_ad') else ''}",
                 f"标题：{n.get('title') or '（无标题）'}",
                 f"正文开头：{n.get('desc') or '（无）'}"]
        lines.append("评论：" if cs else "评论：（无）")
        lines += [f"- {c['content'].strip()}" for c in cs]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), refs


def _verify_quote(quote: dict[str, Any], refs: dict[str, dict]) -> dict[str, Any] | None:
    """原话是对应来源原文的子串则返回标准化后的证据，否则返回 None。"""
    text = _clean(quote.get("text", ""))
    ref = refs.get(quote.get("note", ""))
    if not text or ref is None:
        return None
    n = ref["note"]
    if quote.get("source") == "正文":
        pool = [n.get("title") or "", n.get("desc") or ""]
    else:
        pool = [c.get("content") or "" for c in ref["comments"]]
    if not any(text in s for s in pool):
        return None
    return {
        "text": text,
        "source": quote["source"],
        "note_id": n["note_id"],
        "note_rank": n.get("rank"),
        "note_title": n.get("title") or "",
        "is_ad": bool(n.get("is_ad")),
    }


def _check(data: dict[str, Any], refs: dict[str, dict]) -> tuple[list[dict[str, Any]], int]:
    needs, dropped = [], 0
    for i, item in enumerate(data["needs"], start=1):
        kept = []
        for qt in item["quotes"]:
            v = _verify_quote(qt, refs)
            if v is None:
                dropped += 1
                logger.warning(
                    f"原话在原文中找不到，已丢弃：需求 {i}「{str(qt.get('text'))[:40]}」（{qt.get('source')} {qt.get('note')}）",
                    extra={"event": "insight_quote_dropped", "need_no": i},
                )
            else:
                kept.append(v)
        needs.append({
            "no": i,
            "title": item["title"].strip(),
            "description": item["description"].strip(),
            "type": item["type"],
            "quotes": kept,
            "missing_evidence": not kept,
        })
    return needs, dropped


def extract(
    llm: Any,
    notes: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    *,
    cache_dir: Path | None = None,
) -> InsightResult:
    """调用大模型提炼需求并校验原话。llm 需提供 chat_json(messages, schema, cache_dir=..., operation=...)。"""
    if not notes:
        raise InsightInputError("没有可分析的笔记，请先完成「①数据抓取」")
    start = time.perf_counter()
    material, refs = build_material(notes, comments)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": material}]
    logger.info("需求洞察开始", extra={"event": "insight_start", "count": len(notes), "comments": len(comments)})

    calls, cached_all = 0, True
    res = llm.chat_json(messages, NEEDS_SCHEMA, cache_dir=cache_dir, operation="insight")
    calls += res.attempts
    cached_all &= res.cached
    needs, dropped = _check(res.data, refs)
    retried = False
    if any(n["missing_evidence"] for n in needs):
        retried = True
        logger.warning("有需求丢光原话，整体重试 1 次", extra={"event": "insight_retry"})
        res = llm.chat_json(
            [*messages, {"role": "user", "content": RETRY_HINT}], NEEDS_SCHEMA, cache_dir=cache_dir,
            operation="insight_retry",
        )
        calls += res.attempts
        cached_all &= res.cached
        needs, dropped2 = _check(res.data, refs)
        dropped += dropped2

    stats = {
        "needs": len(needs),
        "notes": len(notes),
        "comments": len(comments),
        "ads": sum(1 for n in notes if n.get("is_ad")),
        "dropped_quotes": dropped,
        "missing_evidence": sum(1 for n in needs if n["missing_evidence"]),
        "retried": retried,
        "llm_calls": calls,
        "cached": cached_all,
    }
    elapsed = round((time.perf_counter() - start) * 1000)
    logger.info("需求洞察完成", extra={"event": "insight_done", "elapsed_ms": elapsed, **stats})
    return InsightResult(needs, stats)
