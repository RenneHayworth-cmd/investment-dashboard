"""实盘账户仪表盘：只使用正式收盘历史。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from components.live_record.account import render_live_account_section
from components.live_record.formatting import money
from components.live_record.tables import render_live_positions_table
from core.return_calendar import render_return_calendar
from core.ui import DEFAULT_CHART_HEIGHT, apply_plotly_layout, render_metric_grid
from services.fund_analysis import FUND_ADJUST_NONE
from services.live_trading import (
    build_live_account_snapshot,
    build_live_daily_returns,
    list_live_cash_flows,
    list_live_trades,
    live_close_refresh_due,
)
from services.position_analysis import (
    latest_final_etf_trade_date,
    load_or_fetch_etf,
)


def _load_live_formal_histories(
    trades: pd.DataFrame,
    *,
    api_key: str,
    market_now: datetime,
    enable_network: bool,
    save_to_cache: bool,
    state_prefix: str,
) -> tuple[dict[str, pd.DataFrame], list[str], list[str], bool]:
    symbols = (
        sorted(trades["symbol"].dropna().astype(str).str.extract(r"(\d{6})", expand=False).dropna().unique())
        if trades is not None and not trades.empty
        else []
    )
    target_date = latest_final_etf_trade_date(market_now)
    refresh_scope = "|".join(symbols)
    attempt_key = f"{state_prefix}_formal_last_attempt"
    target_key = f"{state_prefix}_formal_last_target"
    scope_key = f"{state_prefix}_formal_last_scope"
    refresh_due = bool(
        enable_network
        and symbols
        and live_close_refresh_due(
            target_date=target_date,
            market_now=market_now,
            last_attempt=st.session_state.get(attempt_key),
            last_target_date=st.session_state.get(target_key),
            refresh_scope=refresh_scope,
            last_refresh_scope=st.session_state.get(scope_key),
        )
    )
    histories: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    warnings: list[str] = []
    complete = True
    for symbol in symbols:
        item = load_or_fetch_etf(
            symbol,
            api_key=api_key,
            count=5000,
            adjust=FUND_ADJUST_NONE,
            allow_fetch=refresh_due,
            force_refresh=False,
            save_to_cache=save_to_cache,
            market_now=market_now,
        )
        if item.dataframe is not None and not item.dataframe.empty:
            histories[symbol] = item.dataframe
        item_date = pd.to_datetime(item.latest_date, errors="coerce")
        if item.error:
            detail = f"{symbol}：{item.error}"
            (failures if refresh_due else warnings).append(detail)
        if pd.isna(item_date) or item_date.date() < target_date:
            complete = False
            warnings.append(
                f"{symbol}：正式收盘最新到{item.latest_date or '-'}，目标为{target_date}"
            )
    if refresh_due:
        st.session_state[attempt_key] = market_now.replace(tzinfo=None).isoformat()
        st.session_state[target_key] = str(target_date)
        st.session_state[scope_key] = refresh_scope
    return histories, failures, list(dict.fromkeys(warnings)), complete


def _render_performance_history(
    snapshot: dict[str, object],
    trades: pd.DataFrame,
    *,
    key_prefix: str,
) -> None:
    st.subheader("每日正式收盘盈亏")
    scope = st.radio(
        "收益口径",
        ["账户口径", "持仓口径"],
        horizontal=True,
        key=f"{key_prefix}_return_scope",
    )
    if scope == "账户口径":
        daily = snapshot.get("formal_account_daily")
        daily = daily if isinstance(daily, pd.DataFrame) else pd.DataFrame()
        if daily.empty:
            st.info("请先初始化账户资金，并补齐正式收盘数据后查看账户收益。")
            return
        latest = daily.iloc[-1]
        render_metric_grid(
            [
                ("账户总资产", money(latest["total_assets"]), "持仓市值加账户现金"),
                ("账户现金", money(latest["cash"]), "截至该估值日的成交和资金流水"),
                ("累计盈亏", money(latest["account_pnl"]), "总资产减累计净外部投入"),
                ("当日盈亏", money(latest["daily_pnl"]), "已剔除资金转入转出"),
                ("累计收益率", f"{float(latest['cumulative_return_pct']):.2f}%", "按每日账户收益复合"),
            ]
        )
        returns = daily[["date", "daily_pnl", "daily_return_pct"]].rename(
            columns={"daily_pnl": "pnl_amount", "daily_return_pct": "return_pct"}
        )
        chart_dates = pd.to_datetime(daily["date"], errors="coerce").dt.strftime(
            "%Y-%m-%d"
        )
        pnl_colors = [
            "#ef4444" if float(val) >= 0 else "#22c55e"
            for val in daily["daily_pnl"].fillna(0.0)
        ]
        figure = make_subplots(specs=[[{"secondary_y": True}]])
        figure.add_trace(
            go.Scatter(
                x=chart_dates,
                y=daily["nav"],
                mode="lines+markers",
                name="账户净值",
                line={"color": "#2563eb", "width": 2.4},
                marker={"size": 5},
                customdata=daily[["daily_return_pct", "cumulative_return_pct"]],
                hovertemplate=(
                    "净值：%{y:.4f}<br>当日收益率：%{customdata[0]:.2f}%"
                    "<br>累计收益率：%{customdata[1]:.2f}%<extra></extra>"
                ),
            ),
            secondary_y=False,
        )
        figure.add_trace(
            go.Bar(
                x=chart_dates,
                y=daily["daily_pnl"],
                name="每日盈亏",
                marker={"color": pnl_colors},
                opacity=0.75,
                customdata=daily[["account_pnl", "total_assets", "daily_return_pct", "nav"]],
                hovertemplate=(
                    "当日盈亏：%{y:,.2f} 元<br>当日收益率：%{customdata[2]:.2f}%"
                    "<br>累计盈亏：%{customdata[0]:,.2f} 元<br>总资产：%{customdata[1]:,.2f} 元"
                    "<br>净值：%{customdata[3]:.4f}<extra></extra>"
                ),
            ),
            secondary_y=True,
        )
        apply_plotly_layout(figure, height=DEFAULT_CHART_HEIGHT)
        figure.update_xaxes(
            title_text="交易日",
            type="category",
            categoryorder="array",
            categoryarray=chart_dates.tolist(),
        )
        figure.update_yaxes(title_text="账户净值", tickformat=".4f", secondary_y=False)
        figure.update_yaxes(title_text="每日盈亏（元）", secondary_y=True)
        detail = daily.rename(
            columns={
                "date": "日期",
                "market_value": "持仓市值",
                "cash": "现金",
                "total_assets": "账户总资产",
                "external_flow": "外部资金净流入",
                "cumulative_external_capital": "累计净投入",
                "account_pnl": "累计盈亏",
                "daily_pnl": "当日盈亏",
                "return_base": "收益率分母",
                "daily_return_pct": "当日收益率(%)",
                "nav": "净值",
                "cumulative_return_pct": "累计收益率(%)",
            }
        )
    else:
        daily = snapshot.get("formal_holding_daily")
        daily = daily if isinstance(daily, pd.DataFrame) else pd.DataFrame()
        if daily.empty:
            st.info("尚无可用于完整估值的正式收盘数据。")
            return
        latest = daily.iloc[-1]
        render_metric_grid(
            [
                ("持仓市值", money(latest["market_value"]), "按当日不复权收盘价计算"),
                ("未实现盈亏", money(latest["unrealized_pnl"]), "持仓市值减剩余成本"),
                ("已实现盈亏", money(latest["realized_pnl"]), "已扣除卖出手续费"),
                ("总盈亏", money(latest["total_pnl"]), "已实现与未实现盈亏合计"),
                ("累计收益率", f"{float(latest['return_pct']):.2f}%", "总盈亏除以累计买入成本"),
            ]
        )
        returns = build_live_daily_returns(daily)
        daily = daily.copy()
        nav_rates = (
            returns.set_index("date")["return_pct"]
            if not returns.empty
            else pd.Series(dtype="float64")
        )
        pnl_amounts = (
            returns.set_index("date")["pnl_amount"]
            if not returns.empty
            else pd.Series(dtype="float64")
        )
        normalized_dates = pd.to_datetime(daily["date"], errors="coerce").dt.normalize()
        daily["daily_pnl"] = pd.to_numeric(normalized_dates.map(pnl_amounts), errors="coerce").fillna(0.0)
        daily["nav"] = (
            1.0
            + pd.to_numeric(normalized_dates.map(nav_rates), errors="coerce") / 100.0
        ).cumprod()
        daily["cumulative_return_pct"] = (daily["nav"] - 1.0) * 100.0
        chart_dates = normalized_dates.dt.strftime("%Y-%m-%d")
        holding_pnl_colors = [
            "#ef4444" if float(val) >= 0 else "#22c55e"
            for val in daily["daily_pnl"]
        ]
        figure = make_subplots(specs=[[{"secondary_y": True}]])
        figure.add_trace(
            go.Scatter(
                x=chart_dates,
                y=daily["nav"],
                mode="lines+markers",
                name="持仓净值",
                line={"color": "#2563eb", "width": 2.4},
                marker={"size": 5},
                customdata=daily[["cumulative_return_pct"]],
                hovertemplate=(
                    "净值：%{y:.4f}<br>累计复合收益率：%{customdata[0]:.2f}%"
                    "<extra></extra>"
                ),
            ),
            secondary_y=False,
        )
        figure.add_trace(
            go.Bar(
                x=chart_dates,
                y=daily["daily_pnl"],
                name="持仓当日盈亏",
                marker={"color": holding_pnl_colors},
                opacity=0.75,
                customdata=daily[["total_pnl", "market_value", "realized_pnl", "unrealized_pnl"]],
                hovertemplate=(
                    "当日盈亏：%{y:,.2f} 元<br>累计总盈亏：%{customdata[0]:,.2f} 元"
                    "<br>持仓市值：%{customdata[1]:,.2f} 元<br>已实现盈亏：%{customdata[2]:,.2f} 元"
                    "<br>未实现盈亏：%{customdata[3]:,.2f} 元<extra></extra>"
                ),
            ),
            secondary_y=True,
        )
        apply_plotly_layout(figure, height=DEFAULT_CHART_HEIGHT)
        figure.update_xaxes(
            title_text="交易日",
            type="category",
            categoryorder="array",
            categoryarray=chart_dates.tolist(),
        )
        figure.update_yaxes(title_text="持仓净值", tickformat=".4f", secondary_y=False)
        figure.update_yaxes(title_text="持仓当日盈亏（元）", secondary_y=True)
        detail = daily.rename(
            columns={
                "date": "日期",
                "market_value": "持仓市值",
                "cost_basis": "剩余成本",
                "realized_pnl": "已实现盈亏",
                "unrealized_pnl": "未实现盈亏",
                "total_pnl": "累计盈亏",
                "cumulative_buy_cost": "累计买入成本",
                "net_investment": "净投入",
                "return_pct": "累计收益率(%)",
                "nav": "净值",
                "cumulative_return_pct": "累计复合收益率(%)",
            }
        )
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    st.caption("横坐标仅排列完整正式估值交易日，周末和节假日已自动跳过。")
    trade_dates = pd.to_datetime(trades.get("trade_date"), errors="coerce").dropna()
    first_date = trade_dates.min() if not trade_dates.empty else None
    render_return_calendar(
        returns,
        title=f"{scope}收益日历",
        key_prefix=f"{key_prefix}_{'account' if scope == '账户口径' else 'holding'}_calendar",
        first_date=first_date,
        caption=(
            "账户收益率包含现金并剔除外部资金流。"
            if scope == "账户口径"
            else "持仓收益率不包含账户未投资现金。"
        ),
    )
    with st.expander("查看每日正式估值明细"):
        st.dataframe(detail.sort_values("日期", ascending=False), width="stretch", hide_index=True)


def render_live_account_dashboard(
    *,
    api_key: str,
    formal_fetch_enabled: bool,
    save_to_cache: bool,
    readonly: bool,
    key_prefix: str,
    market_now: datetime | None = None,
    show_history: bool = True,
) -> None:
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    trades = list_live_trades()
    cash_flows = list_live_cash_flows()
    histories, failures, warnings, _formal_complete = _load_live_formal_histories(
        trades,
        api_key=api_key,
        market_now=market_now,
        enable_network=formal_fetch_enabled,
        save_to_cache=save_to_cache,
        state_prefix=key_prefix,
    )
    if failures:
        st.warning("正式收盘更新失败，继续使用本地缓存：" + "；".join(failures))
    if warnings:
        st.warning("正式收盘数据尚未完全补齐：" + "；".join(warnings))

    snapshot = build_live_account_snapshot(
        trades,
        cash_flows,
        histories,
        market_now=market_now,
        formal_target_date=latest_final_etf_trade_date(market_now),
    )
    render_live_account_section(
        snapshot,
        trades,
        render_positions=render_live_positions_table,
        readonly=readonly,
        key_prefix=key_prefix,
    )
    if show_history:
        _render_performance_history(snapshot, trades, key_prefix=key_prefix)


__all__ = ["render_live_account_dashboard"]
