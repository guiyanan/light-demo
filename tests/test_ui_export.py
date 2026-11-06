"""Step 19（网页部分）：「④产品图」页的导出按钮（R16、R13）。生图全部模拟。"""

from __future__ import annotations

from app.pipeline import runs
from tests.test_images_run import run  # noqa: F401  (run 为夹具)
from tests.test_ui_image import fake, page, texts  # noqa: F401  (fake 为夹具)


def downloads(at):
    return [el for el in at.main if getattr(el, "type", "") == "download_button"]


def test_download_buttons(fake, monkeypatch):  # noqa: F811
    calls = []
    real = runs.export_and_save
    monkeypatch.setattr(runs, "export_and_save", lambda base, rid: calls.append(rid) or real(base, rid))
    at = page()
    assert "export_run" not in [b.key for b in at.button]  # 还没有图片，不显示导出
    at.button(key="image_run").click().run()
    assert not at.exception and calls == []  # 不点按钮不导出
    at.button(key="export_run").click().run()
    assert not at.exception and calls == [fake["run_id"]]
    assert len(downloads(at)) == 2
    text = texts(at)
    assert f"data/runs/{fake['run_id']}/export/" in text
    assert (fake["dir"] / fake["run_id"] / "export" / f"光明新品概念报告_{fake['run_id']}.html").is_file()


def test_export_warns_failed_image(fake):  # noqa: F811
    from app.clients.http import ServerError

    fake["fail"] = {"零添加清爽发酵乳": ServerError("HTTP 503：busy", service="image", status_code=503)}
    at = page()
    at.button(key="image_run").click().run()
    at.button(key="export_run").click().run()
    assert not at.exception
    assert any("第 2 张" in w.value and "失败" in w.value for w in at.warning)
    assert len(downloads(at)) == 2
