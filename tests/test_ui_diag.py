"""Step 5：网页「接口检查」页（R13、R14）。diag 全部模拟，不发真实请求。"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app import diag

PAGE = str(Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "0_接口检查.py")


@pytest.fixture()
def fake_diag(monkeypatch):
    calls = []

    def fake(service, **kw):
        calls.append(service)
        ok = service != "llm"
        return diag.CheckResult(
            service=service,
            label=diag.SERVICES[service],
            ok=ok,
            status_code=200 if ok else 401,
            elapsed_ms=123,
            message="连通正常" if ok else "密钥无效或无权限",
            request_id=f"rid-{service}",
            error_type=None if ok else "auth_failed",
            raw_files=["logs/diag/x.json"],
        )

    monkeypatch.setattr(diag, "run_check", fake)
    return calls


def test_page_renders_three_checks(fake_diag):
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert not at.exception
    labels = [b.label for b in at.button]
    assert len(labels) == 3
    assert any("聚道云" in x for x in labels)
    assert any("Kimi" in x for x in labels)
    assert any("生图" in x for x in labels)
    assert fake_diag == []  # 不点按钮不调用接口


def test_click_shows_success_and_failure(fake_diag):
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    at.button[0].click().run()
    assert fake_diag == ["xhs"]
    assert any("成功" in s.value for s in at.success)
    at.button[1].click().run()
    assert fake_diag == ["xhs", "llm"]
    errors = " ".join(e.value for e in at.error)
    assert "失败" in errors and "401" in errors and "rid-llm" in errors
    # 之前的成功结果仍然保留
    assert any("成功" in s.value for s in at.success)
