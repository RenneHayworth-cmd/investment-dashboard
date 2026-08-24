"""持仓卡片、ETF择时表格和通用摘要表组件。"""

from __future__ import annotations

import html
from urllib.parse import quote
from collections.abc import Callable

import pandas as pd
import streamlit as st

from components.position.formatting import (
    display_position_code,
    format_number,
    position_key,
)
from services import position_analysis as position


TableValueFormatter = Callable[[str, object], str]


def render_etf_timing_table(
    df: pd.DataFrame,
    *,
    value_formatter: TableValueFormatter,
) -> None:
    if df is None or df.empty:
        st.info("当前没有可展示的 ETF 汇总数据。")
        return
    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in df.columns)
    timing_column_index = df.columns.get_loc("择时判断") + 1
    rows = []
    for _, row in df.iterrows():
        timing_action = str(row.get("择时判断", ""))
        row_class = (
            " timing-buy"
            if timing_action in {"买入", "加至满仓"}
            else " timing-sell"
            if timing_action in {"卖出", "降至半仓"}
            else ""
        )
        cells = "".join(
            f"<td>{html.escape(value_formatter(column, row[column]))}</td>"
            for column in df.columns
        )
        rows.append(f'<tr class="{row_class.strip()}">{cells}</tr>')
    st.markdown(
        f"""
        <style>
        .position-etf-summary-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
        }}
        .position-etf-summary-table-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .position-etf-summary-table th,
        .position-etf-summary-table td {{
            text-align: center;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            white-space: nowrap;
        }}
        .position-etf-summary-table th {{
            font-weight: 600;
            background: rgba(49, 51, 63, 0.04);
        }}
        .position-etf-summary-table tr.timing-buy td {{
            background: rgba(254, 226, 226, 0.72);
        }}
        .position-etf-summary-table tr.timing-sell td {{
            background: rgba(220, 252, 231, 0.78);
        }}
        .position-etf-summary-table tr.timing-buy td:first-child,
        .position-etf-summary-table tr.timing-buy td:nth-child({timing_column_index}) {{
            color: rgb(190, 18, 60);
            font-weight: 700;
        }}
        .position-etf-summary-table tr.timing-sell td:first-child,
        .position-etf-summary-table tr.timing-sell td:nth-child({timing_column_index}) {{
            color: rgb(22, 101, 52);
            font-weight: 700;
        }}
        </style>
        <div class="position-etf-summary-table-scroll">
        <table class="position-etf-summary-table">
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_etf_operation_guidance(
    df: pd.DataFrame,
    *,
    value_formatter: TableValueFormatter,
) -> None:
    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in df.columns)
    rows = []
    for _, row in df.iterrows():
        action = str(row.get("操作指引", ""))
        row_class = (
            "timing-buy"
            if action in {"买入", "加至满仓"}
            else "timing-sell"
            if action in {"卖出", "降至半仓"}
            else ""
        )
        cells = "".join(
            f"<td>{html.escape(value_formatter(column, row[column]))}</td>"
            for column in df.columns
        )
        rows.append(f'<tr class="{row_class}">{cells}</tr>')
    st.markdown(
        f"""
        <style>
        .position-operation-guidance-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
        }}
        .position-operation-guidance-table-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .position-operation-guidance-table th,
        .position-operation-guidance-table td {{
            text-align: center;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            white-space: nowrap;
        }}
        .position-operation-guidance-table th {{
            font-weight: 600;
            background: rgba(49, 51, 63, 0.04);
        }}
        .position-operation-guidance-table tr.timing-buy td {{
            background: rgba(254, 226, 226, 0.72);
        }}
        .position-operation-guidance-table tr.timing-sell td {{
            background: rgba(220, 252, 231, 0.78);
        }}
        .position-operation-guidance-table tr.timing-buy td:nth-child(5) {{
            color: rgb(190, 18, 60);
            font-weight: 700;
        }}
        .position-operation-guidance-table tr.timing-sell td:nth-child(5) {{
            color: rgb(22, 101, 52);
            font-weight: 700;
        }}
        </style>
        <div class="position-operation-guidance-table-scroll">
        <table class="position-operation-guidance-table">
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def primary_value(item: position.PositionItem) -> tuple[str, object, int]:
    if item.category == "ETF":
        return "最新价", item.metrics.get("最新价"), 3
    if item.category == "期货价差":
        return "最新价差", item.metrics.get("最新价差"), 1
    return "最新收盘", item.metrics.get("最新收盘"), 1


def delta_value(item: position.PositionItem) -> tuple[str, object, str]:
    if item.category == "期货价差":
        return "日变化", item.metrics.get("价差日变化"), ""
    return "日涨跌", item.metrics.get("日涨跌(%)"), "%"


