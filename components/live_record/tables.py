"""实盘持仓和逐标的历史盈亏表格。"""

import html

import pandas as pd
import streamlit as st

from components.live_record.formatting import format_live_number
from services.live_trading import summarize_live_position_performance


def render_live_positions_table(
    positions: pd.DataFrame,
    *,
    summarize_positions=summarize_live_position_performance,
) -> None:
    headers = [
        "标的名称",
        "代码",
        "市值",
        "现价",
        "行情状态",
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
            f"<div>{html.escape(format_live_number(amount_value))}</div>"
            f'<div class="live-pnl-rate">{html.escape(format_live_number(rate_value))}%</div>'
            "</td>"
        )

    total = summarize_positions(positions)
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
            (
                f"<td title=\"{html.escape(str(getattr(row, 'price_time', '') or ''))}\">"
                f"{html.escape(str(getattr(row, 'price_status', '-') or '-'))}</td>"
            ),
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
            min-width: 1220px;
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


__all__ = ["render_live_positions_table", "render_live_symbol_history_table"]
