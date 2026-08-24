from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.cache import load_dataset
from services.fund_analysis import FUND_ADJUSTMENT_OPTIONS, FUND_ADJUST_NONE


FULL_HISTORY_COUNT = 10000
FULL_HISTORY_CACHE_PERIOD = "full_1d"
LEGACY_CACHE_PERIODS = ("10000_1d", "5000_1d")
BACKTEST_ADJUSTMENT_OPTIONS = {
    label: mode
    for label, mode in FUND_ADJUSTMENT_OPTIONS.items()
    if mode != FUND_ADJUST_NONE
}


def format_value(value, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def format_cache_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def default_backtest_dates() -> tuple[object, object]:
    end_date = pd.Timestamp.today().normalize()
    start_date = end_date - pd.DateOffset(years=5)
    return start_date.date(), end_date.date()


def load_rotation_cache(cache_symbol: str):
    for period in (FULL_HISTORY_CACHE_PERIOD, *LEGACY_CACHE_PERIODS):
        cached_df, cache_meta = load_dataset(
            cache_symbol,
            "tickflow_fund_rotation",
            "fund_rotation_raw",
            period=period,
        )
        if cached_df is not None:
            return cached_df, cache_meta, period
    return None, None, FULL_HISTORY_CACHE_PERIOD


def render_drawdown_chart(drawdown_df: pd.DataFrame) -> None:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=drawdown_df["日期"],
            y=drawdown_df["回撤(%)"],
            mode="lines",
            name="回撤",
            fill="tozeroy",
            hovertemplate="%{x|%Y-%m-%d}<br>回撤=%{y:.2f}%<extra></extra>",
            line=dict(width=1.8, color="#2ca02c"),
        )
    )
    fig.update_layout(
        height=360,
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        xaxis_title="日期",
        yaxis_title="回撤(%)",
    )
    st.plotly_chart(fig, use_container_width=True)


__all__ = [
    "BACKTEST_ADJUSTMENT_OPTIONS",
    "FULL_HISTORY_CACHE_PERIOD",
    "FULL_HISTORY_COUNT",
    "LEGACY_CACHE_PERIODS",
    "default_backtest_dates",
    "format_cache_time",
    "format_value",
    "load_rotation_cache",
    "render_drawdown_chart",
    "to_csv_bytes",
]
