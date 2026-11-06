"""新品概念（R9，需求 v1.4）。

输入：第②步的需求（含原话证据）。输出：3 个光明新品概念。
程序检查三件事，有问题整体重试 1 次（重试时把问题告诉模型）：
- 需求编号必须在 1–实际需求条数之内
- 不出现竞品品牌名（名单在 config/app.yaml，不区分大小写）
- 3 个概念的主需求（need_ids 第一个）各不相同（需求少于 3 条时不要求）
重试后仍有问题：去掉越界编号；竞品名、主需求重复写进 warnings，在页面提示。
营养数值统一标注「AI 估算」。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.pipeline.schemas import CONCEPTS_SCHEMA

logger = logging.getLogger(__name__)

AI_ESTIMATE = "AI 估算"
TEXT_FIELDS = ("name", "tagline", "target", "packaging", "spec", "image_prompt")

SYSTEM_PROMPT = """你是光明乳业的新品开发经理。下面是从小红书笔记和评论中提炼的消费者需求（附消费者原话）。
请据此提出 3 个光明低温酸奶 / 发酵乳新品概念。

要求：
1. 每个概念对应 1–3 条需求，need_ids 写需求编号，第一个是这个概念的主需求；3 个概念的主需求要各不相同，覆盖不同的需求。
2. 品名由你起，按光明的新品来写；概念的任何文字里都不要出现以下竞品品牌名：{brands}。
3. 营养为每 100g 估算值：protein_g 蛋白质（克）、carbs_g 碳水（克）、sugar_g 糖（克）、fat_g 脂肪（克）、energy_kcal 热量（千卡），数值要符合低温酸奶的常识范围。
4. image_prompt 用一段话描述包装效果图：包装形状、材质、颜色、画面风格；包装上不要出现任何文字、字母、数字和 logo。
5. 只输出一个 JSON 对象，不要其他文字，格式：
{{"concepts": [{{"name": "品名", "tagline": "一句话卖点", "target": "目标人群", "ingredients": ["主要原料"],
  "packaging": "包装形式", "spec": "规格，如 135g/杯", "nutrition": {{"protein_g": 4.0, "carbs_g": 8.0, "sugar_g": 5.0,
  "fat_g": 3.0, "energy_kcal": 80}}, "need_ids": [1, 2], "image_prompt": "包装效果图描述"}}]}}"""


class ConceptInputError(ValueError):
    """没有可用的需求（还没做②需求洞察）。"""


@dataclass
class ConceptResult:
    concepts: list[dict[str, Any]]
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"concepts": self.concepts, "stats": self.stats}


def build_material(needs: list[dict[str, Any]]) -> str:
    blocks = []
    for n in needs:
        lines = [f"需求 {n['no']}：{n['title']}（{n.get('type', '')}）", f"描述：{n.get('description', '')}"]
        lines += [f"- 原话：{q['text']}" for q in n.get("quotes", [])]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _concept_text(c: dict[str, Any]) -> str:
    return "\n".join([*(str(c.get(k, "")) for k in TEXT_FIELDS), *map(str, c.get("ingredients", []))])


def find_problems(concepts: list[dict[str, Any]], n_needs: int, brands: list[str]) -> dict[str, list[str]]:
    """返回 {"range": [...], "brand": [...], "primary": [...]}，每项是给人看的说明。"""
    out: dict[str, list[str]] = {"range": [], "brand": [], "primary": []}
    for i, c in enumerate(concepts, start=1):
        bad = [x for x in c["need_ids"] if not 1 <= x <= n_needs]
        if bad:
            out["range"].append(f"概念 {i} 的需求编号 {bad} 超出 1–{n_needs}")
        text = _concept_text(c).lower()
        hit = [b for b in brands if b.lower() in text]
        if hit:
            out["brand"].append(f"概念 {i} 出现竞品名：{'、'.join(hit)}")
    if n_needs >= len(concepts):
        primaries = [c["need_ids"][0] for c in concepts if c["need_ids"]]
        dups = sorted({p for p in primaries if primaries.count(p) > 1})
        if dups:
            out["primary"].append(f"多个概念的主需求相同（需求 {dups}）")
    return out


def _finalize(data: dict[str, Any], needs: list[dict[str, Any]], problems: dict[str, list[str]]) -> tuple[list, list]:
    titles = {n["no"]: n["title"] for n in needs}
    concepts, warnings = [], []
    for i, c in enumerate(data["concepts"], start=1):
        ids = [x for x in c["need_ids"] if x in titles]
        if not ids:
            warnings.append(f"概念 {i} 没有对应到有效的需求编号")
        nutrition = {k: c["nutrition"][k] for k in ("protein_g", "carbs_g", "sugar_g", "fat_g", "energy_kcal")}
        concepts.append({
            "no": i,
            **{k: str(c[k]).strip() for k in TEXT_FIELDS},
            "ingredients": [s.strip() for s in c["ingredients"]],
            "nutrition": {**nutrition, "label": AI_ESTIMATE},
            "need_ids": ids,
            "needs": [{"no": x, "title": titles[x]} for x in ids],
        })
    warnings += problems["brand"] + problems["primary"]
    return concepts, warnings


def generate(
    llm: Any,
    insight: dict[str, Any],
    *,
    brands: list[str],
    cache_dir: Path | None = None,
) -> ConceptResult:
    """调用大模型生成 3 个概念并检查。llm 需提供 chat_json(messages, schema, cache_dir=..., operation=...)。"""
    needs = list(insight.get("needs") or [])
    if not needs:
        raise ConceptInputError("还没有需求，请先完成「②需求洞察」")
    start = time.perf_counter()
    system = SYSTEM_PROMPT.format(brands="、".join(brands) if brands else "（无）")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": build_material(needs)}]
    logger.info("新品概念开始", extra={"event": "concept_start", "count": len(needs)})

    res = llm.chat_json(messages, CONCEPTS_SCHEMA, cache_dir=cache_dir, operation="concept")
    calls, cached_all = res.attempts, res.cached
    problems = find_problems(res.data["concepts"], len(needs), brands)
    retried = False
    if any(problems.values()):
        retried = True
        issues = "；".join(problems["range"] + problems["brand"] + problems["primary"])
        logger.warning(f"新品概念有问题，整体重试 1 次：{issues}", extra={"event": "concept_retry"})
        hint = (f"上一次的结果有问题：{issues}。请修正后重新输出完整 JSON：需求编号只能在 1–{len(needs)} 之内，"
                "不要出现竞品名，3 个概念的主需求各不相同。")
        res = llm.chat_json([*messages, {"role": "user", "content": hint}], CONCEPTS_SCHEMA, cache_dir=cache_dir,
                            operation="concept_retry")
        calls += res.attempts
        cached_all = cached_all and res.cached
        problems = find_problems(res.data["concepts"], len(needs), brands)

    concepts, warnings = _finalize(res.data, needs, problems)
    stats = {"concepts": len(concepts), "needs": len(needs), "retried": retried, "warnings": warnings,
             "llm_calls": calls, "cached": cached_all}
    elapsed = round((time.perf_counter() - start) * 1000)
    if warnings:
        logger.warning(f"新品概念仍有提示：{'；'.join(warnings)}", extra={"event": "concept_warnings"})
    logger.info("新品概念完成", extra={"event": "concept_done", "elapsed_ms": elapsed,
                                     **{k: v for k, v in stats.items() if k != "warnings"}})
    return ConceptResult(concepts, stats)
