"""Dashboard UI for the isolated microcap live account."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import os

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.ui import (
    DEFAULT_CHART_HEIGHT,
    DOWN_COLOR,
    UP_COLOR,
    adaptive_bar_width,
    apply_plotly_layout,
    build_sparse_trading_date_ticks,
    filter_by_time_range,
)

from components.live_record.tables import render_live_positions_table
from core.return_calendar import render_return_calendar
from services.market_calendar import get_market_window, is_market_trading_day
from services.microcap_live_market import fetch_microcap_realtime_quotes, load_microcap_histories, microcap_target_date
from services.microcap_live_trading import (
    build_microcap_account_snapshot,
    build_microcap_positions,
    build_microcap_symbol_history,
    list_microcap_cash_flows,
    list_microcap_position_adjustments,
    list_microcap_trades,
)

TZ = ZoneInfo("Asia/Shanghai")


def _money(value):
    return "-" if value is None or pd.isna(value) else f"{float(value):,.2f}"


def _return(value):
    return "-" if value is None or pd.isna(value) else f"{float(value):+.2f}%"


def _pnl_value(value):
    if value is None or pd.isna(value):
        return "-"
    val = float(value)
    if abs(val) < 1e-6:
        return "0.00"
    if val > 0:
        return f"+{val:,.2f}"
    return f"{val:,.2f}"


def _return_delta(value):
    if value is None or pd.isna(value):
        return None
    val = float(value)
    if abs(val) < 1e-6:
        return "0.00%"
    return f"{val:+.2f}%"


def _ratio(value):
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):.2f}%"


def _market_open(now: datetime) -> bool:
    market = get_market_window("A股")
    if not is_market_trading_day(market, now):
        return False
    return any(start <= now.time().replace(tzinfo=None) < end for start, end in market.sessions)


def _summarize_positions(positions: pd.DataFrame) -> dict:
    if positions is None or positions.empty:
        return {key: 0.0 for key in ("market_value", "daily_pnl", "daily_return_pct", "cumulative_pnl", "cumulative_return_pct", "realized_pnl", "fee_amount")}
    def sum_complete(column):
        values = pd.to_numeric(positions[column], errors="coerce")
        return float(values.sum()) if values.notna().all() else pd.NA
    mv = sum_complete("market_value")
    daily = sum_complete("daily_pnl")
    cumulative = sum_complete("cumulative_pnl")
    basis_column = "cumulative_buy_cost" if "cumulative_buy_cost" in positions else "cost_basis"
    basis = float(pd.to_numeric(positions[basis_column], errors="coerce").sum())
    return {
        "market_value": mv,
        "daily_pnl": daily,
        "daily_return_pct": (
            float(positions["daily_pnl"].sum()) / float(positions["daily_return_base"].sum()) * 100
            if "daily_return_base" in positions and pd.to_numeric(positions["daily_return_base"], errors="coerce").sum() > 0 and pd.to_numeric(positions["daily_pnl"], errors="coerce").notna().all()
            else pd.NA
        ),
        "cumulative_pnl": cumulative,
        "cumulative_return_pct": float(cumulative) / basis * 100 if pd.notna(cumulative) and basis > 0 else pd.NA,
        "realized_pnl": float(pd.to_numeric(positions["realized_pnl"], errors="coerce").sum()),
        "fee_amount": float(pd.to_numeric(positions["fee_amount"], errors="coerce").sum()),
    }


def _ledger_codes(trades, adjustments):
    values = set()
    for frame, col in ((trades, "symbol"), (adjustments, "symbol")):
        if frame is not None and not frame.empty and col in frame:
            values.update(frame[col].dropna().astype(str).str.extract(r"(\d{6})", expand=False).dropna())
    return sorted(values)


def _first_event_date(trades, flows, adjustments):
    values = []
    for frame, col in ((trades, "trade_date"), (flows, "flow_date"), (adjustments, "event_date")):
        if frame is not None and not frame.empty and col in frame:
            values.extend(frame[col].dropna().astype(str).tolist())
    return min(values) if values else None


def _load_market_state(codes, first_date, current, manual_refresh):
    target = microcap_target_date(current)
    state_key = "microcap_live_history_state"
    old = st.session_state.get(state_key, {})
    scope = tuple(codes)
    attempted = pd.to_datetime(old.get("attempted_at"), errors="coerce")
    if not pd.isna(attempted) and attempted.tzinfo is not None:
        attempted = attempted.tz_convert(TZ).tz_localize(None)
    current_naive = pd.Timestamp(current).tz_localize(None) if pd.Timestamp(current).tzinfo is not None else pd.Timestamp(current)
    elapsed = (current_naive - attempted).total_seconds() if not pd.isna(attempted) else 999999
    should_fetch = bool(
        manual_refresh or old.get("scope") != scope or old.get("target") != target or elapsed >= 600
    )
    if should_fetch:
        st.session_state[state_key] = {"scope": scope, "target": target, "attempted_at": current.isoformat()}
    histories, failures = load_microcap_histories(
        codes, start_date=first_date, api_key=st.session_state.get("microcap_live_tickflow_key", os.getenv("TICKFLOW_API_KEY", "")),
        allow_fetch=should_fetch, market_now=current,
    )
    return target, histories, failures, should_fetch


def render_microcap_dashboard() -> None:
    st.subheader("账户总览")
    current = datetime.now(TZ)
    trades = list_microcap_trades()
    flows = list_microcap_cash_flows()
    adjustments = list_microcap_position_adjustments()
    codes = _ledger_codes(trades, adjustments)
    first_date = _first_event_date(trades, flows, adjustments)
    manual_refresh = st.button("更新正式行情", key="microcap_live_refresh_formal")
    target, histories, failures, history_refreshed = _load_market_state(codes, first_date, current, manual_refresh)

    quote_state = st.session_state.get("microcap_live_quote_state", {})
    quote_fetched_at = pd.to_datetime(quote_state.get("fetched_at"), errors="coerce")
    if not pd.isna(quote_fetched_at):
        quote_fetched_at = quote_fetched_at.tz_localize(TZ) if quote_fetched_at.tzinfo is None else quote_fetched_at.tz_convert(TZ)
        quote_age = (current - quote_fetched_at.to_pydatetime()).total_seconds()
    else:
        quote_age = 999999
    quote_failures = quote_state.get("failures", {})
    quotes = quote_state.get("quotes", {})
    if _market_open(current) and codes and quote_age >= 110:
        quotes, quote_failures = fetch_microcap_realtime_quotes(
            build_microcap_positions(trades, adjustments).get("symbol", pd.Series(dtype=str)).tolist(),
            api_key=st.session_state.get("microcap_live_tickflow_key", os.getenv("TICKFLOW_API_KEY", "")), market_now=current,
        )
        quote_state = {"fetched_at": current.isoformat(), "quotes": quotes, "failures": quote_failures}
        st.session_state["microcap_live_quote_state"] = quote_state
    elif not _market_open(current):
        quotes = {}

    snapshot = build_microcap_account_snapshot(
        trades, flows, adjustments, histories, quotes=quotes, market_now=current,
    )
    summary = snapshot["summary"]
    status_cols = st.columns(3)
    status_cols[0].caption("账户状态：" + ("已建账" if snapshot["initialized"] else "待录入期初资金"))
    status_cols[1].caption(f"正式行情目标日：{target}")
    status_cols[2].caption(f"估值时间：{current:%Y-%m-%d %H:%M:%S} 北京时间")
    position_ratio = _ratio(summary.get("position_ratio_pct")) if snapshot["initialized"] else "-"
    caption_text = "已检查本地缓存并尝试补齐最新完整交易日正式收盘；盘中报价不进入历史曲线。" if history_refreshed else ""
    st.markdown(
        f"""
        <div style="display: flex; justify-content: space-between; align-items: baseline; margin: 0.15rem 0 0.45rem 0;">
            <span style="color: var(--ui-muted); font-size: 0.86rem;">{caption_text}</span>
            <span style="color: var(--ui-muted); font-size: 0.88rem; white-space: nowrap;">当前仓位：<strong style="color: var(--ui-text); font-weight: 650; font-size: 0.98rem;">{position_ratio}</strong></span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if failures:
        for code, error in failures.items():
            st.warning(f"{code} 行情不完整：{error}")
    if quote_failures and _market_open(current):
        st.caption("盘中报价未覆盖：" + "；".join(f"{code}：{error}" for code, error in quote_failures.items()))
    for warning in snapshot["warnings"]:
        st.warning(warning)

    initialized = snapshot["initialized"]
    row1_items = [
        ("账户总资产", _money(summary["total_assets"]) if initialized else "-", None, None),
        ("可用现金", _money(summary["cash"]) if initialized else "-", None, None),
        ("持仓市值", _money(summary["market_value"]), None, f"当前仓位：{position_ratio}"),
    ]
    row2_items = [
        (
            "当日盈亏",
            _pnl_value(summary.get("daily_pnl")) if initialized else "-",
            _return_delta(summary.get("daily_return_pct")) if initialized else None,
            None,
        ),
        (
            "累计盈亏",
            _pnl_value(summary.get("account_pnl")) if initialized else "-",
            _return_delta(summary.get("cumulative_return_pct")) if initialized else None,
            None,
        ),
        ("累计费用", _money(summary.get("fee_amount")), None, None),
    ]

    cols1 = st.columns(3)
    for col, (label, value, delta, help_text) in zip(cols1, row1_items):
        if delta is not None:
            col.metric(label, value, delta=delta, delta_color="inverse", help=help_text)
        else:
            col.metric(label, value, help=help_text)

    with st.container(key="microcap_live_pnl_metrics"):
        cols2 = st.columns(3)
        for col, (label, value, delta, help_text) in zip(cols2, row2_items):
            if delta is not None:
                col.metric(label, value, delta=delta, delta_color="inverse", help=help_text)
            else:
                col.metric(label, value, help=help_text)
    st.caption("买入佣金计入持仓成本；卖出佣金与印花税从卖出回款扣除。转入/转出作为外部资金流剔除，现金分红计入收益。")

    positions = snapshot["positions"]
    st.markdown("#### 当前持仓")
    if positions.empty:
        st.info("暂无持仓。录入期初资金后，可手工记录成交、导入交割单或建立期初持仓。")
    else:
        render_live_positions_table(
            positions, total_assets=summary["total_assets"], summarize_positions=_summarize_positions,
        )
        options = {str(row.symbol): f"{row.name}（{row.symbol}）" for row in positions.itertuples(index=False)}
        selected = st.selectbox("查看标的成交明细", list(options), format_func=options.get, key="microcap_live_detail_symbol")
        detail = trades.loc[trades["symbol"].astype(str).eq(selected)].copy() if not trades.empty else pd.DataFrame()
        if not detail.empty:
            detail["成交金额"] = (pd.to_numeric(detail["price"], errors="coerce") * pd.to_numeric(detail["quantity"], errors="coerce")).round(2)
            detail["总费用"] = (
                pd.to_numeric(detail["commission_amount"], errors="coerce") + pd.to_numeric(detail["stamp_tax_amount"], errors="coerce")
                + pd.to_numeric(detail["other_fee_amount"], errors="coerce").fillna(0)
            ).round(2)
            detail = detail.rename(columns={"trade_date": "成交日期", "symbol": "代码", "name": "名称", "side": "方向", "price": "成交价", "quantity": "股数", "commission_amount": "佣金", "stamp_tax_amount": "印花税", "other_fee_amount": "其他费用"})
            cols = ["成交日期", "代码", "名称", "方向", "成交价", "股数", "成交金额", "佣金", "印花税", "其他费用", "总费用"]
            st.dataframe(detail[cols], hide_index=True, width="stretch", column_config={"成交价": st.column_config.NumberColumn(format="%.3f")})

    daily = snapshot["formal_daily"]
    st.markdown("#### 正式收盘盈亏曲线")
    if daily.empty:
        st.info("缺少完整正式行情，暂不能生成收益曲线。所有持仓标的在某日均有正式未复权收盘价后，该日才纳入。")
    else:
        daily = daily.copy()
        daily["daily_pnl"] = pd.to_numeric(daily["pnl_amount"], errors="coerce").fillna(0.0)
        daily["daily_return_pct"] = pd.to_numeric(daily["return_pct"], errors="coerce").fillna(0.0)
        daily["account_pnl"] = pd.to_numeric(daily["total_pnl"], errors="coerce").fillna(0.0)
        daily["nav"] = pd.to_numeric(daily["nav"], errors="coerce").fillna(1.0)
        daily["cumulative_return_pct"] = pd.to_numeric(daily["cumulative_return_pct"], errors="coerce").fillna(0.0)
        daily["total_assets"] = pd.to_numeric(daily["total_assets"], errors="coerce").fillna(0.0)

        period = st.segmented_control(
            "时间范围",
            ["近1月", "近3月", "近1年", "全部"],
            default="全部",
            key="microcap_live_period",
            label_visibility="collapsed",
        )
        chart_view_daily = filter_by_time_range(daily, date_column="date", period=period or "全部")
        if chart_view_daily.empty:
            chart_view_daily = daily
        chart_dates = pd.to_datetime(chart_view_daily["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        pnl_colors = [
            UP_COLOR if float(val) >= 0 else DOWN_COLOR
            for val in chart_view_daily["daily_pnl"]
        ]
        figure = make_subplots(specs=[[{"secondary_y": True}]])
        figure.add_trace(
            go.Scatter(
                x=chart_dates,
                y=chart_view_daily["nav"],
                mode="lines+markers",
                name="账户净值",
                line={"color": "#2563eb", "width": 2.4},
                marker={"size": 5},
                customdata=chart_view_daily[["daily_return_pct", "cumulative_return_pct"]],
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
                y=chart_view_daily["daily_pnl"],
                name="每日盈亏",
                width=adaptive_bar_width(len(chart_dates)),
                marker={"color": pnl_colors},
                opacity=0.75,
                customdata=chart_view_daily[["account_pnl", "total_assets", "daily_return_pct", "nav"]],
                hovertemplate=(
                    "当日盈亏：%{y:,.2f} 元<br>当日收益率：%{customdata[2]:.2f}%"
                    "<br>累计盈亏：%{customdata[0]:,.2f} 元<br>总资产：%{customdata[1]:,.2f} 元"
                    "<br>净值：%{customdata[3]:.4f}<extra></extra>"
                ),
            ),
            secondary_y=True,
        )
        apply_plotly_layout(figure, height=DEFAULT_CHART_HEIGHT)
        tickvals, ticktext = build_sparse_trading_date_ticks(chart_dates.tolist(), max_ticks=7)
        figure.update_xaxes(
            title_text="交易日",
            type="category",
            categoryorder="array",
            categoryarray=chart_dates.tolist(),
            tickmode="array",
            tickvals=tickvals,
            ticktext=ticktext,
        )
        figure.update_yaxes(title_text="账户净值", tickformat=".4f", secondary_y=False)
        figure.update_yaxes(title_text="每日盈亏（元）", secondary_y=True)

        st.plotly_chart(figure, width="stretch", key="microcap_live_pnl_curve", config={"displayModeBar": False})
        st.caption("横坐标仅排列完整正式估值交易日，周末和节假日已自动跳过。")
        render_return_calendar(
            daily[["date", "pnl_amount", "return_pct"]],
            title="收益日历",
            key_prefix="microcap_live_return_calendar",
            caption="按正式未复权收盘计算；缺少任一当日持仓行情的日期不估值。盘中报价仅用于账户卡片。",
            first_date=daily["date"].min(),
        )
        detail = daily.rename(
            columns={
                "date": "日期",
                "market_value": "持仓市值",
                "cost_basis": "剩余成本",
                "cash": "可用资金",
                "total_assets": "账户总资产",
                "external_flow": "外部资金净流入",
                "realized_pnl": "已实现盈亏",
                "unrealized_pnl": "未实现盈亏",
                "account_pnl": "累计盈亏",
                "daily_pnl": "当日盈亏",
                "daily_return_pct": "当日收益率(%)",
                "nav": "净值",
                "cumulative_return_pct": "累计收益率(%)",
            }
        )
        detail_cols = [
            c for c in [
                "日期", "账户总资产", "可用资金", "持仓市值", "已实现盈亏", "未实现盈亏",
                "累计盈亏", "当日盈亏", "当日收益率(%)", "净值", "累计收益率(%)",
            ] if c in detail.columns
        ]
        with st.expander("查看每日正式估值明细"):
            st.dataframe(detail[detail_cols].sort_values("日期", ascending=False), width="stretch", hide_index=True)



    symbol_history = build_microcap_symbol_history(trades, adjustments, histories, cash_flows=flows)
    if not symbol_history.empty:
        st.markdown("#### 逐标的盈亏（含已清仓标的）")
        history_display = symbol_history.rename(columns={
            "symbol": "代码", "name": "名称", "status": "状态", "first_date": "建账日期",
            "last_date": "最近事件", "quantity": "当前股数", "cumulative_buy_cost": "累计买入/期初成本",
            "cumulative_sell_proceeds": "累计卖出净回款", "market_value": "当前市值",
            "realized_pnl": "已实现盈亏", "unrealized_pnl": "未实现盈亏", "total_pnl": "累计盈亏",
            "return_pct": "累计收益率(%)", "fee_amount": "累计费用", "valuation_date": "估值日期",
        })
        st.dataframe(history_display, hide_index=True, width="stretch")


__all__ = ["render_microcap_dashboard"]
