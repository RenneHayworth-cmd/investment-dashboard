from __future__ import annotations

import html
from typing import Iterable

import plotly.graph_objects as go
import streamlit as st


DEFAULT_CHART_HEIGHT = 520
LARGE_CHART_HEIGHT = 900
SECONDARY_CHART_HEIGHT = 720


def apply_global_style() -> None:
    st.markdown(
        """
        <style>
        .main .block-container {
            padding-top: 1.75rem;
            padding-bottom: 2.5rem;
        }
        .dashboard-header {
            margin: 0 0 1.15rem;
        }
        .dashboard-eyebrow {
            color: rgba(31, 41, 55, 0.58);
            font-size: 0.92rem;
            font-weight: 650;
            letter-spacing: 0;
            margin-bottom: 0.2rem;
        }
        .dashboard-title {
            color: rgb(31, 41, 55);
            font-size: 2rem;
            font-weight: 760;
            line-height: 1.2;
            margin: 0;
        }
        .dashboard-caption {
            color: rgba(31, 41, 55, 0.62);
            font-size: 0.98rem;
            line-height: 1.55;
            margin-top: 0.45rem;
            max-width: 76rem;
        }
        .metric-card {
            border: 1px solid rgba(148, 163, 184, 0.34);
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.92);
            padding: 0.85rem 0.95rem;
            min-height: 84px;
            box-shadow: 0 8px 22px rgba(15, 23, 42, 0.045);
        }
        .metric-card-label {
            color: rgba(31, 41, 55, 0.64);
            font-size: 0.86rem;
            line-height: 1.25;
            margin-bottom: 0.42rem;
            overflow-wrap: anywhere;
        }
        .metric-card-value {
            color: rgb(31, 41, 55);
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
        div[data-testid="stDataFrame"] {
            border-radius: 8px;
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
        plot_bgcolor="rgba(255,255,255,0)",
    )
    fig.update_xaxes(hoverformat="%Y-%m-%d", gridcolor="rgba(148,163,184,0.18)")
    fig.update_yaxes(gridcolor="rgba(148,163,184,0.18)")
    return fig
