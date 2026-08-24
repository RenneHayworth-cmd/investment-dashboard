import streamlit as st

from components.annual_etf_dynamic import render_annual_dynamic_mode
from components.backtest.common import (
    BACKTEST_ADJUSTMENT_OPTIONS,
    FULL_HISTORY_CACHE_PERIOD,
    FULL_HISTORY_COUNT,
    LEGACY_CACHE_PERIODS,
    default_backtest_dates,
    format_cache_time,
    format_value,
    load_rotation_cache,
    render_drawdown_chart,
    to_csv_bytes,
)
from components.backtest.fund_rotation import (
    build_rotation_period_table,
    render_fund_rotation_mode,
    render_nav_chart,
)
from components.backtest.ma_timing import (
    build_timing_period_table,
    render_ma20_timing_mode,
    render_timing_nav_chart,
    render_timing_signal_chart,
)
from components.backtest.portfolio_timing import (
    DEFAULT_PORTFOLIO_CONFIG,
    PORTFOLIO_STRATEGY_DISPLAY,
    PORTFOLIO_STRATEGY_LABELS,
    build_portfolio_timing_period_table,
    parse_portfolio_allocations,
    render_portfolio_timing_mode,
    render_portfolio_timing_nav_chart,
)
from core.db import init_db
from services.fund_analysis import FUND_ADJUSTMENT_OPTIONS


st.set_page_config(page_title="策略回测", layout="wide")
init_db()

st.title("策略回测")
st.caption("支持单标的均线择时、多ETF配置择时、历史年度ETF动态组合，以及按动量排名执行多基金轮动。")

st.markdown(
    """
    <style>
    div[data-testid="stMetric"] * {
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: clip !important;
        overflow-wrap: anywhere;
    }
    div[data-testid="stMetricValue"] {
        font-size: clamp(1.05rem, 1.7vw, 1.55rem) !important;
        line-height: 1.2 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

strategy_mode = st.radio(
    "策略类型",
    options=["单标的MA20择时", "多ETF配置择时", "年度动态组合", "多基金动量轮动"],
    horizontal=True,
)
if strategy_mode == "单标的MA20择时":
    render_ma20_timing_mode()
    st.stop()
if strategy_mode == "多ETF配置择时":
    render_portfolio_timing_mode()
    st.stop()
if strategy_mode == "年度动态组合":
    render_annual_dynamic_mode()
    st.stop()
render_fund_rotation_mode()
