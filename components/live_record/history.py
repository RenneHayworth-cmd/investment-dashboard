"""实盘记录的逐标的历史盈亏组件。"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from services.live_categories import summarize_live_pnl_by_category
from services.live_price_history import load_live_price_histories
from services.live_trading import (
    append_live_symbol_pnl_total,
    build_live_symbol_pnl_history,
    list_live_cash_flows,
    list_live_trades,
)


def render_live_symbol_pnl_history(
    *,
    list_trades=list_live_trades,
    list_cash_flows=list_live_cash_flows,
    load_histories=load_live_price_histories,
    build_history=build_live_symbol_pnl_history,
    append_total=append_live_symbol_pnl_total,
    render_history_table,
    market_now: datetime | None = None,
    api_key: str | None = None,
    market_now_provider=None,
    api_key_provider=None,
) -> None:
    st.subheader("历史盈亏")
    all_trades = list_trades()
    if all_trades.empty:
        st.info("暂无可汇总的历史成交。")
        return

    market_now = market_now or (
        market_now_provider()
        if market_now_provider is not None
        else datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    # 只读本地正式收盘缓存，联网补数由上方每日正式收盘盈亏负责。
    price_histories, _failures, _warnings, _complete = load_histories(
        all_trades,
        market_now=market_now,
        allow_fetch=False,
        save_to_cache=False,
        api_key=(
            api_key
            if api_key is not None
            else api_key_provider()
            if api_key_provider is not None
            else os.getenv("TICKFLOW_API_KEY", "")
        ),
    )

    symbol_history = build_history(all_trades, price_histories)
    history = append_total(symbol_history)
    history_display = history.rename(
        columns={
            "name": "标的名称",
            "symbol": "代码",
            "status": "状态",
            "first_trade_date": "首次交易日",
            "last_trade_date": "最近交易日",
            "quantity": "当前数量",
            "cumulative_buy_cost": "累计买入成本",
            "cumulative_sell_proceeds": "累计卖出回款",
            "market_value": "当前市值",
            "realized_pnl": "已实现盈亏",
            "unrealized_pnl": "未实现盈亏",
            "total_pnl": "累计盈亏",
            "return_pct": "累计盈亏率(%)",
            "fee_amount": "累计手续费",
            "valuation_date": "估值日期",
        }
    )
    history_display = history_display[
        [
            "标的名称",
            "代码",
            "状态",
            "首次交易日",
            "最近交易日",
            "估值日期",
            "当前数量",
            "累计买入成本",
            "累计卖出回款",
            "当前市值",
            "已实现盈亏",
            "未实现盈亏",
            "累计盈亏",
            "累计盈亏率(%)",
            "累计手续费",
        ]
    ]
    render_history_table(history_display)
    st.caption(
        "包含当前持仓和已清仓标的；买入成本含买入手续费，"
        "卖出回款已扣除卖出手续费。"
    )
    render_live_category_summary(symbol_history, list_cash_flows())


def _money(value: object) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value):,.2f}"


def render_live_category_summary(symbol_history: pd.DataFrame, cash_flows: pd.DataFrame) -> None:
    """按 ETF、LOF套利、可转债、现金管理汇总累计盈亏。"""
    summary = summarize_live_pnl_by_category(symbol_history, cash_flows)
    st.markdown("#### 按类别汇总")
    if summary.empty:
        st.info("暂无可汇总的盈亏。")
        return
    display = summary.copy()
    display["标的数"] = display["标的数"].map(lambda value: f"{int(value)}")
    for column in ("已实现盈亏", "未实现盈亏", "利息分红等", "累计盈亏"):
        display[column] = display[column].map(_money)
    st.dataframe(display, hide_index=True, width="stretch")
    st.caption(
        "可转债含中签和兑息；现金管理含短融ETF、逆回购、金自来与账户结息；"
        "LOF套利含场内申购后卖出及转托转入份额。"
    )


__all__ = ["render_live_category_summary", "render_live_symbol_pnl_history"]
