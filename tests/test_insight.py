"""Step 11：提炼 5 条需求 + 原话真实性校验（R8a、R8b，需求 v1.3）。

样本来自真实运行 20261106-022356-2214（10 篇 / 88 条评论，已去掉 xsec_token），大模型一律模拟。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.clients.llm import JsonResult
from app.pipeline import insight, runs
from app.pipeline.schemas import NEEDS_SCHEMA, validate

FIX = Path(__file__).parent / "fixtures" / "insight"


def load(name):
    return [json.loads(line) for line in (FIX / name).read_text(encoding="utf-8").splitlines() if line.strip()]


NOTES = load("notes.jsonl")
COMMENTS = load("comments.jsonl")
NOTE = {n["rank"]: n for n in NOTES}  # rank → 笔记；提示词里用 N<rank> 指代笔记


def q(text, source, note):
    return {"text": text, "source": source, "note": note}


def need(title, quotes, type_="期待"):
    return {"title": title, "description": f"{title}的描述", "type": type_, "quotes": quotes}


FIVE = {
    "needs": [
        need("想知道吃多少合适", [q("请问一天吃多少的量？", "评论", "N2")]),
        need("宝宝便秘想靠酸奶改善", [q("宝宝酸奶确实👍", "正文", "N1")], "正面"),
        need("讨厌香精味", [q("好难吃，一股香精味", "评论", "N7")], "负面"),
        need("自制酸奶不够浓稠", [q("我做出来的酸奶没你这么浓稠", "评论", "N4")], "负面"),
        need("关心含糖量", [q("父爱配方里有含糖吗？", "评论", "N5"), q("澳洲的奶皮子酸奶", "正文", "N3")]),
    ]
}


class FakeLlm:
    """按顺序返回预设 JSON；记录每次调用的消息。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat_json(self, messages, schema, *, cache_dir=None, operation="chat_json", **kw):
        self.calls.append({"messages": messages, "schema": schema, "cache_dir": cache_dir})
        return JsonResult(self.replies.pop(0), cached=False, attempts=1, content="")


def prompt_text(call):
    return "\n".join(m["content"] for m in call["messages"])


def test_five_needs_schema_valid():
    llm = FakeLlm(FIVE)
    res = insight.extract(llm, NOTES, COMMENTS)
    assert len(res.needs) == 5
    assert [n["no"] for n in res.needs] == [1, 2, 3, 4, 5]
    assert validate(FIVE, NEEDS_SCHEMA) == []
    assert llm.calls[0]["schema"] is NEEDS_SCHEMA
    # 输入包含每篇标题、正文开头和全部 88 条评论
    text = prompt_text(llm.calls[0])
    for n in NOTES:
        assert n["desc"] in text
        if n["title"]:
            assert n["title"] in text
    assert all(c["content"].strip() in text for c in COMMENTS)
    assert res.stats["notes"] == 10 and res.stats["comments"] == 88
    # 原话映射回真实 note_id 和笔记信息
    first = res.needs[0]["quotes"][0]
    assert first["note_id"] == NOTE[2]["note_id"] and first["note_rank"] == 2 and first["source"] == "评论"
    assert res.stats["dropped_quotes"] == 0 and res.stats["llm_calls"] == 1


def test_quote_source_note_or_comment():
    data = {
        "needs": [
            need(
                "来源校验",
                [
                    q("澳洲的奶皮子酸奶", "正文", "N3"),  # 出自标题：算正文
                    q("宝宝爱喝安心酸奶", "评论", "N9"),  # 出自评论
                ],
            ),
            need(
                "来源写错",
                [
                    q("宝宝爱喝安心酸奶", "正文", "N9"),  # 是评论却说是正文 → 丢弃
                    q("好难吃，一股香精味", "评论", "N1"),  # 真实评论但不是这篇的 → 丢弃
                    q("请问一天吃多少的量？", "评论", "N2"),
                ],
            ),
        ]
    }
    res = insight.extract(FakeLlm(data), NOTES, COMMENTS)
    kept = res.needs[0]["quotes"]
    assert [(x["source"], x["note_rank"]) for x in kept] == [("正文", 3), ("评论", 9)]
    assert [x["text"] for x in res.needs[1]["quotes"]] == ["请问一天吃多少的量？"]
    assert res.stats["dropped_quotes"] == 2


