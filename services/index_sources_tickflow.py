from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_config import (
    CFFEX_FUTURES_MAIN_PRODUCTS, INDEX_CONFIG, INDEX_REPORT_DISPLAY_DAYS,
    YAHOO_CHART_HOSTS, YAHOO_REQUEST_GATE,
)
from services.index_frames import (
    build_export_df, extract_raw_from_export_df, filter_market_trading_dates,
    is_sparse_daily_history, merge_newer_index_rows, merge_raw_index_data,
    normalize_akshare_index_df,
)
from services.market_calendar import (
    expected_latest_trade_date, get_market_window, is_market_holiday,
    is_market_trading_day, latest_completed_trade_date, latest_settled_trade_date,
)

def get_index_data_from_tickflow(api_key: str, index_code: str, index_name: str, days: int = 30):
    from tickflow import TickFlow

    client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    count = max(days * 2, 80)
    df = client.klines.get(index_code, period="1d", count=count, as_dataframe=True)
    if df is None or df.empty:
        return None
    if api_key:
        df = append_tickflow_quote_row(client, df, index_code)
    return build_export_df(df, index_name, days=days)

def tickflow_quote_date(symbol: str, timestamp) -> pd.Timestamp:
    quote_time = pd.to_numeric(timestamp, errors="coerce")
    if pd.isna(quote_time):
        if str(symbol).upper().endswith(".US"):
            return pd.Timestamp(datetime.now(ZoneInfo("America/New_York")).date())
        return pd.Timestamp(datetime.now().date())

    timestamp_value = float(quote_time)
    if timestamp_value > 10_000_000_000:
        timestamp_value = timestamp_value / 1000

    market_zone = ZoneInfo("America/New_York") if str(symbol).upper().endswith(".US") else ZoneInfo("Asia/Shanghai")
    quote_dt = datetime.fromtimestamp(timestamp_value, tz=timezone.utc).astimezone(market_zone)
    return pd.Timestamp(quote_dt.date())

def append_tickflow_quote_row(client, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = normalize_tickflow_index_df(df)
    try:
        quote_df = client.quotes.get_by_symbols([index_code], as_dataframe=True)
        if quote_df is None or quote_df.empty:
            return normalized
        quote_row = quote_df.iloc[0]
        latest_price = pd.to_numeric(quote_row.get("last_price"), errors="coerce")
        if pd.isna(latest_price):
            return normalized

        quote_timestamp = quote_row.get("timestamp")
        quote_date = tickflow_quote_date(index_code, quote_timestamp)
        market_name = "美股" if str(index_code).upper().endswith(".US") else "A股"
        quote_frame = pd.DataFrame([{"trade_date": quote_date, "close": float(latest_price)}])
        filtered_quote = filter_market_trading_dates(quote_frame, market_name)
        if filtered_quote is None or filtered_quote.empty:
            return normalized
        latest_history_date = normalized["trade_date"].max()
        if quote_date < latest_history_date:
            return normalized

        supplement = pd.DataFrame([{"trade_date": quote_date, "close": float(latest_price)}])
        return merge_raw_index_data(normalized, supplement)
    except Exception:
        return normalized

def normalize_tickflow_index_df(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    normalized.columns = [str(col).strip() for col in normalized.columns]
    if "trade_date" not in normalized.columns or "close" not in normalized.columns:
        raise ValueError(f"TickFlow返回列无法识别：{list(df.columns)}")
    result = normalized[["trade_date", "close"]].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result = result.dropna(subset=["trade_date", "close"])
    return result.sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)

def get_index_raw_from_tickflow(api_key: str, index_code: str, count: int = 80) -> pd.DataFrame | None:
    from tickflow import TickFlow

    client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    df = client.klines.get(index_code, period="1d", count=count, as_dataframe=True)
    if df is None or df.empty:
        return None
    return normalize_tickflow_index_df(df)

__all__ = ['get_index_data_from_tickflow', 'tickflow_quote_date', 'append_tickflow_quote_row', 'normalize_tickflow_index_df', 'get_index_raw_from_tickflow']