def render_position_cards(items: list[position.PositionItem]) -> None:
    for start in range(0, len(items), 4):
        columns = st.columns(4)
        for col, item in zip(columns, items[start : start + 4]):
            with col:
                render_position_card(item)


def render_position_card(item: position.PositionItem) -> None:
    _, value, digits = primary_value(item)
    delta_label, delta, delta_suffix = delta_value(item)
    is_available = item.status not in {"失败", "无缓存"} and not item.dataframe.empty
    detail_href = f"?position_detail={quote(position_key(item))}"

    if pd.isna(delta):
        delta_class = "neutral"
        arrow = "·"
        delta_text = "-"
    else:
        delta_class = "positive" if float(delta) >= 0 else "negative"
        arrow = "↑" if float(delta) >= 0 else "↓"
        delta_digits = 1 if item.category in {"期货", "期货价差", "期权"} else 2
        delta_text = f"{float(delta):+.{delta_digits}f}{delta_suffix}"

    if is_available:
        value_text = format_number(value, digits=digits)
        status_text = item.latest_date or item.status
    else:
        value_text = "暂无缓存" if item.status == "无缓存" else "获取失败"
        status_text = item.error or item.status

    title = item.name if item.name else item.code
    card_html = (
        "<style>"
        ".position-card-single{min-height:12.25rem;border:1px solid rgba(49,51,63,.14);border-radius:8px;background:rgba(255,255,255,.78);padding:1.25rem 1.35rem;box-shadow:0 10px 26px rgba(15,23,42,.06);margin:.35rem 0 .75rem;}"
        ".position-card-single:hover{border-color:rgba(37,99,235,.42);box-shadow:0 14px 30px rgba(15,23,42,.11);}"
        ".position-card-title{min-height:2.45rem;font-size:1.08rem;font-weight:700;line-height:1.25;overflow-wrap:anywhere;}"
        ".position-card-title a{color:rgba(49,51,63,.72);text-decoration:none;}"
        ".position-card-title a:hover{color:rgb(37,99,235);text-decoration:none;}"
        ".position-card-code{min-height:1.6rem;margin-top:.22rem;color:rgba(49,51,63,.58);font-size:.98rem;line-height:1.25;overflow-wrap:anywhere;}"
        ".position-card-value{margin-top:1rem;color:rgb(31,41,55);font-size:1.7rem;font-weight:650;line-height:1.08;font-variant-numeric:tabular-nums;white-space:normal;overflow-wrap:anywhere;}"
        ".position-card-foot{display:flex;align-items:center;justify-content:space-between;gap:.5rem;margin-top:1rem;}"
        ".position-card-delta{display:inline-flex;align-items:center;justify-content:center;gap:.28rem;border-radius:999px;padding:.32rem .75rem;font-size:.98rem;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;}"
        ".position-card-delta.positive{color:rgb(190,18,60);background:rgba(254,226,226,.9);}"
        ".position-card-delta.negative{color:rgb(22,101,52);background:rgba(220,252,231,.9);}"
        ".position-card-delta.neutral{color:rgb(75,85,99);background:rgba(243,244,246,.95);}"
        ".position-card-date{min-width:0;color:rgba(49,51,63,.58);font-size:.84rem;line-height:1.25;text-align:right;overflow-wrap:anywhere;}"
        "</style>"
        '<div class="position-card-single">'
        f'<div class="position-card-title"><a href="{detail_href}">{html.escape(title)}</a></div>'
        f'<div class="position-card-code">{html.escape(item.category)} · {html.escape(display_position_code(item))}</div>'
        f'<div class="position-card-value">{html.escape(value_text)}</div>'
        '<div class="position-card-foot">'
        f'<div class="position-card-delta {delta_class}" title="{html.escape(delta_label)}">'
        f"<span>{arrow}</span><span>{html.escape(delta_text)}</span>"
        "</div>"
        f'<div class="position-card-date">{html.escape(status_text)}</div>'
        "</div>"
        "</div>"
    )
    st.markdown(card_html, unsafe_allow_html=True)


def metric_row(items: list[tuple[str, object, str, int]]) -> None:
    columns = st.columns(len(items))
    for column, (label, value, suffix, digits) in zip(columns, items):
        column.metric(label, format_number(value, digits=digits, suffix=suffix))


def render_summary_table(rows: list[tuple[str, object]]) -> None:
    summary_df = pd.DataFrame(rows, columns=["指标", "数值"])
    summary_df["数值"] = summary_df["数值"].map(
        lambda value: "-" if value is None or pd.isna(value) else str(value)
    )
    st.dataframe(summary_df, use_container_width=True, hide_index=True)
