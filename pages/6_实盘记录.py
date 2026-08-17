import html
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.db import init_db
from core.return_calendar import render_return_calendar
from core.ui import (
    DEFAULT_CHART_HEIGHT,
    apply_global_style,
    apply_plotly_layout,
    render_metric_grid,
    render_page_header,
)
from services.fund_analysis import FUND_ADJUST_NONE
from services.live_trading import (
    add_live_trade,
    append_live_symbol_pnl_total,
    build_live_daily_pnl,
    build_live_daily_returns,
    build_live_period_returns,
    build_live_return_month_grid,
    build_live_position_performance,
    build_live_symbol_pnl_history,
    delete_live_trade,
    enrich_live_trades,
    live_close_refresh_due,
    list_live_trades,
    summarize_live_position_performance,
    summarize_live_trades,
)
from services.market_calendar import get_market_holiday_label, get_market_window
from services.position_analysis import (
    latest_final_etf_trade_date,
    load_or_fetch_etf,
)


st.set_page_config(page_title="实盘记录", layout="wide")
init_db()
apply_global_style()

render_page_header(
    "实盘记录",
    "记录实际成交、手续费和持仓成本，与策略回测结果分开核算。",
    eyebrow="Live Trading",
)


def money(value: object) -> str:
    return f"{float(value):,.2f}"


LIVE_RETURN_PERIOD_OPTIONS = {
    "日收益": "day",
    "周收益": "week",
    "月收益": "month",
    "年收益": "year",
}
LIVE_RETURN_VALUE_OPTIONS = ("收益金额", "收益率")


def format_signed_return(value: object, *, percentage: bool = False) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return "-"
    suffix = "%" if percentage else ""
    sign = "+" if float(number) > 0 else ""
    return f"{sign}{float(number):,.2f}{suffix}"


def _live_return_tile(
    *,
    label: str,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    pnl_amount: object,
    return_pct: object,
    display_mode: str,
    max_abs_value: float,
    detail_label: str = "",
) -> str:
    selected_value = return_pct if display_mode == "收益率" else pnl_amount
    selected_number = pd.to_numeric(selected_value, errors="coerce")
    amount_number = pd.to_numeric(pnl_amount, errors="coerce")
    rate_number = pd.to_numeric(return_pct, errors="coerce")
    tooltip = (
        f"{period_start:%Y-%m-%d} 至 {period_end:%Y-%m-%d}｜"
        f"收益金额 {format_signed_return(amount_number)}｜"
        f"收益率 {format_signed_return(rate_number, percentage=True)}"
    )
    if pd.isna(selected_number):
        value_class = "live-return-unavailable"
        background = "rgba(148, 163, 184, 0.12)"
    elif float(selected_number) > 0:
        intensity = min(abs(float(selected_number)) / max(max_abs_value, 1e-12), 1.0)
        value_class = "live-return-positive"
        background = f"rgba(239, 68, 68, {0.14 + intensity * 0.28:.3f})"
    elif float(selected_number) < 0:
        intensity = min(abs(float(selected_number)) / max(max_abs_value, 1e-12), 1.0)
        value_class = "live-return-negative"
        background = f"rgba(34, 197, 94, {0.14 + intensity * 0.28:.3f})"
    else:
        value_class = "live-return-zero"
        background = "rgba(148, 163, 184, 0.18)"
    value_text = format_signed_return(
        selected_number,
        percentage=display_mode == "收益率",
    )
    detail_html = (
        f'<div class="live-return-detail">{html.escape(detail_label)}</div>'
        if detail_label
        else ""
    )
    return (
        f'<div class="live-return-tile {value_class}" '
        f'style="background:{background}" title="{html.escape(tooltip, quote=True)}">'
        f'<div class="live-return-label">{html.escape(label)}</div>'
        f'<div class="live-return-value">{html.escape(value_text)}</div>'
        f"{detail_html}</div>"
    )


def _live_return_holiday_tile(
    *,
    calendar_date,
    holiday_label: str,
    outside_month: bool,
) -> str:
    outside_class = " live-return-outside-month" if outside_month else ""
    tooltip = f"{calendar_date:%Y-%m-%d}｜A股{holiday_label}休市"
    return (
        f'<div class="live-return-empty-day live-return-holiday{outside_class}" '
        f'title="{html.escape(tooltip, quote=True)}">'
        f'<div class="live-return-label">{calendar_date.day:02d}</div>'
        f'<div class="live-return-holiday-name">{html.escape(holiday_label)}</div>'
        "</div>"
    )


