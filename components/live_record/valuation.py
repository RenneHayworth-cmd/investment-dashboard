"""实盘记录的正式收盘估值、收益日历与走势图。"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from components.live_record.formatting import money
from core.return_calendar import render_return_calendar
from core.ui import DEFAULT_CHART_HEIGHT, apply_plotly_layout, render_metric_grid
from services.fund_analysis import FUND_ADJUST_NONE
from services.live_trading import (
    build_live_daily_pnl,
    build_live_daily_returns,
    build_live_position_performance,
    list_live_trades,
    live_close_refresh_due,
)
from services.position_analysis import latest_final_etf_trade_date, load_or_fetch_etf


def render_live_return_calendar(
    daily_pnl: pd.DataFrame,
    *,
    first_trade_date: object = None,
    build_daily_returns=build_live_daily_returns,
    calendar_renderer=render_return_calendar,
) -> None:
    calendar_renderer(
        build_daily_returns(daily_pnl),
        title="收益日历",
        key_prefix="live_return_calendar",
        first_date=first_trade_date,
        caption=(
            "收益率按每日实际持仓资金计算后复合；买入视为当日新增投入，"
            "同日卖出回款优先抵扣买入，不包含账户未投资现金。"
        ),
    )


def render_daily_close_pnl(
    *,
    list_trades=list_live_trades,
    load_etf=load_or_fetch_etf,
    latest_trade_date=latest_final_etf_trade_date,
    refresh_due=live_close_refresh_due,
    build_position_performance=build_live_position_performance,
    build_daily_pnl=build_live_daily_pnl,
    render_positions,
    render_calendar=render_live_return_calendar,
    market_now: datetime | None = None,
    api_key: str | None = None,
    market_now_provider=None,
    api_key_provider=None,
    adjustment: str = FUND_ADJUST_NONE,
) -> None:
    current_trades = list_trades()
    if current_trades.empty:
        st.subheader("当前实盘持仓")
        st.info("暂无实盘持仓。")
        st.subheader("每日收盘盈亏")
        st.info("录入成交后，将按正式收盘价生成每日盈亏走势。")
        return

    market_now = market_now or (
        market_now_provider()
        if market_now_provider is not None
        else datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    target_date = latest_trade_date(market_now)
    attempt_key = "live_pnl_close_last_attempt"
    attempt_target_key = "live_pnl_close_last_target"
    attempt_scope_key = "live_pnl_close_last_scope"
    market_now_naive = market_now.replace(tzinfo=None)
    symbols = sorted(current_trades["symbol"].dropna().astype(str).unique())
    refresh_scope = "|".join(symbols)
    network_refresh_due = refresh_due(
        target_date=target_date,
        market_now=market_now,
        last_attempt=st.session_state.get(attempt_key),
        last_target_date=st.session_state.get(attempt_target_key),
        refresh_scope=refresh_scope,
        last_refresh_scope=st.session_state.get(attempt_scope_key),
    )

    price_histories: dict[str, pd.DataFrame] = {}
    update_failures: list[str] = []
    data_warnings: list[str] = []
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
            allow_fetch=network_refresh_due,
            force_refresh=False,
            save_to_cache=True,
            market_now=market_now,
        )
        if item.dataframe is not None and not item.dataframe.empty:
            price_histories[symbol] = item.dataframe
        item_date = pd.to_datetime(item.latest_date, errors="coerce")
        if item.error:
            detail = (
                f"{symbol}：本地暂无正式收盘缓存；"
                "页面将在下次自动检查时联网补齐。"
                if item.status == "无缓存"
                else f"{symbol}：{item.error}"
            )
            if network_refresh_due and item.status in {"失败", "缓存"}:
                update_failures.append(detail)
            else:
                data_warnings.append(detail)
        elif pd.isna(item_date) or item_date.date() < target_date:
            data_warnings.append(
                f"{symbol}：正式收盘数据最新到{item.latest_date or '-'}，目标为{target_date}"
            )
    if network_refresh_due:
        st.session_state[attempt_key] = market_now_naive.isoformat()
        st.session_state[attempt_target_key] = str(target_date)
        st.session_state[attempt_scope_key] = refresh_scope

    failure_state_key = "live_pnl_close_failures"
    if network_refresh_due:
        if update_failures:
            st.session_state[failure_state_key] = {
                "target_date": str(target_date),
                "details": update_failures,
            }
        elif not data_warnings:
            st.session_state.pop(failure_state_key, None)
    persisted_failure = st.session_state.get(failure_state_key, {})
    if (
        not update_failures
        and str(persisted_failure.get("target_date", "")) == str(target_date)
    ):
        update_failures = list(persisted_failure.get("details") or [])

    if update_failures:
        st.warning(
            "收盘价更新失败，当前继续使用本地缓存；"
            "页面保持打开时将在10分钟后重试："
            + "；".join(update_failures)
        )
    if data_warnings:
        st.warning("正式收盘数据尚未完全补齐：" + "；".join(data_warnings))

    st.subheader("当前实盘持仓")
    position_performance = build_position_performance(
        current_trades,
        price_histories,
    )
    if position_performance.empty:
        st.info("暂无实盘持仓。")
    else:
        render_positions(position_performance)
        st.caption(
            "当日盈亏按本次与上一个正式收盘估值的累计盈亏差额计算；"
            "当日新增买入成本计入当日收益率分母。"
        )

    daily_pnl = build_daily_pnl(current_trades, price_histories)
    if daily_pnl.empty:
        st.subheader("每日收盘盈亏")
        st.info("尚无可用于完整估值的正式收盘数据。")
        return

    latest = daily_pnl.iloc[-1]
    valuation_date = pd.Timestamp(latest["date"]).strftime("%Y-%m-%d")
    st.subheader(f"每日收盘盈亏（{valuation_date}）")
    render_metric_grid(
        [
            ("持仓市值", money(latest["market_value"]), "按当日不复权收盘价计算"),
            ("未实现盈亏", money(latest["unrealized_pnl"]), "持仓市值减剩余成本"),
            ("已实现盈亏", money(latest["realized_pnl"]), "已扣除卖出手续费"),
            ("总盈亏", money(latest["total_pnl"]), "已实现与未实现盈亏合计"),
            ("累计收益率", f"{float(latest['return_pct']):.2f}%", "总盈亏除以累计买入成本"),
        ]
    )
    trade_dates = pd.to_datetime(current_trades["trade_date"], errors="coerce").dropna()
    first_trade_date = trade_dates.min() if not trade_dates.empty else None
    render_calendar(daily_pnl, first_trade_date=first_trade_date)

    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=daily_pnl["date"],
            y=daily_pnl["total_pnl"],
            mode="lines+markers",
            name="总盈亏",
            line={"color": "#dc2626", "width": 2.4},
            marker={"size": 5},
            customdata=daily_pnl[
                ["market_value", "cost_basis", "realized_pnl", "unrealized_pnl"]
            ],
            hovertemplate=(
                "总盈亏：%{y:,.2f}<br>持仓市值：%{customdata[0]:,.2f}"
                "<br>剩余成本：%{customdata[1]:,.2f}<br>已实现盈亏：%{customdata[2]:,.2f}"
                "<br>未实现盈亏：%{customdata[3]:,.2f}<extra></extra>"
            ),
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=daily_pnl["date"],
            y=daily_pnl["return_pct"],
            mode="lines",
            name="累计收益率",
            line={"color": "#0f766e", "width": 2, "dash": "dot"},
            hovertemplate="累计收益率：%{y:.2f}%<extra></extra>",
        ),
        secondary_y=True,
    )
    figure.add_hline(y=0, line_width=1, line_color="rgba(87,83,78,0.45)")
    apply_plotly_layout(figure, height=DEFAULT_CHART_HEIGHT)
    figure.update_yaxes(title_text="盈亏金额（元）", secondary_y=False)
    figure.update_yaxes(title_text="累计收益率（%）", secondary_y=True)
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    st.caption(
        "由实盘成交记录与不复权正式日线重算；买入手续费计入成本，"
        "卖出按移动平均成本确认盈亏。交易日15:05后自动检查当天收盘数据。"
    )

    with st.expander("每日盈亏明细"):
        daily_display = daily_pnl.rename(
            columns={
                "date": "日期",
                "market_value": "持仓市值",
                "cost_basis": "剩余成本",
                "realized_pnl": "已实现盈亏",
                "unrealized_pnl": "未实现盈亏",
                "total_pnl": "总盈亏",
                "cumulative_buy_cost": "累计买入成本",
                "net_investment": "净投入",
                "return_pct": "累计收益率(%)",
            }
        )
        st.dataframe(
            daily_display.sort_values("日期", ascending=False),
            width="stretch",
            hide_index=True,
            column_config={
                "日期": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "持仓市值": st.column_config.NumberColumn(format="%.2f"),
                "剩余成本": st.column_config.NumberColumn(format="%.2f"),
                "已实现盈亏": st.column_config.NumberColumn(format="%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="%.2f"),
                "总盈亏": st.column_config.NumberColumn(format="%.2f"),
                "累计买入成本": st.column_config.NumberColumn(format="%.2f"),
                "净投入": st.column_config.NumberColumn(format="%.2f"),
                "累计收益率(%)": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )


__all__ = ["render_daily_close_pnl", "render_live_return_calendar"]
