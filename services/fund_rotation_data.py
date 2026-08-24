from __future__ import annotations

import pandas as pd

from services.fund_rotation_models import (
    DATE_COLUMNS,
    OPEN_COLUMNS,
    PRICE_COLUMNS,
    STANDARD_BACKTEST_PERIODS,
    RotationInput,
)

def normalize_rotation_dataframe(df: pd.DataFrame, fallback_name: str) -> RotationInput:
    if df is None or df.empty:
        raise ValueError("文件中没有可回测的数据。")

    data = df.copy()
    data.columns = [str(col).strip().lstrip("\ufeff") for col in data.columns]
    date_col = _find_column(data.columns, DATE_COLUMNS)
    price_col = _find_column(data.columns, PRICE_COLUMNS)
    open_col = _find_column(data.columns, OPEN_COLUMNS)
    if not date_col or not price_col:
        raise ValueError(f"无法识别日期列或价格列。当前列名：{list(data.columns)}")

    symbol = _first_text(data, ("symbol", "代码", "基金代码")) or fallback_name
    name = _first_text(data, ("name", "基金名称", "名称", "简称")) or symbol

    selected_columns = [date_col]
    if open_col:
        selected_columns.append(open_col)
    selected_columns.append(price_col)
    normalized = data[selected_columns].copy()
    normalized.columns = ["trade_date", "open", "close"] if open_col else ["trade_date", "close"]
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
    if "open" not in normalized.columns:
        normalized["open"] = normalized["close"]
    normalized["open"] = pd.to_numeric(normalized["open"], errors="coerce")
    normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
    normalized = normalized.dropna(subset=["trade_date", "open", "close"])
    normalized = normalized.sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)
    if normalized.empty:
        raise ValueError("日期和价格列解析后没有有效数据。")

    return RotationInput(
        symbol=str(symbol),
        name=str(name),
        dataframe=normalized,
        apply_slippage=open_col is not None,
    )


def _normalize_date_range(
    start_date: str | pd.Timestamp | None,
    end_date: str | pd.Timestamp | None,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    start = pd.Timestamp(start_date).normalize() if start_date is not None else None
    end = pd.Timestamp(end_date).normalize() if end_date is not None else None
    if start is not None and pd.isna(start):
        raise ValueError("开始日期无效。")
    if end is not None and pd.isna(end):
        raise ValueError("结束日期无效。")
    if start is not None and end is not None and start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    return start, end


def build_standard_backtest_periods(end_date: str | pd.Timestamp) -> list[tuple[str, pd.Timestamp | None]]:
    end = pd.Timestamp(end_date).normalize()
    starts = {
        "近一年": end - pd.DateOffset(years=1),
        "今年来": pd.Timestamp(end.year, 1, 1),
        "近三年": end - pd.DateOffset(years=3),
        "近五年": end - pd.DateOffset(years=5),
        "成立来": None,
    }
    return [(label, starts[label]) for label in STANDARD_BACKTEST_PERIODS]

def _find_column(columns, keywords: tuple[str, ...]) -> str | None:
    normalized = [str(column).strip().lstrip("\ufeff") for column in columns]
    normalized_lower = {column.lower(): column for column in normalized}
    for keyword in keywords:
        exact_match = normalized_lower.get(keyword.lower())
        if exact_match is not None:
            return exact_match
    for keyword in keywords:
        keyword_lower = keyword.lower()
        for column in normalized:
            if keyword_lower in column.lower():
                return column
    return None


def _first_text(df: pd.DataFrame, columns: tuple[str, ...]) -> str | None:
    for column in columns:
        if column in df.columns and df[column].notna().any():
            value = str(df[column].dropna().iloc[0]).strip()
            if value:
                return value
    return None


def _prepare_merged_data(source_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    merged = None
    for symbol, df in source_data.items():
        current = df[["trade_date", "open", "close"]].copy()
        current.columns = ["trade_date", f"{symbol}__open", symbol]
        current[f"{symbol}__raw_close"] = current[symbol]
        merged = current if merged is None else pd.merge(merged, current, on="trade_date", how="outer")
    if merged is None or merged.empty:
        raise ValueError("没有可合并的基金数据。")
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    for symbol in source_data:
        merged[f"{symbol}__raw_close"] = pd.to_numeric(merged[f"{symbol}__raw_close"], errors="coerce")
        merged[symbol] = pd.to_numeric(merged[symbol], errors="coerce").ffill()
        merged[f"{symbol}__open"] = pd.to_numeric(merged[f"{symbol}__open"], errors="coerce")
    return merged

__all__ = [
    "normalize_rotation_dataframe",
    "build_standard_backtest_periods",
    "_normalize_date_range",
    "_find_column",
    "_first_text",
    "_prepare_merged_data",
]
