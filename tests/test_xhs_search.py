"""Step 6：搜索笔记解析 + 广告标注 + 隐私过滤（R4、R5、R7）。

聚道云接口全部用 responses 模拟；测试数据 tests/fixtures/xhs/search_page1.json 来自真实响应，
用户信息已换成假值。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from app.clients import xhs
from app.clients.xhs import KeywordError, XhsClient, scrub_identity, search_top_notes
from app.config import load_config

FIXTURES = Path(__file__).parent / "fixtures" / "xhs"
BASE = "https://xhs.example.com"
SEARCH_URL = BASE + xhs.SEARCH_PATH
NOTE_FIELDS = {"note_id", "keyword", "rank", "title", "desc", "liked", "collected", "comments", "tags", "is_ad"}
IDENTITY_KEYS = {"user", "nickname", "userid", "user_id", "red_id", "images", "avatar", "ip_location",
                 "author_id", "author_name", "at_users"}


def page1() -> dict:
    return json.loads((FIXTURES / "search_page1.json").read_text(encoding="utf-8"))


def make_page(ids: list[str], *, title_prefix: str = "笔记") -> dict:
    """用真实结构生成一页指定 note_id 的搜索结果。"""
    base = page1()
    template = base["data"]["result"]["list"]["items"][0]
    items = []
    for nid in ids:
        it = copy.deepcopy(template)
        it["note"]["id"] = nid
        it["note"]["title"] = f"{title_prefix}{nid}"
        items.append(it)
    base["data"]["result"]["list"]["items"] = items
    return base


def empty_page() -> dict:
    return make_page([])


@pytest.fixture()
def client():
    env = {"XHS_BASE_URL": BASE, "XHS_API_KEY": "test-key-123456"}
    cfg = load_config(env)
    return XhsClient(cfg, sleep=lambda s: None)


def serve(pages: dict[tuple[str, int], dict]):
    """按 (keyword, page) 返回对应数据；没列出的返回空页。记录调用顺序。"""
    calls: list[tuple[str, int]] = []

    def cb(request):
        q = parse_qs(urlparse(request.url).query)
        key = (q["keyword"][0], int(q["page"][0]))
        calls.append(key)
        return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(pages.get(key, empty_page()), ensure_ascii=False)

    responses.add_callback(responses.GET, SEARCH_URL, callback=cb)
    return calls


# ---------------- 解析 ----------------
@responses.activate
def test_parse_real_structure(client):
    serve({("低温酸奶", 1): page1()})
    res = search_top_notes(client, ["低温酸奶"], max_notes=10)
    assert len(res.notes) == 10
    first = res.notes[0]
    assert NOTE_FIELDS <= set(first)
    assert first["note_id"] == "6a44c674000000000803e588"
    assert first["keyword"] == "低温酸奶"
    assert first["title"] == "澳洲的奶皮子酸奶也是给我吃上了"
    assert first["liked"] == 655 and first["collected"] == 451 and first["comments"] == 60
    assert first["desc"]
    # 评论接口需要 xsec_token
    assert first["xsec_token"] == "FAKE_XSEC_00"
    # 正文里的 #话题 被识别为标签
    second = res.notes[1]
    assert "低温酸奶消费" in second["tags"]
    # 原始响应保留，供运行记录保存
    assert len(res.raw_pages) == 1


@responses.activate
def test_keep_api_order_and_rank(client):
    serve({("低温酸奶", 1): page1()})
    res = search_top_notes(client, ["低温酸奶"], max_notes=10)
    expected = [it["note"]["id"] for it in page1()["data"]["result"]["list"]["items"][:10]]
    assert [n["note_id"] for n in res.notes] == expected
    assert [n["rank"] for n in res.notes] == list(range(1, 11))


@responses.activate
def test_stop_at_10(client):
    calls = serve({("低温酸奶", 1): page1(), ("低温酸奶", 2): page1()})
    res = search_top_notes(client, ["低温酸奶"], max_notes=10)
    assert len(res.notes) == 10
    assert calls == [("低温酸奶", 1)]  # 第 1 页已够 10 篇，不再翻页


@responses.activate
def test_stop_when_no_more(client):
    calls = serve({("低温酸奶", 1): make_page(["a1", "a2", "a3"])})
    res = search_top_notes(client, ["低温酸奶"], max_notes=10)
    assert [n["note_id"] for n in res.notes] == ["a1", "a2", "a3"]
    assert calls == [("低温酸奶", 1), ("低温酸奶", 2)]  # 第 2 页为空即停止


@responses.activate
def test_stop_when_has_more_false(client):
    p = make_page(["a1", "a2"])
    p["data"]["result"]["has_more"] = False
    calls = serve({("低温酸奶", 1): p})
    res = search_top_notes(client, ["低温酸奶"], max_notes=10)
    assert len(res.notes) == 2
    assert calls == [("低温酸奶", 1)]


@responses.activate
def test_multi_keyword_round_robin_dedupe(client):
    serve(
        {
            ("酸奶", 1): make_page(["a1", "a2", "dup", "a3"]),
            ("发酵乳", 1): make_page(["dup", "b1", "b2"]),
            ("冷萃", 1): make_page(["c1", "a1", "c2"]),
        }
    )
    res = search_top_notes(client, ["酸奶", "发酵乳", "冷萃"], max_notes=8)
    ids = [n["note_id"] for n in res.notes]
    # 轮流取：酸奶#1, 发酵乳#1, 冷萃#1, 酸奶#2, 发酵乳#2(重复跳过→b1), 冷萃#2(重复跳过→c2) ...
    assert ids == ["a1", "dup", "c1", "a2", "b1", "c2", "a3", "b2"]
    assert len(ids) == len(set(ids))
    kw = {n["note_id"]: n["keyword"] for n in res.notes}
    assert kw["dup"] == "发酵乳" and kw["c1"] == "冷萃" and kw["a1"] == "酸奶"
    assert [n["rank"] for n in res.notes] == list(range(1, 9))


@pytest.mark.parametrize("kws", [[], ["", "  "], ["a", "b", "c", "d"]])
def test_keyword_count_1_to_3(client, kws):
    with pytest.raises(KeywordError):
        search_top_notes(client, kws, max_notes=10)


@responses.activate
def test_keywords_trimmed_and_deduped(client):
    calls = serve({("酸奶", 1): page1()})
    res = search_top_notes(client, [" 酸奶 ", "酸奶", ""], max_notes=10)
    assert res.keywords == ["酸奶"]
    assert calls == [("酸奶", 1)]


@responses.activate
def test_business_error_raises(client):
    serve({("酸奶", 1): {"code": 1001, "message": "余额不足", "data": None}})
    with pytest.raises(xhs.XhsBusinessError) as e:
        search_top_notes(client, ["酸奶"], max_notes=10)
    assert "余额不足" in str(e.value)


@responses.activate
def test_unexpected_structure_raises(client):
    serve({("酸奶", 1): {"code": 0, "data": {"result": {"foo": 1}}}})
    with pytest.raises(xhs.InvalidResponseError):
        search_top_notes(client, ["酸奶"], max_notes=10)


# ---------------- 广告标注 ----------------
@responses.activate
def test_ad_flag(client):
    serve({("低温酸奶", 1): page1()})
    res = search_top_notes(client, ["低温酸奶"], max_notes=10)
    ads = {n["note_id"] for n in res.notes if n["is_ad"]}
    # 真实数据中带「商业合作」标记（order_cooperate / commercial_bridge_note）的笔记
    assert ads == {"6abced55000000001b01c323", "698ac6e70000000009039cb4", "6985d83b000000000a03e095"}


def test_ad_flag_from_model_type():
    item = page1()["data"]["result"]["list"]["items"][0]
    item["model_type"] = "ads"
    assert xhs.normalize_note(item, "k")["is_ad"] is True


def test_no_ad_field_defaults_false_and_logs(read_logs):
    item = page1()["data"]["result"]["list"]["items"][2]  # 原本是商业合作笔记
    del item["note"]["widgets_context"]
    note = xhs.normalize_note(item, "k")
    assert note["is_ad"] is False
    logs = read_logs()
    assert any(r.get("event") == "ad_field_missing" and r.get("note_id") == item["note"]["id"] for r in logs)


# ---------------- 隐私 ----------------
def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_keys(v)


@responses.activate
def test_no_user_identity_fields(client):
    serve({("低温酸奶", 1): page1()})
    res = search_top_notes(client, ["低温酸奶"], max_notes=10)
    for n in res.notes:
        assert not (set(n) & IDENTITY_KEYS)
    # 原始响应在保存前也要去掉身份信息
    raw_text = json.dumps(res.raw_pages, ensure_ascii=False)
    assert not (set(_all_keys(res.raw_pages)) & IDENTITY_KEYS)
    for s in ("假昵称", "fakeuser", "fakered", "example.com/avatar"):
        assert s not in raw_text
    # 笔记内容本身保留
    assert "澳洲的奶皮子酸奶也是给我吃上了" in raw_text


def test_scrub_identity_handles_widgets_context_and_mentions():
    data = {
        "note": {"widgets_context": json.dumps({"author_id": "u1", "author_name": "张三", "order_cooperate": True})},
        "comments": [{"content": "@李 四 好吃", "at_users": [{"nickname": "李 四", "userid": "u2"}], "ip_location": "上海"}],
    }
    out = scrub_identity(data)
    text = json.dumps(out, ensure_ascii=False)
    assert "张三" not in text and "u1" not in text and "李 四" not in text and "上海" not in text
    assert json.loads(out["note"]["widgets_context"]) == {"order_cooperate": True}
    assert out["comments"][0]["content"] == "好吃"
    assert data["comments"][0]["content"] == "@李 四 好吃"  # 不修改原对象
