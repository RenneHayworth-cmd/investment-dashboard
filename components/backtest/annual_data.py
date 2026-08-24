from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .annual_config import AUDIT_MARKET_DIR, DIRECTION_LABELS, ROOT


def date_column(frame: pd.DataFrame) -> str | None:
    for column in ("日期", "trade_date", "date", "datetime"):
        if column in frame.columns:
            return column
    return None


def completed_a_share_date(namespace):
    market = namespace["get_market_window"]("A股")
    if market is None:
        return pd.Timestamp.today().date()
    now = namespace["datetime"].now(namespace["ZoneInfo"]("Asia/Shanghai"))
    return namespace["latest_completed_trade_date"](market, now)


def filter_completed_rows(
    frame: pd.DataFrame,
    completed_date,
    *,
    date_column_fn=date_column,
) -> pd.DataFrame:
    column = date_column_fn(frame)
    if column is None:
        raise ValueError("正式日线缺少日期列。")
    dates = pd.to_datetime(frame[column], errors="coerce").dt.date
    return frame.loc[dates <= completed_date].copy().reset_index(drop=True)


def append_unseen_dates(
    existing: pd.DataFrame | None,
    fetched: pd.DataFrame,
    *,
    date_column_fn=date_column,
) -> pd.DataFrame:
    if existing is None or existing.empty:
        return fetched.copy()
    old_column = date_column_fn(existing)
    new_column = date_column_fn(fetched)
    if old_column is None or new_column is None:
        raise ValueError("正式日线缺少日期列，拒绝覆盖原缓存。")
    old = existing.copy()
    new = fetched.copy()
    old["_merge_date"] = pd.to_datetime(
        old[old_column], errors="coerce"
    ).dt.normalize()
    new["_merge_date"] = pd.to_datetime(
        new[new_column], errors="coerce"
    ).dt.normalize()
    old_dates = set(old["_merge_date"].dropna())
    new = new[~new["_merge_date"].isin(old_dates)]
    return (
        pd.concat([old, new], ignore_index=True, sort=False)
        .sort_values("_merge_date")
        .drop(columns="_merge_date")
        .reset_index(drop=True)
    )


def append_dividends(
    existing: pd.DataFrame | None,
    fetched: pd.DataFrame,
) -> pd.DataFrame:
    if existing is None or existing.empty:
        return fetched.drop_duplicates().reset_index(drop=True)
    return (
        pd.concat([existing, fetched], ignore_index=True, sort=False)
        .drop_duplicates(keep="first")
        .reset_index(drop=True)
    )


def read_raw_fallback(
    record,
    *,
    audit_market_dir=AUDIT_MARKET_DIR,
    root=ROOT,
) -> tuple[pd.DataFrame | None, str]:
    audit_path = audit_market_dir / f"{record.symbol}_raw.csv"
    if audit_path.exists():
        return pd.read_csv(audit_path), "既有ETF审计未复权缓存"
    exchange = record.exchange.upper()
    patterns = (
        f"fund_close_v2_{record.symbol}.{exchange}_none_*_1d.csv",
        f"fund_close_{record.symbol}.{exchange}_none_*_1d.csv",
    )
    for pattern in patterns:
        matches = sorted((root / "data" / "raw" / "tickflow").glob(pattern))
        if matches:
            return pd.read_csv(matches[-1]), "既有TickFlow未复权缓存"
    return None, ""


def load_dividends(namespace) -> tuple[pd.DataFrame, str]:
    cached, meta = namespace["load_dataset"](
        namespace["ANNUAL_DIVIDEND_CACHE_KEY"],
        namespace["ANNUAL_CACHE_SOURCE"],
        namespace["ANNUAL_DIVIDEND_DATA_TYPE"],
        namespace["ANNUAL_CACHE_PERIOD"],
    )
    if cached is not None:
        timestamp = (meta or {}).get("last_update_time", "")
        return cached, f"年度缓存 {str(timestamp).replace('T', ' ')}"
    fallback = namespace["AUDIT_MARKET_DIR"] / "official_dividends.csv"
    if fallback.exists():
        return pd.read_csv(fallback), "既有ETF审计分红缓存"
    return pd.DataFrame(), "缺少分红缓存"


