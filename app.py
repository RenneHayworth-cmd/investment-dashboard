import streamlit as st

from core.db import init_db


st.set_page_config(
    page_title="投资分析工作台",
    page_icon="📈",
    layout="wide",
)

init_db()

st.title("投资分析工作台")
st.caption("本地缓存行情数据，集中查看指数、A股、基金轮动、相关性、期货期权、期货价差和美股分析结果。")

col1, col2, col3, col4, col5, col6, col7 = st.columns(7)
with col1:
    st.metric("指数监控", "指数摘要 + 偏离率")
with col2:
    st.metric("A股分析", "均线 + 回撤")
with col3:
    st.metric("基金轮动", "22日动量回测")
with col4:
    st.metric("相关性分析", "价格/收益率 r")
with col5:
    st.metric("期货期权", "日线 + 指标")
with col6:
    st.metric("期货价差", "基准-其他价差")
with col7:
    st.metric("美股分析", "均线 + 回撤")

st.subheader("功能入口")
st.write("从左侧页面进入「指数监控」「A股分析」「基金轮动」「相关性分析」「期货期权」「期货价差」「美股分析」或「任务与数据」。")
st.write("基金轮动支持上传文件、TickFlow 场内基金/ETF 和东方财富场外基金累计净值。")
st.write("相关性分析支持上传文件、A 股 ETF、美股和期货主连，按共同日期对齐后计算收盘价或日收益率相关系数。")
st.write("数据会写入本地 CSV，并在 SQLite 中记录数据集和任务状态。")
