"""持仓类表格共用的居中HTML样式与单元格格式。"""

from __future__ import annotations

import html

import pandas as pd
import streamlit as st


def format_position_number(value: object, digits: int = 2) -> str:
    number = pd.to_numeric(value, errors="coerce")
    return "-" if pd.isna(number) else f"{float(number):,.{digits}f}"


def position_text_cell(
    value: object,
    *,
    class_name: str = "",
    title: object = "",
) -> str:
    text = "-" if value is None or pd.isna(value) else str(value)
    class_attr = f' class="{html.escape(class_name)}"' if class_name else ""
    title_text = "" if title is None or pd.isna(title) else str(title)
    title_attr = f' title="{html.escape(title_text)}"' if title_text else ""
    return f"<td{class_attr}{title_attr}>{html.escape(text)}</td>"


def position_number_cell(
    value: object,
    *,
    digits: int = 2,
    suffix: str = "",
) -> str:
    text = format_position_number(value, digits)
    if text != "-":
        text += suffix
    return position_text_cell(text)


def position_quantity_cell(value: object) -> str:
    number = pd.to_numeric(value, errors="coerce")
    return position_text_cell("-" if pd.isna(number) else f"{int(number):,}")


def position_pnl_cell(amount: object, rate: object = None) -> str:
    amount_value = pd.to_numeric(amount, errors="coerce")
    rate_value = pd.to_numeric(rate, errors="coerce")
    if pd.isna(amount_value):
        return '<td class="position-pnl-cell">-</td>'
    value_class = (
        "position-pnl-positive"
        if float(amount_value) > 0
        else "position-pnl-negative"
        if float(amount_value) < 0
        else ""
    )
    rate_html = (
        ""
        if pd.isna(rate_value)
        else (
            '<div class="position-pnl-rate">'
            f"{html.escape(format_position_number(rate_value))}%</div>"
        )
    )
    return (
        f'<td class="position-pnl-cell {value_class}">'
        f"<div>{html.escape(format_position_number(amount_value))}</div>"
        f"{rate_html}</td>"
    )


def render_position_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    total_cells: list[str] | None = None,
    min_width: int = 1220,
) -> None:
    body_rows = [f"<tr>{''.join(cells)}</tr>" for cells in rows]
    if total_cells is not None:
        body_rows.append(
            f'<tr class="position-table-total">{"".join(total_cells)}</tr>'
        )
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    st.markdown(
        f"""
        <style>
        .position-table-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .position-data-table {{
            width: 100%;
            min-width: {int(min_width)}px;
            border-collapse: collapse;
            font-size: 0.9rem;
        }}
        .position-data-table th,
        .position-data-table td {{
            padding: 0.5rem 0.55rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            text-align: center;
            white-space: nowrap;
        }}
        .position-data-table th {{
            background: rgba(49, 51, 63, 0.04);
            font-weight: 600;
        }}
        .position-data-table .position-table-total td {{
            border-top: 2px solid rgba(49, 51, 63, 0.22);
            background: rgba(49, 51, 63, 0.035);
            font-weight: 600;
        }}
        .position-data-table .position-total-label {{
            font-weight: 700;
        }}
        .position-data-table .position-pnl-cell {{
            line-height: 1.25;
            font-variant-numeric: tabular-nums;
        }}
        .position-data-table .position-pnl-rate {{
            margin-top: 0.16rem;
            font-size: 0.82rem;
            opacity: 0.82;
        }}
        .position-data-table .position-pnl-positive {{
            color: rgb(190, 18, 60);
            font-weight: 600;
        }}
        .position-data-table .position-pnl-negative {{
            color: rgb(22, 101, 52);
            font-weight: 600;
        }}
        </style>
        <div class="position-table-scroll">
            <table class="position-data-table">
                <thead><tr>{header_html}</tr></thead>
                <tbody>{''.join(body_rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


__all__ = [
    "format_position_number",
    "position_number_cell",
    "position_pnl_cell",
    "position_quantity_cell",
    "position_text_cell",
    "render_position_table",
]