def load_proxy_data(records, completed_date, namespace):
    proxy_data: dict[str, pd.DataFrame] = {}
    rows = []
    for record in records:
        if not record.proxy_path:
            continue
        path = namespace["Path"](record.proxy_path)
        if not path.is_absolute():
            path = namespace["ROOT"] / path
        try:
            raw = pd.read_csv(path)
            raw = namespace["_filter_completed_rows"](raw, completed_date)
            proxy_data[record.symbol] = namespace["normalize_annual_market_data"](raw)
            rows.append(
                {
                    "代码": f"{record.symbol}代理",
                    "名称": record.tracked_index,
                    "方向": namespace["DIRECTION_LABELS"].get(
                        record.direction, record.direction
                    ),
                    "状态": "可读取",
                    "来源": str(path),
                    "行数": len(proxy_data[record.symbol]),
                    "首日": proxy_data[record.symbol]["trade_date"].min(),
                    "末日": proxy_data[record.symbol]["trade_date"].max(),
                    "错误": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "代码": f"{record.symbol}代理",
                    "名称": record.tracked_index,
                    "方向": namespace["DIRECTION_LABELS"].get(
                        record.direction, record.direction
                    ),
                    "状态": "缺口",
                    "来源": str(path),
                    "行数": 0,
                    "首日": pd.NaT,
                    "末日": pd.NaT,
                    "错误": str(exc),
                }
            )
    return proxy_data, pd.DataFrame(rows)


def load_market_bundle(records, whitelist, completed_date, namespace):
    dividends, dividend_source = namespace["_load_dividends"]()
    market_data: dict[str, pd.DataFrame] = {}
    rows = []
    for record in records:
        raw, _meta = namespace["load_dataset"](
            namespace["annual_raw_cache_key"](record.symbol),
            namespace["ANNUAL_CACHE_SOURCE"],
            namespace["ANNUAL_RAW_DATA_TYPE"],
            namespace["ANNUAL_CACHE_PERIOD"],
        )
        source = "年度专用缓存"
        if raw is None:
            raw, source = namespace["_read_raw_fallback"](record)
        try:
            if raw is None or raw.empty:
                raise ValueError("缺少未复权正式日线")
            raw = namespace["_filter_completed_rows"](raw, completed_date)
            normalized = namespace["normalize_annual_market_data"](
                raw,
                namespace["dividends_for_symbol"](dividends, record.symbol),
                namespace["share_splits_for_symbol"](whitelist, record.symbol),
            )
            market_data[record.symbol] = normalized
            rows.append(
                {
                    "代码": record.symbol,
                    "名称": record.name,
                    "方向": namespace["DIRECTION_LABELS"].get(
                        record.direction, record.direction
                    ),
                    "状态": "可读取",
                    "来源": source,
                    "行数": len(normalized),
                    "首日": normalized["trade_date"].min(),
                    "末日": normalized["trade_date"].max(),
                    "错误": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "代码": record.symbol,
                    "名称": record.name,
                    "方向": namespace["DIRECTION_LABELS"].get(
                        record.direction, record.direction
                    ),
                    "状态": "缺口",
                    "来源": source,
                    "行数": 0,
                    "首日": pd.NaT,
                    "末日": pd.NaT,
                    "错误": str(exc),
                }
            )
    return market_data, pd.DataFrame(rows), dividends, dividend_source


