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

from services.index_update_frames import (
    build_index_update_message, build_stale_quote_message, build_timing_row,
    cached_report_satisfies_current_quotes, extract_cached_index_report,
    has_current_index_quote, merge_index_report, trim_index_report,
)
from services.index_update_persistence import (
    append_cached_index_rows, persist_confirmed_index_report_row,
    refresh_cached_eastmoney_index_report, sync_index_long_history,
)

def run_index_ma20_update(
    api_key: str = "",
    days: int = INDEX_REPORT_DISPLAY_DAYS,
    cache_source: str = "auto",
    use_fresh_cache: bool = True,
    progress_callback: ProgressCallback | None = None,
    market_names: set[str] | list[str] | tuple[str, ...] | None = None,
    index_names: set[str] | list[str] | tuple[str, ...] | None = None,
    max_workers: int = 4,
) -> UpdateResult:
    selected_markets = set(market_names or [])
    selected_indexes = set(index_names or [])
    if selected_indexes:
        job_name = f"更新指数MA20（{'、'.join(sorted(selected_indexes))}）"
    elif selected_markets:
        job_name = f"更新指数MA20（{'、'.join(sorted(selected_markets))}）"
    else:
        job_name = "更新指数MA20"
    job_id = start_job(job_name)
    all_data = []
    errors = []
    timings = []
    selected_items = [
        (index_name, index_config)
        for index_name, index_config in INDEX_CONFIG.items()
        if (not selected_markets or index_config.get("market_group") in selected_markets)
        and (not selected_indexes or index_name in selected_indexes)
    ]
    total = len(selected_items)

    try:
        if not selected_items:
            if selected_indexes:
                raise RuntimeError(f"没有匹配的指数：{'、'.join(sorted(selected_indexes))}")
            raise RuntimeError(f"没有匹配的指数市场分组：{'、'.join(sorted(selected_markets))}")

        cached_df = None
        is_partial_update = bool(selected_markets or selected_indexes)
        if use_fresh_cache and not is_partial_update:
            cached_df, meta = load_dataset(
                "index_ma20_latest",
                cache_source,
                "index_ma20_report",
            )
            last_update_time = (meta or {}).get("last_update_time")
            if cached_df is not None and last_update_time:
                last_update_date = datetime.fromisoformat(last_update_time).date()
                if last_update_date == datetime.now().date() and cached_report_satisfies_current_quotes(
                    cached_df,
                    selected_items,
                ):
                    message = f"使用今日缓存，更新时间：{last_update_time}"
                    finish_job(job_id, "success", message)
                    return UpdateResult("success", message, cached_df)
        elif is_partial_update:
            cached_df, _ = load_dataset("index_ma20_latest", cache_source, "index_ma20_report")
        if cached_df is None:
            cached_df, _ = load_dataset("index_ma20_latest", cache_source, "index_ma20_report")

        selected_futures = {
            name: contract
            for name, contract in load_futures_main_contract_mapping().items()
            if name in {selected_name for selected_name, _ in selected_items}
        }
        contract_history_errors = refresh_futures_current_contract_histories(
            selected_futures,
            max_workers=max_workers,
        )
        errors.extend(
            f"当前合约正式日线: {message}" for message in contract_history_errors
        )

        workers = min(max(int(max_workers), 1), total)
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(fetch_index_report, index_name, index_config, api_key, days, cached_df): (
                    index_name,
                    index_config,
                    time.perf_counter(),
                )
                for index_name, index_config in selected_items
            }
            for future in as_completed(future_map):
                index_name, index_config, started_at = future_map[future]
                completed += 1
                elapsed_seconds = time.perf_counter() - started_at
                try:
                    df = future.result()
                    if df is not None and not df.empty:
                        current_enough = has_current_index_quote(df, index_name, index_config)
                        all_data.append(df)
                        status = "success" if current_enough else "stale"
                        timings.append(build_timing_row(index_name, index_config, status, elapsed_seconds))
                        if not current_enough and index_config.get("require_current_quote"):
                            errors.append(build_stale_quote_message(index_name, index_config, df, "已保存最新可得数据"))
                        if progress_callback:
                            progress_callback(index_name, completed, total, status, elapsed_seconds)
                    else:
                        timings.append(build_timing_row(index_name, index_config, "empty", elapsed_seconds))
                        if not index_config.get("optional"):
                            errors.append(f"{index_name}: 无数据")
                            if progress_callback:
                                progress_callback(index_name, completed, total, "empty", elapsed_seconds)
                        elif progress_callback:
                            progress_callback(index_name, completed, total, "empty", elapsed_seconds)
                except Exception as exc:
                    request_error = str(exc).strip() or type(exc).__name__
                    cached_index_df = extract_cached_index_report(cached_df, index_name)
                    if cached_index_df is not None and not cached_index_df.empty:
                        cached_index_df = refresh_cached_eastmoney_index_report(
                            cached_index_df,
                            index_name,
                            index_config,
                            days,
                        )
                        current_enough = has_current_index_quote(cached_index_df, index_name, index_config)
                        status = "success" if current_enough else "cached"
                        if current_enough:
                            persist_confirmed_index_report_row(index_name, index_config, cached_index_df)
                        if current_enough and index_config.get("source") == "eastmoney_kline":
                            message = "东方财富K线接口失败，已使用本地历史序列 + 东方财富列表最新价补齐当天数据"
                        elif current_enough:
                            message = f"联网获取异常，本地正式日线已达到预期日期：{request_error}"
                        else:
                            message = request_error
                        all_data.append(cached_index_df)
                        timings.append(build_timing_row(index_name, index_config, status, elapsed_seconds, message))
                        if index_config.get("require_current_quote") and not current_enough:
                            stale_message = build_stale_quote_message(
                                index_name,
                                index_config,
                                cached_index_df,
                                "已沿用本地缓存",
                            )
                            errors.append(f"{stale_message}；请求异常：{request_error}")
                        if progress_callback:
                            progress_callback(index_name, completed, total, status, elapsed_seconds)
                    elif index_config.get("optional"):
                        timings.append(build_timing_row(index_name, index_config, "empty", elapsed_seconds, request_error))
                        if progress_callback:
                            progress_callback(index_name, completed, total, "empty", elapsed_seconds)
                    else:
                        timings.append(build_timing_row(index_name, index_config, "failed", elapsed_seconds, request_error))
                        errors.append(f"{index_name}: {request_error}")
                        if progress_callback:
                            progress_callback(index_name, completed, total, "failed", elapsed_seconds)

        if not all_data:
            raise RuntimeError("未获取到任何指数数据。" + " | ".join(errors))

        report = merge_by_date(all_data)
        if cached_df is not None and not cached_df.empty:
            report = merge_index_report(
                cached_df,
                report,
                prefer_update_index_names={name for name, _ in selected_items},
            )
        report = enrich_index_report_indicators(report)
        report = sanitize_index_report_market_dates(report)
        report = trim_index_report(report, days=INDEX_REPORT_DISPLAY_DAYS)
        report.attrs["errors"] = errors
        save_dataset(
            symbol="index_ma20_latest",
            name="指数MA20分列结果",
            source=cache_source,
            data_type="index_ma20_report",
            df=report,
        )

        message = build_index_update_message(
            selected_indexes=selected_indexes,
            selected_markets=selected_markets,
            timings=timings,
            errors=errors,
        )
        if timings:
            slowest = max(timings, key=lambda item: item["耗时(秒)"])
            message += f"；最慢：{slowest['指数']} {slowest['耗时(秒)']:.2f}秒"

        finish_job(job_id, "success", message)
        return UpdateResult("success", message, report, errors, timings)
    except Exception as exc:
        finish_job(job_id, "failed", str(exc))
        return UpdateResult("failed", f"更新失败：{exc}", errors=errors, timings=timings)

