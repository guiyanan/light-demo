"""Step 17（网页部分）：「④产品图」页（R13、R10、R11）。生图全部模拟，不发真实请求。"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.clients.http import ServerError
from app.config import load_config
from app.pipeline import runs
from tests.test_images_run import FakeImage, run  # noqa: F401  (run 为夹具)

PAGE = str(Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "4_产品图与导出.py")


@pytest.fixture()
def fake(monkeypatch, run):  # noqa: F811
    base, run_id = run
    state = {"calls": [], "fail": {}, "dir": base, "run_id": run_id}
    monkeypatch.setattr(runs, "runs_dir", lambda cfg: base)
    real = runs.images_and_save

    def fake_images_and_save(rid, cfg=None, *, only=None, **kw):
        state["calls"].append(only)
        fail, state["fail"] = state["fail"], {}
        return real(rid, load_config(env={}), base_dir=base, image_client=FakeImage(fail=fail), only=only)

    monkeypatch.setattr(runs, "images_and_save", fake_images_and_save)
    return state


def page() -> AppTest:
    return AppTest.from_file(PAGE, default_timeout=30).run()


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [s.value for s in at.subheader]
    parts += [e.value for e in at.error] + [i.value for i in at.info] + [w.value for w in at.warning]
    return "\n".join(parts)


def images(at: AppTest) -> list:
    return [el for el in at.main if getattr(el, "type", "") == "imgs"]


def test_three_images_shown(fake):
    at = page()
    assert not at.exception and fake["calls"] == []  # 不点按钮不调用
    assert not images(at)
    at.button(key="image_run").click().run()
    assert not at.exception and fake["calls"] == [None]
    assert len(images(at)) == 3
    text = texts(at)
    for name in ("小牛活菌宝宝杯", "零添加清爽发酵乳", "厚奶皮高蛋白酸奶"):
        assert name in text
    # 重新打开页面：直接展示已保存的图，不再调用
    at2 = page()
    assert len(images(at2)) == 3 and len(fake["calls"]) == 1


def test_single_image_retry(fake):
    fake["fail"] = {"零添加清爽发酵乳": ServerError("HTTP 503：busy", service="image", status_code=503,
                                                  request_id="rid-img-ui")}
    at = page()
    at.button(key="image_run").click().run()
    assert not at.exception
    err = texts(at)
    assert "rid-img-ui" in err and "第 2 张" in err
    assert len(images(at)) == 3  # 失败的那张显示灰色占位图
    at.button(key="image_retry_2").click().run()
    assert fake["calls"][-1] == [2]  # 只重画第 2 张
    assert "rid-img-ui" not in texts(at)


def test_concepts_missing_hint(fake):
    (fake["dir"] / fake["run_id"] / "concepts.json").unlink()
    at = page()
    assert not at.exception
    assert any("新品概念" in i.value for i in at.info)
    assert fake["calls"] == []
