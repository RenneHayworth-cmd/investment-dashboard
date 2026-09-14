"""实盘持仓和逐标的历史盈亏表格。"""

import html

import pandas as pd
import streamlit as st

from components.live_record.formatting import format_live_number
from components.position_table import (
    position_number_cell,
    position_pnl_cell,
    position_quantity_cell,
    position_text_cell,
    render_position_table,
)
from services.live_trading import summarize_live_position_performance


def _display_price_status(status: object, price_time: object) -> str:
    """Keep the status compact while exposing the timestamp in the table."""
    status_text = "-" if status is None or pd.isna(status) else str(status).strip()
    if status_text != "实时":
        return status_text or "-"
    timestamp = pd.to_datetime(price_time, errors="coerce")
    if pd.isna(timestamp):
        return status_text
    return f"实时 {timestamp:%H:%M}"


def _cost_price_cell(cost: object, price: object) -> str:
    cost_text = format_live_number(cost, digits=3)
    price_text = format_live_number(price, digits=3)
    return (
        '<td><div>'
        f"{html.escape(cost_text)}"
        "</div><div>"
        f"{html.escape(price_text)}"
        "</div></td>"
    )


def render_live_positions_table(
    positions: pd.DataFrame,
    *,
    total_assets: float | None = None,
    summarize_positions=summarize_live_position_performance,
) -> None:
    headers = [
        "标的名称",
        "代码",
        "市值",
        "成本/现价",
        "持仓数量",
        "当日盈亏",
        "累计盈亏",
        "仓位",
        "已实现盈亏",
        "累计手续费",
        "行情状态",
    ]

    total = summarize_positions(positions)
    total_market_value = pd.to_numeric(total["market_value"], errors="coerce")
    base_assets = (
        float(total_assets)
        if total_assets is not None and not pd.isna(total_assets) and float(total_assets) > 0
        else (float(total_market_value) if not pd.isna(total_market_value) and float(total_market_value) > 0 else 0.0)
    )

    rows: list[list[str]] = []
    for row in positions.itertuples(index=False):
        market_value = pd.to_numeric(row.market_value, errors="coerce")
        weight_pct = (
            float(market_value) / base_assets * 100
            if not pd.isna(market_value) and base_assets > 0
            else pd.NA
        )
        rows.append(
            [
                position_text_cell(row.name),
                position_text_cell(row.symbol),
                position_number_cell(row.market_value),
                _cost_price_cell(row.average_cost, row.latest_price),
                position_quantity_cell(row.quantity),
                position_pnl_cell(row.daily_pnl, row.daily_return_pct),
                position_pnl_cell(row.cumulative_pnl, row.cumulative_return_pct),
                position_number_cell(weight_pct, suffix="%"),
                position_number_cell(row.realized_pnl),
                position_number_cell(row.fee_amount),
                position_text_cell(
                    _display_price_status(
                        getattr(row, "price_status", "-"),
                        getattr(row, "price_time", ""),
                    ),
                    title=getattr(row, "price_time", "") or "",
                ),
            ]
        )

    total_weight_pct = (
        float(total_market_value) / base_assets * 100
        if not pd.isna(total_market_value) and base_assets > 0
        else pd.NA
    )

    total_cells = [
        position_text_cell("合计", class_name="position-total-label"),
        position_text_cell("-"),
        position_number_cell(total["market_value"]),
        position_text_cell("-"),
        position_text_cell("-"),
        position_pnl_cell(total["daily_pnl"], total["daily_return_pct"]),
        position_pnl_cell(total["cumulative_pnl"], total["cumulative_return_pct"]),
        position_number_cell(total_weight_pct, suffix="%"),
        position_number_cell(total["realized_pnl"]),
        position_number_cell(total["fee_amount"]),
        position_text_cell("-"),
    ]
    render_position_table(headers, rows, total_cells=total_cells, min_width=1220)


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
