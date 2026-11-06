"""网页「③新品概念」页（R13、R9）。

界面只负责调用 app.pipeline.runs 并展示结果：
- 选一次已提炼需求的抓取结果 → 点「生成概念」→ Kimi 根据需求生成 3 个光明新品概念
- 每个概念一张卡片：品名、卖点、目标人群、原料、包装、规格、营养估算（AI 估算）、对应需求
- 结果保存在这次抓取的目录里，再次打开直接展示，不再调用接口
- 失败时显示原因和编号（request_id），并提供「重试这一步」
"""

import logging

import pandas as pd
import streamlit as st

from app.clients.http import ApiError
from app.config import ROOT, ConfigError, load_config
from app.logging_setup import request_context, setup_logging
from app.pipeline import runs
from app.pipeline.concept import ConceptInputError

logger = logging.getLogger("app.ui.concept")

st.set_page_config(page_title="③ 新品概念", page_icon="🧪", layout="wide")
st.title("🧪 ③ 新品概念")
st.caption("根据「②需求洞察」提炼的需求（含原话），让 Kimi 提出 3 个光明低温酸奶 / 发酵乳新品概念。营养数值为 AI 估算。")

cfg = load_config()
setup_logging(cfg.paths.logs_dir if cfg.paths.logs_dir.is_absolute() else ROOT / cfg.paths.logs_dir)
RUNS = runs.runs_dir(cfg)
ss = st.session_state
NUTRITION_ROWS = [("蛋白质", "protein_g", "g"), ("碳水", "carbs_g", "g"), ("糖", "sugar_g", "g"),
                  ("脂肪", "fat_g", "g"), ("热量", "energy_kcal", "kcal")]


def do_concept(run_id: str) -> None:
    ss.pop("concept_error", None)
    ss["concept_last_run"] = run_id
    with request_context() as rid:
        try:
            with st.spinner("Kimi 正在构思新品，大约需要 1–3 分钟……"):
                runs.concept_and_save(run_id, cfg)
        except (ConfigError, ConceptInputError, runs.RunNotFound) as e:
            ss["concept_error"] = {"summary": str(e), "request_id": rid, "retry": False}
        except ApiError as e:
            ss["concept_error"] = {"summary": e.summary(), "request_id": e.request_id or rid, "retry": True}
        except Exception as e:  # 兜底：记录完整堆栈，页面给出编号便于查日志
            logger.exception("新品概念出现未预期错误", extra={"event": "ui_concept_failed"})
            ss["concept_error"] = {"summary": f"程序出错：{type(e).__name__}：{e}", "request_id": rid, "retry": True}


def nutrition_table(n: dict) -> pd.DataFrame:
    return pd.DataFrame(
        {"每 100g（AI 估算）": [f"{n.get(key, 0):g} {unit}" for _, key, unit in NUTRITION_ROWS]},
        index=[label for label, _, _ in NUTRITION_ROWS],
    )


# ---------------- 选择抓取结果 ----------------
history = runs.list_runs(RUNS)
if not history:
    st.info("还没有抓取记录，请先到「① 数据抓取」页抓取，再到「② 需求洞察」页提炼需求。")
    st.stop()

labels = {r.run_id: r.label() for r in history}
if ss.get("concept_run_id") not in labels:
    for key in ("insight_run_id", "collect_history"):  # 默认选前两页当前打开的那次
        if ss.get(key) in labels:
            ss["concept_run_id"] = ss[key]
            break
    else:
        ss["concept_run_id"] = history[0].run_id
run_id = st.selectbox("用哪次抓取结果", options=list(labels), format_func=lambda rid: labels[rid], key="concept_run_id")

try:
    insight = runs.load_insight(RUNS, run_id)
    saved = runs.load_concepts(RUNS, run_id)
except runs.RunNotFound as e:
    st.error(f"❌ 无法打开这次结果：{e}")
    st.stop()

if not insight or not insight.get("needs"):
    st.info("这次抓取还没有提炼需求，请先到「② 需求洞察」页点「提炼需求」。")
    st.stop()

with st.expander(f"本次依据的 {len(insight['needs'])} 条需求"):
    st.markdown("\n".join(f"{n['no']}. **{n['title']}**（{n['type']}）：{n['description']}" for n in insight["needs"]))

if st.button("重新生成" if saved else "生成概念", type="primary", key="concept_run"):
    do_concept(run_id)
    if "concept_error" not in ss:
        saved = runs.load_concepts(RUNS, run_id)

err = ss.get("concept_error")
if err and ss.get("concept_last_run") == run_id:
    st.error(f"❌ 生成概念失败：{err['summary']}\n\n编号 {err['request_id']}（可凭编号在 logs/ 中查找详细日志）")
    if err["retry"] and st.button("重试这一步", key="concept_retry"):
        do_concept(run_id)
        st.rerun()

if not saved:
    st.info("这次还没有生成概念。点上方「生成概念」开始（约 1–3 分钟）。")
    st.stop()

# ---------------- 结果展示 ----------------
s = saved.get("stats", {})
for w in s.get("warnings") or []:
    st.warning(f"请留意：{w}")
info = []
if s.get("retried"):
    info.append("第一次结果有问题（竞品名 / 需求编号 / 主需求重复），已自动重新生成 1 次")
if s.get("cached"):
    info.append("同样的需求之前生成过，直接用了保存的结果，没有重新调用 Kimi")
info.append(f"保存位置 data/runs/{run_id}/concepts.json")
st.caption(" · ".join(info))

cols = st.columns(len(saved.get("concepts", [])) or 1)
for col, c in zip(cols, saved.get("concepts", []), strict=False):
    with col, st.container(border=True):
        st.subheader(f"{c['no']}. {c['name']}")
        st.markdown(f"**{c['tagline']}**")
        st.markdown(
            f"- **目标人群**：{c['target']}\n"
            f"- **主要原料**：{'、'.join(c['ingredients'])}\n"
            f"- **包装形式**：{c['packaging']}\n"
            f"- **规格**：{c['spec']}"
        )
        st.table(nutrition_table(c["nutrition"]))
        needs = "、".join(f"需求 {n['no']}「{n['title']}」" for n in c.get("needs", [])) or "（无）"
        st.caption(f"对应需求：{needs}")
        with st.expander("生图描述（第⑤步画包装用）"):
            st.markdown(c["image_prompt"])
