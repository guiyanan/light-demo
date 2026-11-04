"""Step 7：评论抓取 + 每篇取点赞最高 2 条（R6、R7）。

聚道云接口全部用 responses 模拟；评论测试数据来自真实响应（按点赞排序），用户信息已换成假值。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from app.clients import xhs
from app.clients.http import CallCounter
from app.clients.xhs import XhsClient, parse_comments
from app.config import CollectConfig, load_config
from app.pipeline import collect

FIXTURES = Path(__file__).parent / "fixtures" / "xhs"
BASE = "https://xhs.example.com"
SEARCH_URL = BASE + xhs.SEARCH_PATH
COMMENTS_URL = BASE + xhs.COMMENTS_PATH
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
COMMENT_FIELDS = {"comment_id", "note_id", "content", "likes"}
IDENTITY_KEYS = {"user", "nickname", "userid", "user_id", "red_id", "images", "ip_location", "at_users"}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def comments_page(note_id: str, likes: list[int] | None = None) -> dict:
    """真实结构的一页评论；likes 指定每条的点赞数（不指定则用真实数据）。"""
    data = load("comments_like_count.json")
    cs = data["data"]["result"]["list"]["comments"]
    if likes is not None:
        tpl = cs[0]
        cs = []
        for i, n in enumerate(likes):
            c = copy.deepcopy(tpl)
            c.update(id=f"{note_id}-c{i}", like_count=n, content=f"评论{i}（{n} 赞）", at_users=[])
            cs.append(c)
    for c in cs:
        c["note_id"] = note_id
    data["data"]["result"]["list"]["comments"] = cs
    return data


@pytest.fixture()
def cfg():
    return load_config({"XHS_BASE_URL": BASE, "XHS_API_KEY": "test-key-123456"})


@pytest.fixture()
def counter():
    return CallCounter()


@pytest.fixture()
def client(cfg, counter):
    return XhsClient(cfg, sleep=lambda s: None, counter=counter)


def serve_search(page: dict | None = None):
    responses.add(responses.GET, SEARCH_URL, json=page or load("search_page1.json"))


def serve_comments(by_note: dict[str, dict] | None = None, default_likes: list[int] | None = None):
    calls: list[dict] = []

    def cb(request):
        q = {k: v[0] for k, v in parse_qs(urlparse(request.url).query).items()}
        calls.append(q)
        nid = q["note_id"]
        body = (by_note or {}).get(nid) or comments_page(nid, default_likes)
        return 200, JSON_HEADERS, json.dumps(body, ensure_ascii=False)

    responses.add_callback(responses.GET, COMMENTS_URL, callback=cb)
    return calls


# ---------------- 解析 ----------------
def test_parse_comments_real_structure():
    out = parse_comments(load("comments_like_count.json"), "n1")
    assert len(out) == 9  # 只有 @昵称、没有内容的那条被跳过
    assert all(set(c) == COMMENT_FIELDS for c in out)
    assert out[0]["likes"] == 22 and out[0]["content"].startswith("我买错了")
    assert all(c["note_id"] == "n1" for c in out)


def test_parse_comments_strips_mentions_and_highlight():
    out = {c["comment_id"]: c["content"] for c in parse_comments(load("comments_like_count.json"), "n1")}
    texts = " ".join(out.values())
    assert "@" not in texts and "被艾特" not in texts
    assert "[搜索高亮]" not in texts
    assert "替我吃一下[抽泣R]" in out.values()  # @昵称 去掉后保留内容


def test_parse_comments_skips_hidden_invalid_and_empty():
    data = comments_page("n1", [5, 4, 3, 2])
    cs = data["data"]["result"]["list"]["comments"]
    cs[0]["hidden"] = True
    cs[1]["invalid"] = True
    cs[2]["content"] = "@某人"
    cs[2]["at_users"] = [{"nickname": "某人"}]
    out = parse_comments(data, "n1")
    assert [c["likes"] for c in out] == [2]


# ---------------- 每篇取前 2 条 ----------------
def test_top2_by_likes_per_note():
    cs = [
        {"comment_id": "a", "note_id": "n", "content": "a", "likes": 1},
        {"comment_id": "b", "note_id": "n", "content": "b", "likes": 9},
        {"comment_id": "c", "note_id": "n", "content": "c", "likes": 3},
        {"comment_id": "d", "note_id": "n", "content": "d", "likes": 3},
    ]
    assert [c["comment_id"] for c in collect.top_comments(cs, 2)] == ["b", "c"]  # 同赞保持接口顺序


@responses.activate
def test_top2_from_api_page(client, cfg):
    serve_search()
    serve_comments(default_likes=[0, 7, 2, 9, 1])
    res = collect.collect(client, ["低温酸奶"], cfg.collect)
    first = [c for c in res.comments if c["note_id"] == res.notes[0]["note_id"]]
    assert [c["likes"] for c in first] == [9, 7]


@responses.activate
def test_fewer_than_2_comments(client, cfg):
    serve_search()
    note_ids = [it["note"]["id"] for it in load("search_page1.json")["data"]["result"]["list"]["items"][:10]]
    serve_comments({note_ids[0]: comments_page(note_ids[0], [4]), note_ids[1]: comments_page(note_ids[1], [])},
                   default_likes=[1, 2, 3])
    res = collect.collect(client, ["低温酸奶"], cfg.collect)
    per = {nid: [c for c in res.comments if c["note_id"] == nid] for nid in note_ids}
    assert len(per[note_ids[0]]) == 1
    assert len(per[note_ids[1]]) == 0
    assert all(len(per[n]) == 2 for n in note_ids[2:])
    assert res.stats["comments"] == 17


@responses.activate
def test_total_at_most_20(client, cfg):
    serve_search()
    serve_comments(default_likes=[5, 4, 3, 2, 1])
    res = collect.collect(client, ["低温酸奶"], cfg.collect)
    assert len(res.notes) == 10
    assert len(res.comments) == 20
    small = CollectConfig(max_notes=10, comments_per_note=2, max_comments=7, default_keywords=[])
    res2 = collect.collect(client, ["低温酸奶"], small)
    assert len(res2.comments) == 7


@responses.activate
def test_one_page_per_note(client, cfg, counter):
    serve_search()
    calls = serve_comments(default_likes=[1, 2, 3])
    res = collect.collect(client, ["低温酸奶"], cfg.collect)
    assert len(calls) == 10
    assert len({c["note_id"] for c in calls}) == 10
    assert all(c["sort_strategy"] == "like_count" for c in calls)
    assert all(c.get("xsec_token", "").startswith("FAKE_XSEC_") for c in calls)
    assert all("cursor" not in c for c in calls)
    assert counter.get("xhs") == 11  # 1 次搜索 + 10 次评论
    assert res.stats["api_calls"] == {"xhs": 11}
    assert res.stats["points"] == 220


@responses.activate
def test_comment_fields_no_identity(client, cfg):
    serve_search()
    serve_comments()
    res = collect.collect(client, ["低温酸奶"], cfg.collect)
    assert res.comments
    for c in res.comments:
        assert set(c) == COMMENT_FIELDS
    raw_text = json.dumps(res.raw, ensure_ascii=False)
    for key in IDENTITY_KEYS:
        assert f'"{key}"' not in raw_text
    for s in ("假昵称", "假属地", "被艾特", "fakeuser"):
        assert s not in raw_text


@responses.activate
def test_comment_failure_on_one_note_continues(client, cfg):
    serve_search()
    note_ids = [it["note"]["id"] for it in load("search_page1.json")["data"]["result"]["list"]["items"][:10]]
    calls: list[str] = []

    def cb(request):
        nid = parse_qs(urlparse(request.url).query)["note_id"][0]
        calls.append(nid)
        if nid == note_ids[3]:
            return 400, JSON_HEADERS, json.dumps({"code": 400, "message": "note not found"})
        return 200, JSON_HEADERS, json.dumps(comments_page(nid, [1, 2]), ensure_ascii=False)

    responses.add_callback(responses.GET, COMMENTS_URL, callback=cb)
    res = collect.collect(client, ["低温酸奶"], cfg.collect)
    assert len(res.comments) == 18
    assert res.stats["comment_errors"] == [{"note_id": note_ids[3], "error": res.stats["comment_errors"][0]["error"]}]
    assert "400" in res.stats["comment_errors"][0]["error"]


@responses.activate
def test_quota_error_stops_whole_collect(client, cfg):
    serve_search()
    responses.add(responses.GET, COMMENTS_URL, status=402, json={"code": 402, "message": "points not enough"})
    with pytest.raises(xhs.ApiError) as e:
        collect.collect(client, ["低温酸奶"], cfg.collect)
    assert e.value.error_type == "quota_exceeded"
