"""首页账户总览：合计卡片、各账户卡片、总净值曲线和总收益日历。"""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from core.return_calendar import render_return_calendar
from core.ui import (
    DEFAULT_CHART_HEIGHT,
    DOWN_COLOR,
    DOWN_TEXT_COLOR,
    NEUTRAL_TEXT_COLOR,
    PRIMARY_COLOR,
    UP_COLOR,
    UP_TEXT_COLOR,
    adaptive_bar_width,
    apply_plotly_layout,
    build_sparse_trading_date_ticks,
    filter_by_time_range,
    pnl_color,
)
from services.home_overview import ACCOUNT_LABELS, AccountSeries, load_account_overview

MARKET_TZ = ZoneInfo("Asia/Shanghai")


@st.cache_data(ttl=300, show_spinner="正在汇总三个实盘账本…")
def _cached_overview() -> dict[str, object]:
    return load_account_overview(market_now=datetime.now(MARKET_TZ))


def _money(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):,.2f}"


def _signed_money(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):+,.2f}"


def _signed_pct(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):+.2f}%"


def _date(value: object) -> str:
    return "-" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def _color(value: object) -> str:
    return pnl_color(value, up=UP_TEXT_COLOR, down=DOWN_TEXT_COLOR, flat=NEUTRAL_TEXT_COLOR)


def _account_return(account: AccountSeries) -> tuple[float | None, float | None]:
    """账户自身按日复合的累计收益率，以及最新一日收益率（%）。"""
    daily = account.daily
    if daily.empty:
        return None, None
    rates = (daily["pnl_amount"] / daily["return_base"]).where(daily["return_base"].gt(0), 0.0)
    return float(((1.0 + rates).prod() - 1.0) * 100.0), float(rates.iloc[-1] * 100.0)


def _metric(label: str, value: str, *, secondary: str = "", color: str | None = None) -> str:
    style = f' style="color:{color}"' if color else ""
    extra = f'<span class="home-metric-secondary"{style}>{html.escape(secondary)}</span>' if secondary else ""
    return (
        '<div class="home-metric">'
        f'<div class="home-metric-label">{html.escape(label)}</div>'
        f'<div class="home-metric-value"{style}>{html.escape(value)}{extra}</div>'
        "</div>"
    )


_STYLE = """
<style>
.home-grid { display: grid; gap: 0.65rem; margin: 0.6rem 0 0.4rem; }
.home-grid.total { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.home-grid.accounts { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.home-card, .home-metric {
    min-width: 0; padding: 0.72rem 0.8rem; border: 1px solid var(--ui-border);
    border-radius: 8px; background: var(--ui-surface); box-shadow: var(--ui-shadow);
}
.home-metric-label, .home-card-meta { color: var(--ui-muted); font-size: 0.86rem; line-height: 1.25; }
.home-metric-label { margin-bottom: 0.42rem; white-space: nowrap; }
.home-metric-value { color: var(--ui-text); font-size: 1.32rem; font-weight: 680; line-height: 1.2; white-space: nowrap; }
.home-metric-secondary { margin-left: 0.7rem; font-size: 0.9rem; font-weight: 560; }
.home-card-title { display: flex; justify-content: space-between; align-items: baseline; gap: 0.5rem; }
.home-card-name { font-weight: 650; font-size: 1rem; color: var(--ui-text); }
.home-card-assets { font-size: 1.3rem; font-weight: 680; color: var(--ui-text); margin: 0.35rem 0 0.45rem; }
.home-card-row { display: flex; justify-content: space-between; font-size: 0.9rem; line-height: 1.6; }
.home-card-row span:first-child { color: var(--ui-muted); }
@media (max-width: 900px) {
    .home-grid.total { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .home-grid.accounts { grid-template-columns: minmax(0, 1fr); }
    .home-metric-label { white-space: normal; }
}
</style>
"""


