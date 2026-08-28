"""实盘记录页面入口与兼容门面。"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from components.live_record import (
    format_live_number as _format_live_number,
    money as _money,
    render_daily_close_pnl as _render_daily_close_pnl,
    render_live_account_dashboard as _render_live_account_dashboard,
    render_live_cash_flow_details as _render_live_cash_flow_details,
    render_live_cash_flow_form as _render_live_cash_flow_form,
    render_live_positions_table as _render_live_positions_table,
    render_live_return_calendar as _render_live_return_calendar,
    render_live_symbol_history_table as _render_live_symbol_history_table,
    render_live_symbol_pnl_history as _render_live_symbol_pnl_history,
    render_live_trade_details as _render_live_trade_details,
    render_live_trade_form as _render_live_trade_form,
    render_live_trade_summary as _render_live_trade_summary,
)
from core.db import init_db
from core.return_calendar import render_return_calendar
from core.ui import apply_global_style, render_page_header
from services.fund_analysis import FUND_ADJUST_NONE
from services.live_trading import (
    add_live_trade,
    append_live_symbol_pnl_total,
    build_live_daily_pnl,
    build_live_daily_returns,
    build_live_position_performance,
    build_live_symbol_pnl_history,
    delete_live_trade,
    delete_live_cash_flow,
    enrich_live_trades,
    list_live_trades,
    list_live_cash_flows,
    live_close_refresh_due,
    summarize_live_position_performance,
    summarize_live_trades,
)
from services.position_analysis import latest_final_etf_trade_date, load_or_fetch_etf


st.set_page_config(page_title="实盘记录", layout="wide")
init_db()
apply_global_style()

render_page_header(
    "实盘记录",
    "记录实际成交、手续费和持仓成本，与策略回测结果分开核算。",
    eyebrow="Live Trading",
)


def money(value: object) -> str:
    """兼容旧页面内格式化入口。"""
    return _money(value)


def format_live_number(value: object, digits: int = 2, prefix: str = "") -> str:
    """兼容旧页面内格式化入口。"""
    return _format_live_number(value, digits=digits, prefix=prefix)


def render_live_return_calendar(
    daily_pnl: pd.DataFrame,
    *,
    first_trade_date: object = None,
) -> None:
    """兼容旧页面内收益日历入口。"""
    _render_live_return_calendar(
        daily_pnl,
        first_trade_date=first_trade_date,
        build_daily_returns=build_live_daily_returns,
        calendar_renderer=render_return_calendar,
    )


def render_live_positions_table(positions: pd.DataFrame) -> None:
    """兼容旧页面内持仓表入口，并保留旧路径 mock 行为。"""
    _render_live_positions_table(
        positions,
        summarize_positions=summarize_live_position_performance,
    )


def render_live_symbol_history_table(history: pd.DataFrame) -> None:
    """兼容旧页面内逐标的历史表入口。"""
    _render_live_symbol_history_table(history)


@st.fragment(run_every="120s")
def render_daily_close_pnl() -> None:
    """共享账户模型；本页只使用正式收盘历史。"""
    _render_live_account_dashboard(
        api_key=os.getenv("TICKFLOW_API_KEY", ""),
        formal_fetch_enabled=True,
        save_to_cache=True,
        readonly=False,
        key_prefix="live_record",
    )


@st.fragment(run_every="120s")
def render_live_symbol_pnl_history() -> None:
    """只读本地正式收盘缓存渲染逐标的历史盈亏。"""
    _render_live_symbol_pnl_history(
        list_trades=list_live_trades,
        load_etf=load_or_fetch_etf,
        build_history=build_live_symbol_pnl_history,
        append_total=append_live_symbol_pnl_total,
        render_history_table=render_live_symbol_history_table,
        market_now_provider=lambda: datetime.now(ZoneInfo("Asia/Shanghai")),
        api_key_provider=lambda: os.getenv("TICKFLOW_API_KEY", ""),
        adjustment=FUND_ADJUST_NONE,
    )

render_daily_close_pnl()

st.subheader("实盘数据维护")
trade_tab, cash_tab = st.tabs(["新增成交", "账户资金"])
with trade_tab:
    trades = list_live_trades()
    _render_live_trade_summary(trades, summarize_trades=summarize_live_trades)
    _render_live_trade_form(
        add_trade=add_live_trade,
        trade_date_value=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
    )
with cash_tab:
    cash_flows = list_live_cash_flows()
    _render_live_cash_flow_form(
        flow_date_value=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
    )
    _render_live_cash_flow_details(
        cash_flows,
        delete_cash_flow=delete_live_cash_flow,
    )
render_live_symbol_pnl_history()
