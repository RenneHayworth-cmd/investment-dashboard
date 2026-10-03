import streamlit as st

from components.home import render_home_overview
from core.db import init_db
from core.ui import apply_global_style, render_page_header


st.set_page_config(
    page_title="投资分析工作台",
    page_icon="📈",
    layout="wide",
)

init_db()
apply_global_style()

render_page_header(
    "投资分析工作台",
    "ETF、微盘、期货三个实盘账户的合计资产、总盈亏、净值曲线和收益日历。",
    eyebrow="Dashboard",
)

render_home_overview()
