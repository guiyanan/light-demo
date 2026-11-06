"""Step 14：网页「③新品概念」页（R13、R9）。大模型全部模拟，不发真实请求。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.clients.http import RateLimitError
from app.pipeline import concept, runs
from tests.test_concept import BRANDS, INSIGHT, THREE
from tests.test_insight import FakeLlm, _collect_result

PAGE = str(Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "3_新品概念.py")
NOW = dt.datetime(2026, 11, 5, 12, 0)


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    """临时目录里放一次抓取 + 需求洞察结果；concept_and_save 用假大模型。"""
    state = {"calls": [], "fail": None, "dir": tmp_path, "replies": [THREE]}
    monkeypatch.setattr(runs, "runs_dir", lambda cfg: tmp_path)
    state["run_id"] = runs.save_collect(_collect_result(), tmp_path, now=NOW, suffix="cui1")
    (tmp_path / state["run_id"] / "insight.json").write_text(json.dumps(INSIGHT, ensure_ascii=False), encoding="utf-8")

    def fake_concept_and_save(run_id, cfg=None, **kw):
        state["calls"].append(run_id)
        if state["fail"]:
            err, state["fail"] = state["fail"], None
            raise err
        res = concept.generate(FakeLlm(*state["replies"]), INSIGHT, brands=BRANDS)
        runs.save_concepts(tmp_path, run_id, res)
        return res.to_dict()

    monkeypatch.setattr(runs, "concept_and_save", fake_concept_and_save)
    return state


def page() -> AppTest:
    return AppTest.from_file(PAGE, default_timeout=30).run()


def all_text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [s.value for s in at.subheader]
    parts += [w.value for w in at.warning] + [i.value for i in at.info]
    for t in at.table:
        parts.append(t.value.to_string())
    return "\n".join(parts)


def test_three_cards_all_fields(fake):
    at = page()
    assert not at.exception
    assert fake["calls"] == []  # 不点按钮不调用
    at.button(key="concept_run").click().run()
    assert not at.exception and fake["calls"] == [fake["run_id"]]
    text = all_text(at)
    for c in THREE["concepts"]:
        assert c["name"] in text and c["tagline"] in text
    c0 = THREE["concepts"][0]
    for v in (c0["target"], c0["packaging"], c0["spec"], *c0["ingredients"]):
        assert v in text
    assert "AI 估算" in text and "4.5" in text  # 营养估算
    assert "婴幼儿低温活菌" in text  # 对应需求显示标题
    # 重新打开页面：直接展示已保存结果，不再调用
    assert "小牛活菌宝宝杯" in all_text(page()) and len(fake["calls"]) == 1


def test_error_and_retry(fake):
    fake["fail"] = RateLimitError("HTTP 429：too many", service="llm", status_code=429, request_id="rid-cpt-9")
    at = page()
    at.button(key="concept_run").click().run()
    assert not at.exception
    err = " ".join(e.value for e in at.error)
    assert "rid-cpt-9" in err and "频繁" in err
    at.button(key="concept_retry").click().run()
    assert len(fake["calls"]) == 2 and not at.error
    assert "小牛活菌宝宝杯" in all_text(at)


def test_needs_missing_hint(fake):
    (fake["dir"] / fake["run_id"] / "insight.json").unlink()
    at = page()
    assert not at.exception
    assert any("需求洞察" in i.value for i in at.info)
    assert not [b for b in at.button if b.key == "concept_run"]  # 没有需求时不显示生成按钮
    assert fake["calls"] == []


def test_warnings_shown(fake):
    bad = json.loads(json.dumps(THREE))
    bad["concepts"][1]["tagline"] = "比简爱更清爽"
    fake["replies"] = [bad, bad]  # 重试后仍有竞品名
    at = page()
    at.button(key="concept_run").click().run()
    assert any("简爱" in w.value for w in at.warning)
