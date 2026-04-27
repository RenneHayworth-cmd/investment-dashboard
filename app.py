import streamlit as st

from core.db import init_db


st.set_page_config(
    page_title="投资分析工作台",
    page_icon="📈",
    layout="wide",
)

init_db()

st.title("投资分析工作台")
st.caption("本地缓存行情数据，前台展示图表和分析结果。")

col1, col2, col3 = st.columns(3)
with col1:
    st.metric("数据缓存", "SQLite + CSV")
with col2:
    st.metric("运行方式", "本地 Streamlit")
with col3:
    st.metric("当前阶段", "骨架完成")

st.subheader("下一步")
st.write("先进入左侧的「指数监控」页面，导入一份已有的指数 MA20 CSV，验证缓存和图表流程。")

