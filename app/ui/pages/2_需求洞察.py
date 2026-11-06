"""网页「②需求洞察」页（R13、R8a、R8b）。

界面只负责调用 app.pipeline.runs 并展示结果：
- 选一次抓取结果 → 点「提炼需求」→ 大模型提炼 5 条消费者需求，每条附原话证据
- 原话注明出自哪篇笔记的正文还是评论；出自广告帖的标「广告帖」；找不到原话的标「缺少证据」
- 结果保存在这次抓取的目录里，再次打开直接展示，不再调用接口
- 失败时显示原因和编号（request_id），并提供「重试这一步」
"""

import logging

import streamlit as st

from app.clients.http import ApiError
from app.config import ROOT, ConfigError, load_config
from app.logging_setup import request_context, setup_logging
from app.pipeline import runs
from app.pipeline.insight import InsightInputError

logger = logging.getLogger("app.ui.insight")

st.set_page_config(page_title="② 需求洞察", page_icon="💡", layout="wide")
st.title("💡 ② 需求洞察")
st.caption(
    "把这次抓取的笔记（标题 + 正文开头）和全部评论交给 Kimi，提炼 5 条消费者需求，每条附 1–2 句原话。"
    "广告帖也参与分析（代表竞品在主推什么），出自广告帖的原话会标出来。"
)

cfg = load_config()
setup_logging(cfg.paths.logs_dir if cfg.paths.logs_dir.is_absolute() else ROOT / cfg.paths.logs_dir)
RUNS = runs.runs_dir(cfg)
ss = st.session_state
TYPE_BADGE = {"正面": ":green-background[正面]", "负面": ":red-background[负面]", "期待": ":blue-background[期待]"}


def do_insight(run_id: str) -> None:
    ss.pop("insight_error", None)
    ss["insight_last_run"] = run_id
    with request_context() as rid:
        try:
            with st.spinner("Kimi 正在分析，大约需要 1–3 分钟……"):
                runs.insight_and_save(run_id, cfg)
        except (ConfigError, InsightInputError, runs.RunNotFound) as e:
            ss["insight_error"] = {"summary": str(e), "request_id": rid, "retry": False}
        except ApiError as e:
            ss["insight_error"] = {"summary": e.summary(), "request_id": e.request_id or rid, "retry": True}
        except Exception as e:  # 兜底：记录完整堆栈，页面给出编号便于查日志
            logger.exception("需求洞察出现未预期错误", extra={"event": "ui_insight_failed"})
            ss["insight_error"] = {"summary": f"程序出错：{type(e).__name__}：{e}", "request_id": rid, "retry": True}


# ---------------- 选择抓取结果 ----------------
history = runs.list_runs(RUNS)
if not history:
    st.info("还没有抓取记录，请先到「① 数据抓取」页抓取。")
    st.stop()

labels = {r.run_id: r.label() for r in history}
if ss.get("insight_run_id") not in labels:
    # 默认选①页当前打开的那次，否则选最新的
    ss["insight_run_id"] = ss.get("collect_history") if ss.get("collect_history") in labels else history[0].run_id
run_id = st.selectbox(
    "用哪次抓取结果",
    options=list(labels),
    format_func=lambda rid: labels[rid],
    key="insight_run_id",
)

try:
    run = runs.load_run(RUNS, run_id)
    saved = runs.load_insight(RUNS, run_id)
except runs.RunNotFound as e:
    st.error(f"❌ 无法打开这次结果：{e}")
    st.stop()

ads = sum(1 for n in run.notes if n.get("is_ad"))
st.caption(f"这次抓取：{len(run.notes)} 篇笔记（其中广告帖 {ads} 篇）· {len(run.comments)} 条评论")

label = "重新提炼" if saved else "提炼需求"
if st.button(label, type="primary", key="insight_run"):
    do_insight(run_id)
    saved = runs.load_insight(RUNS, run_id) if "insight_error" not in ss else saved

err = ss.get("insight_error")
if err and ss.get("insight_last_run") == run_id:
    st.error(f"❌ 需求洞察失败：{err['summary']}\n\n编号 {err['request_id']}（可凭编号在 logs/ 中查找详细日志）")
    if err["retry"] and st.button("重试这一步", key="insight_retry"):
        do_insight(run_id)
        st.rerun()

if not saved:
    st.info("这次抓取还没有提炼需求。点上方「提炼需求」开始（约 1–3 分钟）。")
    st.stop()

# ---------------- 结果展示 ----------------
s = saved.get("stats", {})
st.subheader(f"提炼出 {s.get('needs', len(saved.get('needs', [])))} 条消费者需求")
notes_line = []
if s.get("dropped_quotes"):
    notes_line.append(f"有 {s['dropped_quotes']} 句原话在原文里找不到，已丢弃")
if s.get("retried"):
    notes_line.append("有需求缺少原话，已自动重新提炼 1 次")
if s.get("cached"):
    notes_line.append("同样的数据之前分析过，直接用了保存的结果，没有重新调用 Kimi")
notes_line.append(f"保存位置 data/runs/{run.run_id}/insight.json")
st.caption(" · ".join(notes_line))

for need in saved.get("needs", []):
    with st.container(border=True):
        st.markdown(f"**#{need['no']} {need['title']}** {TYPE_BADGE.get(need['type'], need['type'])}")
        st.markdown(need["description"])
        if need.get("missing_evidence") or not need.get("quotes"):
            st.warning("缺少证据：大模型给的原话在笔记和评论里都找不到，这条需求请谨慎参考。")
        for qt in need.get("quotes", []):
            st.markdown(f"> {qt['text']}")
            ad = " · :orange-background[广告帖]" if qt.get("is_ad") else ""
            title = qt.get("note_title") or "（无标题）"
            st.caption(f"出自 #{qt.get('note_rank')}「{title}」的{qt['source']}{ad}")
