"""合成展示图（R11、R12）。

1600×1000 PNG：
- 左侧 1000×1000：AI 生成的包装效果图（铺满裁切）；生成失败时用灰色占位图
- 左上角 LOGO_BOX：预留 logo 位置，暂不放任何内容（R12：程序不生成、不仿制 logo）
- 右侧 600 宽信息栏：品名、一句话卖点、目标人群、主要原料、包装与规格、营养估算（标「AI 估算」）
- 右下角小字水印「AI 概念图 · 非最终设计」
图上文字全部来自概念数据；内容太长时自动缩小字号，保证不超出画面。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

logger = logging.getLogger(__name__)

W, H = 1600, 1000
LEFT = 1000
PANEL_X = LEFT + 48
PANEL_W = W - PANEL_X - 48
TOP, BOTTOM = 64, H - 80  # 正文可用的上下边界（底部留给水印）
LOGO_BOX = (32, 32, 232, 132)  # 左上角预留 logo 位置（200×100），客户提供 logo 后贴到这里
PLACEHOLDER_GRAY = (200, 200, 200)
WATERMARK = "AI 概念图 · 非最终设计"

BG = (255, 255, 255)
INK = (33, 37, 41)
MUTED = (108, 117, 125)
ACCENT = (0, 94, 170)

# 按顺序查找中文字体：Docker / CI（Debian fonts-noto-cjk）→ macOS 本机
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]
BOLD_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
]


def find_font(bold: bool = False) -> str | None:
    for p in (BOLD_CANDIDATES if bold else []) + FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


@lru_cache(maxsize=64)
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = find_font(bold)
    if path is None:
        logger.error("找不到中文字体，信息栏会显示方框", extra={"event": "font_missing", "error_type": "FontMissing"})
        return ImageFont.load_default(size)
    return ImageFont.truetype(path, size)


@dataclass
class RenderResult:
    image: Image.Image
    lines: list[tuple[str, str]] = field(default_factory=list)  # (字段, 这一行文字)
    positions: dict[str, tuple[int, int]] = field(default_factory=dict)  # 字段第一行的位置
    boxes: list[tuple[int, int]] = field(default_factory=list)  # 每行 (y, 行高)
    placeholder: bool = False

    def to_png(self) -> bytes:
        buf = io.BytesIO()
        self.image.save(buf, "PNG")
        return buf.getvalue()


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """按像素宽度逐字换行（中文没有空格，不能按词换行）；拼回去与原文完全一致。"""
    lines, cur = [], ""
    for ch in text:
        if cur and font.getlength(cur + ch) > width:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur or not lines:
        lines.append(cur)
    return lines


def wrap_at(text: str, font: ImageFont.FreeTypeFont, width: int, sep: str = " · ") -> list[str]:
    """只在分隔符处换行（营养数字不会被拆开）；单段过长时再逐字换行。拼回去与原文完全一致。"""
    parts = text.split(sep)
    segs = [p + sep for p in parts[:-1]] + [parts[-1]]
    lines, cur = [], ""
    for s in segs:
        if cur and font.getlength((cur + s).rstrip()) > width:
            lines.append(cur)
            cur = ""
        if font.getlength(s.rstrip()) > width:
            *full, cur = wrap(cur + s, font, width)
            lines += full
        else:
            cur += s
    if cur or not lines:
        lines.append(cur)
    return lines


def nutrition_text(n: dict[str, Any]) -> str:
    return (f"蛋白质 {n['protein_g']:g}g · 碳水 {n['carbs_g']:g}g · 糖 {n['sugar_g']:g}g · "
            f"脂肪 {n['fat_g']:g}g · 热量 {n['energy_kcal']:g}kcal")


def panel_items(c: dict[str, Any]) -> list[tuple[str, str, int, bool, tuple[int, int, int], int]]:
    """信息栏内容：(字段, 文字, 基础字号, 粗体, 颜色, 段后间距)。"""
    return [
        ("name", c["name"], 44, True, INK, 16),
        ("tagline", c["tagline"], 26, False, ACCENT, 40),
        ("label_target", "目标人群", 20, True, MUTED, 6),
        ("target", c["target"], 24, False, INK, 26),
        ("label_ingredients", "主要原料", 20, True, MUTED, 6),
        ("ingredients", "、".join(c["ingredients"]), 24, False, INK, 26),
        ("label_packaging", "包装与规格", 20, True, MUTED, 6),
        ("packaging", f"{c['packaging']} · {c['spec']}", 24, False, INK, 26),
        ("label_nutrition", f"营养估算（每 100g · {c['nutrition'].get('label', 'AI 估算')}）", 20, True, MUTED, 6),
        ("nutrition", nutrition_text(c["nutrition"]), 24, False, INK, 0),
    ]


def _layout(items, scale: float):
    """按缩放比例排版，返回 [(字段, 行文字, 字体, 颜色, y, 行高)] 和最终高度。"""
    out, y = [], TOP
    for fld, text, size, bold, color, gap in items:
        font = load_font(max(12, round(size * scale)), bold)
        lh = round(font.size * 1.4)
        for line in (wrap_at if fld == "nutrition" else wrap)(text, font, PANEL_W):
            out.append((fld, line, font, color, y, lh))
            y += lh
        y += round(gap * scale)
    return out, y


def _left_image(image_bytes: bytes | None) -> tuple[Image.Image, bool]:
    if image_bytes:
        try:
            src = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            # 铺满 1000×1000：按短边缩放后居中裁切
            s = max(LEFT / src.width, H / src.height)
            src = src.resize((max(LEFT, round(src.width * s)), max(H, round(src.height * s))), Image.LANCZOS)
            x, y = (src.width - LEFT) // 2, (src.height - H) // 2
            return src.crop((x, y, x + LEFT, y + H)), False
        except (UnidentifiedImageError, OSError, ValueError) as e:
            logger.warning(f"效果图无法打开，改用灰色占位图：{e}",
                           extra={"event": "render_bad_image", "error_type": type(e).__name__})
    return Image.new("RGB", (LEFT, H), PLACEHOLDER_GRAY), True


def render_card(concept: dict[str, Any], image_bytes: bytes | None) -> RenderResult:
    canvas = Image.new("RGB", (W, H), BG)
    left, placeholder = _left_image(image_bytes)
    canvas.paste(left, (0, 0))
    draw = ImageDraw.Draw(canvas)
    res = RenderResult(canvas, placeholder=placeholder)

    if placeholder:
        f = load_font(28)
        msg = "效果图生成失败，可点「重试」重新生成"
        draw.text(((LEFT - f.getlength(msg)) / 2, H / 2 - 20), msg, font=f, fill=(120, 120, 120))

    items = panel_items(concept)
    scale = 1.0
    laid, bottom = _layout(items, scale)
    while bottom > BOTTOM and scale > 0.5:  # 太长就缩小字号
        scale -= 0.05
        laid, bottom = _layout(items, scale)
    draw.rectangle((LEFT, 0, LEFT + 6, H), fill=ACCENT)
    for fld, line, font, color, y, lh in laid:
        draw.text((PANEL_X, y), line, font=font, fill=color)
        res.lines.append((fld, line))
        res.positions.setdefault(fld, (PANEL_X, y))
        res.boxes.append((y, lh))

    wf = load_font(18)
    wx, wy = W - 48 - round(wf.getlength(WATERMARK)), H - 48
    draw.text((wx, wy), WATERMARK, font=wf, fill=MUTED)
    res.lines.append(("watermark", WATERMARK))
    res.positions["watermark"] = (wx, wy)
    logger.info("展示图已合成", extra={"event": "render_done", "concept_no": concept.get("no"),
                                    "placeholder": placeholder, "scale": round(scale, 2)})
    return res
