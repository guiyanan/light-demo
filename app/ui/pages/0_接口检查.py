"""网页「接口检查」页（R13、R14）：分别测试聚道云、Kimi、生图三个接口。

界面只负责调用 app.diag.run_check 并展示结果。
"""

import streamlit as st

from app import diag

st.set_page_config(page_title="接口检查", page_icon="🔌", layout="wide")
st.title("🔌 接口检查")
st.caption(
    "依次点击下面三个按钮，确认接口都能连通。脱敏后的原始响应会保存到项目文件夹的 logs/diag/ 中。"
)

TIPS = {
    "xhs": "搜索「低温酸奶」第 1 页，并读取第 1 篇笔记的评论（约 2 次调用）。",
    "llm": "让模型回复一句「OK」（1 次调用）。",
    "image": "生成一张测试图（1 次调用，约 30–180 秒，会消耗一次生图额度）。",
}


def show_result(r: "diag.CheckResult") -> None:
    code = r.status_code if r.status_code is not None else "-"
    head = f"状态码 {code} · 耗时 {r.elapsed_ms / 1000:.1f} 秒 · 编号 {r.request_id}"
    if r.ok:
        st.success(f"✅ 成功：{r.message}\n\n{head}")
    else:
        st.error(f"❌ 失败：{r.message}\n\n{head}")
    if r.details:
        with st.expander("详细信息"):
            st.json(r.details)
    if r.raw_files:
        with st.expander("原始响应文件（已脱敏）"):
            for f in r.raw_files:
                st.code(f, language=None)


for service, label in diag.SERVICES.items():
    with st.container(border=True):
        st.subheader(label)
        st.write(TIPS[service])
        if st.button(f"检查 {label}", key=f"diag_btn_{service}"):
            with st.spinner(f"正在检查 {label}……"):
                st.session_state[f"diag_result_{service}"] = diag.run_check(service)
        result = st.session_state.get(f"diag_result_{service}")
        if result is not None:
            show_result(result)
