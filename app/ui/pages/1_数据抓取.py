"""网页「①数据抓取」页（R13、R5、R15）。

界面只负责调用 app.pipeline.runs 并展示结果：
- 输入 1–3 个关键词 → 点「开始抓取」→ 前 10 篇笔记 + 每篇点赞最高的 2 条评论
- 广告（商业合作）笔记显示「广告」标签
- 每次结果自动保存；可从「历史结果」直接打开，不再调用接口
- 失败时显示原因和编号（request_id），并提供「重试这一步」
"""

import logging
import re

import streamlit as st

from app.clients.http import ApiError
from app.clients.xhs import KeywordError, clean_keywords
from app.config import ROOT, ConfigError, load_config
from app.logging_setup import request_context, setup_logging
from app.pipeline import runs

logger = logging.getLogger("app.ui.collect")

st.set_page_config(page_title="① 数据抓取", page_icon="🔎", layout="wide")
st.title("🔎 ① 数据抓取")
st.caption(
    "从小红书（聚道云接口）取搜索结果前 40 篇作候选，挑出评论最多的 10 篇（评论少于 3 条的不要），"
    "再取这 10 篇的评论。一次约 12 次接口调用、约 240 点。"
)

cfg = load_config()
setup_logging(cfg.paths.logs_dir if cfg.paths.logs_dir.is_absolute() else ROOT / cfg.paths.logs_dir)
RUNS = runs.runs_dir(cfg)
ss = st.session_state


def split_keywords(text: str) -> list[str]:
    return [k for k in re.split(r"[,，、;；\s]+", text or "") if k]


def do_collect(keywords: list[str]) -> None:
    ss.pop("collect_error", None)
    ss["collect_last_keywords"] = keywords
    with request_context() as rid:
        try:
            with st.spinner("正在抓取，大约需要 20–60 秒……"):
                run_id = runs.collect_and_save(keywords, cfg)
        except (KeywordError, ConfigError) as e:
            ss["collect_error"] = {"summary": str(e), "request_id": rid, "retry": False}
            return
        except ApiError as e:
            ss["collect_error"] = {"summary": e.summary(), "request_id": e.request_id or rid, "retry": True}
            return
        except Exception as e:  # 兜底：记录完整堆栈，页面给出编号便于查日志
            logger.exception("数据抓取出现未预期错误", extra={"event": "ui_collect_failed"})
            ss["collect_error"] = {"summary": f"程序出错：{type(e).__name__}：{e}", "request_id": rid, "retry": True}
            return
    ss["collect_history"] = run_id


# ---------------- 输入 ----------------
with st.container(border=True):
    text = st.text_input(
        "关键词（1–3 个，用逗号、空格或顿号分隔）",
        value="、".join(cfg.collect.default_keywords),
        key="collect_keywords",
    )
    if st.button("开始抓取", type="primary", key="collect_run"):
        try:
            do_collect(clean_keywords(split_keywords(text)))
        except KeywordError as e:
            ss["collect_error"] = {"summary": str(e), "request_id": "-", "retry": False}

err = ss.get("collect_error")
if err:
    st.error(f"❌ 抓取失败：{err['summary']}\n\n编号 {err['request_id']}（可凭编号在 logs/ 中查找详细日志）")
    if err["retry"] and st.button("重试这一步", key="collect_retry"):
        do_collect(ss.get("collect_last_keywords") or cfg.collect.default_keywords)
        st.rerun()

# ---------------- 历史结果 ----------------
history = runs.list_runs(RUNS)
if not history:
    st.info("还没有抓取记录。输入关键词后点「开始抓取」。")
    st.stop()

labels = {r.run_id: r.label() for r in history}
if ss.get("collect_history") not in labels:
    ss["collect_history"] = history[0].run_id
run_id = st.selectbox(
    "历史结果（每次抓取自动保存，选中即可直接查看，不会重新调用接口）",
    options=list(labels),
    format_func=lambda rid: labels[rid],
    key="collect_history",
)

try:
    run = runs.load_run(RUNS, run_id)
except runs.RunNotFound as e:
    st.error(f"❌ 无法打开这次结果：{e}")
    st.stop()

# ---------------- 结果展示 ----------------
s = run.stats
st.subheader(f"关键词：{'、'.join(s.get('keywords', []))}")
cols = st.columns(5)
cols[0].metric("笔记", len(run.notes))
cols[1].metric("评论", len(run.comments))
cols[2].metric("广告帖", sum(1 for n in run.notes if n.get("is_ad")))
cols[3].metric("接口调用", sum((s.get("api_calls") or {}).values()))
cols[4].metric("消耗点数", s.get("points", 0))
cand = f"从 {s['candidates']} 篇候选中挑出 · " if s.get("candidates") else ""
st.caption(f"{cand}运行编号 {run.run_id} · 保存位置 data/runs/{run.run_id}/")

for e in s.get("comment_errors") or []:
    st.warning(f"笔记 {e['note_id']} 的评论获取失败，已跳过：{e['error']}")

by_note: dict[str, list[dict]] = {}
for c in run.comments:
    by_note.setdefault(c["note_id"], []).append(c)

for n in run.notes:
    with st.container(border=True):
        ad = " :orange-background[广告]" if n.get("is_ad") else ""
        st.markdown(f"**#{n['rank']} {n.get('title') or '（无标题）'}**{ad}")
        st.caption(
            f"👍 {n.get('liked', 0)} · ⭐ {n.get('collected', 0)} · 💬 {n.get('comments', 0)} · "
            f"来自关键词「{n.get('keyword', '')}」"
        )
        if n.get("desc"):
            st.markdown(n["desc"])
        cs = by_note.get(n["note_id"], [])
        if cs:
            st.markdown("\n".join(f"- 💬 {c['content']}（{c['likes']} 赞）" for c in cs))
        else:
            st.caption("（没有可用评论）")
