from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import time
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import load_dataset, save_dataset
from core.db import finish_job, start_job
from services.market_calendar import MARKET_WINDOWS, latest_settled_trade_date
from services.index_ma20 import (
    INDEX_CONFIG,
    INDEX_FINAL_HISTORY_SOURCE,
    INDEX_LONG_HISTORY_SOURCE,
    INDEX_REPORT_DISPLAY_DAYS,
    INDEX_SOURCE_CORRECTION_SOURCE,
    append_eastmoney_latest_index_row,
    extract_raw_from_export_df,
    extract_source_correction_rows,
    build_export_df,
    fetch_index_from_source,
    fetch_one_index,
    filter_completed_market_dates,
    get_index_raw_from_tickflow,
    merge_by_date,
    merge_raw_index_data,
    missing_recent_market_trade_dates,
    overlay_finalized_index_rows,
    raw_cache_symbol,
    sanitize_index_report_market_dates,
    source_correction_fetch_days,
)


ProgressCallback = Callable[[str, int, int, str, float | None], None]
INDEX_HISTORY_BOOTSTRAP_DAYS = 1000
INDEX_HISTORY_BOOTSTRAP_BARS = 1000
INDEX_HISTORY_MIN_ROWS = 252
INDEX_HISTORY_INCREMENTAL_DAYS = 30


@dataclass
class UpdateResult:
    status: str
    message: str
    dataframe: pd.DataFrame | None = None
    errors: list[str] = field(default_factory=list)
    timings: list[dict] = field(default_factory=list)


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


def append_cached_index_rows(old_df: pd.DataFrame | None, new_df: pd.DataFrame) -> pd.DataFrame:
    """Append unseen dates while keeping every existing cached row unchanged."""
    normalized_new = merge_raw_index_data(None, new_df)
    if old_df is None or old_df.empty:
        return normalized_new

    normalized_old = merge_raw_index_data(None, old_df)
    unseen = normalized_new[~normalized_new["trade_date"].isin(normalized_old["trade_date"])]
    if unseen.empty:
        return normalized_old
    return pd.concat([normalized_old, unseen], ignore_index=True).sort_values("trade_date").reset_index(drop=True)


def sync_index_long_history(cache_symbol: str, index_name: str, new_df: pd.DataFrame) -> None:
    long_cached_raw, _ = load_dataset(
        cache_symbol,
        INDEX_LONG_HISTORY_SOURCE,
        "index_daily_raw",
    )
    if long_cached_raw is None or long_cached_raw.empty:
        return

    merged = append_cached_index_rows(long_cached_raw, new_df)
    if len(merged) == len(long_cached_raw):
        return
    save_dataset(
        symbol=cache_symbol,
        name=f"{index_name} 指数长历史日线",
        source=INDEX_LONG_HISTORY_SOURCE,
        data_type="index_daily_raw",
        df=merged,
    )


def enrich_index_report_indicators(report_df: pd.DataFrame) -> pd.DataFrame:
    """Fill report indicators from long history plus unsaved current display rows."""
    if report_df is None or report_df.empty:
        return report_df

    enriched = report_df.copy()
    for index_name, index_config in INDEX_CONFIG.items():
        cache_symbol = raw_cache_symbol(index_name, index_config)
        history_raw, _ = load_dataset(
            cache_symbol,
            INDEX_LONG_HISTORY_SOURCE,
            "index_daily_raw",
        )
        if history_raw is None or history_raw.empty:
            history_raw, _ = load_dataset(
                cache_symbol,
                "index_history",
                "index_daily_raw",
            )
        finalized_raw, _ = load_dataset(
            cache_symbol,
            INDEX_FINAL_HISTORY_SOURCE,
            "index_daily_raw",
        )
        history_raw = overlay_finalized_index_rows(history_raw, finalized_raw)
        correction_raw, _ = load_dataset(
            cache_symbol,
            INDEX_SOURCE_CORRECTION_SOURCE,
            "index_daily_raw",
        )
        correction_raw = extract_source_correction_rows(correction_raw, index_config)
        history_raw = overlay_finalized_index_rows(history_raw, correction_raw)
        report_raw = extract_raw_from_export_df(enriched, index_name)
        if report_raw is None or report_raw.empty:
            continue

        combined_raw = append_cached_index_rows(history_raw, report_raw)
        combined_raw = filter_completed_market_dates(
            combined_raw,
            str(index_config.get("market_group") or ""),
        )
        calculated = build_export_df(
            combined_raw,
            index_name,
            days=INDEX_REPORT_DISPLAY_DAYS,
        )
        if calculated is not None and not calculated.empty:
            enriched = merge_index_report(
                enriched,
                calculated,
                prefer_update_index_names={index_name},
            )
    return enriched


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


