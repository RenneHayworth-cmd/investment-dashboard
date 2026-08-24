from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_config import (
    CFFEX_FUTURES_MAIN_PRODUCTS, INDEX_CONFIG, INDEX_FINAL_HISTORY_SOURCE,
    INDEX_LONG_HISTORY_BARS, INDEX_LONG_HISTORY_SOURCE,
    INDEX_RECENT_GAP_LOOKBACK_SESSIONS, INDEX_REPORT_DISPLAY_DAYS,
    INDEX_SOURCE_CORRECTION_SOURCE, YAHOO_CHART_HOSTS, YAHOO_REQUEST_GATE,
)
from services.market_calendar import (
    expected_latest_trade_date, get_market_window, is_market_holiday,
    is_market_trading_day, latest_completed_trade_date, latest_settled_trade_date,
)

from services.index_signals import calculate_ma20_transition_history

def overlay_finalized_index_rows(
    history_df: pd.DataFrame | None,
    finalized_df: pd.DataFrame | None,
) -> pd.DataFrame | None:
    """Overlay separately cached post-close rows without changing raw history."""
    if history_df is None or history_df.empty:
        return merge_raw_index_data(None, finalized_df) if finalized_df is not None and not finalized_df.empty else None
    normalized_history = merge_raw_index_data(None, history_df)
    if finalized_df is None or finalized_df.empty:
        return normalized_history
    normalized_finalized = merge_raw_index_data(None, finalized_df)
    combined = pd.concat([normalized_history, normalized_finalized], ignore_index=True)
    return combined.drop_duplicates("trade_date", keep="last").sort_values("trade_date").reset_index(drop=True)

def missing_recent_market_trade_dates(
    history_df: pd.DataFrame | None,
    market_name: str,
    target_date,
    *,
    lookback_sessions: int = INDEX_RECENT_GAP_LOOKBACK_SESSIONS,
) -> list:
    """Return missing sessions among the latest completed market trading days."""
    market = get_market_window(str(market_name or ""))
    if market is None or target_date is None:
        return []
    target = pd.Timestamp(target_date).date()
    observed: set = set()
    if history_df is not None and not history_df.empty and "trade_date" in history_df.columns:
        observed = set(
            pd.to_datetime(history_df["trade_date"], errors="coerce")
            .dropna()
            .dt.date
            .tolist()
        )
    expected = []
    current = target
    required_sessions = max(int(lookback_sessions), 1)
    while len(expected) < required_sessions:
        if current.weekday() < 5 and not is_market_holiday(market, current):
            expected.append(current)
        current -= timedelta(days=1)
    expected.reverse()
    return [day for day in expected if day not in observed]

def source_correction_start(index_config: dict | None) -> pd.Timestamp | None:
    if not isinstance(index_config, dict):
        return None
    start = pd.to_datetime(index_config.get("source_correction_start"), errors="coerce")
    return None if pd.isna(start) else pd.Timestamp(start).normalize()

def extract_source_correction_rows(
    df: pd.DataFrame | None,
    index_config: dict | None,
) -> pd.DataFrame | None:
    start = source_correction_start(index_config)
    if start is None or df is None or df.empty:
        return None
    normalized = merge_raw_index_data(None, df)
    corrected = normalized[normalized["trade_date"] >= start]
    return corrected.reset_index(drop=True) if not corrected.empty else None

def source_correction_fetch_days(
    index_config: dict | None,
    correction_df: pd.DataFrame | None,
    market_name: str,
    minimum_days: int,
) -> int:
    start = source_correction_start(index_config)
    target_date = _latest_completed_date_for_market(market_name)
    if start is None or target_date is None or target_date < start.date():
        return minimum_days

    latest_date = _latest_raw_date(correction_df)
    first_missing = start.date() if latest_date is None else latest_date + timedelta(days=1)
    return max(int(minimum_days), (target_date - first_missing).days + 14)

def filter_market_trading_dates(
    df: pd.DataFrame | None,
    market_name: str,
    date_column: str = "trade_date",
) -> pd.DataFrame | None:
    if df is None or df.empty or date_column not in df.columns:
        return df
    market = get_market_window(market_name)
    if market is None:
        return df

    result = df.copy()
    result[date_column] = pd.to_datetime(result[date_column], errors="coerce")
    valid_dates = result[date_column].dt.date.map(
        lambda day: not pd.isna(day) and day.weekday() < 5 and not is_market_holiday(market, day)
    )
    return result[result[date_column].notna() & valid_dates].reset_index(drop=True)

