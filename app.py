import streamlit as st

from core.db import init_db


st.set_page_config(
    page_title="投资分析工作台",
    page_icon="📈",
    layout="wide",
)

init_db()

st.title("投资分析工作台")
st.caption("本地缓存行情数据，前台展示指数、基金、期货和美股分析结果。")

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("指数监控", "MA20")
with col2:
    st.metric("基金分析", "净值 + 回撤")
with col3:
    st.metric("期货价差", "合约对比")
with col4:
    st.metric("美股分析", "TickFlow")

st.subheader("功能入口")
st.write("从左侧页面进入「指数监控」「基金分析」「期货价差」「美股分析」或「任务与数据」。")
st.write("数据会写入本地 CSV，并在 SQLite 中记录数据集和任务状态。")
