from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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


ProgressCallback = Callable[[str, int, int, str], None]


@dataclass
class UpdateResult:
    status: str
    message: str
    dataframe: pd.DataFrame | None = None
    errors: list[str] = field(default_factory=list)


def run_index_ma20_update(
    api_key: str = "",
    days: int = 30,
    cache_source: str = "auto",
    use_fresh_cache: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> UpdateResult:
    job_id = start_job("更新指数MA20")
    all_data = []
    errors = []
    total = len(INDEX_CONFIG)

    try:
        if use_fresh_cache:
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

        for idx, (index_name, index_config) in enumerate(INDEX_CONFIG.items(), start=1):
            if progress_callback:
                progress_callback(index_name, idx, total, "running")

            try:
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

                if df is not None and not df.empty:
                    all_data.append(df)
                    if progress_callback:
                        progress_callback(index_name, idx, total, "success")
                else:
                    errors.append(f"{index_name}: 无数据")
                    if progress_callback:
                        progress_callback(index_name, idx, total, "empty")
            except Exception as exc:
                errors.append(f"{index_name}: {exc}")
                if progress_callback:
                    progress_callback(index_name, idx, total, "failed")

        if not all_data:
            raise RuntimeError("未获取到任何指数数据。" + " | ".join(errors))

        report = merge_by_date(all_data)
        report.attrs["errors"] = errors
        save_dataset(
            symbol="index_ma20_latest",
            name="指数MA20分列结果",
            source=cache_source,
            data_type="index_ma20_report",
            df=report,
        )

        message = "更新成功"
        if errors:
            message += "；部分指数失败：" + " | ".join(errors)

        finish_job(job_id, "success", message)
        return UpdateResult("success", message, report, errors)
    except Exception as exc:
        finish_job(job_id, "failed", str(exc))
        return UpdateResult("failed", f"更新失败：{exc}", errors=errors)