def filter_completed_market_dates(
    df: pd.DataFrame | None,
    market_name: str,
    date_column: str = "trade_date",
) -> pd.DataFrame | None:
    result = filter_market_trading_dates(df, market_name, date_column=date_column)
    if result is None or result.empty or date_column not in result.columns:
        return result
    market = get_market_window(market_name)
    if market is None:
        return result
    market_now = datetime.now(ZoneInfo(market.timezone))
    completed_date = latest_completed_trade_date(market, market_now)
    dates = pd.to_datetime(result[date_column], errors="coerce")
    return result[dates.dt.date <= completed_date].reset_index(drop=True)

def build_export_df(
    df: pd.DataFrame,
    index_name: str,
    days: int = INDEX_REPORT_DISPLAY_DAYS,
) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None

    result = df.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result = result.sort_values("trade_date").reset_index(drop=True)
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result = result.dropna(subset=["trade_date", "close"])

    if result.empty:
        return None

    result["MA20"] = result["close"].rolling(window=20).mean()
    result["偏离率"] = (result["close"] - result["MA20"]) / result["MA20"] * 100
    transition_history = calculate_ma20_transition_history(
        result,
        "close",
        "MA20",
        date_col="trade_date",
    )
    result["状态转变时间"] = transition_history["状态转变时间"]
    result["区间涨幅"] = transition_history["区间涨幅"]
    result["上一状态转换时间"] = transition_history["上一状态转换时间"]
    result["上一区间涨幅"] = transition_history["上一区间涨幅"]

    start_date = pd.Timestamp(datetime.now()).normalize() - pd.Timedelta(days=days)
    recent_data = result[result["trade_date"] >= start_date].copy()
    if recent_data.empty:
        return None

    export_df = recent_data[
        [
            "trade_date",
            "close",
            "MA20",
            "偏离率",
            "状态转变时间",
            "区间涨幅",
            "上一状态转换时间",
            "上一区间涨幅",
        ]
    ].copy()
    export_df["trade_date"] = export_df["trade_date"].dt.strftime("%Y-%m-%d")
    export_df["close"] = export_df["close"].round(2)
    export_df["MA20"] = export_df["MA20"].round(2)
    export_df["偏离率"] = export_df["偏离率"].round(2)
    export_df["区间涨幅"] = pd.to_numeric(export_df["区间涨幅"], errors="coerce").round(2)
    export_df["上一区间涨幅"] = pd.to_numeric(
        export_df["上一区间涨幅"], errors="coerce"
    ).round(2)
    export_df.columns = [
        "日期",
        f"{index_name}_收盘价",
        f"{index_name}_MA20",
        f"{index_name}_偏离率(%)",
        f"{index_name}_状态转变时间",
        f"{index_name}_区间涨幅(%)",
        f"{index_name}_上一状态转换时间",
        f"{index_name}_上一区间涨幅(%)",
    ]
    return export_df

