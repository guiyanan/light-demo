"""网页首页。各步骤页面在 app/ui/pages/ 下，界面只负责调用 pipeline 和展示。"""

import streamlit as st

from app import __version__

st.set_page_config(page_title="光明 · 新品概念 Demo", page_icon="🥛", layout="wide")

st.title("光明乳业 · 小红书洞察驱动的新品概念生成")
st.caption(f"Demo 版本 {__version__}")

st.markdown(
    """
**使用顺序**（左侧菜单）：

0. **接口检查**：先确认聚道云、Kimi、生图三个接口都能连通
1. **数据抓取**：输入关键词，挑出讨论最多的 10 篇笔记和它们的评论；每次结果自动保存，可选历史结果
2. **需求洞察**：选一次抓取结果，让 Kimi 提炼 5 条消费者需求，每条附原话证据
3. 新品概念（第 4 轮上线）
4. 产品图与导出（第 5、6 轮上线）
"""
)
