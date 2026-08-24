from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import time
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import load_dataset, save_dataset
from core.db import finish_job, start_job
from services.market_calendar import MARKET_WINDOWS, latest_settled_trade_date
from services.index_ma20 import (
    INDEX_CONFIG, INDEX_FINAL_HISTORY_SOURCE, INDEX_LONG_HISTORY_SOURCE,
    INDEX_REPORT_DISPLAY_DAYS, INDEX_SOURCE_CORRECTION_SOURCE,
    append_eastmoney_latest_index_row, build_export_df, extract_raw_from_export_df,
    extract_source_correction_rows, fetch_index_from_source, fetch_one_index,
    filter_completed_market_dates, get_index_data_from_yahoo,
    get_index_raw_from_tickflow, merge_by_date, merge_raw_index_data,
    missing_recent_market_trade_dates, overlay_finalized_index_rows,
    raw_cache_symbol, sanitize_index_report_market_dates, source_correction_fetch_days,
)
from services.index_update_models import (
    FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE, FUTURES_MAIN_CONTRACT_CACHE_SOURCE,
    FUTURES_MAIN_CONTRACT_CACHE_SYMBOL, FUTURES_MAIN_INDEX_NAMES,
    INDEX_HISTORY_BOOTSTRAP_BARS, INDEX_HISTORY_BOOTSTRAP_DAYS,
    INDEX_HISTORY_INCREMENTAL_DAYS, INDEX_HISTORY_MIN_ROWS,
    INDEX_VERIFICATION_TOLERANCE_PCT, ProgressCallback, UpdateResult,
)

def build_index_update_message(
    *,
    selected_indexes: set[str],
    selected_markets: set[str],
    timings: list[dict],
    errors: list[str],
) -> str:
    names_by_status: dict[str, list[str]] = {}
    for timing in timings:
        status = str(timing.get("状态") or "")
        index_name = str(timing.get("指数") or "")
        if index_name:
            names_by_status.setdefault(status, []).append(index_name)

    parts = []
    status_labels = (
        ("成功", "正式日线更新成功"),
        ("最新可得", "仅保存最新可得数据"),
        ("使用缓存", "正式日线未更新，沿用缓存"),
        ("无数据", "未取得数据"),
        ("失败", "获取失败"),
    )
    for status, label in status_labels:
        names = sorted(set(names_by_status.get(status, [])))
        if names:
            parts.append(f"{label}：{'、'.join(names)}")

    if not parts:
        if selected_indexes:
            parts.append(f"已处理：{'、'.join(sorted(selected_indexes))}")
        elif selected_markets:
            parts.append(f"已处理：{'、'.join(sorted(selected_markets))}")
        else:
            parts.append("指数更新处理完成")
    if errors:
        parts.append("未完成原因：" + " | ".join(errors))
    return "；".join(parts)

def build_timing_row(
    index_name: str,
    index_config: dict,
    status: str,
    elapsed_seconds: float,
    message: str = "",
) -> dict:
    status_labels = {
        "success": "成功",
        "stale": "最新可得",
        "cached": "使用缓存",
        "empty": "无数据",
        "failed": "失败",
    }
    return {
        "指数": index_name,
        "市场": index_config.get("market_group", "-"),
        "来源": index_config.get("source", "-"),
        "状态": status_labels.get(status, status),
        "耗时(秒)": round(elapsed_seconds, 2),
        "说明": message[:240],
    }

def cached_report_satisfies_current_quotes(
    cached_df: pd.DataFrame | None,
    selected_items: list[tuple[str, dict]],
) -> bool:
    if cached_df is None or cached_df.empty:
        return False

    for index_name, index_config in selected_items:
        cached_index_df = extract_cached_index_report(cached_df, index_name)
        if cached_index_df is None or cached_index_df.empty:
            return False
        if not has_current_index_quote(cached_index_df, index_name, index_config):
            return False
    return True

