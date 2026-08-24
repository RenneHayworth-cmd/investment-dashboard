"""持仓页面的格式化、查询参数与摘要数据变换。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import unquote

import pandas as pd
import streamlit as st

from services import position_analysis as position


def format_number(value: object, digits: int = 2, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}{suffix}"
    return str(value)


def format_etf_table_value(column: str, value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text == "-":
        return text
    try:
        if column in {"最新价", "对应均线", "触发收盘价"}:
            return format(
                Decimal(text).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP),
                ".3f",
            )
        if column in {"当日涨跌幅(%)", "偏离率(%)", "区间涨幅(%)", "上一区间涨幅(%)"}:
            return format(
                Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                ".2f",
            )
    except (InvalidOperation, ValueError):
        return text
    return text


def display_digits(item: position.PositionItem, metric_name: str) -> int:
    if item.category == "ETF" and metric_name == "最新价":
        return 3
    if item.category in {"期货", "期货价差", "期权"}:
        if metric_name in {"最新成交量", "最新持仓量"}:
            return 0
        return 1
    return 2


def format_metric_for_item(
    item: position.PositionItem,
    metric_name: str,
) -> str:
    value = item.metrics.get(metric_name)
    return format_number(value, digits=display_digits(item, metric_name))


def position_key(item: position.PositionItem) -> str:
    return f"{item.category}::{item.code}"


def display_position_code(item: position.PositionItem) -> str:
    return (
        position.normalize_etf_base_code(item.code)
        if item.category == "ETF"
        else item.code
    )


def get_query_position_detail(
    items: list[position.PositionItem],
) -> str | None:
    keys = {position_key(item) for item in items}
    value = st.query_params.get("position_detail")
    if isinstance(value, list):
        value = value[0] if value else None
    if not value:
        return None
    detail_key = unquote(str(value))
    return detail_key if detail_key in keys else None


def clear_position_detail() -> None:
    st.query_params.clear()


def build_overview_table(items: list[position.PositionItem]) -> pd.DataFrame:
    metric_order = [
        "最新价",
        "最新收盘",
        "最新价差",
        "日涨跌(%)",
        "价差日变化",
        "最新占比(%)",
        "20日涨跌(%)",
        "60日涨跌(%)",
        "20日波动(%)",
        "MA20偏离(%)",
        "价格百分位",
        "最新成交量",
        "最新持仓量",
    ]
    rows = []
    for item in items:
        for key in item.metrics:
            if key not in metric_order:
                metric_order.append(key)
        row = {
            "类别": item.category,
            "代码": display_position_code(item),
            "名称": item.name,
            "状态": item.status,
            "最新日期": item.latest_date or "-",
            "来源": item.source or "-",
            "缓存时间": item.cache_time or "-",
            "备注": item.error or "",
        }
        for key in metric_order:
            row[key] = format_metric_for_item(item, key)
        rows.append(row)
    return pd.DataFrame(rows)


def filter_range(
    df: pd.DataFrame,
    date_col: str,
    range_label: str,
) -> pd.DataFrame:
    if df.empty or date_col not in df.columns:
        return df
    dates = pd.to_datetime(df[date_col], errors="coerce")
    latest_date = dates.max()
    if pd.isna(latest_date):
        return df
    if range_label == "今年来":
        start_date = pd.Timestamp(year=latest_date.year, month=1, day=1)
    elif range_label == "近3年":
        start_date = latest_date - pd.DateOffset(years=3)
    elif range_label == "近5年":
        start_date = latest_date - pd.DateOffset(years=5)
    elif range_label == "成立来":
        return df
    else:
        start_date = latest_date - pd.DateOffset(years=1)
    return df[dates >= start_date].copy()


def round_numeric_columns(
    df: pd.DataFrame,
    digits: int = 1,
    integer_columns: tuple[str, ...] = (),
) -> pd.DataFrame:
    result = df.copy()
    for column in result.select_dtypes(include="number").columns:
        if column in integer_columns:
            result[column] = result[column].round(0)
        else:
            result[column] = result[column].round(digits)
    return result


def rolling_annual_label(df: pd.DataFrame) -> str:
    if len(df) >= 252 * 3:
        return "三年滚动年化收益率(%)"
    if len(df) >= 252:
        return "一年滚动年化收益率(%)"
    return "滚动年化收益率(%)"


def format_pct(value: object, digits: int = 2) -> str:
    return format_number(value, digits=digits, suffix="%")
