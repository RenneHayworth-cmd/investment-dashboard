"""期货实盘页面入口与兼容门面。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.futures_live import (
    decode_warnings as _decode_warnings,
    format_money as _format_money,
    format_number as _format_number,
    format_ratio as _format_ratio,
    has_unresolved_price_gaps as _has_unresolved_price_gaps,
    render_account_summary,
    render_account_trend,
    render_cash_flows,
    render_contract_history,
    render_current_positions,
    render_daily_pnl_override_form,
    render_daily_pnl_reconciliation,
    render_data_update,
    render_manual_cash_flow_form,
    render_manual_trade_form,
    render_monthly_status,
    render_option_expiry,
    render_refresh_status,
    render_trade_history,
    run_session_auto_refresh,
)
from core.db import init_db
from core.ui import apply_global_style, render_page_header
from services import futures_live_trading as futures_live


st.set_page_config(page_title="期货实盘", layout="wide")
init_db()
apply_global_style()

render_page_header(
    "期货实盘",
    "以月结单为正式账户与持仓依据，汇总全部成交，并用已完成交易日的收盘价和结算价更新盈亏。",
    eyebrow="Futures Live",
)


def format_money(value: object) -> str:
    """兼容原页面格式化入口。"""
    return _format_money(value)


def format_number(value: object, digits: int = 2) -> str:
    """兼容原页面格式化入口。"""
    return _format_number(value, digits=digits)


def format_ratio(value: object) -> str:
    """兼容原页面格式化入口。"""
    return _format_ratio(value)


def decode_warnings(value: object) -> list[str]:
    """兼容原页面导入警告解码入口。"""
    return _decode_warnings(value)


def has_unresolved_price_gaps(daily_pnl: pd.DataFrame) -> bool:
    """兼容原页面行情缺口判断入口。"""
    return _has_unresolved_price_gaps(daily_pnl)


refresh_state = render_data_update()
account = futures_live.latest_monthly_account()
if account is None:
    st.info("当前没有可用月结单。请确认目录后点击“重新同步月结单”。")
    st.stop()

render_account_summary(account)
close_daily_pnl = futures_live.build_daily_account_pnl(valuation_mode="close")
settlement_daily_pnl = futures_live.build_daily_account_pnl(
    valuation_mode="settlement"
)
render_refresh_status(close_daily_pnl, settlement_daily_pnl)
render_option_expiry()
render_current_positions()
render_account_trend(close_daily_pnl, settlement_daily_pnl)
render_daily_pnl_override_form()
render_daily_pnl_reconciliation()
render_contract_history()
render_manual_trade_form(account)
render_manual_cash_flow_form(account)
render_cash_flows()
render_trade_history()
render_monthly_status()

# 保持原页面顺序：所有本地缓存内容渲染完后，每会话对同一目标日只尝试一次自动补齐。
run_session_auto_refresh(account, refresh_state)