def normalize_akshare_index_df(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for column in df.columns:
        column_text = str(column).strip()
        if column_text in {"日期", "date", "trade_date", "交易日期"}:
            rename_map[column] = "trade_date"
        elif column_text in {"收盘", "收盘价", "最新价", "close", "Close"}:
            rename_map[column] = "close"

    normalized = df.rename(columns=rename_map)
    if "trade_date" not in normalized.columns or "close" not in normalized.columns:
        raise ValueError(f"AkShare返回列无法识别：{list(df.columns)}")
    return normalized[["trade_date", "close"]].copy()

def extract_raw_from_export_df(export_df: pd.DataFrame, index_name: str) -> pd.DataFrame | None:
    if export_df is None or export_df.empty:
        return None
    close_col = f"{index_name}_收盘价"
    if "日期" not in export_df.columns or close_col not in export_df.columns:
        return None
    raw_df = export_df[["日期", close_col]].rename(columns={"日期": "trade_date", close_col: "close"}).copy()
    raw_df["trade_date"] = pd.to_datetime(raw_df["trade_date"], errors="coerce")
    raw_df["close"] = pd.to_numeric(raw_df["close"], errors="coerce")
    raw_df = raw_df.dropna(subset=["trade_date", "close"])
    if raw_df.empty:
        return None
    return raw_df

def merge_newer_index_rows(df: pd.DataFrame, newer_df: pd.DataFrame | None) -> pd.DataFrame:
    if newer_df is None or newer_df.empty:
        return df
    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
    newer = newer_df.copy()
    newer["trade_date"] = pd.to_datetime(newer["trade_date"], errors="coerce")
    latest_history_date = normalized["trade_date"].max()
    newer_rows = newer[newer["trade_date"] > latest_history_date]
    if newer_rows.empty:
        return normalized
    return pd.concat([normalized, newer_rows], ignore_index=True)

def is_sparse_daily_history(df: pd.DataFrame) -> bool:
    if df is None or df.empty or "trade_date" not in df.columns:
        return True
    dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna().sort_values()
    if len(dates) < 20:
        return True
    gaps = dates.diff().dt.days.dropna()
    if gaps.empty:
        return True
    return bool(gaps.median() > 7 or (gaps > 10).sum() > len(gaps) * 0.3)

def _append_unseen_raw_history(old_df: pd.DataFrame | None, new_df: pd.DataFrame | None) -> pd.DataFrame | None:
    if new_df is None or new_df.empty:
        return merge_raw_index_data(None, old_df) if old_df is not None and not old_df.empty else None
    normalized_new = merge_raw_index_data(None, new_df)
    if old_df is None or old_df.empty:
        return normalized_new

    normalized_old = merge_raw_index_data(None, old_df)
    unseen = normalized_new[~normalized_new["trade_date"].isin(normalized_old["trade_date"])]
    if unseen.empty:
        return normalized_old
    return pd.concat([normalized_old, unseen], ignore_index=True).sort_values("trade_date").reset_index(drop=True)

def _latest_completed_date_for_market(market_name: str):
    market = get_market_window(market_name)
    if market is None:
        return None
    market_now = datetime.now(ZoneInfo(market.timezone))
    return latest_completed_trade_date(market, market_now)

def _latest_raw_date(df: pd.DataFrame | None):
    if df is None or df.empty or "trade_date" not in df.columns:
        return None
    dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna()
    return dates.max().date() if not dates.empty else None

def merge_raw_index_data(old_df: pd.DataFrame | None, new_df: pd.DataFrame) -> pd.DataFrame:
    if old_df is None or old_df.empty:
        merged = new_df.copy()
    else:
        merged = pd.concat([old_df, new_df], ignore_index=True)
    merged["trade_date"] = pd.to_datetime(merged["trade_date"], errors="coerce")
    merged["close"] = pd.to_numeric(merged["close"], errors="coerce")
    merged = merged.dropna(subset=["trade_date", "close"])
    return merged.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)

def raw_cache_symbol(index_name: str, index_config) -> str:
    if isinstance(index_config, dict) and index_config.get("tickflow_symbol"):
        return f"index_raw_{index_config['tickflow_symbol']}"
    return f"index_raw_{index_name}"

def display_index_symbol(index_config) -> str:
    if not isinstance(index_config, dict):
        return str(index_config).split(".", 1)[0]
    symbol = index_config.get(
        "display_symbol",
        index_config.get("tickflow_symbol", index_config.get("code", "")),
    )
    symbol_text = str(symbol).strip()
    if symbol_text.startswith("."):
        return symbol_text.rsplit(".", 1)[0].lstrip(".")
    return symbol_text.split(".", 1)[0]

def merge_by_date(all_data: list[pd.DataFrame]) -> pd.DataFrame:
    merged_df = all_data[0]
    for df in all_data[1:]:
        merged_df = pd.merge(merged_df, df, on="日期", how="outer")
    return merged_df.sort_values("日期").reset_index(drop=True)

__all__ = ['overlay_finalized_index_rows', 'missing_recent_market_trade_dates', 'source_correction_start', 'extract_source_correction_rows', 'source_correction_fetch_days', 'filter_market_trading_dates', 'filter_completed_market_dates', 'build_export_df', 'normalize_akshare_index_df', 'extract_raw_from_export_df', 'merge_newer_index_rows', 'is_sparse_daily_history', '_append_unseen_raw_history', '_latest_completed_date_for_market', '_latest_raw_date', 'merge_raw_index_data', 'raw_cache_symbol', 'display_index_symbol', 'merge_by_date']
