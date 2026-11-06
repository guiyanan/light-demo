"""Step 13：生成 3 个新品概念（R9，需求 v1.4）。

需求样本来自第 3 轮用户验收的真实结果（运行 20261106-022356-2214 的 insight.json），大模型一律模拟。
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from app.config import load_config
from app.pipeline import concept, runs
from app.pipeline.schemas import CONCEPTS_SCHEMA, validate
from tests.test_insight import FakeLlm, _collect_result, prompt_text

INSIGHT = json.loads((Path(__file__).parent / "fixtures" / "concept" / "insight.json").read_text(encoding="utf-8"))
BRANDS = ["父爱配方", "简爱", "卡士", "Danone"]


def make_concept(name, need_ids, **over):
    c = {
        "name": name,
        "tagline": f"{name}的一句话卖点",
        "target": "25–40 岁注重健康的城市白领",
        "ingredients": ["生牛乳", "保加利亚乳杆菌", "嗜热链球菌"],
        "packaging": "透明杯装，铝箔封口",
        "spec": "135g/杯",
        "nutrition": {"protein_g": 4.5, "carbs_g": 9.0, "sugar_g": 6.0, "fat_g": 3.2, "energy_kcal": 85},
        "need_ids": need_ids,
        "image_prompt": "一杯低温酸奶的包装效果图，白色背景，柔和光线，包装上没有任何文字和 logo",
    }
    c.update(over)
    return c


THREE = {
    "concepts": [
        make_concept("小牛活菌宝宝杯", [1, 2]),
        make_concept("零添加清爽发酵乳", [2]),
        make_concept("厚奶皮高蛋白酸奶", [4, 5]),
    ]
}


def gen(*replies, insight=INSIGHT, brands=BRANDS):
    llm = FakeLlm(*replies)
    return concept.generate(llm, insight, brands=brands), llm


def test_three_concepts_schema_valid():
    assert validate(THREE, CONCEPTS_SCHEMA) == []
    res, llm = gen(THREE)
    assert llm.calls[0]["schema"] is CONCEPTS_SCHEMA
    assert [c["no"] for c in res.concepts] == [1, 2, 3]
    c = res.concepts[0]
    for key in ("name", "tagline", "target", "ingredients", "packaging", "spec", "nutrition", "need_ids", "image_prompt"):
        assert c[key]
    # 对应需求带上标题，页面直接展示
    assert c["needs"] == [{"no": 1, "title": "婴幼儿低温活菌"}, {"no": 2, "title": "低甜干净无香精"}]
    assert res.stats["warnings"] == [] and res.stats["retried"] is False and res.stats["llm_calls"] == 1
    # Schema 拒绝：不是 3 个、缺字段、营养为负数
    assert validate({"concepts": THREE["concepts"][:2]}, CONCEPTS_SCHEMA)
    bad = copy.deepcopy(THREE)
    del bad["concepts"][0]["spec"]
    bad["concepts"][1]["nutrition"]["fat_g"] = -1
    assert len(validate(bad, CONCEPTS_SCHEMA)) == 2


def test_need_ids_within_1_to_5():
    assert validate({"concepts": [make_concept("a", [6])] * 3}, CONCEPTS_SCHEMA)  # Schema 限定 1–5
    # 只有 3 条需求时，编号 4、5 越界 → 整体重试 1 次
    small = {"needs": INSIGHT["needs"][:3], "stats": {}}
    out_of_range = {"concepts": [make_concept("a", [1, 4]), make_concept("b", [2]), make_concept("c", [5])]}
    res, llm = gen(out_of_range, out_of_range, insight=small)
    assert len(llm.calls) == 2 and "编号" in prompt_text(llm.calls[1])
    # 仍越界 → 去掉越界编号，丢光的给出提示
    assert res.concepts[0]["need_ids"] == [1] and res.concepts[2]["need_ids"] == []
    assert any("概念 3" in w and "需求" in w for w in res.stats["warnings"])
    # 重试后合格
    ok = {"concepts": [make_concept("a", [1]), make_concept("b", [2]), make_concept("c", [3])]}
    res, llm = gen(out_of_range, ok, insight=small)
    assert res.stats["warnings"] == [] and [c["need_ids"] for c in res.concepts] == [[1], [2], [3]]


def test_nutrition_marked_ai_estimate():
    data = copy.deepcopy(THREE)
    data["concepts"][0]["nutrition"]["label"] = "实测"  # 模型自己写的标注一律改为 AI 估算
    res, _ = gen(data)
    for c in res.concepts:
        assert c["nutrition"]["label"] == "AI 估算"
    assert res.concepts[0]["nutrition"]["protein_g"] == 4.5


def test_image_prompt_present():
    res, _ = gen(THREE)
    assert all(c["image_prompt"].strip() for c in res.concepts)
    bad = copy.deepcopy(THREE)
    bad["concepts"][0]["image_prompt"] = ""
    assert validate(bad, CONCEPTS_SCHEMA)
    assert "logo" in concept.SYSTEM_PROMPT  # 提示词要求生图描述不含文字和 logo


def test_input_includes_needs_and_quotes():
    _, llm = gen(THREE)
    text = prompt_text(llm.calls[0])
    for n in INSIGHT["needs"]:
        assert f"需求 {n['no']}" in text and n["title"] in text and n["description"] in text
        for q in n["quotes"]:
            assert q["text"] in text
    assert "光明" in llm.calls[0]["messages"][0]["content"]
    with pytest.raises(concept.ConceptInputError):
        concept.generate(FakeLlm(THREE), {"needs": []}, brands=BRANDS)


def test_competitor_names_retry_then_warn(read_logs):
    bad = copy.deepcopy(THREE)
    bad["concepts"][1]["tagline"] = "比简爱更清爽"
    bad["concepts"][2]["ingredients"].append("danone 菌种")  # 英文不区分大小写
    res, llm = gen(bad, bad)
    assert len(llm.calls) == 2
    retry = prompt_text(llm.calls[1])
    assert "简爱" in retry and "Danone" in retry
    assert any("简爱" in w and "概念 2" in w for w in res.stats["warnings"])
    assert any("Danone" in w and "概念 3" in w for w in res.stats["warnings"])
    assert any(r.get("event") == "concept_retry" for r in read_logs("app"))
    # 竞品名单出现在系统提示里
    assert "父爱配方" in llm.calls[0]["messages"][0]["content"]
    # 重试后合格 → 没有提示
    res, _ = gen(bad, THREE)
    assert res.stats["warnings"] == [] and res.stats["retried"] is True


def test_primary_needs_distinct():
    dup = {"concepts": [make_concept("a", [2, 1]), make_concept("b", [2]), make_concept("c", [3])]}
    res, llm = gen(dup, dup)
    assert len(llm.calls) == 2 and "主需求" in prompt_text(llm.calls[1])
    assert any("主需求" in w for w in res.stats["warnings"])
    # 需求少于 3 条时允许重复，不重试
    two = {"needs": INSIGHT["needs"][:2], "stats": {}}
    dup2 = {"concepts": [make_concept("a", [1]), make_concept("b", [2]), make_concept("c", [1, 2])]}
    res, llm = gen(dup2, insight=two)
    assert len(llm.calls) == 1 and res.stats["warnings"] == []


def test_concepts_saved_and_loaded(tmp_path):
    run_id = runs.save_collect(_collect_result(), tmp_path, suffix="cpt1", now=dt.datetime(2026, 11, 5, 12, 0))
    cfg = load_config(env={})
    # 还没做②需求洞察 → 提示先做
    with pytest.raises(concept.ConceptInputError):
        runs.concept_and_save(run_id, cfg, base_dir=tmp_path, llm=FakeLlm(THREE))
    (tmp_path / run_id / "insight.json").write_text(json.dumps(INSIGHT, ensure_ascii=False), encoding="utf-8")
    assert runs.load_concepts(tmp_path, run_id) is None
    llm = FakeLlm(THREE)
    out = runs.concept_and_save(run_id, cfg, base_dir=tmp_path, llm=llm)
    assert str(llm.calls[0]["cache_dir"]).endswith("cache/llm")
    back = runs.load_concepts(tmp_path, run_id)
    assert back == out and len(back["concepts"]) == 3
    stats = json.loads((tmp_path / run_id / "stats.json").read_text(encoding="utf-8"))
    assert stats["steps"] == ["collect", "concept"]
    runs.concept_and_save(run_id, cfg, base_dir=tmp_path, llm=FakeLlm(THREE))
    stats = json.loads((tmp_path / run_id / "stats.json").read_text(encoding="utf-8"))
    assert stats["steps"].count("concept") == 1
    # 竞品名单来自 config/app.yaml
    assert "父爱配方" in cfg.concept.competitor_brands
