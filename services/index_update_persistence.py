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
    latest_index_trade_date, merge_index_report,
)

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

def futures_contract_history_cache_symbol(contract: str) -> str:
    return f"index_futures_contract_{str(contract).strip().upper()}"

def load_futures_main_contract_mapping() -> dict[str, str]:
    cached, _ = load_dataset(
        FUTURES_MAIN_CONTRACT_CACHE_SYMBOL,
        FUTURES_MAIN_CONTRACT_CACHE_SOURCE,
        "futures_main_contracts",
    )
    if cached is None or cached.empty or not {"index_name", "contract"}.issubset(cached.columns):
        return {}
    return {
        str(row["index_name"]): str(row["contract"]).strip().upper()
        for _, row in cached.dropna(subset=["index_name", "contract"]).iterrows()
        if str(row["contract"]).strip()
    }

def load_futures_current_contract_history(index_name: str) -> tuple[str | None, pd.DataFrame | None]:
    if index_name not in FUTURES_MAIN_INDEX_NAMES:
        return None, None
    contract = load_futures_main_contract_mapping().get(index_name)
    if not contract:
        return None, None
    cached, _ = load_dataset(
        futures_contract_history_cache_symbol(contract),
        FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE,
        "index_daily_raw",
    )
    return contract, merge_raw_index_data(None, cached) if cached is not None and not cached.empty else None

def _fetch_and_cache_futures_contract_history(
    index_name: str,
    contract: str,
    *,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    from services.futures_spread import fetch_futures_daily_from_akshare

    cache_symbol = futures_contract_history_cache_symbol(contract)
    cached, _ = load_dataset(
        cache_symbol,
        FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE,
        "index_daily_raw",
    )
    market = next((item for item in MARKET_WINDOWS if item.name == "A股"), None)
    market_now = (
        market_now.astimezone(ZoneInfo(market.timezone))
        if market is not None and market_now is not None
        else datetime.now(ZoneInfo(market.timezone))
        if market is not None
        else None
    )
    target_date = latest_settled_trade_date(market, market_now) if market is not None else None
    normalized_cached = merge_raw_index_data(None, cached) if cached is not None and not cached.empty else None
    if normalized_cached is not None and not normalized_cached.empty and target_date is not None:
        latest_cached = pd.to_datetime(normalized_cached["trade_date"], errors="coerce").max()
        if pd.notna(latest_cached) and latest_cached.date() >= target_date:
            return normalized_cached

    fetched = fetch_futures_daily_from_akshare(contract).rename(columns={"date": "trade_date"})
    fetched = filter_completed_market_dates(fetched, "A股")
    if fetched is not None and not fetched.empty and target_date is not None:
        fetched_dates = pd.to_datetime(fetched["trade_date"], errors="coerce")
        fetched = fetched.loc[fetched_dates.dt.date <= target_date].reset_index(drop=True)
    if fetched is None or fetched.empty:
        target_text = target_date.isoformat() if target_date is not None else "-"
        raise RuntimeError(f"{contract} 未返回截至 {target_text} 的已确认收盘数据")
    merged = append_cached_index_rows(normalized_cached, fetched)
    if normalized_cached is None or len(merged) > len(normalized_cached):
        save_dataset(
            symbol=cache_symbol,
            name=f"{index_name}（{contract}）正式日线",
            source=FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE,
            data_type="index_daily_raw",
            df=merged,
        )
    return merged

def refresh_futures_current_contract_histories(
    contract_names: dict[str, str],
    max_workers: int = 4,
    *,
    market_now: datetime | None = None,
) -> list[str]:
    selected = {
        name: str(contract).strip().upper()
        for name, contract in contract_names.items()
        if name in FUTURES_MAIN_INDEX_NAMES and str(contract).strip()
    }
    if not selected:
        return []

    errors: list[str] = []
    workers = min(max(int(max_workers), 1), len(selected))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _fetch_and_cache_futures_contract_history,
                name,
                contract,
                market_now=market_now,
            ): (name, contract)
            for name, contract in selected.items()
        }
        for future in as_completed(futures):
            name, contract = futures[future]
            try:
                future.result()
            except Exception as exc:
                errors.append(f"{name}（{contract}）：{str(exc).strip() or type(exc).__name__}")
    return errors