def _render_total_cards(
    accounts: list[AccountSeries], combined: pd.DataFrame, start: object, inception_pnl: float
) -> None:
    total_assets = sum(account.total_assets or 0.0 for account in accounts)
    latest = combined.iloc[-1] if not combined.empty else None
    daily_pnl = latest["pnl_amount"] if latest is not None else None
    daily_rate = latest["return_pct"] if latest is not None else None
    cumulative = latest["cumulative_pnl"] if latest is not None else None
    cumulative_rate = latest["cumulative_return_pct"] if latest is not None else None
    since = _date(start)
    cards = [
        _metric("合计总资产", _money(total_assets)),
        _metric("当日盈亏", _signed_money(daily_pnl), secondary=_signed_pct(daily_rate), color=_color(daily_pnl)),
        _metric(
            "总盈亏（开户以来）",
            _signed_money(inception_pnl),
            secondary=f"{since[5:]}起 {_signed_money(cumulative)}" if cumulative is not None else "",
            color=_color(inception_pnl),
        ),
        _metric(f"累计收益率（{since}起）", _signed_pct(cumulative_rate) or "-", color=_color(cumulative_rate)),
    ]
    st.markdown(_STYLE + '<div class="home-grid total">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def _render_account_cards(accounts: list[AccountSeries]) -> None:
    cards = []
    for account in accounts:
        cumulative_rate, daily_rate = _account_return(account)
        daily_pnl = account.daily["pnl_amount"].iloc[-1] if not account.daily.empty else None
        asset_label = "客户权益" if account.key == "futures" else "总资产"
        ledger_start = _date(account.daily["date"].min())
        rows = [
            ("当日盈亏", f"{_signed_money(daily_pnl)}  {_signed_pct(daily_rate)}", _color(daily_pnl)),
            ("累计盈亏（开户以来）", _signed_money(account.inception_pnl), _color(account.inception_pnl)),
        ]
        if account.pre_ledger_pnl:
            rows += [
                (f"其中建账前（至{_date(account.pre_ledger_through)[5:]}）", _signed_money(account.pre_ledger_pnl),
                 _color(account.pre_ledger_pnl)),
                ("其中建账后", _signed_money(account.cumulative_pnl), _color(account.cumulative_pnl)),
            ]
        rows.append((f"收益率（{ledger_start[5:]}起）", _signed_pct(cumulative_rate) or "-", _color(cumulative_rate)))
        row_html = "".join(
            f'<div class="home-card-row"><span>{html.escape(label)}</span>'
            f'<span style="color:{color}">{html.escape(value)}</span></div>'
            for label, value, color in rows
        )
        cards.append(
            '<div class="home-card">'
            '<div class="home-card-title">'
            f'<span class="home-card-name">{html.escape(account.label)}</span>'
            f'<span class="home-card-meta">估值 {_date(account.valuation_date)}</span></div>'
            f'<div class="home-card-meta">{html.escape(asset_label)} · {ledger_start} 建账</div>'
            f'<div class="home-card-assets">{_money(account.total_assets)}</div>'
            f"{row_html}</div>"
        )
    st.markdown('<div class="home-grid accounts">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def _render_nav_chart(combined: pd.DataFrame) -> None:
    st.subheader("总净值与每日盈亏")
    period = st.segmented_control(
        "时间范围",
        ["近1月", "近3月", "今年以来", "近1年", "全部"],
        default="全部",
        key="home_total_period",
        label_visibility="collapsed",
    )
    view = filter_by_time_range(combined, date_column="date", period=period or "全部")
    chart_dates = pd.to_datetime(view["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    colors = [UP_COLOR if float(value) >= 0 else DOWN_COLOR for value in view["pnl_amount"]]
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=chart_dates,
            y=view["nav"],
            mode="lines+markers",
            name="合计净值",
            line={"color": PRIMARY_COLOR, "width": 2.4},
            marker={"size": 5},
            customdata=view[["return_pct", "cumulative_return_pct", "cumulative_pnl"]],
            hovertemplate=(
                "净值：%{y:.4f}<br>当日收益率：%{customdata[0]:.2f}%"
                "<br>累计收益率：%{customdata[1]:.2f}%"
                "<br>总盈亏：%{customdata[2]:,.2f} 元<extra></extra>"
            ),
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Bar(
            x=chart_dates,
            y=view["pnl_amount"],
            name="每日盈亏",
            width=adaptive_bar_width(len(chart_dates)),
            marker={"color": colors},
            opacity=0.75,
            customdata=view[["return_base", "return_pct", "nav"]],
            hovertemplate=(
                "当日盈亏：%{y:,.2f} 元<br>当日收益率：%{customdata[1]:.2f}%"
                "<br>收益率分母：%{customdata[0]:,.2f} 元<br>净值：%{customdata[2]:.4f}<extra></extra>"
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
    figure.update_yaxes(title_text="合计净值", tickformat=".4f", secondary_y=False)
    figure.update_yaxes(title_text="每日盈亏（元）", secondary_y=True)
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})


def render_home_overview() -> None:
    """首页唯一内容：三个实盘账本的合计与分账户总览。"""
    if st.button("重新计算", help="账本或正式数据更新后立即重算；平时结果缓存5分钟"):
        _cached_overview.clear()
    overview = _cached_overview()
    accounts: list[AccountSeries] = overview["accounts"]
    combined: pd.DataFrame = overview["combined"]
    start = overview.get("start")

    for key, message in overview["errors"].items():
        st.warning(f"{ACCOUNT_LABELS.get(key, '建账前盈亏')}读取失败：{message}")
    for account in accounts:
        for message in account.warnings:
            st.warning(f"{account.label}：{message}")
    if not accounts:
        st.info("暂无可汇总的实盘账本。请先在 ETF实盘、微盘实盘或期货实盘录入数据。")
        return

    valuation_dates = {account.label: _date(account.valuation_date) for account in accounts}
    if len(set(valuation_dates.values())) > 1:
        st.warning(
            "各账户正式估值日期不一致："
            + "、".join(f"{label} {day}" for label, day in valuation_dates.items())
            + "。合计曲线截止到最早的估值日。"
        )
    _render_total_cards(accounts, combined, start, float(overview.get("inception_pnl") or 0.0))
    _render_account_cards(accounts)
    st.caption(
        "只读本地账本和正式收盘/结算缓存，不联网、不使用盘中报价。期货按券商客户权益口径：盯市结算价并扣申报费等账户级费用。"
        "总盈亏为开户以来累计，ETF实盘含建账前股票账户盈亏（开户至建账日收盘的总资产减累计银证净转入）；"
        f"净值曲线、收益日历和累计收益率从ETF实盘建账日（{_date(start)}）收盘起算，微盘实盘资金由ETF实盘划出。"
        "账户卡片的收益率从各自建账日起按日复合。"
    )
    if combined.empty:
        st.info("暂无完整的合计估值数据。")
        return
    _render_nav_chart(combined)
    render_return_calendar(
        combined[["date", "pnl_amount", "return_base", "return_pct", "confirmation_status"]],
        title="总收益日历",
        key_prefix="home_total_calendar",
        first_date=start,
        caption=(
            "每日收益金额为三个账户当日盈亏之和（已剔除资金转入转出）；"
            "收益率以各账户当日收益率分母之和为基数，周、月、年按每日收益率复合。"
        ),
    )


__all__ = ["render_home_overview"]