def refresh_cached_eastmoney_index_report(
    cached_index_df: pd.DataFrame,
    index_name: str,
    index_config: dict,
    days: int,
) -> pd.DataFrame:
    if index_config.get("source") != "eastmoney_kline":
        return cached_index_df

    raw_df = extract_raw_from_export_df(cached_index_df, index_name)
    if raw_df is None or raw_df.empty:
        return cached_index_df

    refreshed_raw = append_eastmoney_latest_index_row(
        None,
        raw_df,
        str(index_config.get("code") or ""),
        board_symbol=index_config.get("akshare_board_symbol"),
        hk_em_symbol=index_config.get("akshare_hk_em_symbol"),
    )
    refreshed_df = build_export_df(refreshed_raw, index_name, days=days)
    if refreshed_df is None or refreshed_df.empty:
        return cached_index_df

    cached_latest = latest_index_trade_date(cached_index_df, index_name)
    refreshed_latest = latest_index_trade_date(refreshed_df, index_name)
    if cached_latest is None or refreshed_latest is None:
        return refreshed_df
    if refreshed_latest == cached_latest:
        ma20_column = f"{index_name}_MA20"
        cached_ma20 = (
            pd.to_numeric(cached_index_df[ma20_column], errors="coerce").dropna()
            if ma20_column in cached_index_df.columns
            else pd.Series(dtype=float)
        )
        refreshed_ma20 = (
            pd.to_numeric(refreshed_df[ma20_column], errors="coerce").dropna()
            if ma20_column in refreshed_df.columns
            else pd.Series(dtype=float)
        )
        if not cached_ma20.empty and refreshed_ma20.empty:
            return cached_index_df
    if refreshed_latest >= cached_latest:
        return refreshed_df
    return cached_index_df


def persist_confirmed_index_report_row(
    index_name: str,
    index_config: dict,
    report_df: pd.DataFrame,
) -> None:
    market_name = str(index_config.get("market_group") or "")
    market = next((item for item in MARKET_WINDOWS if item.name == market_name), None)
    if market is None:
        return
    market_now = datetime.now(ZoneInfo(market.timezone))
    target_date = latest_settled_trade_date(market, market_now)
    report_raw = extract_raw_from_export_df(report_df, index_name)
    report_raw = filter_completed_market_dates(report_raw, market_name)
    if report_raw is None or report_raw.empty:
        return
    report_dates = pd.to_datetime(report_raw["trade_date"], errors="coerce")
    target_rows = report_raw.loc[report_dates.dt.date == target_date].copy()
    if target_rows.empty:
        return

    cache_symbol = raw_cache_symbol(index_name, index_config)
    finalized_raw, _ = load_dataset(
        cache_symbol,
        INDEX_FINAL_HISTORY_SOURCE,
        "index_daily_raw",
    )
    previous_count = 0 if finalized_raw is None else len(finalized_raw)
    finalized_raw = append_cached_index_rows(finalized_raw, target_rows)
    if len(finalized_raw) <= previous_count:
        return
    save_dataset(
        symbol=cache_symbol,
        name=f"{index_name} 指数收盘确认日线",
        source=INDEX_FINAL_HISTORY_SOURCE,
        data_type="index_daily_raw",
        df=finalized_raw,
    )


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
