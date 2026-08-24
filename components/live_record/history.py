"""实盘记录的逐标的历史盈亏组件。"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from services.fund_analysis import FUND_ADJUST_NONE
from services.live_trading import (
    append_live_symbol_pnl_total,
    build_live_symbol_pnl_history,
    list_live_trades,
)
from services.position_analysis import load_or_fetch_etf


def render_live_symbol_pnl_history(
    *,
    list_trades=list_live_trades,
    load_etf=load_or_fetch_etf,
    build_history=build_live_symbol_pnl_history,
    append_total=append_live_symbol_pnl_total,
    render_history_table,
    market_now: datetime | None = None,
    api_key: str | None = None,
    market_now_provider=None,
    api_key_provider=None,
    adjustment: str = FUND_ADJUST_NONE,
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
    price_histories: dict[str, pd.DataFrame] = {}
    symbols = sorted(all_trades["symbol"].dropna().astype(str).unique())
    for symbol in symbols:
        item = load_etf(
            symbol,
            api_key=(
                api_key
                if api_key is not None
                else api_key_provider()
                if api_key_provider is not None
                else os.getenv("TICKFLOW_API_KEY", "")
            ),
            count=5000,
            adjust=adjustment,
            allow_fetch=False,
            force_refresh=False,
            save_to_cache=False,
            market_now=market_now,
        )
        if item.dataframe is not None and not item.dataframe.empty:
            price_histories[symbol] = item.dataframe

    history = append_total(build_history(all_trades, price_histories))
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


__all__ = ["render_live_symbol_pnl_history"]
