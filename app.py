import pandas as pd
import streamlit as st

from core.cache import list_datasets
from core.db import init_db


st.set_page_config(
    page_title="投资分析工作台",
    page_icon="📈",
    layout="wide",
)

init_db()

st.title("投资分析工作台")
st.caption("本地缓存行情数据，左侧进入各分析页面。分析页统一左侧设置、右侧获取数据。")


def format_cache_time(value: object) -> str:
    if value is None:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


try:
    datasets = list_datasets()
except Exception:
    datasets = None

cache_count = 0
latest_update = "-"
latest_trade_date = "-"
if datasets is not None and not datasets.empty:
    cache_count = len(datasets)
    latest_update = format_cache_time(datasets["last_update_time"].dropna().iloc[0])
    trade_dates = datasets["last_trade_date"].dropna()
    if not trade_dates.empty:
        latest_trade_date = str(trade_dates.max())

status_cols = st.columns(3)
status_cols[0].metric("缓存数据集", cache_count)
status_cols[1].metric("最近缓存更新时间", latest_update)
status_cols[2].metric("最近交易日", latest_trade_date)

st.subheader("今日重点")
st.write("先看「指数监控」确认主要指数与 MA20 偏离情况；需要做标的比较时进入「相关性分析」查看按类别合并的相关矩阵。")
st.write("如果要回测策略，进入「基金轮动」；如果要看单个标的走势和回撤，进入「A股分析」或「美股分析」。")

st.subheader("常用入口")
st.markdown(
    """
- 指数监控：指数摘要、MA20 偏离率和排序表。
- A股分析：场内基金/股票走势、均线和回撤分析。
- 基金轮动：22 日动量轮动回测、交易明细和持仓金额。
- 相关性分析：A股ETF/股票、美股、期货主连按类别展示相关矩阵。
- 期货期权：期货主连走势、摘要和回撤。
- 期货价差：多个合约的基准价差对比。
- 美股分析：TickFlow 美股日线、均线和回撤分析，界面结构与 A股分析保持一致。
- 任务与数据：按 A股、港股、日韩和美股交易时段分市场管理后台更新。
"""
)

st.caption("数据会写入本地 CSV，并在 SQLite 中记录数据集和任务状态。")
