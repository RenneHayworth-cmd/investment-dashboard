from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import load_dataset, save_dataset
from core.db import finish_job, start_job
from services.market_calendar import MARKET_WINDOWS, expected_latest_trade_date
from services.index_ma20 import (
    INDEX_CONFIG,
    append_eastmoney_latest_index_row,
    extract_raw_from_export_df,
    build_export_df,
    fetch_index_from_source,
    fetch_one_index,
    get_index_raw_from_tickflow,
    merge_by_date,
    merge_raw_index_data,
    raw_cache_symbol,
)


ProgressCallback = Callable[[str, int, int, str, float | None], None]


@dataclass
class UpdateResult:
    status: str
    message: str
    dataframe: pd.DataFrame | None = None
    errors: list[str] = field(default_factory=list)
    timings: list[dict] = field(default_factory=list)


def run_index_ma20_update(
    api_key: str = "",
    days: int = 30,
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
                executor.submit(fetch_index_report, index_name, index_config, api_key, days): (
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
                        message = (
                            "\u4e1c\u65b9\u8d22\u5bccK\u7ebf\u63a5\u53e3\u5931\u8d25\uff0c\u5df2\u4f7f\u7528\u672c\u5730\u5386\u53f2\u5e8f\u5217 + \u4e1c\u65b9\u8d22\u5bcc\u5217\u8868\u6700\u65b0\u4ef7\u8865\u9f50\u5f53\u5929\u6570\u636e"
                            if current_enough
                            else str(exc)
                        )
                        all_data.append(cached_index_df)
                        timings.append(build_timing_row(index_name, index_config, status, elapsed_seconds, message))
                        if index_config.get("require_current_quote") and not current_enough:
                            errors.append(build_stale_quote_message(index_name, index_config, cached_index_df, "\u5df2\u6cbf\u7528\u672c\u5730\u7f13\u5b58"))
                        if progress_callback:
                            progress_callback(index_name, completed, total, status, elapsed_seconds)
                    elif index_config.get("optional"):
                        timings.append(build_timing_row(index_name, index_config, "empty", elapsed_seconds, str(exc)))
                        if progress_callback:
                            progress_callback(index_name, completed, total, "empty", elapsed_seconds)
                    else:
                        timings.append(build_timing_row(index_name, index_config, "failed", elapsed_seconds, str(exc)))
                        errors.append(f"{index_name}: {exc}")
                        if progress_callback:
                            progress_callback(index_name, completed, total, "failed", elapsed_seconds)

        if not all_data:
            raise RuntimeError("未获取到任何指数数据。" + " | ".join(errors))

        report = merge_by_date(all_data)
        if is_partial_update and cached_df is not None and not cached_df.empty:
            report = merge_index_report(cached_df, report)
        report.attrs["errors"] = errors
        save_dataset(
            symbol="index_ma20_latest",
            name="指数MA20分列结果",
            source=cache_source,
            data_type="index_ma20_report",
            df=report,
        )

        if selected_indexes:
            message = f"{'、'.join(sorted(selected_indexes))}更新成功"
        elif selected_markets:
            message = f"{'、'.join(sorted(selected_markets))}更新成功"
        else:
            message = "更新成功"
        if errors:
            message += "；部分指数失败：" + " | ".join(errors)
        if timings:
            slowest = max(timings, key=lambda item: item["耗时(秒)"])
            message += f"；最慢：{slowest['指数']} {slowest['耗时(秒)']:.2f}秒"

        finish_job(job_id, "success", message)
        return UpdateResult("success", message, report, errors, timings)
    except Exception as exc:
        finish_job(job_id, "failed", str(exc))
        return UpdateResult("failed", f"更新失败：{exc}", errors=errors, timings=timings)


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


def merge_index_report(existing_df: pd.DataFrame, update_df: pd.DataFrame) -> pd.DataFrame:
    existing = existing_df.copy()
    update = update_df.copy()
    if existing.empty:
        return update
    if update.empty:
        return existing

    existing["日期"] = existing["日期"].astype(str)
    update["日期"] = update["日期"].astype(str)
    replaced_columns = [column for column in update.columns if column != "日期"]
    preserved = existing.drop(columns=[column for column in replaced_columns if column in existing.columns])
    merged = pd.merge(preserved, update, on="日期", how="outer")
    return merged.sort_values("日期").reset_index(drop=True)


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
    if cached_latest is None or refreshed_latest is None or refreshed_latest >= cached_latest:
        return refreshed_df
    return cached_index_df


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
    source_label = (
        "东方财富"
        if index_config.get("source") == "eastmoney_kline"
        else str(index_config.get("source", "上游数据源"))
    )
    latest_date = latest_index_trade_date(df, index_name)
    latest_text = latest_date.strftime("%Y-%m-%d") if latest_date is not None else "未知日期"

    expected_text = "预期交易日"
    market_name = index_config.get("market_group")
    market = next((item for item in MARKET_WINDOWS if item.name == market_name), None)
    if market is not None:
        market_now = datetime.now(ZoneInfo(market.timezone))
        expected_text = expected_latest_trade_date(market, market_now).strftime("%Y-%m-%d")

    return f"{index_name}: {source_label} 最新到 {latest_text}，预期 {expected_text}，{action_text}"


def has_current_index_quote(df: pd.DataFrame, index_name: str, index_config: dict) -> bool:
    if df is None or df.empty or not index_config.get("require_current_quote"):
        return True

    market_name = index_config.get("market_group")
    market = next((item for item in MARKET_WINDOWS if item.name == market_name), None)
    if market is None:
        return True
    market_now = datetime.now(ZoneInfo(market.timezone))
    expected_date = expected_latest_trade_date(market, market_now)

    latest_date = latest_index_trade_date(df, index_name)
    return not pd.isna(latest_date) and latest_date.date() >= expected_date


def fetch_index_report(index_name: str, index_config: dict, api_key: str, days: int) -> pd.DataFrame | None:
    df = None
    tickflow_symbol = index_config.get("tickflow_symbol") if isinstance(index_config, dict) else None
    if tickflow_symbol:
        try:
            cache_symbol = raw_cache_symbol(index_name, index_config)
            cached_raw, _ = load_dataset(
                cache_symbol,
                "tickflow",
                "index_daily_raw",
            )
            fetch_count = 30 if cached_raw is not None and not cached_raw.empty else max(days * 2, 80)
            latest_raw = get_index_raw_from_tickflow(
                api_key,
                tickflow_symbol,
                count=fetch_count,
            )
            if latest_raw is not None and not latest_raw.empty:
                raw_df = merge_raw_index_data(cached_raw, latest_raw)
                save_dataset(
                    symbol=cache_symbol,
                    name=f"{index_name} 指数原始日线",
                    source="tickflow",
                    data_type="index_daily_raw",
                    df=raw_df,
                )
                df = build_export_df(raw_df, index_name, days=days)
                if df is not None and not df.empty and not has_current_index_quote(df, index_name, index_config):
                    df = fetch_index_from_source(index_name, index_config, days=days)
        except Exception:
            df = None

    if df is None:
        df = fetch_one_index(index_name, index_config, api_key=api_key, days=days)
    return df
