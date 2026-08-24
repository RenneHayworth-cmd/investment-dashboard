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

from services.index_frames import extract_raw_from_export_df

def _verification_report(
    index_name: str,
    index_config: dict,
    *,
    api_key: str,
    days: int,
) -> tuple[pd.DataFrame | None, str]:
    if api_key.strip() and index_config.get("tickflow_symbol"):
        from services.index_realtime import index_update_source_labels

        _, verification_source = index_update_source_labels(index_name, tickflow_enabled=True)
        return fetch_index_from_source(index_name, index_config, days=days), verification_source
    yahoo_symbol = str(index_config.get("yahoo_symbol") or "").strip()
    if index_name == "VIX恐慌指数":
        yahoo_symbol = "^VIX"
    elif index_name == "恒生科技":
        yahoo_symbol = "^HSTECH"
    if yahoo_symbol:
        return get_index_data_from_yahoo(yahoo_symbol, index_name, days=days), "Yahoo Finance 日线"
    return None, "暂无独立日线复核源"

def verify_updated_index_data(
    index_names: set[str] | list[str] | tuple[str, ...],
    target_dates: dict[str, date | str],
    *,
    api_key: str = "",
    max_workers: int = 4,
    tolerance_pct: float = INDEX_VERIFICATION_TOLERANCE_PCT,
) -> pd.DataFrame:
    """Compare saved formal closes with an independent source without overwriting data."""
    ordered_names = [name for name in INDEX_CONFIG if name in set(index_names)]

    def verify_one(index_name: str) -> dict[str, object]:
        config = INDEX_CONFIG[index_name]
        target_value = target_dates.get(index_name)
        target = pd.to_datetime(target_value, errors="coerce")
        base = {
            "指数": index_name,
            "目标交易日": "" if pd.isna(target) else target.date().isoformat(),
            "正式收盘价": None,
            "复核收盘价": None,
            "偏差(%)": None,
            "复核源": "",
            "复核结果": "",
        }
        if pd.isna(target):
            return {**base, "复核结果": "目标日期无效"}
        formal_raw, _ = load_dataset(
            raw_cache_symbol(index_name, config),
            INDEX_FINAL_HISTORY_SOURCE,
            "index_daily_raw",
        )
        if formal_raw is None or formal_raw.empty or "trade_date" not in formal_raw.columns:
            return {**base, "复核结果": "主数据未更新到目标日"}
        formal_dates = pd.to_datetime(formal_raw["trade_date"], errors="coerce")
        formal_row = formal_raw[formal_dates.dt.date == target.date()]
        if formal_row.empty:
            return {**base, "复核结果": "主数据未更新到目标日"}
        formal_close = pd.to_numeric(formal_row.iloc[-1].get("close"), errors="coerce")
        if pd.isna(formal_close):
            return {**base, "复核结果": "主数据收盘价无效"}
        base["正式收盘价"] = float(formal_close)
        try:
            report, source_label = _verification_report(
                index_name,
                config,
                api_key=api_key,
                days=60,
            )
            base["复核源"] = source_label
            if report is None or report.empty:
                return {**base, "复核结果": "无法复核"}
            raw = extract_raw_from_export_df(report, index_name)
            raw = filter_completed_market_dates(raw, str(config.get("market_group") or ""))
            if raw is None or raw.empty or "trade_date" not in raw.columns:
                return {**base, "复核结果": "复核源无有效日线"}
            verification_dates = pd.to_datetime(raw["trade_date"], errors="coerce")
            verification_row = raw[verification_dates.dt.date == target.date()]
            if verification_row.empty:
                return {**base, "复核结果": "复核源缺少目标日"}
            verification_close = pd.to_numeric(verification_row.iloc[-1].get("close"), errors="coerce")
            if pd.isna(verification_close):
                return {**base, "复核结果": "复核收盘价无效"}
            if float(formal_close) == 0:
                return {**base, "复核收盘价": float(verification_close), "复核结果": "主数据收盘价为零"}
            deviation_pct = abs(float(verification_close) - float(formal_close)) / abs(float(formal_close)) * 100
            base["复核收盘价"] = float(verification_close)
            base["偏差(%)"] = round(deviation_pct, 4)
            base["复核结果"] = "一致" if deviation_pct <= tolerance_pct else f"偏差超过{tolerance_pct:.2f}%"
            return base
        except Exception as exc:
            return {**base, "复核结果": f"复核失败：{str(exc).strip() or type(exc).__name__}"}

    if not ordered_names:
        return pd.DataFrame()
    workers = min(max(int(max_workers), 1), len(ordered_names))
    rows_by_name: dict[str, dict[str, object]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(verify_one, name): name for name in ordered_names}
        for future in as_completed(futures):
            name = futures[future]
            rows_by_name[name] = future.result()
    return pd.DataFrame([rows_by_name[name] for name in ordered_names])

__all__ = ['_verification_report', 'verify_updated_index_data']
