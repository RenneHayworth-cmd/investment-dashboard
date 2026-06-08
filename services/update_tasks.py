from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Callable

import pandas as pd

from core.cache import load_dataset, save_dataset
from core.db import finish_job, start_job
from services.index_ma20 import (
    INDEX_CONFIG,
    build_export_df,
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
    max_workers: int = 4,
) -> UpdateResult:
    selected_markets = set(market_names or [])
    job_name = "更新指数MA20" if not selected_markets else f"更新指数MA20（{'、'.join(sorted(selected_markets))}）"
    job_id = start_job(job_name)
    all_data = []
    errors = []
    timings = []
    selected_items = [
        (index_name, index_config)
        for index_name, index_config in INDEX_CONFIG.items()
        if not selected_markets or index_config.get("market_group") in selected_markets
    ]
    total = len(selected_items)

    try:
        if not selected_items:
            raise RuntimeError(f"没有匹配的指数市场分组：{'、'.join(sorted(selected_markets))}")

        cached_df = None
        if use_fresh_cache and not selected_markets:
            cached_df, meta = load_dataset(
                "index_ma20_latest",
                cache_source,
                "index_ma20_report",
            )
            last_update_time = (meta or {}).get("last_update_time")
            if cached_df is not None and last_update_time:
                last_update_date = datetime.fromisoformat(last_update_time).date()
                if last_update_date == datetime.now().date():
                    message = f"使用今日缓存，更新时间：{last_update_time}"
                    finish_job(job_id, "success", message)
                    return UpdateResult("success", message, cached_df)
        elif selected_markets:
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
                        all_data.append(df)
                        timings.append(build_timing_row(index_name, index_config, "success", elapsed_seconds))
                        if progress_callback:
                            progress_callback(index_name, completed, total, "success", elapsed_seconds)
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
                        all_data.append(cached_index_df)
                        timings.append(build_timing_row(index_name, index_config, "cached", elapsed_seconds, str(exc)))
                        if progress_callback:
                            progress_callback(index_name, completed, total, "cached", elapsed_seconds)
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
        if selected_markets and cached_df is not None and not cached_df.empty:
            report = merge_index_report(cached_df, report)
        report.attrs["errors"] = errors
        save_dataset(
            symbol="index_ma20_latest",
            name="指数MA20分列结果",
            source=cache_source,
            data_type="index_ma20_report",
            df=report,
        )

        message = "更新成功" if not selected_markets else f"{'、'.join(sorted(selected_markets))}更新成功"
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
        except Exception:
            df = None

    if df is None:
        df = fetch_one_index(index_name, index_config, api_key=api_key, days=days)
    return df