def fetch_index_report(
    index_name: str,
    index_config: dict,
    api_key: str,
    days: int,
    cached_report: pd.DataFrame | None = None,
) -> pd.DataFrame | None:
    df = None
    cache_symbol = raw_cache_symbol(index_name, index_config)
    cached_history_storage_raw, _ = load_dataset(
        cache_symbol,
        "index_history",
        "index_daily_raw",
    )
    if cached_history_storage_raw is None or cached_history_storage_raw.empty:
        cached_index_report = extract_cached_index_report(cached_report, index_name)
        cached_history_storage_raw = extract_raw_from_export_df(cached_index_report, index_name)
    market_name = str(index_config.get("market_group") or "")
    cached_history_raw = filter_completed_market_dates(cached_history_storage_raw, market_name)
    finalized_raw, _ = load_dataset(
        cache_symbol,
        INDEX_FINAL_HISTORY_SOURCE,
        "index_daily_raw",
    )
    finalized_raw = filter_completed_market_dates(finalized_raw, market_name)
    correction_storage_raw, _ = load_dataset(
        cache_symbol,
        INDEX_SOURCE_CORRECTION_SOURCE,
        "index_daily_raw",
    )
    correction_raw = filter_completed_market_dates(
        extract_source_correction_rows(correction_storage_raw, index_config),
        market_name,
    )
    effective_history_raw = overlay_finalized_index_rows(cached_history_raw, finalized_raw)
    effective_history_raw = overlay_finalized_index_rows(effective_history_raw, correction_raw)
    market = next((item for item in MARKET_WINDOWS if item.name == market_name), None)
    market_now = datetime.now(ZoneInfo(market.timezone)) if market is not None else None
    target_date = latest_settled_trade_date(market, market_now) if market is not None else None
    missing_recent_dates = missing_recent_market_trade_dates(
        effective_history_raw,
        market_name,
        target_date,
    )
    needs_history_bootstrap = effective_history_raw is None or len(effective_history_raw) < INDEX_HISTORY_MIN_ROWS
    source_days = (
        max(int(days), INDEX_HISTORY_BOOTSTRAP_DAYS)
        if needs_history_bootstrap
        else INDEX_HISTORY_INCREMENTAL_DAYS
    )
    source_days = source_correction_fetch_days(
        index_config,
        correction_raw,
        market_name,
        source_days,
    )
    if missing_recent_dates:
        missing_span_days = (target_date - min(missing_recent_dates)).days + 14
        source_days = max(source_days, missing_span_days)
    tickflow_symbol = index_config.get("tickflow_symbol") if isinstance(index_config, dict) else None
    if tickflow_symbol:
        try:
            cached_raw, _ = load_dataset(
                cache_symbol,
                "tickflow",
                "index_daily_raw",
            )
            fetch_count = (
                INDEX_HISTORY_BOOTSTRAP_BARS
                if needs_history_bootstrap
                else 30
            )
            latest_raw = get_index_raw_from_tickflow(
                api_key,
                tickflow_symbol,
                count=fetch_count,
            )
            latest_raw = filter_completed_market_dates(latest_raw, market_name)
            if latest_raw is not None and not latest_raw.empty:
                previous_finalized_count = 0 if finalized_raw is None else len(finalized_raw)
                finalized_raw = append_cached_index_rows(finalized_raw, latest_raw)
                if len(finalized_raw) > previous_finalized_count:
                    save_dataset(
                        symbol=cache_symbol,
                        name=f"{index_name} 指数收盘确认日线",
                        source=INDEX_FINAL_HISTORY_SOURCE,
                        data_type="index_daily_raw",
                        df=finalized_raw,
                    )
                previous_tickflow_count = 0 if cached_raw is None else len(cached_raw)
                raw_df = append_cached_index_rows(cached_raw, latest_raw)
                if len(raw_df) > previous_tickflow_count:
                    save_dataset(
                        symbol=cache_symbol,
                        name=f"{index_name} 指数原始日线",
                        source="tickflow",
                        data_type="index_daily_raw",
                        df=raw_df,
                    )
                previous_history_count = 0 if cached_history_storage_raw is None else len(cached_history_storage_raw)
                history_storage_raw = append_cached_index_rows(cached_history_storage_raw, raw_df)
                if len(history_storage_raw) > previous_history_count:
                    save_dataset(
                        symbol=cache_symbol,
                        name=f"{index_name} 指数累计日线",
                        source="index_history",
                        data_type="index_daily_raw",
                        df=history_storage_raw,
                    )
                cached_history_storage_raw = history_storage_raw
                cached_history_raw = filter_completed_market_dates(history_storage_raw, market_name)
                effective_history_raw = overlay_finalized_index_rows(cached_history_raw, finalized_raw)
                effective_history_raw = overlay_finalized_index_rows(effective_history_raw, correction_raw)
                df = build_export_df(effective_history_raw, index_name, days=days)
                if df is not None and not df.empty and not has_current_index_quote(df, index_name, index_config):
                    df = fetch_index_from_source(index_name, index_config, days=source_days)
        except Exception:
            df = None

    if df is None or missing_recent_dates:
        df = fetch_one_index(index_name, index_config, api_key=api_key, days=source_days)

    latest_raw = extract_raw_from_export_df(df, index_name)
    latest_raw = filter_completed_market_dates(latest_raw, market_name)
    if latest_raw is None or latest_raw.empty:
        return build_export_df(effective_history_raw, index_name, days=days)

    latest_correction_raw = extract_source_correction_rows(latest_raw, index_config)
    if latest_correction_raw is not None and not latest_correction_raw.empty:
        previous_correction_count = 0 if correction_storage_raw is None else len(correction_storage_raw)
        correction_storage_raw = append_cached_index_rows(correction_storage_raw, latest_correction_raw)
        if len(correction_storage_raw) > previous_correction_count:
            save_dataset(
                symbol=cache_symbol,
                name=f"{index_name} 指数来源校正日线",
                source=INDEX_SOURCE_CORRECTION_SOURCE,
                data_type="index_daily_raw",
                df=correction_storage_raw,
            )
        correction_raw = filter_completed_market_dates(correction_storage_raw, market_name)

    previous_finalized_count = 0 if finalized_raw is None else len(finalized_raw)
    finalized_raw = append_cached_index_rows(finalized_raw, latest_raw)
    if len(finalized_raw) > previous_finalized_count:
        save_dataset(
            symbol=cache_symbol,
            name=f"{index_name} 指数收盘确认日线",
            source=INDEX_FINAL_HISTORY_SOURCE,
            data_type="index_daily_raw",
            df=finalized_raw,
        )

    previous_history_count = 0 if cached_history_storage_raw is None else len(cached_history_storage_raw)
    accumulated_storage_raw = append_cached_index_rows(cached_history_storage_raw, latest_raw)
    if len(accumulated_storage_raw) > previous_history_count:
        save_dataset(
            symbol=cache_symbol,
            name=f"{index_name} 指数累计日线",
            source="index_history",
            data_type="index_daily_raw",
            df=accumulated_storage_raw,
        )
        sync_index_long_history(cache_symbol, index_name, accumulated_storage_raw)
    accumulated_raw = filter_completed_market_dates(accumulated_storage_raw, market_name)
    effective_history_raw = overlay_finalized_index_rows(accumulated_raw, finalized_raw)
    effective_history_raw = overlay_finalized_index_rows(effective_history_raw, correction_raw)
    rebuilt = build_export_df(effective_history_raw, index_name, days=days)
    return df if rebuilt is None or rebuilt.empty else rebuilt

__all__ = ['run_index_ma20_update', 'fetch_index_report']
