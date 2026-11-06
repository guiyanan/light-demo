"""导出 HTML 报告 + 图片 zip（R16，需求 v1.5）。

- 报告：单个 HTML 文件，图片以 base64 内嵌，不联网也能打开；带打印样式（每个概念单独一页），
  可用浏览器「打印 → 存为 PDF」。不放原笔记链接（链接含临时访问码会失效），不含用户身份信息。
- zip：3 张合成展示图 `<n>_<品名>.png` + AI 原图 `<n>_<品名>_原图.<ext>`；生成失败的那张没有原图。
- 某张图失败照常导出，报告该处写「效果图生成失败」。
只读本地文件，不调用任何接口。
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from app.pipeline import runs
from app.pipeline.render import nutrition_text

logger = logging.getLogger(__name__)

TEMPLATE = Path(__file__).parent / "templates" / "report.html"
BEIJING = dt.timezone(dt.timedelta(hours=8))
_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


@dataclass
class ExportResult:
    html: str
    zip_bytes: bytes
    html_name: str
    zip_name: str
    failed: list[int] = field(default_factory=list)  # 效果图生成失败的概念编号


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def safe_name(text: str) -> str:
    """文件名里去掉 / : 等系统不允许的字符和空白。"""
    return _UNSAFE.sub("_", text).strip("_") or "未命名"


def beijing_time(iso: str) -> str:
    try:
        t = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso or "-")
    if t.tzinfo is None:  # 保存时用的是 UTC
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")


def _needs_html(needs: list[dict[str, Any]]) -> str:
    parts = []
    for n in needs:
        quotes = []
        for q in n.get("quotes", []):
            ad = '<span class="ad">广告</span>' if q.get("is_ad") else ""
            quotes.append(f'<blockquote>“{esc(q["text"])}”<br><span class="src">——{esc(q["source"])} · '
                          f'笔记《{esc(q.get("note_title") or "")}》</span>{ad}</blockquote>')
        if not quotes:
            quotes.append('<p class="warn">没有找到可核对的原话</p>')
        parts.append(f'<div class="need"><h3>{esc(n["no"])}. {esc(n["title"])}<span class="tag">{esc(n["type"])}'
                     f'</span></h3><p>{esc(n["description"])}</p>{"".join(quotes)}</div>')
    return "\n".join(parts)


def _concept_html(c: dict[str, Any], item: dict[str, Any] | None, card: bytes | None) -> str:
    if item and item.get("ok") and card:
        img = f'<img src="data:image/png;base64,{base64.b64encode(card).decode()}" alt="{esc(c["name"])}">'
    else:
        reason = esc(item.get("error") or "") if item else "还没有生成"
        img = (f'<p class="failed">效果图生成失败（{reason}），请回到「④ 产品图」页点「重试第 {esc(c["no"])} 张」后再导出。</p>')
        if card:  # 合成图里信息栏照常，左侧为灰色占位图
            img += f'<img src="data:image/png;base64,{base64.b64encode(card).decode()}" alt="{esc(c["name"])}">'
    n = c["nutrition"]
    needs = "、".join(f'{esc(x["no"])}. {esc(x["title"])}' for x in c.get("needs", []))
    nut = (f'<table><tr><th>蛋白质</th><th>碳水</th><th>糖</th><th>脂肪</th><th>热量</th></tr>'
           f'<tr><td>{n["protein_g"]:g} g</td><td>{n["carbs_g"]:g} g</td><td>{n["sugar_g"]:g} g</td>'
           f'<td>{n["fat_g"]:g} g</td><td>{n["energy_kcal"]:g} kcal</td></tr></table>')
    return (f'<section class="concept"><h2>概念 {esc(c["no"])}：{esc(c["name"])}</h2>'
            f'<p><strong>{esc(c["tagline"])}</strong></p>{img}<dl>'
            f'<dt>目标人群</dt><dd>{esc(c["target"])}</dd>'
            f'<dt>主要原料</dt><dd>{esc("、".join(c["ingredients"]))}</dd>'
            f'<dt>包装</dt><dd>{esc(c["packaging"])}</dd>'
            f'<dt>规格</dt><dd>{esc(c["spec"])}</dd>'
            f'<dt>营养估算（每 100g · {esc(n.get("label", "AI 估算"))}）</dt><dd>{nut}'
            f'<span class="src">{esc(nutrition_text(n))}</span></dd>'
            f'<dt>对应需求</dt><dd>{needs or "-"}</dd></dl></section>')


def _read(d: Path, rel: str | None) -> bytes | None:
    if not rel:
        return None
    p = d / rel
    if not p.is_file():
        logger.warning(f"导出时找不到图片文件：{rel}", extra={"event": "export_image_missing", "path": rel})
        return None
    return p.read_bytes()


def build(base_dir: Path, run_id: str) -> ExportResult:
    """读取这次运行的需求、概念和图片，生成报告和 zip（不写文件）。"""
    run = runs.load_run(base_dir, run_id)
    insight = runs.load_insight(base_dir, run_id)
    if not insight or not insight.get("needs"):
        raise runs.StepInputError("这次还没有提炼需求，请先完成「②需求洞察」")
    concepts = (runs.load_concepts(base_dir, run_id) or {}).get("concepts")
    if not concepts:
        raise runs.StepInputError("这次还没有生成新品概念，请先完成「③新品概念」")
    images = runs.load_images(base_dir, run_id)
    if not images or not images.get("items"):
        raise runs.StepInputError("这次还没有生成图片，请先在「④产品图」页点「生成图片」")

    d = run.path
    by_no = {it["no"]: it for it in images["items"]}
    sections, failed = [], []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for c in concepts:
            item = by_no.get(c["no"])
            card = _read(d, item.get("card")) if item else None
            raw = _read(d, item.get("raw")) if item and item.get("ok") else None
            if not (item and item.get("ok") and card):
                failed.append(c["no"])
            base = f'{c["no"]}_{safe_name(c["name"])}'
            if card:
                z.writestr(f"{base}.png", card)
            if raw:
                z.writestr(f'{base}_原图{Path(item["raw"]).suffix}', raw)
            sections.append(_concept_html(c, item, card))

    s = run.stats
    page = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        title="光明乳业 · 小红书洞察新品概念报告",
        keywords=esc("、".join(s.get("keywords") or []) or "-"),
        created_at=esc(beijing_time(s.get("created_at", ""))),
        notes=esc(s.get("notes", "-")), comments=esc(s.get("comments", "-")), ads=esc(s.get("ads", "-")),
        need_count=len(insight["needs"]), needs=_needs_html(insight["needs"]),
        concept_count=len(concepts), concepts="\n".join(sections), run_id=esc(run_id),
    )
    logger.info("导出内容已生成", extra={"event": "export_built", "run_id": run_id, "failed": failed})
    return ExportResult(html=page, zip_bytes=buf.getvalue(), html_name=f"光明新品概念报告_{run_id}.html",
                        zip_name=f"光明新品概念图片_{run_id}.zip", failed=failed)