def _return_period_summary(period_returns: pd.DataFrame) -> tuple[object, object]:
    if period_returns is None or period_returns.empty:
        return pd.NA, pd.NA
    amount = float(pd.to_numeric(period_returns["pnl_amount"], errors="coerce").sum())
    rates = pd.to_numeric(period_returns["return_pct"], errors="coerce")
    if rates.isna().any():
        return amount, pd.NA
    return amount, float(((1.0 + rates / 100.0).prod() - 1.0) * 100.0)


def _month_index(value: pd.Timestamp) -> int:
    return int(value.year) * 12 + int(value.month) - 1


def _month_from_index(value: int) -> tuple[int, int]:
    return int(value) // 12, int(value) % 12 + 1


def _bounded_session_value(key: str, *, minimum: int, maximum: int, default: int) -> int:
    value = int(st.session_state.get(key, default))
    value = min(max(value, minimum), maximum)
    st.session_state[key] = value
    return value


def _render_return_navigation(
    *,
    state_key: str,
    current: int,
    minimum: int,
    maximum: int,
    title_formatter,
    unit: int = 1,
) -> int:
    previous_col, title_col, next_col = st.columns([1, 5, 1])
    previous_clicked = previous_col.button(
        "‹",
        key=f"{state_key}_previous",
        help="上一个期间",
        disabled=current <= minimum,
        use_container_width=True,
    )
    next_clicked = next_col.button(
        "›",
        key=f"{state_key}_next",
        help="下一个期间",
        disabled=current >= maximum,
        use_container_width=True,
    )
    if previous_clicked:
        current = max(minimum, current - unit)
        st.session_state[state_key] = current
    elif next_clicked:
        current = min(maximum, current + unit)
        st.session_state[state_key] = current
    title_col.markdown(
        f'<div class="live-return-nav-title">{html.escape(title_formatter(current))}</div>',
        unsafe_allow_html=True,
    )
    return current


