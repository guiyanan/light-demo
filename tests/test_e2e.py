"""Step 19：端到端（S1–S7）。三个外部接口全部模拟，不发真实请求、不花钱：

聚道云（responses 模拟 HTTP）→ 抓取 → Kimi（假的 openai 客户端，走真实的 Schema 校验 / 缓存）
→ 5 条需求 → 3 个概念 → BananaRouter（responses 模拟 HTTP）→ 3 张图 → 导出报告和 zip。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import zipfile

import responses

from app.clients.http import CallCounter
from app.clients.image import ImageClient
from app.clients.llm import LlmClient
from app.config import load_config
from app.pipeline import runs
from tests import test_collect as tc
from tests import test_image as ti
from tests.test_concept import THREE
from tests.test_llm import FakeCompletions, SimpleNamespace

ENV = {"XHS_BASE_URL": tc.BASE, "XHS_API_KEY": "test-key-123456",
       "LLM_BASE_URL": "https://llm.example.com/v1", "LLM_API_KEY": "llm-KEY-1234567890", "LLM_MODEL": "kimi-test",
       **ti.ENV}
NEED_TITLES = ["清爽不腻", "低糖健康", "高蛋白饱腹", "宝宝能吃", "包装好开"]


def needs_reply(base, run_id) -> dict:
    """按这次抓到的评论原文造 5 条需求（原话必须是评论原文的片段，才能通过核对）。"""
    run = runs.load_run(base, run_id)
    rank = {n["note_id"]: n["rank"] for n in run.notes}
    picked = [c for c in run.comments if len(c["content"].strip()) >= 6][:5]
    assert len(picked) == 5
    return {"needs": [
        {"title": t, "description": f"{t}的说明", "type": "期待",
         "quotes": [{"text": c["content"].strip()[:12], "source": "评论", "note": f"N{rank[c['note_id']]}"}]}
        for t, c in zip(NEED_TITLES, picked, strict=True)
    ]}


@responses.activate
def test_full_flow_mocked(tmp_path):
    cfg = load_config(env=ENV)
    cfg = dataclasses.replace(cfg, paths=dataclasses.replace(cfg.paths, data_dir=tmp_path))
    base = runs.runs_dir(cfg)
    base.mkdir()

    # ① 抓取
    tc.serve_search()
    tc.serve_comments(default_likes=[5, 3, 1])
    run_id = runs.collect_and_save(["低温酸奶"], cfg, sleep=lambda s: None, now=dt.datetime(2026, 11, 6, 2, 0))
    run = runs.load_run(base, run_id)
    assert run.stats["notes"] == 10 and run.comments

    # ② 需求 ③ 概念：两次大模型调用
    comp = FakeCompletions([json.dumps(needs_reply(base, run_id), ensure_ascii=False),
                            json.dumps(THREE, ensure_ascii=False)])
    llm_counter = CallCounter()
    llm = LlmClient(cfg, client=SimpleNamespace(chat=SimpleNamespace(completions=comp)),
                    sleep=lambda s: None, counter=llm_counter)
    ins = runs.insight_and_save(run_id, cfg, llm=llm)
    assert [n["title"] for n in ins["needs"]] == NEED_TITLES
    assert all(n["quotes"] and not n["missing_evidence"] for n in ins["needs"])
    con = runs.concept_and_save(run_id, cfg, llm=llm)
    assert len(con["concepts"]) == 3 and con["stats"]["warnings"] == []
    assert len(comp.calls) == 2

    # ④ 产品图：3 次生图
    responses.add(responses.POST, ti.URL, json=ti.FIX)
    img_counter = CallCounter()
    imgs = runs.images_and_save(run_id, cfg, image_client=ImageClient(cfg, sleep=lambda s: None, counter=img_counter))
    assert imgs["stats"] == {"images": 3, "failed": 0, "image_calls": 3}
    assert sum(1 for c in responses.calls if c.request.url == ti.URL) == 3

    # 导出
    out = runs.export_and_save(base, run_id)
    for t in NEED_TITLES:
        assert t in out.html
    for c in THREE["concepts"]:
        assert c["name"] in out.html
    assert out.html.count("data:image/png;base64,") == 3
    with zipfile.ZipFile(io.BytesIO(out.zip_bytes)) as z:
        assert len(z.namelist()) == 6
    stats = json.loads((base / run_id / "stats.json").read_text(encoding="utf-8"))
    assert stats["steps"] == ["collect", "insight", "concept", "image", "export"]

    # 再次打开：直接读取已保存结果，不再调用任何接口
    n_http = len(responses.calls)
    assert runs.load_insight(base, run_id) == ins and runs.load_images(base, run_id) == imgs
    assert runs.export_and_save(base, run_id).html == out.html
    assert len(responses.calls) == n_http and len(comp.calls) == 2
