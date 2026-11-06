"""Step 16：合成展示图（R11、R12）。只用 Pillow 本地合成，不调用任何接口。"""

from __future__ import annotations

import io

from PIL import Image

from app.pipeline import render

CONCEPT = {
    "no": 1,
    "name": "光明倍护A2婴幼儿低温发酵乳",
    "tagline": "A2 奶源低温活菌，宝宝断奶期的温和好伙伴",
    "target": "6–36 个月宝宝的父母，关注肠道健康与配料干净",
    "ingredients": ["A2 生牛乳", "鼠李糖乳杆菌 LGG", "嗜热链球菌", "低聚半乳糖", "维生素 D3", "乳铁蛋白"],
    "packaging": "磨砂半透明 PP 小杯，三连杯装",
    "spec": "90g/杯",
    "nutrition": {"protein_g": 3.6, "carbs_g": 8.2, "sugar_g": 5.0, "fat_g": 3.4, "energy_kcal": 78, "label": "AI 估算"},
    "need_ids": [1, 2],
    "needs": [{"no": 1, "title": "婴幼儿低温活菌"}],
    "image_prompt": "三只小巧圆润的酸奶杯",
}


def png(color=(180, 200, 230), size=(1024, 1024)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def joined(res, field):
    return "".join(t for f, t in res.lines if f == field)


def test_canvas_1600x1000():
    res = render.render_card(CONCEPT, png())
    assert res.image.size == (1600, 1000) and res.image.mode == "RGB"
    data = res.to_png()
    assert data.startswith(b"\x89PNG") and Image.open(io.BytesIO(data)).size == (1600, 1000)
    # 左侧是效果图（缩放到 1000×1000）
    assert res.image.getpixel((500, 500)) == (180, 200, 230)
    assert res.placeholder is False
    # 非正方形图也能铺满左侧
    assert render.render_card(CONCEPT, png(size=(1536, 1024))).image.getpixel((10, 990)) == (180, 200, 230)


def test_rendered_text_equals_concept_data():
    res = render.render_card(CONCEPT, png())
    n = CONCEPT["nutrition"]
    expected = {
        "name": CONCEPT["name"],
        "tagline": CONCEPT["tagline"],
        "target": CONCEPT["target"],
        "ingredients": "、".join(CONCEPT["ingredients"]),
        "packaging": f"{CONCEPT['packaging']} · {CONCEPT['spec']}",
        "nutrition": (f"蛋白质 {n['protein_g']:g}g · 碳水 {n['carbs_g']:g}g · 糖 {n['sugar_g']:g}g · "
                      f"脂肪 {n['fat_g']:g}g · 热量 {n['energy_kcal']:g}kcal"),
    }
    for field, text in expected.items():
        assert joined(res, field) == text, field  # 自动换行后拼回去与数据完全一致
    assert "AI 估算" in joined(res, "label_nutrition")
    # 营养只在「 · 」处换行，数字和单位不会被拆开
    for f, line in res.lines:
        if f == "nutrition":
            assert line.rstrip().endswith(("g", "kcal", "·"))
    # 除标签 / 水印外，图上的文字全部来自概念数据
    allowed = set(expected) | {"label_target", "label_ingredients", "label_packaging", "label_nutrition", "watermark"}
    assert {f for f, _ in res.lines} <= allowed
    # 很长的原料也不会超出画面（自动缩小字号）
    long = dict(CONCEPT, ingredients=[f"原料{i}号成分" for i in range(10)], target="很长的目标人群描述" * 6)
    res2 = render.render_card(long, png())
    assert joined(res2, "ingredients") == "、".join(long["ingredients"])
    assert all(y + h <= 1000 for y, h in res2.boxes)


def test_cjk_font_available():
    path = render.find_font()
    assert path is not None, "找不到中文字体：Docker / CI 需安装 fonts-noto-cjk"
    font = render.load_font(32)
    left, _, right, _ = font.getbbox("光明酸奶")
    assert right - left > 64  # 中文字真的有字形（不是方框 / 0 宽）


def test_watermark_present():
    res = render.render_card(CONCEPT, png())
    assert joined(res, "watermark") == "AI 概念图 · 非最终设计"
    # 水印画在右下角
    x, y = res.positions["watermark"]
    assert x > 1300 and y > 900


def test_logo_slot_reserved_empty():
    res = render.render_card(CONCEPT, png(color=(10, 120, 60)))
    x0, y0, x1, y1 = render.LOGO_BOX
    assert x1 <= 1000 and (x1 - x0) >= 120  # 在左上角、留出足够大的位置
    region = res.image.crop(render.LOGO_BOX)
    assert set(region.getdata()) == {(10, 120, 60)}  # 暂不放任何内容（R12：不生成、不仿制 logo）
    for f, _ in res.lines:
        px, py = res.positions[f]
        assert not (x0 <= px < x1 and y0 <= py < y1)


def test_failed_image_uses_gray_placeholder():
    for bad in (None, b"not an image"):
        res = render.render_card(CONCEPT, bad)
        assert res.placeholder is True
        assert res.image.getpixel((500, 900)) == render.PLACEHOLDER_GRAY
        assert joined(res, "name") == CONCEPT["name"]  # 信息栏照常生成
