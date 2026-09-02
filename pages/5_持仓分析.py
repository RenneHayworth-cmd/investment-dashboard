"""持仓分析页面入口与兼容门面。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.position import (
    build_overview_table,
    clear_position_detail,
    delta_value,
    display_digits,
    display_position_code,
    filter_range,
    format_etf_table_value as _format_etf_table_value,
    format_metric_for_item,
    format_number,
    format_pct,
    get_query_position_detail,
    metric_row,
    position_key,
    primary_value,
    render_etf_detail,
    render_etf_operation_guidance as _render_etf_operation_guidance,
    render_etf_timing_section_impl,
    render_etf_timing_table as _render_etf_timing_table,
    render_option_detail,
    render_position_card,
    render_position_cards,
    render_position_detail,
    render_position_page,
    render_spread_detail,
    render_summary_table,
    rolling_annual_label,
    round_numeric_columns,
)
from core.db import init_db
from core.ui import apply_global_style, render_page_header
from services.position_analysis import (
    ETF_REALTIME_TIMING_REFRESH_SECONDS,
    PositionItem,
)


st.set_page_config(page_title="持仓分析", layout="wide")
init_db()
apply_global_style()
st.markdown(
    """
    <style>
    div[data-testid="stElementContainer"][data-stale="true"] {
        opacity: 1 !important;
        transition: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

render_page_header(
    "持仓分析",
    "按指数监控的方式展示个人持仓标的。早盘每10分钟、盘中每30分钟、尾盘每2分钟更新卡片与择时预判。",
    eyebrow="Positions",
)


def format_etf_table_value(column: str, value: object) -> str:
    """兼容原页面 ETF 表格格式化入口。"""
    return _format_etf_table_value(column, value)


def render_etf_timing_table(df: pd.DataFrame) -> None:
    """兼容原页面 ETF 择时表入口。"""
    _render_etf_timing_table(df, value_formatter=format_etf_table_value)


def render_etf_operation_guidance(df: pd.DataFrame) -> None:
    """兼容原页面近期操作指引表入口。"""
    _render_etf_operation_guidance(df, value_formatter=format_etf_table_value)


@st.fragment(run_every=f"{ETF_REALTIME_TIMING_REFRESH_SECONDS}s")
def render_etf_timing_section(
    etf_codes: list[str],
    *,
    quote_codes: list[str] | None = None,
    position_items: list[PositionItem],
    show_cache_caption: bool,
    api_key: str,
    count: int,
    market_count: int,
    max_workers: int,
    adjust: str | None,
    updates_enabled: bool,
    derivative_refresh_request: int,
    save_to_cache: bool,
) -> None:
    """保留单一实时 fragment，实现由组件承载。"""
    render_etf_timing_section_impl(
        etf_codes,
        quote_codes=quote_codes,
        position_items=position_items,
        show_cache_caption=show_cache_caption,
        api_key=api_key,
        count=count,
        market_count=market_count,
        max_workers=max_workers,
        adjust=adjust,
        updates_enabled=updates_enabled,
        derivative_refresh_request=derivative_refresh_request,
        save_to_cache=save_to_cache,
        value_formatter=format_etf_table_value,
    )


render_position_page(render_etf_timing_section)
