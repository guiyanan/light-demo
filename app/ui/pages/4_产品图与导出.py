"""网页「④产品图与导出」页（R13、R10、R11、R16）。

界面只负责调用 app.pipeline.runs 并展示结果：
- 选一次已生成概念的抓取结果 → 点「生成图片」→ 3 张同时画，合成 1600×1000 展示图
- 失败的那张显示灰色占位图 + 原因和编号，可单独点「重画第 n 张」
- 图片保存在这次抓取的目录里，再次打开直接展示，不再调用接口
- 点「生成报告和压缩包」→ 两个下载按钮；导出文件同时存到 export/ 目录
"""

import logging

import streamlit as st

from app.clients.http import ApiError
from app.config import ROOT, ConfigError, load_config
from app.logging_setup import request_context, setup_logging
from app.pipeline import runs

logger = logging.getLogger("app.ui.image")

st.set_page_config(page_title="④ 产品图", page_icon="🖼️", layout="wide")
st.title("🖼️ ④ 产品图")
st.caption(
    "用 BananaRouter（gpt-image-2）给 3 个概念各画一张包装效果图（要求包装上不出现文字和 logo），"
    "再由程序在右侧排版概念信息。3 张同时画，大约 1–3 分钟。"
)

cfg = load_config()
setup_logging(cfg.paths.logs_dir if cfg.paths.logs_dir.is_absolute() else ROOT / cfg.paths.logs_dir)
RUNS = runs.runs_dir(cfg)
ss = st.session_state


def do_images(run_id: str, only: list[int] | None = None) -> None:
    ss.pop("image_error", None)
    with request_context() as rid:
        try:
            with st.spinner("正在画图，大约需要 1–3 分钟……" if only is None else f"正在重画第 {only[0]} 张……"):
                runs.images_and_save(run_id, cfg, only=only)
        except (ConfigError, runs.StepInputError, runs.RunNotFound) as e:
            ss["image_error"] = {"summary": str(e), "request_id": rid, "run_id": run_id}
        except ApiError as e:
            ss["image_error"] = {"summary": e.summary(), "request_id": e.request_id or rid, "run_id": run_id}
        except Exception as e:  # 兜底：记录完整堆栈，页面给出编号便于查日志
            logger.exception("产品图出现未预期错误", extra={"event": "ui_image_failed"})
            ss["image_error"] = {"summary": f"程序出错：{type(e).__name__}：{e}", "request_id": rid, "run_id": run_id}


# ---------------- 选择抓取结果 ----------------
history = runs.list_runs(RUNS)
if not history:
    st.info("还没有抓取记录，请先完成「① 数据抓取」「② 需求洞察」「③ 新品概念」。")
    st.stop()

labels = {r.run_id: r.label() for r in history}
if ss.get("image_run_id") not in labels:
    for key in ("concept_run_id", "insight_run_id", "collect_history"):  # 默认选前几页当前打开的那次
        if ss.get(key) in labels:
            ss["image_run_id"] = ss[key]
            break
    else:
        ss["image_run_id"] = history[0].run_id
run_id = st.selectbox("用哪次抓取结果", options=list(labels), format_func=lambda rid: labels[rid], key="image_run_id")

try:
    concepts = runs.load_concepts(RUNS, run_id)
    saved = runs.load_images(RUNS, run_id)
except runs.RunNotFound as e:
    st.error(f"❌ 无法打开这次结果：{e}")
    st.stop()

if not concepts or not concepts.get("concepts"):
    st.info("这次还没有生成新品概念，请先到「③ 新品概念」页点「生成概念」。")
    st.stop()

if st.button("全部重新生成" if saved else "生成图片", type="primary", key="image_run"):
    do_images(run_id)
    if "image_error" not in ss:
        saved = runs.load_images(RUNS, run_id)

err = ss.get("image_error")
if err and err.get("run_id") == run_id:
    st.error(f"❌ 生成图片失败：{err['summary']}\n\n编号 {err['request_id']}（可凭编号在 logs/ 中查找详细日志）")

if not saved:
    st.info("这次还没有生成图片。点上方「生成图片」开始（3 张同时画，约 1–3 分钟）。")
    st.stop()

# ---------------- 结果展示 ----------------
s = saved.get("stats", {})
st.caption(f"共 {s.get('images', 0)} 张，失败 {s.get('failed', 0)} 张 · 累计调用生图 {s.get('image_calls', 0)} 次 · "
           f"保存位置 data/runs/{run_id}/images/")

for it in saved.get("items", []):
    with st.container(border=True):
        st.subheader(f"{it['no']}. {it['name']}")
        if not it["ok"]:
            st.error(f"第 {it['no']} 张效果图生成失败，左侧暂用灰色占位图：{it.get('error')}\n\n"
                     f"编号 {it.get('request_id') or '-'}")
        path = runs.card_path(RUNS, run_id, it)
        if path.is_file():
            st.image(str(path), width="stretch")
        else:
            st.warning(f"找不到图片文件：{it['card']}")
        label = f"重画第 {it['no']} 张" if it["ok"] else f"重试第 {it['no']} 张"
        if st.button(label, key=f"image_retry_{it['no']}"):
            do_images(run_id, only=[it["no"]])
            st.rerun()

# ---------------- 导出（第 6 轮，R16） ----------------
st.divider()
st.subheader("📦 导出")
st.caption("报告是一个 HTML 文件，图片已放在里面，双击用浏览器打开即可，不需要联网；在浏览器里「打印 → 存为 PDF」"
           "可得到每个概念一页的 PDF。压缩包里是 3 张合成展示图和 AI 原图。")
if st.button("生成报告和压缩包", key="export_run"):
    ss.pop("export", None)
    with request_context() as rid:
        try:
            res = runs.export_and_save(RUNS, run_id)
            ss["export"] = {"run_id": run_id, "html": res.html, "zip": res.zip_bytes, "html_name": res.html_name,
                            "zip_name": res.zip_name, "failed": res.failed}
        except (runs.StepInputError, runs.RunNotFound) as e:
            logger.warning(f"导出失败：{e}", extra={"event": "ui_export_failed", "error_type": type(e).__name__})
            st.error(f"❌ 导出失败：{e}\n\n编号 {rid}")
        except Exception as e:  # 兜底：记录完整堆栈，页面给出编号便于查日志
            logger.exception("导出出现未预期错误", extra={"event": "ui_export_failed"})
            st.error(f"❌ 导出失败：程序出错：{type(e).__name__}：{e}\n\n编号 {rid}（可凭编号在 logs/ 中查找详细日志）")

ex = ss.get("export")
if ex and ex["run_id"] == run_id:
    if ex["failed"]:
        nos = "、".join(f"第 {n} 张" for n in ex["failed"])
        st.warning(f"{nos}效果图生成失败，报告里该处已标注。建议先点上面的「重试」，再重新生成报告。")
    st.caption(f"已同时保存到 data/runs/{run_id}/export/")
    c1, c2 = st.columns(2)
    c1.download_button("⬇️ 下载报告（HTML）", ex["html"].encode("utf-8"), file_name=ex["html_name"],
                       mime="text/html", key="export_html", width="stretch")
    c2.download_button("⬇️ 下载图片压缩包（zip）", ex["zip"], file_name=ex["zip_name"],
                       mime="application/zip", key="export_zip", width="stretch")