def _render_live_return_calendar_css() -> None:
    st.markdown(
        """
        <style>
        .live-return-nav-title {
            min-height: 2.45rem;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 650;
            font-size: 1rem;
        }
        .live-return-calendar-scroll {
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }
        .live-return-weekdays,
        .live-return-day-grid {
            min-width: 500px;
            display: grid;
            grid-template-columns: repeat(5, minmax(88px, 1fr));
            gap: 0.38rem;
        }
        .live-return-weekdays {
            margin-bottom: 0.38rem;
            color: rgba(49, 51, 63, 0.64);
            font-size: 0.8rem;
            font-weight: 600;
            text-align: center;
        }
        .live-return-period-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(138px, 1fr));
            gap: 0.5rem;
        }
        .live-return-tile,
        .live-return-empty-day {
            min-height: 88px;
            border-radius: 6px;
            padding: 0.55rem 0.45rem;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            box-sizing: border-box;
            font-variant-numeric: tabular-nums;
        }
        .live-return-tile {
            border: 1px solid rgba(49, 51, 63, 0.08);
        }
        .live-return-empty-day {
            color: rgba(49, 51, 63, 0.42);
            border: 1px solid rgba(49, 51, 63, 0.05);
        }
        .live-return-outside-month {
            border-color: transparent;
            color: rgba(49, 51, 63, 0.24);
        }
        .live-return-label {
            font-size: 0.78rem;
            line-height: 1.2;
            opacity: 0.78;
        }
        .live-return-value {
            margin-top: 0.34rem;
            font-size: 0.95rem;
            line-height: 1.25;
            font-weight: 700;
            white-space: nowrap;
        }
        .live-return-detail {
            margin-top: 0.25rem;
            font-size: 0.7rem;
            line-height: 1.2;
            opacity: 0.7;
            white-space: nowrap;
        }
        .live-return-holiday {
            background: rgba(148, 163, 184, 0.06);
        }
        .live-return-holiday-name {
            margin-top: 0.34rem;
            color: rgb(217, 119, 6);
            font-size: 0.92rem;
            font-weight: 650;
            line-height: 1.25;
            white-space: nowrap;
        }
        .live-return-positive { color: rgb(159, 18, 57); }
        .live-return-negative { color: rgb(21, 94, 55); }
        .live-return-zero,
        .live-return-unavailable { color: rgb(71, 85, 105); }
        .live-return-summary {
            margin-top: 0.65rem;
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            gap: 0.55rem 1.5rem;
            font-size: 0.92rem;
        }
        .live-return-summary strong { font-variant-numeric: tabular-nums; }
        .live-return-summary .positive { color: rgb(190, 18, 60); }
        .live-return-summary .negative { color: rgb(22, 101, 52); }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_live_return_calendar(
    daily_pnl: pd.DataFrame,
    *,
    first_trade_date: object = None,
) -> None:
    st.subheader("收益日历")
    daily_returns = build_live_daily_returns(daily_pnl)
    if daily_returns.empty:
        st.info("尚无可用于收益日历的完整估值数据。")
        return

    controls = st.columns([3, 2])
    period_label = controls[0].segmented_control(
        "统计周期",
        options=list(LIVE_RETURN_PERIOD_OPTIONS),
        default="日收益",
        key="live_return_calendar_period",
    ) or "日收益"
    display_mode = controls[1].segmented_control(
        "显示口径",
        options=list(LIVE_RETURN_VALUE_OPTIONS),
        default="收益金额",
        key="live_return_calendar_value",
    ) or "收益金额"
    period = LIVE_RETURN_PERIOD_OPTIONS[period_label]
    first_valuation_date = pd.Timestamp(daily_returns["date"].min())
    latest_date = pd.Timestamp(daily_returns["date"].max())
    requested_start = pd.to_datetime(first_trade_date, errors="coerce")
    first_date = (
        pd.Timestamp(requested_start).normalize()
        if not pd.isna(requested_start)
        else first_valuation_date
    )
    first_date = min(first_date, first_valuation_date)
    a_share_market = get_market_window("A股")
    holiday_dates = {
        pd.Timestamp(value).date()
        for value in daily_returns["date"]
        if get_market_holiday_label(a_share_market, pd.Timestamp(value).date())
    }
    period_returns = build_live_period_returns(
        daily_returns,
        period=period,
        excluded_dates=holiday_dates,
    )
    _render_live_return_calendar_css()

    if len(daily_returns) == 1:
        st.info("当前仅有1个完整估值日，收益日历会保留该日真实收益，周期比较需等待更多数据。")

    if period == "day":
        state_key = "live_return_calendar_month"
        minimum = _month_index(first_date)
        maximum = _month_index(latest_date)
        current = _bounded_session_value(
            state_key,
            minimum=minimum,
            maximum=maximum,
            default=maximum,
        )
        current = _render_return_navigation(
            state_key=state_key,
            current=current,
            minimum=minimum,
            maximum=maximum,
            title_formatter=lambda value: (
                f"{_month_from_index(value)[0]}年{_month_from_index(value)[1]}月"
            ),
        )
        selected_year, selected_month = _month_from_index(current)
        visible = period_returns[
            period_returns["period_start"].dt.year.eq(selected_year)
            & period_returns["period_start"].dt.month.eq(selected_month)
        ].copy()
        lookup = {
            pd.Timestamp(row.period_start).date(): row
            for row in visible.itertuples(index=False)
        }
        values = (
            pd.to_numeric(visible["return_pct"], errors="coerce")
            if display_mode == "收益率"
            else pd.to_numeric(visible["pnl_amount"], errors="coerce")
        )
        max_abs_value = float(values.abs().max()) if values.notna().any() else 0.0
        day_cells: list[str] = []
        month_grid = build_live_return_month_grid(selected_year, selected_month)
        for calendar_date in (day for week in month_grid for day in week):
            holiday_label = get_market_holiday_label(a_share_market, calendar_date)
            if holiday_label:
                day_cells.append(
                    _live_return_holiday_tile(
                        calendar_date=calendar_date,
                        holiday_label=holiday_label,
                        outside_month=calendar_date.month != selected_month,
                    )
                )
                continue
            row = lookup.get(calendar_date)
            if row is not None:
                day_cells.append(
                    _live_return_tile(
                        label=f"{calendar_date.day:02d}",
                        period_start=pd.Timestamp(row.period_start),
                        period_end=pd.Timestamp(row.period_end),
                        pnl_amount=row.pnl_amount,
                        return_pct=row.return_pct,
                        display_mode=display_mode,
                        max_abs_value=max_abs_value,
                    )
                )
                continue
            outside_class = (
                " live-return-outside-month"
                if calendar_date.month != selected_month
                else ""
            )
            day_cells.append(
                f'<div class="live-return-empty-day{outside_class}">'
                f'<div class="live-return-label">{calendar_date.day:02d}</div></div>'
            )
        st.markdown(
            """
            <div class="live-return-calendar-scroll">
                <div class="live-return-weekdays">
                    <div>一</div><div>二</div><div>三</div><div>四</div>
                    <div>五</div>
                </div>
                <div class="live-return-day-grid">
            """
            + "".join(day_cells)
            + "</div></div>",
            unsafe_allow_html=True,
        )
        summary_label = f"{selected_year}年{selected_month}月"
    else:
        if period == "week":
            iso_years = period_returns["period_start"].map(
                lambda value: pd.Timestamp(value).isocalendar().year
            )
            minimum = int(first_date.isocalendar().year)
            maximum = int(latest_date.isocalendar().year)
        else:
            minimum = int(first_date.year)
            maximum = int(latest_date.year)

        if period in {"week", "month"}:
            state_key = f"live_return_calendar_{period}_year"
            current_year = _bounded_session_value(
                state_key,
                minimum=minimum,
                maximum=maximum,
                default=maximum,
            )
            current_year = _render_return_navigation(
                state_key=state_key,
                current=current_year,
                minimum=minimum,
                maximum=maximum,
                title_formatter=lambda value: f"{value}年",
            )
        else:
            current_year = maximum

        if period == "week":
            iso_years = period_returns["period_start"].map(
                lambda value: pd.Timestamp(value).isocalendar().year
            )
            visible = period_returns[iso_years.eq(current_year)].copy()
            lookup = {
                int(pd.Timestamp(row.period_start).isocalendar().week): row
                for row in visible.itertuples(index=False)
            }
            last_week = datetime(current_year, 12, 28).isocalendar().week
            slots = []
            for week_number in range(1, last_week + 1):
                week_start = pd.Timestamp(datetime.fromisocalendar(current_year, week_number, 1))
                slots.append((f"第{week_number:02d}周", week_start, lookup.get(week_number)))
        elif period == "month":
            visible = period_returns[
                period_returns["period_start"].dt.year.eq(current_year)
            ].copy()
            lookup = {
                int(pd.Timestamp(row.period_start).month): row
                for row in visible.itertuples(index=False)
            }
            slots = [
                (f"{month}月", pd.Timestamp(current_year, month, 1), lookup.get(month))
                for month in range(1, 13)
            ]
        else:
            visible = period_returns.copy()
            lookup = {
                int(pd.Timestamp(row.period_start).year): row
                for row in visible.itertuples(index=False)
            }
            slots = [
                (f"{year}年", pd.Timestamp(year, 1, 1), lookup.get(year))
                for year in range(minimum, maximum + 1)
            ]

        values = (
            pd.to_numeric(visible["return_pct"], errors="coerce")
            if display_mode == "收益率"
            else pd.to_numeric(visible["pnl_amount"], errors="coerce")
        )
        max_abs_value = float(values.abs().max()) if values.notna().any() else 0.0
        period_cells: list[str] = []
        for slot_label, _slot_start, row in slots:
            if row is None:
                period_cells.append(
                    '<div class="live-return-empty-day">'
                    f'<div class="live-return-label">{html.escape(slot_label)}</div></div>'
                )
                continue
            detail_label = (
                f"{pd.Timestamp(row.period_start):%m-%d} 至 "
                f"{pd.Timestamp(row.period_end):%m-%d}"
                if period == "week"
                else ""
            )
            period_cells.append(
                _live_return_tile(
                    label=slot_label,
                    period_start=pd.Timestamp(row.period_start),
                    period_end=pd.Timestamp(row.period_end),
                    pnl_amount=row.pnl_amount,
                    return_pct=row.return_pct,
                    display_mode=display_mode,
                    max_abs_value=max_abs_value,
                    detail_label=detail_label,
                )
            )
        st.markdown(
            '<div class="live-return-period-grid">'
            + "".join(period_cells)
            + "</div>",
            unsafe_allow_html=True,
        )
        summary_label = f"{current_year}年" if period in {"week", "month"} else "全部年度"

    if visible.empty:
        st.info("当前期间没有完整的正式收盘估值，日历保持空白。")

    summary_amount, summary_return_pct = _return_period_summary(visible)
    amount_number = pd.to_numeric(summary_amount, errors="coerce")
    amount_class = (
        "positive"
        if not pd.isna(amount_number) and float(amount_number) > 0
        else "negative"
        if not pd.isna(amount_number) and float(amount_number) < 0
        else ""
    )
    rate_number = pd.to_numeric(summary_return_pct, errors="coerce")
    rate_class = (
        "positive"
        if not pd.isna(rate_number) and float(rate_number) > 0
        else "negative"
        if not pd.isna(rate_number) and float(rate_number) < 0
        else ""
    )
    st.markdown(
        '<div class="live-return-summary">'
        f'<span>{html.escape(summary_label)}收益金额：'
        f'<strong class="{amount_class}">{html.escape(format_signed_return(summary_amount))}</strong></span>'
        f'<span>{html.escape(summary_label)}收益率：'
        f'<strong class="{rate_class}">{html.escape(format_signed_return(summary_return_pct, percentage=True))}</strong></span>'
        "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "收益率按每日实际持仓资金计算后复合；买入视为当日新增投入，"
        "同日卖出回款优先抵扣买入，不包含账户未投资现金。"
    )


def render_live_return_calendar(
    daily_pnl: pd.DataFrame,
    *,
    first_trade_date: object = None,
) -> None:
    render_return_calendar(
        build_live_daily_returns(daily_pnl),
        title="收益日历",
        key_prefix="live_return_calendar",
        first_date=first_trade_date,
        caption=(
            "收益率按每日实际持仓资金计算后复合；买入视为当日新增投入，"
            "同日卖出回款优先抵扣买入，不包含账户未投资现金。"
        ),
    )


def format_live_number(value: object, digits: int = 2, prefix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{prefix}{float(value):,.{digits}f}"


def render_live_positions_table(positions: pd.DataFrame) -> None:
    headers = [
        "标的名称",
        "代码",
        "市值",
        "现价",
        "持仓数量",
        "成本",
        "当日盈亏",
        "累计盈亏",
        "仓位",
        "已实现盈亏",
        "累计手续费",
    ]

    def pnl_cell(amount: object, rate: object) -> str:
        if amount is None or pd.isna(amount) or rate is None or pd.isna(rate):
            return '<td class="live-pnl-cell">-</td>'
        amount_value = float(amount)
        rate_value = float(rate)
        value_class = (
            "live-pnl-positive"
            if amount_value > 0
            else "live-pnl-negative"
            if amount_value < 0
            else ""
        )
        return (
            f'<td class="live-pnl-cell {value_class}">'
            f'<div>{html.escape(format_live_number(amount_value))}</div>'
            f'<div class="live-pnl-rate">{html.escape(format_live_number(rate_value))}%</div>'
            "</td>"
        )

    total = summarize_live_position_performance(positions)
    total_market_value = pd.to_numeric(total["market_value"], errors="coerce")
    rows: list[str] = []
    for row in positions.itertuples(index=False):
        market_value = pd.to_numeric(row.market_value, errors="coerce")
        weight_pct = (
            float(market_value) / float(total_market_value) * 100
            if not pd.isna(market_value)
            and not pd.isna(total_market_value)
            and float(total_market_value) > 0
            else pd.NA
        )
        cells = [
            f"<td>{html.escape(str(row.name))}</td>",
            f"<td>{html.escape(str(row.symbol))}</td>",
            f"<td>{html.escape(format_live_number(row.market_value))}</td>",
            f"<td>{html.escape(format_live_number(row.latest_price, 3))}</td>",
            f"<td>{int(row.quantity):,}</td>",
            f"<td>{html.escape(format_live_number(row.average_cost, 3))}</td>",
            pnl_cell(row.daily_pnl, row.daily_return_pct),
            pnl_cell(row.cumulative_pnl, row.cumulative_return_pct),
            f"<td>{html.escape(format_live_number(weight_pct))}%</td>"
            if not pd.isna(weight_pct)
            else "<td>-</td>",
            f"<td>{html.escape(format_live_number(row.realized_pnl))}</td>",
            f"<td>{html.escape(format_live_number(row.fee_amount))}</td>",
        ]
        rows.append(f"<tr>{''.join(cells)}</tr>")

    total_cells = [
        '<td class="live-total-label">合计</td>',
        "<td>-</td>",
        f"<td>{html.escape(format_live_number(total['market_value']))}</td>",
        "<td>-</td>",
        "<td>-</td>",
        "<td>-</td>",
        pnl_cell(total["daily_pnl"], total["daily_return_pct"]),
        pnl_cell(total["cumulative_pnl"], total["cumulative_return_pct"]),
        "<td>100.00%</td>"
        if not pd.isna(total_market_value) and float(total_market_value) > 0
        else "<td>-</td>",
        f"<td>{html.escape(format_live_number(total['realized_pnl']))}</td>",
        f"<td>{html.escape(format_live_number(total['fee_amount']))}</td>",
    ]
    rows.append(f'<tr class="live-position-total">{"".join(total_cells)}</tr>')

    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    st.markdown(
        f"""
        <style>
        .live-position-table-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .live-position-table {{
            width: 100%;
            min-width: 1120px;
            border-collapse: collapse;
            font-size: 0.9rem;
        }}
        .live-position-table th,
        .live-position-table td {{
            padding: 0.5rem 0.55rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            text-align: center;
            white-space: nowrap;
        }}
        .live-position-table th {{
            background: rgba(49, 51, 63, 0.04);
            font-weight: 600;
        }}
        .live-position-table .live-position-total td {{
            border-top: 2px solid rgba(49, 51, 63, 0.22);
            background: rgba(49, 51, 63, 0.035);
            font-weight: 600;
        }}
        .live-position-table .live-total-label {{
            font-weight: 700;
        }}
        .live-position-table .live-pnl-cell {{
            line-height: 1.25;
            font-variant-numeric: tabular-nums;
        }}
        .live-position-table .live-pnl-rate {{
            margin-top: 0.16rem;
            font-size: 0.82rem;
            opacity: 0.82;
        }}
        .live-position-table .live-pnl-positive {{
            color: rgb(190, 18, 60);
            font-weight: 600;
        }}
        .live-position-table .live-pnl-negative {{
            color: rgb(22, 101, 52);
            font-weight: 600;
        }}
        </style>
        <div class="live-position-table-scroll">
            <table class="live-position-table">
                <thead><tr>{header_html}</tr></thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_live_symbol_history_table(history: pd.DataFrame) -> None:
    date_columns = {"首次交易日", "最近交易日", "估值日期"}
    pnl_columns = {"已实现盈亏", "未实现盈亏", "累计盈亏", "累计盈亏率(%)"}
    numeric_columns = {
        "累计买入成本",
        "累计卖出回款",
        "当前市值",
        "已实现盈亏",
        "未实现盈亏",
        "累计盈亏",
        "累计盈亏率(%)",
        "累计手续费",
    }

    def cell_html(column: str, value: object) -> str:
        cell_class = ""
        if column in date_columns:
            date_value = pd.to_datetime(value, errors="coerce")
            text = "-" if pd.isna(date_value) else pd.Timestamp(date_value).strftime("%Y-%m-%d")
        elif column == "当前数量":
            number = pd.to_numeric(value, errors="coerce")
            text = "-" if pd.isna(number) else f"{int(number):,}"
        elif column in numeric_columns:
            number = pd.to_numeric(value, errors="coerce")
            text = "-" if pd.isna(number) else f"{float(number):,.2f}"
            if column in pnl_columns and not pd.isna(number):
                cell_class = (
                    "live-history-positive"
                    if float(number) > 0
                    else "live-history-negative"
                    if float(number) < 0
                    else ""
                )
        else:
            text = "-" if value is None or pd.isna(value) else str(value)
        return f'<td class="{cell_class}">{html.escape(text)}</td>'

    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in history.columns)
    rows: list[str] = []
    for _, row in history.iterrows():
        row_class = "live-symbol-history-total" if str(row.get("标的名称", "")) == "合计" else ""
        cells = "".join(cell_html(str(column), row[column]) for column in history.columns)
        rows.append(f'<tr class="{row_class}">{cells}</tr>')
    st.markdown(
        f"""
        <style>
        .live-symbol-history-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .live-symbol-history-table {{
            width: 100%;
            min-width: 1680px;
            border-collapse: collapse;
            font-size: 0.88rem;
        }}
        .live-symbol-history-table th,
        .live-symbol-history-table td {{
            padding: 0.5rem 0.55rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            text-align: center;
            white-space: nowrap;
        }}
        .live-symbol-history-table th {{
            background: rgba(49, 51, 63, 0.04);
            font-weight: 600;
        }}
        .live-symbol-history-table .live-symbol-history-total td {{
            border-top: 2px solid rgba(49, 51, 63, 0.22);
            background: rgba(49, 51, 63, 0.035);
            font-weight: 600;
        }}
        .live-symbol-history-table .live-history-positive {{
            color: rgb(190, 18, 60);
            font-weight: 600;
        }}
        .live-symbol-history-table .live-history-negative {{
            color: rgb(22, 101, 52);
            font-weight: 600;
        }}
        </style>
        <div class="live-symbol-history-scroll">
            <table class="live-symbol-history-table">
                <thead><tr>{headers}</tr></thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


trades = list_live_trades()
summary = summarize_live_trades(trades)
render_metric_grid(
    [
        ("成交记录", str(summary["record_count"]), "已保存的实际成交笔数"),
        ("当前标的", str(summary["position_count"]), "当前数量大于零的标的数量"),
        ("累计买入金额", money(summary["buy_amount"]), "不含手续费的累计买入金额"),
        ("累计手续费", money(summary["fee_amount"]), "全部买卖成交手续费合计"),
        ("当前净投入", money(summary["net_investment"]), "买入支出减去卖出回款"),
    ]
)

@st.fragment(run_every="120s")
def render_daily_close_pnl() -> None:
    current_trades = list_live_trades()
    if current_trades.empty:
        st.subheader("当前实盘持仓")
        st.info("暂无实盘持仓。")
        st.subheader("每日收盘盈亏")
        st.info("录入成交后，将按正式收盘价生成每日盈亏走势。")
        return

    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target_date = latest_final_etf_trade_date(market_now)
    attempt_key = "live_pnl_close_last_attempt"
    attempt_target_key = "live_pnl_close_last_target"
    attempt_scope_key = "live_pnl_close_last_scope"
    market_now_naive = market_now.replace(tzinfo=None)
    symbols = sorted(current_trades["symbol"].dropna().astype(str).unique())
    refresh_scope = "|".join(symbols)
    network_refresh_due = live_close_refresh_due(
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
        item = load_or_fetch_etf(
            symbol,
            api_key=os.getenv("TICKFLOW_API_KEY", ""),
            count=5000,
            adjust=FUND_ADJUST_NONE,
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
    position_performance = build_live_position_performance(
        current_trades,
        price_histories,
    )
    if position_performance.empty:
        st.info("暂无实盘持仓。")
    else:
        render_live_positions_table(position_performance)
        st.caption(
            "当日盈亏按本次与上一个正式收盘估值的累计盈亏差额计算；"
            "当日新增买入成本计入当日收益率分母。"
        )

    daily_pnl = build_live_daily_pnl(current_trades, price_histories)
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
    render_live_return_calendar(daily_pnl, first_trade_date=first_trade_date)

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
render_daily_close_pnl()

st.subheader("新增成交")
with st.form("live_trade_form", clear_on_submit=True):
    row1 = st.columns([1.1, 1, 1.6, 1])
    with row1[0]:
        trade_date = st.date_input(
            "成交日期",
            value=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
        )
    with row1[1]:
        side = st.selectbox("成交方向", ["买入", "卖出"])
    with row1[2]:
        symbol = st.text_input("代码", placeholder="例如：159501")
    with row1[3]:
        quantity = st.number_input("数量", min_value=0, value=0, step=100)

    row2 = st.columns([1.4, 1, 1, 2])
    with row2[0]:
        name = st.text_input("标的名称")
    with row2[1]:
        price = st.number_input(
            "成交价格",
            min_value=0.0,
            value=0.0,
            step=0.001,
            format="%.3f",
        )
    with row2[2]:
        fee_rate_pct = st.number_input(
            "手续费率(%)",
            min_value=0.0,
            value=0.006,
            step=0.001,
            format="%.4f",
        )
    with row2[3]:
        strategy = st.text_input("策略说明")
    notes = st.text_input("备注")
    submitted = st.form_submit_button("保存成交", type="primary")

if submitted:
    try:
        add_live_trade(
            trade_date=trade_date,
            symbol=symbol,
            name=name,
            side=side,
            price=price,
            quantity=int(quantity),
            fee_rate_pct=fee_rate_pct,
            strategy=strategy,
            notes=notes,
        )
        st.success("成交记录已保存。")
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))

st.subheader("成交明细")
if trades.empty:
    st.info("暂无成交记录。")
else:
    detail = enrich_live_trades(trades).rename(
        columns={
            "id": "记录ID",
            "trade_date": "成交日期",
            "symbol": "代码",
            "name": "标的名称",
            "side": "方向",
            "price": "成交价格",
            "quantity": "数量",
            "fee_rate_pct": "手续费率(%)",
            "gross_amount": "成交金额",
            "fee_amount": "手续费",
            "cash_amount": "实际收付金额",
            "strategy": "策略说明",
            "notes": "备注",
            "created_at": "记录时间",
        }
    )
    detail = detail[
        [
            "记录ID",
            "成交日期",
            "代码",
            "标的名称",
            "方向",
            "成交价格",
            "数量",
            "手续费率(%)",
            "成交金额",
            "手续费",
            "实际收付金额",
            "策略说明",
            "备注",
        ]
    ]
    st.dataframe(
        detail,
        width="stretch",
        hide_index=True,
        column_config={
            "成交价格": st.column_config.NumberColumn(format="%.3f"),
            "数量": st.column_config.NumberColumn(format="%d"),
            "手续费率(%)": st.column_config.NumberColumn(format="%.4f%%"),
            "成交金额": st.column_config.NumberColumn(format="%.2f"),
            "手续费": st.column_config.NumberColumn(format="%.2f"),
            "实际收付金额": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    st.download_button(
        "导出成交记录",
        data=detail.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
        file_name="实盘成交记录.csv",
        mime="text/csv",
    )

    with st.expander("删除误录记录"):
        trade_options = {
            int(row.id): (
                f"#{int(row.id)}｜{row.trade_date}｜{row.symbol}｜{row.side} "
                f"{int(row.quantity)}份 @ {float(row.price):.3f}"
            )
            for row in trades.itertuples(index=False)
        }
        selected_id = st.selectbox(
            "选择记录",
            options=list(trade_options),
            format_func=trade_options.get,
        )
        if st.button("删除所选记录", type="secondary"):
            try:
                if delete_live_trade(int(selected_id)):
                    st.success("记录已删除。")
                    st.rerun()
                else:
                    st.error("记录不存在或已经删除。")
            except ValueError as exc:
                st.error(str(exc))


@st.fragment(run_every="120s")
def render_live_symbol_pnl_history() -> None:
    st.subheader("历史盈亏")
    all_trades = list_live_trades()
    if all_trades.empty:
        st.info("暂无可汇总的历史成交。")
        return

    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    price_histories: dict[str, pd.DataFrame] = {}
    symbols = sorted(all_trades["symbol"].dropna().astype(str).unique())
    for symbol in symbols:
        item = load_or_fetch_etf(
            symbol,
            api_key=os.getenv("TICKFLOW_API_KEY", ""),
            count=5000,
            adjust=FUND_ADJUST_NONE,
            allow_fetch=False,
            force_refresh=False,
            save_to_cache=False,
            market_now=market_now,
        )
        if item.dataframe is not None and not item.dataframe.empty:
            price_histories[symbol] = item.dataframe

    history = append_live_symbol_pnl_total(
        build_live_symbol_pnl_history(all_trades, price_histories)
    )
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
    render_live_symbol_history_table(history_display)
    st.caption(
        "包含当前持仓和已清仓标的；买入成本含买入手续费，"
        "卖出回款已扣除卖出手续费。"
    )


render_live_symbol_pnl_history()
