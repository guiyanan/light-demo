"""Step 12：网页「②需求洞察」页（R13、R8a、R8b）。大模型全部模拟，不发真实请求。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.clients.http import ServerError
from app.pipeline import insight, runs
from tests.test_insight import COMMENTS, FIVE, NOTES, FakeLlm, _collect_result

PAGE = str(Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "2_需求洞察.py")
NOW = dt.datetime(2026, 11, 5, 12, 0)


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    """runs_dir 指向临时目录，放一次抓取结果；insight_and_save 用假大模型。"""
    state = {"calls": [], "fail": None, "dir": tmp_path}
    monkeypatch.setattr(runs, "runs_dir", lambda cfg: tmp_path)
    state["run_id"] = runs.save_collect(_collect_result(), tmp_path, now=NOW, suffix="ui01")

    def fake_insight_and_save(run_id, cfg=None, **kw):
        state["calls"].append(run_id)
        if state["fail"]:
            err, state["fail"] = state["fail"], None
            raise err
        res = insight.extract(FakeLlm(FIVE), NOTES, COMMENTS)
        runs.save_insight(tmp_path, run_id, res)
        return res.to_dict()

    monkeypatch.setattr(runs, "insight_and_save", fake_insight_and_save)
    return state


def page() -> AppTest:
    return AppTest.from_file(PAGE, default_timeout=30).run()


def all_text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [s.value for s in at.subheader]
    parts += [str(m.value) for m in at.metric] + [m.label for m in at.metric] + [w.value for w in at.warning]
    return "\n".join(parts)


def test_five_needs_with_quotes_rendered(fake):
    at = page()
    assert not at.exception
    assert fake["calls"] == []  # 不点按钮不调用
    at.button(key="insight_run").click().run()
    assert not at.exception
    assert fake["calls"] == [fake["run_id"]]
    text = all_text(at)
    for n in FIVE["needs"]:
        assert n["title"] in text
    assert "请问一天吃多少的量？" in text and "好难吃，一股香精味" in text
    assert "评论" in text and "正文" in text
    # 重新打开页面：直接展示已保存结果，不再调用
    at2 = page()
    assert "想知道吃多少合适" in all_text(at2) and len(fake["calls"]) == 1


def test_ad_quote_labeled(fake):
    at = page()
    at.button(key="insight_run").click().run()
    sources = [x for x in all_text(at).splitlines() if x.startswith("出自")]
    ad = next(x for x in sources if x.startswith("出自 #5"))  # N5 是广告帖
    normal = next(x for x in sources if x.startswith("出自 #2"))
    assert "广告帖" in ad and "广告帖" not in normal


def test_error_and_retry(fake):
    fake["fail"] = ServerError("HTTP 503：busy", service="llm", status_code=503, request_id="rid-llm-1")
    at = page()
    at.button(key="insight_run").click().run()
    assert not at.exception
    err = " ".join(e.value for e in at.error)
    assert "rid-llm-1" in err and "服务端" in err
    at.button(key="insight_retry").click().run()
    assert len(fake["calls"]) == 2 and not at.error
    assert "想知道吃多少合适" in all_text(at)


def test_no_collect_run_yet(monkeypatch, tmp_path):
    monkeypatch.setattr(runs, "runs_dir", lambda cfg: tmp_path)
    at = page()
    assert not at.exception
    assert any("数据抓取" in i.value for i in at.info)
