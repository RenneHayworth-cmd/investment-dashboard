import pandas as pd
import streamlit as st

from core.cache import list_datasets
from core.db import init_db
from core.ui import apply_global_style, render_metric_card, render_page_header


st.set_page_config(
    page_title="投资分析工作台",
    page_icon="📈",
    layout="wide",
)

init_db()
apply_global_style()

render_page_header(
    "投资分析工作台",
    "本地缓存行情数据，左侧进入各分析页面。分析页统一左侧设置、右侧获取数据。",
    eyebrow="Dashboard",
)


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
with status_cols[0]:
    render_metric_card("缓存数据集", cache_count, "当前 SQLite 记录的数据集数量")
with status_cols[1]:
    render_metric_card("最近缓存更新时间", latest_update, "最近一次写入本地缓存的时间")
with status_cols[2]:
    render_metric_card("最近交易日", latest_trade_date, "本地缓存中记录的最新交易日")

st.subheader("今日重点")
st.write("先看「指数监控」确认主要指数与 MA20 偏离情况；需要跟踪固定持仓时进入「持仓分析」查看 ETF、期货价差和期权摘要。")
st.write("ETF 实际成交进入「实盘记录」，期货及期权账户进入「期货实盘」；策略验证进入「策略回测」。")

st.subheader("常用入口")
st.markdown(
    """
- 指数监控：指数摘要、MA20 偏离率和排序表。
- A股分析：场内基金/股票走势、均线和回撤分析。
- 策略回测：单标的均线择时、多ETF配置择时、历史年度ETF动态组合和22日动量轮动回测。
- 相关性分析：A股ETF/股票、美股、期货主连按类别展示相关矩阵。
- 持仓分析：固定持仓清单的 ETF、期货价差和期权摘要，默认优先读取本地缓存。
- 实盘记录：保存实际买卖成交、手续费、持仓成本和已实现盈亏。
- 期货实盘：读取本地月结单，展示官方/预计持仓、盯市/收盘双口径收益日历、资金流水和历史盈亏，并支持同花顺日盈亏补录、月末后成交、入出金及期权到期确认。
- 期货期权：期货主连走势、摘要和回撤。
- 期货价差：多个合约的基准价差对比。
- 美股分析：TickFlow 美股日线、均线和回撤分析，界面结构与 A股分析保持一致。
- 微盘股：东方财富 BK1158 成分股按总市值升序筛选最小市值名单。
- 任务与数据：查看本地缓存和任务记录。
"""
)

st.caption("数据会写入本地 CSV，并在 SQLite 中记录数据集和任务状态。")