def network_fill(
    records,
    completed_date,
    start_year: int,
    refresh: bool,
    batch_size: int,
    namespace,
):
    rows = []
    candidates = []
    for record in records:
        cached, _meta = namespace["load_dataset"](
            namespace["annual_raw_cache_key"](record.symbol),
            namespace["ANNUAL_CACHE_SOURCE"],
            namespace["ANNUAL_RAW_DATA_TYPE"],
            namespace["ANNUAL_CACHE_PERIOD"],
        )
        needs_history = cached is None or cached.empty
        if not needs_history and refresh:
            column = namespace["_date_column"](cached)
            latest = (
                pd.to_datetime(cached[column], errors="coerce").max().date()
                if column is not None
                and pd.notna(pd.to_datetime(cached[column], errors="coerce").max())
                else None
            )
            needs_history = latest is None or latest < completed_date
        if needs_history:
            candidates.append(record)
    for record in candidates[:batch_size]:
        try:
            fetched = namespace["fetch_annual_etf_raw_history"](
                record,
                start_date="20000101",
                end_date=pd.Timestamp(completed_date).strftime("%Y%m%d"),
            )
            fetched = namespace["_filter_completed_rows"](fetched, completed_date)
            existing, _meta = namespace["load_dataset"](
                namespace["annual_raw_cache_key"](record.symbol),
                namespace["ANNUAL_CACHE_SOURCE"],
                namespace["ANNUAL_RAW_DATA_TYPE"],
                namespace["ANNUAL_CACHE_PERIOD"],
            )
            merged = namespace["_append_unseen_dates"](existing, fetched)
            namespace["normalize_annual_market_data"](merged)
            namespace["save_dataset"](
                namespace["annual_raw_cache_key"](record.symbol),
                record.name,
                namespace["ANNUAL_CACHE_SOURCE"],
                namespace["ANNUAL_RAW_DATA_TYPE"],
                merged,
                namespace["ANNUAL_CACHE_PERIOD"],
            )
            rows.append({"代码": record.symbol, "状态": "已补齐", "错误": ""})
        except Exception as exc:
            rows.append(
                {"代码": record.symbol, "状态": "失败，保留原缓存", "错误": str(exc)}
            )

    dividends, _meta = namespace["load_dataset"](
        namespace["ANNUAL_DIVIDEND_CACHE_KEY"],
        namespace["ANNUAL_CACHE_SOURCE"],
        namespace["ANNUAL_DIVIDEND_DATA_TYPE"],
        namespace["ANNUAL_CACHE_PERIOD"],
    )
    dividend_years = []
    if dividends is None or dividends.empty:
        dividend_years = list(
            range(max(2005, start_year - 5), pd.Timestamp(completed_date).year + 1)
        )
    elif refresh:
        dividend_years = [pd.Timestamp(completed_date).year]
    if dividend_years:
        parts = []
        for year in dividend_years:
            try:
                parts.append(namespace["fetch_annual_dividends"](year))
            except Exception as exc:
                rows.append(
                    {
                        "代码": f"分红{year}",
                        "状态": "失败，保留原缓存",
                        "错误": str(exc),
                    }
                )
        if parts:
            merged = namespace["_append_dividends"](
                dividends, pd.concat(parts, ignore_index=True, sort=False)
            )
            namespace["save_dataset"](
                namespace["ANNUAL_DIVIDEND_CACHE_KEY"],
                "年度ETF官方分红",
                namespace["ANNUAL_CACHE_SOURCE"],
                namespace["ANNUAL_DIVIDEND_DATA_TYPE"],
                merged,
                namespace["ANNUAL_CACHE_PERIOD"],
            )
    return pd.DataFrame(rows), max(
        0, len(candidates) - min(len(candidates), batch_size)
    )


def qualification_summary(
    frame: pd.DataFrame,
    *,
    direction_labels=DIRECTION_LABELS,
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    summary = (
        frame.groupby(["year", "direction"], as_index=False)
        .agg(
            注册表候选数=("symbol", "size"),
            初筛合格数=("qualified_before_index_dedup", "sum"),
            代表ETF数=("qualified", "sum"),
            最高代理占比=("proxy_ratio_pct", "max"),
        )
        .rename(columns={"year": "年度", "direction": "方向"})
    )
    summary["方向"] = summary["方向"].map(direction_labels).fillna(summary["方向"])
    return summary


__all__ = [
    "append_dividends",
    "append_unseen_dates",
    "completed_a_share_date",
    "date_column",
    "filter_completed_rows",
    "load_dividends",
    "load_market_bundle",
    "load_proxy_data",
    "network_fill",
    "qualification_summary",
    "read_raw_fallback",
]