def test_non_substring_quote_dropped_and_counted(read_logs):
    data = json.loads(json.dumps(FIVE))
    data["needs"][4]["quotes"].append(q("这是编造的原话", "评论", "N5"))
    data["needs"][4]["quotes"] = data["needs"][4]["quotes"][1:]  # 保持最多 2 条
    data["needs"][0]["quotes"].append(q("不存在的笔记", "评论", "N99"))
    res = insight.extract(FakeLlm(data), NOTES, COMMENTS)
    assert res.stats["dropped_quotes"] == 2
    assert [x["text"] for x in res.needs[4]["quotes"]] == ["澳洲的奶皮子酸奶"]
    assert all(not n["missing_evidence"] for n in res.needs)
    assert any(r.get("event") == "insight_quote_dropped" for r in read_logs("app"))
    # 首尾空格、引号包裹不影响匹配
    data2 = {"needs": [need("引号", [q("「请问一天吃多少的量？」 ", "评论", "N2")])]}
    res2 = insight.extract(FakeLlm(data2), NOTES, COMMENTS)
    assert res2.needs[0]["quotes"][0]["text"] == "请问一天吃多少的量？"


def test_need_without_quotes_retry_then_flag():
    bad = json.loads(json.dumps(FIVE))
    bad["needs"][2]["quotes"] = [q("编造一", "评论", "N7")]
    # 重试后仍丢光 → 标注缺少证据
    llm = FakeLlm(bad, bad)
    res = insight.extract(llm, NOTES, COMMENTS)
    assert len(llm.calls) == 2
    assert "逐字" in prompt_text(llm.calls[1])  # 重试时提醒模型逐字摘抄
    assert res.needs[2]["missing_evidence"] is True and res.needs[2]["quotes"] == []
    assert res.stats["retried"] is True and res.stats["missing_evidence"] == 1
    # 重试后合格 → 用重试结果
    llm = FakeLlm(bad, FIVE)
    res = insight.extract(llm, NOTES, COMMENTS)
    assert len(llm.calls) == 2 and res.stats["missing_evidence"] == 0
    assert res.needs[2]["quotes"][0]["text"] == "好难吃，一股香精味"


def test_fewer_than_5_returns_actual():
    data = {"needs": FIVE["needs"][:3]}
    res = insight.extract(FakeLlm(data), NOTES, COMMENTS)
    assert len(res.needs) == 3 and res.stats["needs"] == 3
    with pytest.raises(insight.InsightInputError):
        insight.extract(FakeLlm(FIVE), [], [])


def test_ads_included_and_marked():
    llm = FakeLlm(FIVE)
    res = insight.extract(llm, NOTES, COMMENTS)
    text = prompt_text(llm.calls[0])
    for n in NOTES:
        header = next(line for line in text.splitlines() if line.startswith(f"[N{n['rank']}]"))
        assert ("（广告帖）" in header) == bool(n["is_ad"])
    assert "广告帖" in llm.calls[0]["messages"][0]["content"]  # 系统提示说明广告帖代表竞品
    ad_quote = res.needs[4]["quotes"][0]  # N5 是广告帖
    assert ad_quote["is_ad"] is True
    assert res.needs[0]["quotes"][0]["is_ad"] is False
    assert res.stats["ads"] == 4


def test_insight_saved_and_loaded(tmp_path):
    run_id = runs.save_collect(_collect_result(), tmp_path, suffix="ins1",
                               now=__import__("datetime").datetime(2026, 11, 5, 12, 0))
    run = runs.load_run(tmp_path, run_id)
    assert runs.load_insight(tmp_path, run_id) is None
    res = insight.extract(FakeLlm(FIVE), run.notes, run.comments)
    runs.save_insight(tmp_path, run_id, res)
    back = runs.load_insight(tmp_path, run_id)
    assert back["needs"] == res.needs and back["stats"]["needs"] == 5
    stats = json.loads((tmp_path / run_id / "stats.json").read_text(encoding="utf-8"))
    assert stats["steps"] == ["collect", "insight"]
    runs.save_insight(tmp_path, run_id, res)  # 重做不会重复记步骤
    stats = json.loads((tmp_path / run_id / "stats.json").read_text(encoding="utf-8"))
    assert stats["steps"] == ["collect", "insight"]
    with pytest.raises(runs.RunNotFound):
        runs.load_insight(tmp_path, "../etc")
    # 网页入口：读抓取结果 → 调大模型 → 保存
    from app.config import load_config

    llm = FakeLlm(FIVE)
    out = runs.insight_and_save(run_id, load_config(env={}), base_dir=tmp_path, llm=llm)
    assert out["stats"]["needs"] == 5 and runs.load_insight(tmp_path, run_id) == out
    assert str(llm.calls[0]["cache_dir"]).endswith("cache/llm")


def _collect_result():
    from app.pipeline.collect import CollectResult

    return CollectResult(keywords=["低温酸奶"], notes=NOTES, comments=COMMENTS,
                         stats={"keywords": ["低温酸奶"], "notes": 10, "comments": 88})