def find_pending_futures_current_contract_index_names(
    *,
    market_now: datetime | None = None,
    index_names: set[str] | list[str] | tuple[str, ...] | None = None,
) -> set[str]:
    """Return mapped futures indexes whose concrete-contract close is not settled locally."""
    market = next((item for item in MARKET_WINDOWS if item.name == "A股"), None)
    if market is None:
        return set()
    current = (
        market_now.astimezone(ZoneInfo(market.timezone))
        if market_now is not None
        else datetime.now(ZoneInfo(market.timezone))
    )
    target_date = latest_settled_trade_date(market, current)
    selected = set(index_names) if index_names is not None else set(FUTURES_MAIN_INDEX_NAMES)
    mapping = load_futures_main_contract_mapping()
    pending: set[str] = set()
    for index_name, contract in mapping.items():
        if index_name not in selected or index_name not in FUTURES_MAIN_INDEX_NAMES:
            continue
        cached, _ = load_dataset(
            futures_contract_history_cache_symbol(contract),
            FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE,
            "index_daily_raw",
        )
        latest_date = pd.NaT
        if cached is not None and not cached.empty and "trade_date" in cached.columns:
            latest_date = pd.to_datetime(cached["trade_date"], errors="coerce").max()
        if pd.isna(latest_date) or latest_date.date() < target_date:
            pending.add(index_name)
    return pending

def build_futures_current_contract_report(
    index_name: str,
    report_raw: pd.DataFrame | None,
    *,
    days: int = INDEX_REPORT_DISPLAY_DAYS,
) -> pd.DataFrame | None:
    _, contract_raw = load_futures_current_contract_history(index_name)
    if contract_raw is None or len(contract_raw) < 20:
        return None
    if report_raw is not None and not report_raw.empty:
        report_latest = pd.to_datetime(report_raw["trade_date"], errors="coerce").max()
        contract_latest = pd.to_datetime(contract_raw["trade_date"], errors="coerce").max()
        if pd.notna(report_latest) and (pd.isna(contract_latest) or contract_latest < report_latest):
            return None
    report = build_export_df(contract_raw, index_name, days=days)
    if report is None or report.empty:
        return report

    # EastMoney's futures chart formats the unrounded rolling mean directly.
    # NumPy's vectorized rounding differs by 0.01 for values such as 708.675.
    history = contract_raw[["trade_date", "close"]].copy()
    history["trade_date"] = pd.to_datetime(history["trade_date"], errors="coerce").dt.normalize()
    history["close"] = pd.to_numeric(history["close"], errors="coerce")
    history = history.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
    history["MA20"] = history["close"].rolling(window=20).mean()
    ma20_by_date = history.set_index("trade_date")["MA20"]
    report_dates = pd.to_datetime(report["日期"], errors="coerce").dt.normalize()
    report[f"{index_name}_MA20"] = report_dates.map(ma20_by_date).map(
        lambda value: float(f"{float(value):.2f}") if pd.notna(value) else pd.NA
    )
    return report

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
        report_raw = extract_raw_from_export_df(enriched, index_name)
        if report_raw is None or report_raw.empty:
            continue
        current_contract_report = build_futures_current_contract_report(
            index_name,
            report_raw,
            days=INDEX_REPORT_DISPLAY_DAYS,
        )
        if current_contract_report is not None and not current_contract_report.empty:
            enriched = merge_index_report(
                enriched,
                current_contract_report,
                prefer_update_index_names={index_name},
            )
            continue

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

__all__ = ['append_cached_index_rows', 'futures_contract_history_cache_symbol', 'load_futures_main_contract_mapping', 'load_futures_current_contract_history', '_fetch_and_cache_futures_contract_history', 'refresh_futures_current_contract_histories', 'find_pending_futures_current_contract_index_names', 'build_futures_current_contract_report', 'sync_index_long_history', 'enrich_index_report_indicators', 'refresh_cached_eastmoney_index_report', 'persist_confirmed_index_report_row']
