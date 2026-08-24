from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_config import (
    CFFEX_FUTURES_MAIN_PRODUCTS,
    INDEX_CONFIG,
    INDEX_FINAL_HISTORY_SOURCE,
    INDEX_LONG_HISTORY_BARS,
    INDEX_LONG_HISTORY_SOURCE,
    INDEX_RECENT_GAP_LOOKBACK_SESSIONS,
    INDEX_REPORT_DISPLAY_DAYS,
    INDEX_SOURCE_CORRECTION_SOURCE,
    YAHOO_CHART_HOSTS,
    YAHOO_REQUEST_GATE,
)
from services.market_calendar import (
    expected_latest_trade_date,
    get_market_window,
    is_market_holiday,
    is_market_trading_day,
    latest_completed_trade_date,
    latest_settled_trade_date,
)

from services.index_frames import (
    _append_unseen_raw_history,
    _latest_completed_date_for_market,
    _latest_raw_date,
    build_export_df,
    extract_raw_from_export_df,
    extract_source_correction_rows,
    filter_completed_market_dates,
    merge_by_date,
    missing_recent_market_trade_dates,
    overlay_finalized_index_rows,
    raw_cache_symbol,
    source_correction_fetch_days,
    source_correction_start,
)
from services.index_sources import (
    append_eastmoney_quote_row,
    fetch_index_from_source,
    get_index_data_from_tickflow,
    get_index_raw_from_tickflow,
)

def fetch_index_history(index_name: str, index_config, days: int = 10000) -> pd.DataFrame | None:
    if not isinstance(index_config, dict):
        return None

    from core.cache import load_dataset, save_dataset

    cache_symbol = raw_cache_symbol(index_name, index_config)
    market_name = str(index_config.get("market_group") or "")
    long_cached_storage_raw, _ = load_dataset(
        cache_symbol,
        INDEX_LONG_HISTORY_SOURCE,
        "index_daily_raw",
    )
    accumulated_storage_raw, _ = load_dataset(
        cache_symbol,
        "index_history",
        "index_daily_raw",
    )
    finalized_storage_raw, _ = load_dataset(
        cache_symbol,
        INDEX_FINAL_HISTORY_SOURCE,
        "index_daily_raw",
    )
    correction_storage_raw, _ = load_dataset(
        cache_symbol,
        INDEX_SOURCE_CORRECTION_SOURCE,
        "index_daily_raw",
    )
    long_cached_raw = filter_completed_market_dates(long_cached_storage_raw, market_name)
    accumulated_raw = filter_completed_market_dates(accumulated_storage_raw, market_name)
    finalized_raw = filter_completed_market_dates(finalized_storage_raw, market_name)
    correction_raw = filter_completed_market_dates(
        extract_source_correction_rows(correction_storage_raw, index_config),
        market_name,
    )

    is_bootstrap = long_cached_storage_raw is None or long_cached_storage_raw.empty
    combined_storage_raw = accumulated_storage_raw if is_bootstrap else long_cached_storage_raw
    combined_raw = accumulated_raw if is_bootstrap else long_cached_raw
    effective_raw = overlay_finalized_index_rows(combined_raw, finalized_raw)
    effective_raw = overlay_finalized_index_rows(effective_raw, correction_raw)
    target_date = _latest_completed_date_for_market(market_name)
    cached_latest_date = _latest_raw_date(effective_raw)
    missing_recent_dates = missing_recent_market_trade_dates(
        effective_raw,
        market_name,
        target_date,
    )
    correction_start = source_correction_start(index_config)
    correction_latest_date = _latest_raw_date(correction_raw)
    correction_needed = (
        correction_start is not None
        and target_date is not None
        and target_date >= correction_start.date()
        and (correction_latest_date is None or correction_latest_date < target_date)
    )
    if not is_bootstrap and target_date is not None and cached_latest_date is not None:
        if (
            cached_latest_date >= target_date
            and not correction_needed
            and not missing_recent_dates
        ):
            return build_export_df(effective_raw, index_name, days=days)

    missing_calendar_days = (
        max((target_date - cached_latest_date).days, 1)
        if target_date is not None and cached_latest_date is not None
        else 30
    )
    incremental_days = max(missing_calendar_days + 7, 30)

    fetched_raw_parts: list[pd.DataFrame] = []
    fetched_correction_parts: list[pd.DataFrame] = []
    tickflow_symbol = index_config.get("tickflow_symbol")
    if tickflow_symbol:
        try:
            fetch_count = INDEX_LONG_HISTORY_BARS if is_bootstrap else max(missing_calendar_days * 2 + 10, 30)
            tickflow_raw = get_index_raw_from_tickflow("", tickflow_symbol, count=fetch_count)
            tickflow_raw = filter_completed_market_dates(tickflow_raw, market_name)
            if tickflow_raw is not None and not tickflow_raw.empty:
                fetched_raw_parts.append(tickflow_raw)
        except Exception:
            pass

    tickflow_combined = effective_raw
    for fetched_raw in fetched_raw_parts:
        tickflow_combined = _append_unseen_raw_history(tickflow_combined, fetched_raw)
    tickflow_latest_date = _latest_raw_date(tickflow_combined)
    source_needed = (
        is_bootstrap
        or correction_needed
        or bool(missing_recent_dates)
        or target_date is None
        or tickflow_latest_date is None
        or tickflow_latest_date < target_date
    )
    if source_needed:
        try:
            source_days = days if is_bootstrap else incremental_days
            source_days = source_correction_fetch_days(
                index_config,
                correction_raw,
                market_name,
                source_days,
            )
            source_df = fetch_index_from_source(index_name, index_config, days=source_days)
            source_raw = extract_raw_from_export_df(source_df, index_name)
            source_raw = filter_completed_market_dates(source_raw, market_name)
            if source_raw is not None and not source_raw.empty:
                fetched_raw_parts.append(source_raw)
                source_correction_raw = extract_source_correction_rows(source_raw, index_config)
                if source_correction_raw is not None and not source_correction_raw.empty:
                    fetched_correction_parts.append(source_correction_raw)
        except Exception:
            pass

    original_row_count = 0 if combined_storage_raw is None else len(combined_storage_raw)
    original_finalized_count = 0 if finalized_storage_raw is None else len(finalized_storage_raw)
    original_correction_count = 0 if correction_storage_raw is None else len(correction_storage_raw)
    for fetched_raw in fetched_raw_parts:
        combined_storage_raw = _append_unseen_raw_history(combined_storage_raw, fetched_raw)
        finalized_storage_raw = _append_unseen_raw_history(finalized_storage_raw, fetched_raw)
    for fetched_correction_raw in fetched_correction_parts:
        correction_storage_raw = _append_unseen_raw_history(
            correction_storage_raw,
            fetched_correction_raw,
        )
    combined_raw = filter_completed_market_dates(combined_storage_raw, market_name)
    finalized_raw = filter_completed_market_dates(finalized_storage_raw, market_name)
    correction_raw = filter_completed_market_dates(
        extract_source_correction_rows(correction_storage_raw, index_config),
        market_name,
    )
    effective_raw = overlay_finalized_index_rows(combined_raw, finalized_raw)
    effective_raw = overlay_finalized_index_rows(effective_raw, correction_raw)

    if effective_raw is None or effective_raw.empty:
        return None
    if not fetched_raw_parts:
        return build_export_df(effective_raw, index_name, days=days)

    if correction_storage_raw is not None and len(correction_storage_raw) > original_correction_count:
        save_dataset(
            symbol=cache_symbol,
            name=f"{index_name} 指数来源校正日线",
            source=INDEX_SOURCE_CORRECTION_SOURCE,
            data_type="index_daily_raw",
            df=correction_storage_raw,
        )

    if finalized_storage_raw is not None and len(finalized_storage_raw) > original_finalized_count:
        save_dataset(
            symbol=cache_symbol,
            name=f"{index_name} 指数收盘确认日线",
            source=INDEX_FINAL_HISTORY_SOURCE,
            data_type="index_daily_raw",
            df=finalized_storage_raw,
        )

    if not is_bootstrap and len(combined_storage_raw) == original_row_count:
        return build_export_df(effective_raw, index_name, days=days)

    save_dataset(
        symbol=cache_symbol,
        name=f"{index_name} 指数累计日线",
        source="index_history",
        data_type="index_daily_raw",
        df=combined_storage_raw,
    )
    save_dataset(
        symbol=cache_symbol,
        name=f"{index_name} 指数长历史日线",
        source=INDEX_LONG_HISTORY_SOURCE,
        data_type="index_daily_raw",
        df=combined_storage_raw,
    )
    return build_export_df(effective_raw, index_name, days=days)

