"""Step 18：导出 HTML 报告 + 图片 zip（R16，需求 v1.5）。生图一律模拟，不发真实请求。"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile

import pytest

from app.clients.http import ServerError
from app.config import load_config
from app.pipeline import export, runs
from tests.test_concept import INSIGHT
from tests.test_images_run import FakeImage, run  # noqa: F401  (run 为夹具)


def make_images(base, run_id, fail=None):
    return runs.images_and_save(run_id, load_config(env={}), base_dir=base, image_client=FakeImage(fail=fail))


def concepts_of(base, run_id):
    return runs.load_concepts(base, run_id)["concepts"]


@pytest.fixture()
def done(run):  # noqa: F811
    base, run_id = run
    make_images(base, run_id)
    return base, run_id


def test_report_contains_needs_quotes_concepts(done):
    base, run_id = done
    res = export.build(base, run_id)
    h = res.html
    assert "低温酸奶" in h  # 关键词
    assert "2026-11-05 20:00" in h  # 抓取时间（北京时间）
    for n in INSIGHT["needs"]:
        assert n["title"] in h and n["description"] in h and n["type"] in h
        for q in n["quotes"]:
            assert q["text"] in h and q["note_title"] in h  # 原话 + 出处笔记标题
    for c in concepts_of(base, run_id):
        for key in ("name", "tagline", "target", "packaging", "spec"):
            assert c[key] in h
        for ing in c["ingredients"]:
            assert ing in h
        for nd in c["needs"]:
            assert nd["title"] in h
        assert f"{c['nutrition']['protein_g']:g}" in h
    assert "AI 估算" in h
    assert res.html_name == f"光明新品概念报告_{run_id}.html"


def test_images_embedded(done):
    base, run_id = done
    h = export.build(base, run_id).html
    srcs = re.findall(r'<img[^>]+src="([^"]+)"', h)
    assert len(srcs) == 3
    for s in srcs:
        assert s.startswith("data:image/png;base64,")
        assert base64.b64decode(s.split(",", 1)[1]).startswith(b"\x89PNG")


def test_ad_label_in_report(done):
    base, run_id = done
    h = export.build(base, run_id).html
    ad_quotes = [q for n in INSIGHT["needs"] for q in n["quotes"] if q["is_ad"]]
    plain = [q for n in INSIGHT["needs"] for q in n["quotes"] if not q["is_ad"]]
    assert ad_quotes and plain
    assert h.count('class="ad"') == len(ad_quotes)
    # 每条广告原话后面紧跟「广告」标注
    for q in ad_quotes:
        tail = h.split(export.esc(q["text"]), 1)[1][:400]
        assert "广告" in tail


def test_zip_contains_cards_and_raw(done):
    base, run_id = done
    res = export.build(base, run_id)
    names = [c["name"] for c in concepts_of(base, run_id)]
    with zipfile.ZipFile(io.BytesIO(res.zip_bytes)) as z:
        files = sorted(z.namelist())
        assert files == sorted([f"{i}_{n}.png" for i, n in enumerate(names, 1)]
                               + [f"{i}_{n}_原图.png" for i, n in enumerate(names, 1)])
        for f in files:
            assert z.read(f).startswith(b"\x89PNG")
    assert res.zip_name == f"光明新品概念图片_{run_id}.zip"


def test_no_user_identity_or_links(done):
    base, run_id = done
    res = export.build(base, run_id)
    h = res.html
    assert "http://" not in h and "https://" not in h and "xiaohongshu" not in h  # 不放笔记链接、不引用外部资源
    for n in INSIGHT["needs"]:
        for q in n["quotes"]:
            assert q["note_id"] not in h
    for word in ("昵称", "nickname", "user_id", "avatar", "头像", "ip_location", "IP 属地"):
        assert word not in h
    with zipfile.ZipFile(io.BytesIO(res.zip_bytes)) as z:
        assert not any(f.endswith((".json", ".jsonl")) for f in z.namelist())


def test_failed_image_still_exports(run):  # noqa: F811
    base, run_id = run
    names = [c["name"] for c in concepts_of(base, run_id)]
    err = ServerError("HTTP 503：busy", service="image", status_code=503, request_id="rid-x")
    make_images(base, run_id, fail={names[1]: err})
    res = export.build(base, run_id)
    assert res.failed == [2]
    assert res.html.count("效果图生成失败") == 1
    with zipfile.ZipFile(io.BytesIO(res.zip_bytes)) as z:
        files = z.namelist()
    assert f"2_{names[1]}.png" in files and f"2_{names[1]}_原图.png" not in files
    assert len(files) == 5


def test_missing_steps_rejected(run):  # noqa: F811
    base, run_id = run
    with pytest.raises(runs.StepInputError, match="产品图"):  # 还没生成图片
        export.build(base, run_id)
    make_images(base, run_id)
    (base / run_id / "concepts.json").unlink()
    with pytest.raises(runs.StepInputError, match="新品概念"):
        export.build(base, run_id)
    with pytest.raises(runs.RunNotFound):
        export.build(base, "nope")


def test_print_one_concept_per_page(done):
    base, run_id = done
    h = export.build(base, run_id).html
    assert h.count('<section class="concept"') == 3
    assert "@media print" in h and re.search(r"\.concept\s*\{[^}]*break-before:\s*page", h)
    assert '<meta charset="utf-8">' in h


def test_html_escaped(run):  # noqa: F811
    base, run_id = run
    path = base / run_id / "concepts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["concepts"][0]["tagline"] = '<script>alert(1)</script> & "好"'
    data["concepts"][0]["name"] = "光明/鲜:酸奶"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    make_images(base, run_id)
    res = export.build(base, run_id)
    assert "<script>" not in res.html and "&lt;script&gt;" in res.html
    with zipfile.ZipFile(io.BytesIO(res.zip_bytes)) as z:
        assert "1_光明_鲜_酸奶.png" in z.namelist()  # 文件名里不能有 / : 等字符


def test_export_and_save(done, read_logs):
    base, run_id = done
    out = runs.export_and_save(base, run_id)
    d = base / run_id / "export"
    assert (d / out.html_name).read_text(encoding="utf-8") == out.html
    assert (d / out.zip_name).read_bytes() == out.zip_bytes
    assert "export" in json.loads((base / run_id / "stats.json").read_text(encoding="utf-8"))["steps"]
    assert any(r.get("event") == "export_done" for r in read_logs("app"))
