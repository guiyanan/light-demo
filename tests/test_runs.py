"""Step 8：运行记录保存与读取（R15）。"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import responses

from app.clients.http import GLOBAL_COUNTER
from app.pipeline import runs
from app.pipeline.collect import CollectResult

NOW = dt.datetime(2026, 11, 4, 21, 30, 5, tzinfo=dt.UTC)


def sample_result(keyword: str = "低温酸奶") -> CollectResult:
    notes = [
        {"note_id": f"n{i}", "keyword": keyword, "rank": i, "title": f"标题{i}", "desc": "正文 #酸奶", "liked": i,
         "collected": 0, "comments": 2, "shared": 0, "note_type": "normal", "tags": ["酸奶"], "is_ad": i == 2,
         "xsec_token": f"tok{i}"}
        for i in (1, 2)
    ]
    comments = [{"comment_id": "c1", "note_id": "n1", "content": "好喝", "likes": 3}]
    raw = [
        {"kind": "search", "keyword": keyword, "page": 1, "response": {"code": 0, "data": {"x": 1}}},
        {"kind": "comments", "note_id": "n1", "response": {"code": 0, "data": {"y": 2}}},
    ]
    stats = {"keywords": [keyword], "notes": 2, "ads": 1, "comments": 1, "api_calls": {"xhs": 3}, "points": 60,
             "comment_errors": []}
    return CollectResult(keywords=[keyword], notes=notes, comments=comments, raw=raw, stats=stats)


def test_save_layout(tmp_path):
    run_id = runs.save_collect(sample_result(), tmp_path, now=NOW, suffix="abcd")
    assert run_id == "20261104-213005-abcd"
    d = tmp_path / run_id
    assert sorted(p.name for p in d.iterdir()) == ["comments.jsonl", "notes.jsonl", "raw", "stats.json"]
    assert sorted(p.name for p in (d / "raw").iterdir()) == ["001_search.json", "002_comments.json"]
    lines = (d / "notes.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["note_id"] for x in lines] == ["n1", "n2"]
    assert "标题1" in lines[0]  # 中文直接保存，便于直接打开查看
    stats = json.loads((d / "stats.json").read_text(encoding="utf-8"))
    assert stats["run_id"] == run_id
    assert stats["created_at"] == "2026-11-04T21:30:05+00:00"
    assert stats["api_calls"] == {"xhs": 3} and stats["points"] == 60
    assert stats["steps"] == ["collect"]


def test_load_roundtrip(tmp_path):
    res = sample_result()
    run_id = runs.save_collect(res, tmp_path, now=NOW, suffix="abcd")
    run = runs.load_run(tmp_path, run_id)
    assert run.run_id == run_id
    assert run.notes == res.notes
    assert run.comments == res.comments
    assert run.stats["keywords"] == ["低温酸奶"]
    assert run.raw_files() == ["001_search.json", "002_comments.json"]


def test_list_runs_newest_first(tmp_path):
    a = runs.save_collect(sample_result("早"), tmp_path, now=NOW, suffix="aaaa")
    b = runs.save_collect(sample_result("晚"), tmp_path, now=NOW + dt.timedelta(minutes=5), suffix="bbbb")
    c = runs.save_collect(sample_result("中"), tmp_path, now=NOW + dt.timedelta(minutes=1), suffix="cccc")
    (tmp_path / "broken").mkdir()  # 没有 stats.json 的目录被跳过
    listed = runs.list_runs(tmp_path)
    assert [r.run_id for r in listed] == [b, c, a]
    assert listed[0].keywords == ["晚"] and listed[0].notes == 2 and listed[0].comments == 1
    assert "晚" in listed[0].label()


def test_list_runs_empty_or_missing_dir(tmp_path):
    assert runs.list_runs(tmp_path / "nope") == []


@responses.activate
def test_load_makes_zero_api_calls(tmp_path):
    run_id = runs.save_collect(sample_result(), tmp_path, now=NOW, suffix="abcd")
    before = GLOBAL_COUNTER.total()
    runs.list_runs(tmp_path)
    runs.load_run(tmp_path, run_id)
    assert len(responses.calls) == 0
    assert GLOBAL_COUNTER.total() == before


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "..", "x\\y"])
def test_load_rejects_bad_run_id(tmp_path, bad):
    with pytest.raises(runs.RunNotFound):
        runs.load_run(tmp_path, bad)


def test_load_missing_run(tmp_path):
    with pytest.raises(runs.RunNotFound):
        runs.load_run(tmp_path, "20260101-000000-zzzz")


def test_runs_dir_from_config(tmp_path):
    from app.config import load_config

    cfg = load_config({})
    assert runs.runs_dir(cfg).parts[-2:] == ("data", "runs")
    assert runs.runs_dir(cfg).is_absolute()


@responses.activate
def test_collect_and_save(tmp_path):
    """抓取 + 保存一步完成（网页调用）：接口模拟，结果写入指定目录。"""
    from app.config import load_config
    from tests.test_collect import BASE, serve_comments, serve_search

    cfg = load_config({"XHS_BASE_URL": BASE, "XHS_API_KEY": "test-key-123456"})
    serve_search()
    serve_comments(default_likes=[3, 1, 2])
    run_id = runs.collect_and_save(["低温酸奶"], cfg, base_dir=tmp_path, now=NOW, sleep=lambda s: None)
    run = runs.load_run(tmp_path, run_id)
    assert len(run.notes) == 10 and len(run.comments) == 20
    assert run.stats["api_calls"] == {"xhs": 11}  # 每次运行单独计数


def test_collect_and_save_requires_xhs_config(tmp_path):
    from app.config import ConfigError, load_config

    with pytest.raises(ConfigError):
        runs.collect_and_save(["低温酸奶"], load_config({}), base_dir=tmp_path)
