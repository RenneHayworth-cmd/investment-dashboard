"""期货实盘盘中估算：交易时段每两分钟取持仓合约实时价，只做临时显示。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from components.futures_live.formatting import format_money
from services import futures_live_trading as futures_live
from services.futures_live_realtime import (
    build_futures_intraday_preview,
    fetch_futures_live_quotes,
    quote_refresh_due,
    session_trade_date,
)

MARKET_TZ = ZoneInfo("Asia/Shanghai")
STATE_KEY = "futures_live_quote_state"


def _signed(value: object) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value):+,.2f}"


def _price(value: object) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value):,.2f}"


def _latest_settlement_equity(settlement_daily_pnl: pd.DataFrame) -> float | None:
    if settlement_daily_pnl is None or settlement_daily_pnl.empty:
        return None
    valued = settlement_daily_pnl[settlement_daily_pnl["status"].isin(["完整", "手工估算"])]
    if valued.empty:
        return None
    return float(pd.to_numeric(valued.iloc[-1]["economic_equity"], errors="coerce"))


@st.fragment(run_every="120s")
def render_intraday_preview(settlement_daily_pnl: pd.DataFrame) -> None:
    market_now = datetime.now(MARKET_TZ)
    positions = futures_live.build_current_position_pnl(valuation_mode="settlement")
    if positions.empty:
        return
    active = positions[pd.to_numeric(positions["estimated_quantity"], errors="coerce").fillna(0).gt(0)]
    if active.empty:
        return
    contracts = active["contract"].astype(str).unique().tolist()
    state = st.session_state.get(STATE_KEY) or {}
    if quote_refresh_due(contracts, market_now, state):
        fresh, failures = fetch_futures_live_quotes(active, market_now)
        quotes = dict(state.get("quotes") or {})
        quotes.update(fresh)  # 失败合约保留同一交易日的上一次有效报价
        state = {
            "fetched_at": market_now.isoformat(timespec="seconds"),
            "quotes": quotes,
            "failures": failures,
            "succeeded": len(fresh),
        }
        st.session_state[STATE_KEY] = state

    # 正式结算价已覆盖报价所属交易日时，切回正式数据。
    base_dates = dict(zip(active["contract"].astype(str), active["valuation_date"].astype(str)))
    quotes = {
        contract: quote
        for contract, quote in (state.get("quotes") or {}).items()
        if contract in base_dates and str(quote["trade_date"]) > base_dates[contract]
    }
    in_session = any(session_trade_date(contract, market_now) for contract in contracts)
    if not quotes:
        if in_session and state.get("failures"):
            st.warning(
                "期货盘中报价获取失败，继续使用正式结算价："
                + "；".join(f"{contract}：{error}" for contract, error in state["failures"].items())
            )
        return

    preview = build_futures_intraday_preview(active, quotes)
    if preview.empty:
        return
    st.subheader("盘中估算")
    intraday = pd.to_numeric(preview["intraday_pnl"], errors="coerce").sum(min_count=1)
    equity = _latest_settlement_equity(settlement_daily_pnl)
    cols = st.columns(3)
    cols[0].metric("盘中当日盈亏（估算）", _signed(intraday))
    cols[1].metric("估算权益（盯市）", format_money(equity + intraday) if equity is not None and pd.notna(intraday) else "-")
    cols[2].metric("报价时间", str(state.get("fetched_at", ""))[11:19] or "-")
    display = pd.DataFrame(
        {
            "类型": preview["asset_type"],
            "合约": preview["contract"],
            "多空": preview["side"],
            "持仓": preview["estimated_quantity"].map(lambda value: f"{int(value)}"),
            "结算基准": [f"{_price(price)}（{day}）" for price, day in zip(preview["base_price"], preview["base_date"])],
            "实时价": preview["live_price"].map(_price),
            "盘中当日盈亏": preview["intraday_pnl"].map(_signed),
            "浮动盈亏（按持仓均价）": preview["floating_pnl"].map(_signed),
            "报价时间": preview["quote_time"],
            "来源": preview["source"],
        }
    )
    st.dataframe(display, hide_index=True, width="stretch")
    if state.get("failures"):
        st.warning(
            "部分合约本轮取价失败，沿用上一次有效报价："
            + "；".join(f"{contract}：{error}" for contract, error in state["failures"].items())
        )
    st.caption(
        ("交易时段每2分钟更新" if in_session else "已收盘，保留当天最后一次报价，待当日结算价确认后切回正式数据")
        + "；盘中当日盈亏 =（实时价 − 最近正式结算价）× 持仓 × 合约乘数，夜盘归属下一交易日。"
        "报价只作临时估算，不写缓存，也不进入正式收益曲线、日历和策略分析；当日新开平仓按结算基准估算可能有偏差。"
    )


__all__ = ["render_intraday_preview"]