def generate_index_ma20_report(
    api_key: str,
    days: int = INDEX_REPORT_DISPLAY_DAYS,
) -> pd.DataFrame:
    all_data = []
    errors = []
    for index_name, index_config in INDEX_CONFIG.items():
        try:
            df = fetch_one_index(index_name, index_config, api_key=api_key, days=days)

            if df is not None and not df.empty:
                all_data.append(df)
        except Exception as exc:
            errors.append(f"{index_name}: {exc}")

    if not all_data:
        raise RuntimeError("未获取到任何指数数据。" + " | ".join(errors))

    merged_df = merge_by_date(all_data)
    if errors:
        merged_df.attrs["errors"] = errors
    return merged_df

def fetch_one_index(index_name: str, index_config, api_key: str, days: int = 30) -> pd.DataFrame | None:
    if isinstance(index_config, dict):
        tickflow_symbol = index_config.get("tickflow_symbol")
        tickflow_error = None
        if tickflow_symbol:
            try:
                df = get_index_data_from_tickflow(
                    api_key,
                    tickflow_symbol,
                    index_name,
                    days=days,
                )
                if df is not None and not df.empty:
                    eastmoney_quote_secid = index_config.get("eastmoney_quote_secid")
                    if eastmoney_quote_secid:
                        close_col = f"{index_name}_收盘价"
                        raw_df = df[["日期", close_col]].rename(
                            columns={"日期": "trade_date", close_col: "close"}
                        )
                        raw_df = append_eastmoney_quote_row(raw_df, eastmoney_quote_secid)
                        return build_export_df(raw_df, index_name, days=days)
                    return df
            except Exception as exc:
                tickflow_error = exc

        try:
            return fetch_index_from_source(index_name, index_config, days=days)
        except Exception as exc:
            if tickflow_error:
                raise RuntimeError(f"TickFlow失败：{tickflow_error}；AkShare失败：{exc}") from exc
            raise

    return get_index_data_from_tickflow(api_key, index_config, index_name, days=days)

__all__ = ['fetch_index_history', 'generate_index_ma20_report', 'fetch_one_index']
