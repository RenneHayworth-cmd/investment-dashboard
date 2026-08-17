from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
import streamlit as st

from services.live_trading import build_live_period_returns, build_live_return_month_grid
from services.market_calendar import get_market_holiday_label, get_market_window


PERIOD_OPTIONS = {
    "日收益": "day",
    "周收益": "week",
    "月收益": "month",
    "年收益": "year",
}
VALUE_OPTIONS = ("收益金额", "收益率")


def _format_signed(value: object, *, percentage: bool = False) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return "-"
    sign = "+" if float(number) > 0 else ""
    return f"{sign}{float(number):,.2f}{'%' if percentage else ''}"


def _tile(
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
    selected = return_pct if display_mode == "收益率" else pnl_amount
    number = pd.to_numeric(selected, errors="coerce")
    tooltip = (
        f"{period_start:%Y-%m-%d} 至 {period_end:%Y-%m-%d}｜"
        f"收益金额 {_format_signed(pnl_amount)}｜"
        f"收益率 {_format_signed(return_pct, percentage=True)}"
    )
    if pd.isna(number):
        value_class, background = "live-return-unavailable", "rgba(148,163,184,.12)"
    elif float(number) > 0:
        intensity = min(abs(float(number)) / max(max_abs_value, 1e-12), 1.0)
        value_class = "live-return-positive"
        background = f"rgba(239,68,68,{0.14 + intensity * 0.28:.3f})"
    elif float(number) < 0:
        intensity = min(abs(float(number)) / max(max_abs_value, 1e-12), 1.0)
        value_class = "live-return-negative"
        background = f"rgba(34,197,94,{0.14 + intensity * 0.28:.3f})"
    else:
        value_class, background = "live-return-zero", "rgba(148,163,184,.18)"
    detail = (
        f'<div class="live-return-detail">{html.escape(detail_label)}</div>'
        if detail_label
        else ""
    )
    return (
        f'<div class="live-return-tile {value_class}" style="background:{background}" '
        f'title="{html.escape(tooltip, quote=True)}">'
        f'<div class="live-return-label">{html.escape(label)}</div>'
        f'<div class="live-return-value">{html.escape(_format_signed(number, percentage=display_mode == "收益率"))}</div>'
        f"{detail}</div>"
    )


def _calendar_css() -> None:
    st.markdown(
        """
        <style>
        .live-return-nav-title{min-height:2.45rem;display:flex;align-items:center;justify-content:center;font-weight:650;font-size:1rem}
        .live-return-calendar-scroll{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
        .live-return-weekdays,.live-return-day-grid{min-width:500px;display:grid;grid-template-columns:repeat(5,minmax(88px,1fr));gap:.38rem}
        .live-return-weekdays{margin-bottom:.38rem;color:rgba(49,51,63,.64);font-size:.8rem;font-weight:600;text-align:center}
        .live-return-period-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:.5rem}
        .live-return-tile,.live-return-empty-day{min-height:88px;border-radius:6px;padding:.55rem .45rem;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;box-sizing:border-box;font-variant-numeric:tabular-nums}
        .live-return-tile{border:1px solid rgba(49,51,63,.08)}
        .live-return-empty-day{color:rgba(49,51,63,.42);border:1px solid rgba(49,51,63,.05)}
        .live-return-outside-month{border-color:transparent;color:rgba(49,51,63,.24)}
        .live-return-label{font-size:.78rem;line-height:1.2;opacity:.78}
        .live-return-value{margin-top:.34rem;font-size:.95rem;line-height:1.25;font-weight:700;white-space:nowrap}
        .live-return-detail{margin-top:.25rem;font-size:.7rem;line-height:1.2;opacity:.7;white-space:nowrap}
        .live-return-holiday{background:rgba(148,163,184,.06)}
        .live-return-holiday-name{margin-top:.34rem;color:rgb(217,119,6);font-size:.92rem;font-weight:650;line-height:1.25;white-space:nowrap}
        .live-return-positive{color:rgb(159,18,57)}.live-return-negative{color:rgb(21,94,55)}
        .live-return-zero,.live-return-unavailable{color:rgb(71,85,105)}
        .live-return-summary{margin-top:.65rem;display:flex;flex-wrap:wrap;justify-content:space-between;gap:.55rem 1.5rem;font-size:.92rem}
        .live-return-summary strong{font-variant-numeric:tabular-nums}.live-return-summary .positive{color:rgb(190,18,60)}.live-return-summary .negative{color:rgb(22,101,52)}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _navigate(key: str, current: int, minimum: int, maximum: int, formatter) -> int:
    previous, title, following = st.columns([1, 5, 1])
    if previous.button("‹", key=f"{key}_previous", help="上一个期间", disabled=current <= minimum, use_container_width=True):
        current = max(minimum, current - 1)
        st.session_state[key] = current
    if following.button("›", key=f"{key}_next", help="下一个期间", disabled=current >= maximum, use_container_width=True):
        current = min(maximum, current + 1)
        st.session_state[key] = current
    title.markdown(
        f'<div class="live-return-nav-title">{html.escape(formatter(current))}</div>',
        unsafe_allow_html=True,
    )
    return current


def _period_summary(values: pd.DataFrame) -> tuple[object, object]:
    if values.empty:
        return pd.NA, pd.NA
    amount = float(pd.to_numeric(values["pnl_amount"], errors="coerce").sum())
    rates = pd.to_numeric(values["return_pct"], errors="coerce")
    rate = pd.NA if rates.isna().any() else float(((1 + rates / 100).prod() - 1) * 100)
    return amount, rate


def render_return_calendar(
    daily_returns: pd.DataFrame,
    *,
    title: str,
    key_prefix: str,
    caption: str,
    first_date: object = None,
) -> None:
    st.subheader(title)
    required = {"date", "pnl_amount", "return_pct"}
    if daily_returns is None or daily_returns.empty or not required.issubset(daily_returns.columns):
        st.info("尚无可用于收益日历的完整估值数据。")
        return
    data = daily_returns.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data = data.dropna(subset=["date", "pnl_amount"]).sort_values("date")
    pending_dates = set()
    if "confirmation_status" in data.columns:
        pending_dates = set(
            data.loc[
                data["confirmation_status"].astype(str).ne("正式"), "date"
            ].dt.date
        )
    controls = st.columns([3, 2])
    period_label = controls[0].segmented_control(
        "统计周期", list(PERIOD_OPTIONS), default="日收益", key=f"{key_prefix}_period"
    ) or "日收益"
    display_mode = controls[1].segmented_control(
        "显示口径", list(VALUE_OPTIONS), default="收益金额", key=f"{key_prefix}_value"
    ) or "收益金额"
    period = PERIOD_OPTIONS[period_label]
    first = pd.to_datetime(first_date, errors="coerce")
    first = pd.Timestamp(data["date"].min()) if pd.isna(first) else min(pd.Timestamp(first), pd.Timestamp(data["date"].min()))
    latest = pd.Timestamp(data["date"].max())
    market = get_market_window("A股")
    excluded = {
        pd.Timestamp(value).date()
        for value in data["date"]
        if get_market_holiday_label(market, pd.Timestamp(value).date())
    }
    period_returns = build_live_period_returns(data, period=period, excluded_dates=excluded)
    _calendar_css()

    visible = period_returns.copy()
    if period == "day":
        minimum = first.year * 12 + first.month - 1
        maximum = latest.year * 12 + latest.month - 1
        state_key = f"{key_prefix}_month"
        current = min(max(int(st.session_state.get(state_key, maximum)), minimum), maximum)
        st.session_state[state_key] = current
        current = _navigate(
            state_key,
            current,
            minimum,
            maximum,
            lambda value: f"{value // 12}年{value % 12 + 1}月",
        )
        year, month = current // 12, current % 12 + 1
        visible = period_returns[
            period_returns["period_start"].dt.year.eq(year)
            & period_returns["period_start"].dt.month.eq(month)
        ]
        lookup = {pd.Timestamp(row.period_start).date(): row for row in visible.itertuples(index=False)}
        values = pd.to_numeric(visible["return_pct" if display_mode == "收益率" else "pnl_amount"], errors="coerce")
        max_abs = float(values.abs().max()) if values.notna().any() else 0.0
        cells: list[str] = []
        for calendar_date in (day for week in build_live_return_month_grid(year, month) for day in week):
            holiday = get_market_holiday_label(market, calendar_date)
            outside = " live-return-outside-month" if calendar_date.month != month else ""
            if holiday:
                cells.append(
                    f'<div class="live-return-empty-day live-return-holiday{outside}"><div class="live-return-label">{calendar_date.day:02d}</div><div class="live-return-holiday-name">{html.escape(holiday)}</div></div>'
                )
            elif calendar_date in lookup:
                row = lookup[calendar_date]
                cells.append(_tile(label=f"{calendar_date.day:02d}", period_start=pd.Timestamp(row.period_start), period_end=pd.Timestamp(row.period_end), pnl_amount=row.pnl_amount, return_pct=row.return_pct, display_mode=display_mode, max_abs_value=max_abs, detail_label="待月结单确认" if calendar_date in pending_dates else ""))
            else:
                cells.append(f'<div class="live-return-empty-day{outside}"><div class="live-return-label">{calendar_date.day:02d}</div></div>')
        st.markdown('<div class="live-return-calendar-scroll"><div class="live-return-weekdays"><div>一</div><div>二</div><div>三</div><div>四</div><div>五</div></div><div class="live-return-day-grid">' + "".join(cells) + "</div></div>", unsafe_allow_html=True)
        summary_label = f"{year}年{month}月"
    else:
        minimum, maximum = first.year, latest.year
        current = maximum
        if period in {"week", "month"}:
            state_key = f"{key_prefix}_{period}_year"
            current = min(max(int(st.session_state.get(state_key, maximum)), minimum), maximum)
            st.session_state[state_key] = current
            current = _navigate(state_key, current, minimum, maximum, lambda value: f"{value}年")
        if period == "week":
            visible = period_returns[period_returns["period_start"].map(lambda value: pd.Timestamp(value).isocalendar().year).eq(current)]
            lookup = {int(pd.Timestamp(row.period_start).isocalendar().week): row for row in visible.itertuples(index=False)}
            slots = [(f"第{week:02d}周", lookup.get(week)) for week in range(1, datetime(current, 12, 28).isocalendar().week + 1)]
        elif period == "month":
            visible = period_returns[period_returns["period_start"].dt.year.eq(current)]
            lookup = {int(pd.Timestamp(row.period_start).month): row for row in visible.itertuples(index=False)}
            slots = [(f"{month}月", lookup.get(month)) for month in range(1, 13)]
        else:
            lookup = {int(pd.Timestamp(row.period_start).year): row for row in visible.itertuples(index=False)}
            slots = [(f"{year}年", lookup.get(year)) for year in range(minimum, maximum + 1)]
        values = pd.to_numeric(visible["return_pct" if display_mode == "收益率" else "pnl_amount"], errors="coerce")
        max_abs = float(values.abs().max()) if values.notna().any() else 0.0
        cells = []
        for label, row in slots:
            if row is None:
                cells.append(f'<div class="live-return-empty-day"><div class="live-return-label">{html.escape(label)}</div></div>')
            else:
                period_start = pd.Timestamp(row.period_start).date()
                period_end = pd.Timestamp(row.period_end).date()
                pending = any(period_start <= day <= period_end for day in pending_dates)
                detail_parts = []
                if period == "week":
                    detail_parts.append(f"{pd.Timestamp(row.period_start):%m-%d} 至 {pd.Timestamp(row.period_end):%m-%d}")
                if pending:
                    detail_parts.append("待月结单确认")
                detail = " · ".join(detail_parts)
                cells.append(_tile(label=label, period_start=pd.Timestamp(row.period_start), period_end=pd.Timestamp(row.period_end), pnl_amount=row.pnl_amount, return_pct=row.return_pct, display_mode=display_mode, max_abs_value=max_abs, detail_label=detail))
        st.markdown('<div class="live-return-period-grid">' + "".join(cells) + "</div>", unsafe_allow_html=True)
        summary_label = f"{current}年" if period in {"week", "month"} else "全部年度"

    amount, rate = _period_summary(visible)
    amount_class = "positive" if pd.notna(amount) and float(amount) > 0 else "negative" if pd.notna(amount) and float(amount) < 0 else ""
    rate_class = "positive" if pd.notna(rate) and float(rate) > 0 else "negative" if pd.notna(rate) and float(rate) < 0 else ""
    st.markdown(
        f'<div class="live-return-summary"><span>{summary_label}收益金额：<strong class="{amount_class}">{_format_signed(amount)}</strong></span><span>{summary_label}收益率：<strong class="{rate_class}">{_format_signed(rate, percentage=True)}</strong></span></div>',
        unsafe_allow_html=True,
    )
    if pending_dates:
        st.caption("标有“待月结单确认”的收益包含最新月结单截止日之后的手工记录。")
    st.caption(caption)
