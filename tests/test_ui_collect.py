"""Step 9：网页「①数据抓取」页（R13、R5、R15）。pipeline 全部模拟，不发真实请求。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.clients.http import QuotaError
from app.pipeline import runs
from tests.test_runs import NOW, sample_result

PAGE = str(Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "1_数据抓取.py")


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    """runs_dir 指向临时目录；collect_and_save 记录参数并保存一份样例结果。"""
    state = {"calls": [], "fail": None, "n": 0}
    monkeypatch.setattr(runs, "runs_dir", lambda cfg: tmp_path)

    def fake_collect_and_save(keywords, cfg=None, **kw):
        state["calls"].append(list(keywords))
        if state["fail"]:
            err, state["fail"] = state["fail"], None
            raise err
        state["n"] += 1
        return runs.save_collect(
            sample_result("、".join(keywords)), tmp_path, now=NOW + dt.timedelta(minutes=state["n"]),
            suffix=f"run{state['n']}",
        )

    monkeypatch.setattr(runs, "collect_and_save", fake_collect_and_save)
    state["dir"] = tmp_path
    return state


def page() -> AppTest:
    return AppTest.from_file(PAGE, default_timeout=30).run()


def all_text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [s.value for s in at.subheader]
    parts += [str(m.value) for m in at.metric] + [m.label for m in at.metric]
    return "\n".join(parts)


def test_input_keywords_and_run(fake):
    at = page()
    assert not at.exception
    assert fake["calls"] == []  # 不点按钮不调用接口
    at.text_input[0].set_value("低温酸奶，发酵乳 冷萃").run()
    at.button(key="collect_run").click().run()
    assert not at.exception
    assert fake["calls"] == [["低温酸奶", "发酵乳", "冷萃"]]
    text = all_text(at)
    assert "标题1" in text and "标题2" in text
    assert "好喝" in text  # 评论展示
    assert any(m.label == "笔记" and str(m.value) == "2" for m in at.metric)


def test_ad_label_shown(fake):
    at = page()
    at.button(key="collect_run").click().run()
    text = all_text(at)
    assert "广告" in text
    # 只有 n2 是广告：广告标签出现在标题2 那一行
    line = next(x for x in text.splitlines() if "标题2" in x)
    assert "广告" in line
    line1 = next(x for x in text.splitlines() if "标题1" in x)
    assert "广告" not in line1


def test_select_history_run(fake):
    old = runs.save_collect(sample_result("历史关键词"), fake["dir"], now=NOW - dt.timedelta(days=1), suffix="old")
    at = page()
    assert fake["calls"] == []
    sb = at.selectbox(key="collect_history")
    assert old in sb.options or any("历史关键词" in o for o in sb.options)
    sb.select(old).run()
    assert not at.exception
    assert fake["calls"] == []  # 打开历史结果不调用接口
    text = all_text(at)
    assert "历史关键词" in text and "标题1" in text


def test_new_run_selected_in_history(fake):
    runs.save_collect(sample_result("历史关键词"), fake["dir"], now=NOW - dt.timedelta(days=1), suffix="old")
    at = page()
    at.text_input[0].set_value("新关键词").run()
    at.button(key="collect_run").click().run()
    assert at.selectbox(key="collect_history").value.endswith("run1")


def test_error_shows_summary_request_id_and_retry(fake):
    fake["fail"] = QuotaError("HTTP 402：points not enough", service="xhs", status_code=402, request_id="rid123abc")
    at = page()
    at.button(key="collect_run").click().run()
    assert not at.exception
    err = " ".join(e.value for e in at.error)
    assert "额度不足" in err and "rid123abc" in err
    # 重试这一步：再次调用并成功
    at.button(key="collect_retry").click().run()
    assert len(fake["calls"]) == 2
    assert not at.error
    assert "标题1" in all_text(at)


def test_keyword_error_shown(fake):
    at = page()
    at.text_input[0].set_value("a b c d").run()
    at.button(key="collect_run").click().run()
    assert fake["calls"] == []
    assert any("最多 3 个" in e.value for e in at.error)
