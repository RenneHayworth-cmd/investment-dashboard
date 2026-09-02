from __future__ import annotations

import html
from typing import Any, Iterable, Sequence
from datetime import datetime
import pandas as pd

import plotly.graph_objects as go
import streamlit as st


DEFAULT_CHART_HEIGHT = 520
LARGE_CHART_HEIGHT = 900
SECONDARY_CHART_HEIGHT = 720


def apply_global_style() -> None:
    st.markdown(
        """
        <style>
        :root {
            --ui-bg: #f5f5f4;
            --ui-surface: #fafaf9;
            --ui-input: #fcfcfb;
            --ui-border: #e7e5e4;
            --ui-border-muted: #e5e7eb;
            --ui-text: #1f2937;
            --ui-muted: #6b7280;
            --ui-primary: #ef4444;
            --ui-focus: rgba(239, 68, 68, 0.12);
            --ui-shadow: 0 4px 14px rgba(15, 23, 42, 0.026);
        }
        .stApp {
            background: var(--ui-bg);
            color: var(--ui-text);
        }
        .main .block-container {
            padding-top: 1.75rem;
            padding-bottom: 2.5rem;
        }
        section[data-testid="stSidebar"] {
            background: #f3f2f1;
            border-right: 1px solid var(--ui-border);
        }
        section[data-testid="stSidebar"] > div {
            background: #f3f2f1;
        }
        .dashboard-header {
            margin: 0 0 1.15rem;
        }
        .dashboard-eyebrow {
            color: var(--ui-muted);
            font-size: 0.92rem;
            font-weight: 650;
            letter-spacing: 0;
            margin-bottom: 0.2rem;
        }
        .dashboard-title {
            color: var(--ui-text);
            font-size: 2rem;
            font-weight: 760;
            line-height: 1.2;
            margin: 0;
        }
        .dashboard-caption {
            color: var(--ui-muted);
            font-size: 0.98rem;
            line-height: 1.55;
            margin-top: 0.45rem;
            max-width: 76rem;
        }
        .metric-card {
            border: 1px solid var(--ui-border);
            border-radius: 8px;
            background: var(--ui-surface);
            padding: 0.85rem 0.95rem;
            min-height: 84px;
            box-shadow: var(--ui-shadow);
        }
        .metric-card-label {
            color: var(--ui-muted);
            font-size: 0.86rem;
            line-height: 1.25;
            margin-bottom: 0.42rem;
            overflow-wrap: anywhere;
        }
        .metric-card-value {
            color: var(--ui-text);
            font-size: 1.28rem;
            line-height: 1.2;
            font-weight: 680;
            overflow-wrap: anywhere;
        }
        .drawdown-metric-grid {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 1.15rem;
            margin: 0.75rem 0 1.25rem;
        }
        @media (max-width: 760px) {
            .drawdown-metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 480px) {
            .drawdown-metric-grid { grid-template-columns: 1fr; }
        }
        div[data-testid="stTextInput"] input,
        div[data-testid="stNumberInput"] input,
        div[data-testid="stTextArea"] textarea,
        div[data-baseweb="select"] > div,
        div[data-baseweb="input"] input {
            background: var(--ui-input) !important;
            border-color: var(--ui-border) !important;
            color: var(--ui-text) !important;
            box-shadow: none !important;
        }
        div[data-testid="stTextInput"] input:focus,
        div[data-testid="stNumberInput"] input:focus,
        div[data-testid="stTextArea"] textarea:focus,
        div[data-baseweb="select"] > div:focus-within,
        div[data-baseweb="input"] input:focus {
            border-color: rgba(239, 68, 68, 0.38) !important;
            box-shadow: 0 0 0 3px var(--ui-focus) !important;
        }
        div[data-baseweb="popover"],
        div[data-baseweb="menu"],
        ul[role="listbox"] {
            background: var(--ui-input) !important;
            border-color: var(--ui-border) !important;
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.08) !important;
        }
        li[role="option"] {
            background: var(--ui-input) !important;
            color: var(--ui-text) !important;
        }
        li[role="option"]:hover {
            background: #f4f4f2 !important;
        }
        div[data-testid="stExpander"] {
            background: var(--ui-surface);
            border: 1px solid var(--ui-border);
            border-radius: 8px;
            box-shadow: none;
        }
        div[data-testid="stTabs"] button {
            color: var(--ui-muted);
        }
        div[data-testid="stTabs"] button[aria-selected="true"] {
            color: var(--ui-primary);
        }
        div[data-testid="stDataFrame"] {
            border-radius: 8px;
            border: 1px solid var(--ui-border);
            background: var(--ui-surface);
            box-shadow: none;
            overflow: hidden;
        }
        div[data-testid="stPlotlyChart"] {
            border: 1px solid var(--ui-border);
            border-radius: 8px;
            background: var(--ui-surface);
            padding: 0.35rem;
            box-shadow: none;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_page_header(title: str, caption: str | None = None, eyebrow: str | None = None) -> None:
    eyebrow_html = f'<div class="dashboard-eyebrow">{html.escape(eyebrow)}</div>' if eyebrow else ""
    caption_html = f'<div class="dashboard-caption">{html.escape(caption)}</div>' if caption else ""
    st.markdown(
        (
            '<div class="dashboard-header">'
            f"{eyebrow_html}"
            f'<h1 class="dashboard-title">{html.escape(title)}</h1>'
            f"{caption_html}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_metric_card(label: str, value: object, tooltip: str | None = None) -> None:
    safe_tooltip = html.escape(tooltip or label)
    st.markdown(
        (
            f'<div class="metric-card" title="{safe_tooltip}">'
            f'<div class="metric-card-label">{html.escape(str(label))}</div>'
            f'<div class="metric-card-value">{html.escape(str(value))}</div>'
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_metric_grid(items: Iterable[tuple[str, str, str]]) -> None:
    cards = []
    for label, value, tooltip in items:
        cards.append(
            (
                f'<div class="metric-card" title="{html.escape(tooltip)}">'
                f'<div class="metric-card-label">{html.escape(label)}</div>'
                f'<div class="metric-card-value">{html.escape(value)}</div>'
                "</div>"
            )
        )
    st.markdown(f'<div class="drawdown-metric-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def apply_plotly_layout(
    fig: go.Figure,
    *,
    height: int = DEFAULT_CHART_HEIGHT,
    hovermode: str = "x unified",
    showlegend: bool = True,
) -> go.Figure:
    fig.update_layout(
        height=height,
        hovermode=hovermode,
        showlegend=showlegend,
        legend={"orientation": "h", "y": 1.02},
        margin={"l": 10, "r": 10, "t": 36, "b": 16},
        font={"family": "sans serif", "size": 12, "color": "#1f2937"},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#fafaf9",
    )
    fig.update_xaxes(hoverformat="%Y-%m-%d", gridcolor="rgba(120,113,108,0.14)")
    fig.update_yaxes(gridcolor="rgba(120,113,108,0.14)", hoverformat=".2f")
    return fig


def build_sparse_trading_date_ticks(
    dates: Sequence[Any],
    max_ticks: int = 7,
) -> tuple[list[Any], list[str]]:
    """Build sparse, clean tick values and labels for discrete trading-day axes."""
    if not dates:
        return [], []
    
    n = len(dates)
    if n <= max_ticks:
        indices = list(range(n))
    else:
        step = (n - 1) / (max_ticks - 1)
        indices = sorted(list({round(i * step) for i in range(max_ticks)}))
        if indices[-1] != n - 1:
            indices.append(n - 1)
        if indices[0] != 0:
            indices.insert(0, 0)
        indices = sorted(list(dict.fromkeys(indices)))

    selected_vals = [dates[i] for i in indices]
    parsed_dates = [pd.to_datetime(d, errors="coerce") for d in selected_vals]
    valid_years = {ts.year for ts in parsed_dates if pd.notna(ts)}
    multi_year = len(valid_years) > 1

    labels: list[str] = []
    last_year = None
    for i, ts in enumerate(parsed_dates):
        if pd.isna(ts):
            labels.append(str(selected_vals[i]))
            continue
        if multi_year and (last_year is None or ts.year != last_year or i == 0):
            labels.append(ts.strftime("%Y-%m-%d"))
        else:
            labels.append(ts.strftime("%m-%d"))
        last_year = ts.year

    return selected_vals, labels


def filter_by_time_range(
    df: pd.DataFrame,
    date_column: str = "date",
    period: str = "全部",
    market_now: datetime | None = None,
) -> pd.DataFrame:
    """Filter a dataframe by common time range capsules: 近1月, 近3月, 近半年, 近1年, 今年以来, 全部."""
    if df is None or df.empty or date_column not in df.columns or period == "全部":
        return df

    dates = pd.to_datetime(df[date_column], errors="coerce")
    valid_dates = dates.dropna()
    if valid_dates.empty:
        return df

    max_date = valid_dates.max()
    if period == "近1月":
        cutoff = max_date - pd.Timedelta(days=31)
    elif period == "近3月":
        cutoff = max_date - pd.Timedelta(days=92)
    elif period == "近半年":
        cutoff = max_date - pd.Timedelta(days=183)
    elif period == "近1年":
        cutoff = max_date - pd.Timedelta(days=366)
    elif period == "今年以来":
        cutoff = pd.Timestamp(year=max_date.year, month=1, day=1)
    else:
        return df

    filtered = df[dates >= cutoff]
    return filtered if not filtered.empty else df