def merge_index_report(
    existing_df: pd.DataFrame,
    update_df: pd.DataFrame,
    prefer_update_index_names: set[str] | None = None,
) -> pd.DataFrame:
    existing = existing_df.copy()
    update = update_df.copy()
    if existing.empty:
        return update
    if update.empty:
        return existing

    existing["日期"] = existing["日期"].astype(str)
    update["日期"] = update["日期"].astype(str)
    existing = existing.drop_duplicates("日期", keep="first").set_index("日期")
    update = update.drop_duplicates("日期", keep="last").set_index("日期")
    merged = existing.combine_first(update)
    for index_name in prefer_update_index_names or set():
        for column in update.columns:
            if not str(column).startswith(f"{index_name}_"):
                continue
            merged[column] = update[column].combine_first(merged.get(column))
    merged = merged.reset_index()
    return merged.sort_values("日期").reset_index(drop=True)

def trim_index_report(
    report_df: pd.DataFrame,
    days: int = INDEX_REPORT_DISPLAY_DAYS,
) -> pd.DataFrame:
    if report_df is None or report_df.empty or "日期" not in report_df.columns:
        return report_df

    cutoff = pd.Timestamp(datetime.now()).normalize() - pd.Timedelta(days=max(int(days), 1))
    dates = pd.to_datetime(report_df["日期"], errors="coerce")
    return report_df.loc[dates >= cutoff].reset_index(drop=True)

def extract_cached_index_report(cached_df: pd.DataFrame | None, index_name: str) -> pd.DataFrame | None:
    if cached_df is None or cached_df.empty or "日期" not in cached_df.columns:
        return None

    index_columns = [
        column
        for column in cached_df.columns
        if column == "日期" or str(column).startswith(f"{index_name}_")
    ]
    if len(index_columns) <= 1:
        return None

    result = cached_df[index_columns].copy()
    value_columns = [column for column in result.columns if column != "日期"]
    result = result.dropna(how="all", subset=value_columns)
    if result.empty:
        return None
    return result

def latest_index_trade_date(df: pd.DataFrame | None, index_name: str) -> pd.Timestamp | None:
    if df is None or df.empty:
        return None
    close_column = f"{index_name}_收盘价"
    if "日期" not in df.columns or close_column not in df.columns:
        return None
    latest_date = pd.to_datetime(df.loc[df[close_column].notna(), "日期"], errors="coerce").max()
    if pd.isna(latest_date):
        return None
    return pd.Timestamp(latest_date)

def build_stale_quote_message(index_name: str, index_config: dict, df: pd.DataFrame, action_text: str) -> str:
    source_labels = {
        "eastmoney_kline": "东方财富",
        "cboe_vix": "CBOE",
    }
    source_label = source_labels.get(
        str(index_config.get("source") or ""),
        str(index_config.get("source", "上游数据源")),
    )
    latest_date = latest_index_trade_date(df, index_name)
    latest_text = latest_date.strftime("%Y-%m-%d") if latest_date is not None else "未知日期"

    expected_text = "预期交易日"
    market_name = index_config.get("market_group")
    market = next((item for item in MARKET_WINDOWS if item.name == market_name), None)
    if market is not None:
        market_now = datetime.now(ZoneInfo(market.timezone))
        expected_text = latest_settled_trade_date(market, market_now).strftime("%Y-%m-%d")

    return f"{index_name}: {source_label} 最新到 {latest_text}，预期 {expected_text}，{action_text}"

def has_current_index_quote(df: pd.DataFrame, index_name: str, index_config: dict) -> bool:
    if df is None or df.empty or not index_config.get("require_current_quote"):
        return True

    market_name = index_config.get("market_group")
    market = next((item for item in MARKET_WINDOWS if item.name == market_name), None)
    if market is None:
        return True
    market_now = datetime.now(ZoneInfo(market.timezone))
    expected_date = latest_settled_trade_date(market, market_now)

    latest_date = latest_index_trade_date(df, index_name)
    return not pd.isna(latest_date) and latest_date.date() >= expected_date

__all__ = ['build_index_update_message', 'build_timing_row', 'cached_report_satisfies_current_quotes', 'merge_index_report', 'trim_index_report', 'extract_cached_index_report', 'latest_index_trade_date', 'build_stale_quote_message', 'has_current_index_quote']
