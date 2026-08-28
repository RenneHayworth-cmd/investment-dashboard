"""同花顺风格的ETF实盘账户摘要、持仓与标的明细。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.live_record.formatting import format_live_number, money
from components.live_record.trades import render_live_trade_details
from core.ui import render_metric_grid


def _value(value: object, *, suffix: str = "", digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):,.{digits}f}{suffix}"


def render_live_account_summary(snapshot: dict[str, object]) -> None:
    summary = dict(snapshot.get("summary") or {})
    initialized = bool(snapshot.get("initialized"))
    valuation_date = pd.to_datetime(summary.get("valuation_date"), errors="coerce")
    valuation_text = "-" if pd.isna(valuation_date) else valuation_date.strftime("%Y-%m-%d")
    if not initialized:
        st.warning(
            "账户资金待初始化：请在“实盘记录”先录入一笔日期不晚于首笔成交的期初资金。"
        )
    render_metric_grid(
        [
            (
                "账户总资产",
                money(summary.get("total_assets")) if initialized else "-",
                "持仓市值加账户现金；数据源为实盘成交和资金流水",
            ),
            (
                "累计盈亏",
                money(summary.get("account_pnl")) if initialized else "-",
                "总资产减累计净外部投入",
            ),
            (
                "当日盈亏",
                money(summary.get("daily_pnl")) if initialized else "-",
                "相对上一完整估值日，已剔除资金转入转出",
            ),
            ("持仓市值", money(summary.get("market_value")), f"估值日期：{valuation_text}"),
            (
                "账户现金",
                money(summary.get("cash")) if initialized else "-",
                "资金流水与成交收付自动汇总",
            ),
            (
                "仓位比例",
                _value(summary.get("position_ratio_pct"), suffix="%") if initialized else "-",
                "持仓市值除以账户总资产",
            ),
        ]
    )
    if initialized:
        st.caption(
            f"账户累计收益率 {_value(summary.get('cumulative_return_pct'), suffix='%')}｜"
            f"当日收益率 {_value(summary.get('daily_return_pct'), suffix='%')}｜"
            f"账户净值 {_value(summary.get('nav'), digits=4)}｜估值日期 {valuation_text}。"
        )
    cash_warning = str(summary.get("cash_warning") or "")
    if cash_warning:
        st.warning(cash_warning)
    incomplete = list(snapshot.get("incomplete_realtime_symbols") or [])
    if incomplete:
        st.warning(
            "以下持仓尚无同一时点的当日行情；账户总资产为可用当日行情与各标的"
            "最近正式收盘价的临时混合估值："
            + "、".join(incomplete)
        )


def render_live_symbol_detail(
    positions: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    key_prefix: str,
) -> None:
    if positions is None or positions.empty:
        st.info("暂无可查看的持仓标的。")
        return
    options = {
        str(row.symbol): f"{row.name}（{row.symbol}）"
        for row in positions.itertuples(index=False)
    }
    selected_symbol = st.selectbox(
        "查看标的详情",
        options=list(options),
        format_func=options.get,
        key=f"{key_prefix}_symbol_detail",
    )
    row = positions[positions["symbol"].astype(str).eq(selected_symbol)].iloc[0]
    render_metric_grid(
        [
            ("持仓数量", f"{int(row['quantity']):,}", "根据实盘成交记录重建"),
            ("持仓成本", _value(row["average_cost"], digits=3), "含买入手续费的移动平均成本"),
            ("最新价格", _value(row["latest_price"], digits=3), str(row.get("price_status", "-"))),
            ("浮动盈亏", money(row.get("unrealized_pnl")), "当前市值减剩余持仓成本"),
            ("已实现盈亏", money(row["realized_pnl"]), "卖出回款减移动平均成本"),
            ("累计手续费", money(row["fee_amount"]), "该ETF全部买卖手续费"),
        ]
    )
    first_trade_date = pd.to_datetime(row.get("first_trade_date"), errors="coerce")
    st.caption(
        f"首次交易日：{'-' if pd.isna(first_trade_date) else first_trade_date.strftime('%Y-%m-%d')}｜"
        f"行情状态：{row.get('price_status', '-')}｜行情时间：{row.get('price_time', '-')}。"
    )
    symbol_trades = trades[
        trades["symbol"].astype(str).str.extract(r"(\d{6})", expand=False).eq(selected_symbol)
    ].copy()
    render_live_trade_details(
        symbol_trades,
        title=f"{options[selected_symbol]}交易明细",
        allow_delete=False,
        show_download=False,
    )


def render_live_account_section(
    snapshot: dict[str, object],
    trades: pd.DataFrame,
    *,
    render_positions,
    readonly: bool,
    key_prefix: str,
) -> None:
    st.subheader("实盘账户")
    render_live_account_summary(snapshot)
    positions = snapshot.get("positions")
    positions = positions if isinstance(positions, pd.DataFrame) else pd.DataFrame()
    holding_tab, detail_tab, trade_tab = st.tabs(["持仓情况", "标的详情", "交易明细"])
    with holding_tab:
        if positions.empty:
            st.info("暂无实盘持仓。")
        else:
            render_positions(positions)
            st.caption(
                "现价使用正式收盘数据，并标注正式收盘或已过期状态。"
            )
    with detail_tab:
        render_live_symbol_detail(positions, trades, key_prefix=key_prefix)
    with trade_tab:
        render_live_trade_details(
            trades,
            title="全部交易明细",
            allow_delete=not readonly,
            show_download=not readonly,
        )
    if readonly:
        st.info("持仓分析仅供查看；新增、删除成交和资金流水请前往“实盘记录”。")


__all__ = [
    "render_live_account_section",
    "render_live_account_summary",
    "render_live_symbol_detail",
]
