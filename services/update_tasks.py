from __future__ import annotations

# Compatibility facade: validation, persistence and orchestration live in siblings.
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import time
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import load_dataset, save_dataset
from core.db import finish_job, start_job
from services import index_update_frames as _frames
from services import index_update_orchestration as _orchestration
from services import index_update_persistence as _persistence
from services import index_update_validation as _validation
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
from services.market_calendar import MARKET_WINDOWS, latest_settled_trade_date

# Preserve the legacy type identity shown in reports and introspection.
UpdateResult.__module__ = __name__

_MODULES = (_validation, _frames, _persistence, _orchestration)
_OWNER = {
    '_verification_report': _validation,
    'verify_updated_index_data': _validation,
    'build_index_update_message': _frames,
    'build_timing_row': _frames,
    'cached_report_satisfies_current_quotes': _frames,
    'merge_index_report': _frames,
    'trim_index_report': _frames,
    'extract_cached_index_report': _frames,
    'latest_index_trade_date': _frames,
    'build_stale_quote_message': _frames,
    'has_current_index_quote': _frames,
    'append_cached_index_rows': _persistence,
    'futures_contract_history_cache_symbol': _persistence,
    'load_futures_main_contract_mapping': _persistence,
    'load_futures_current_contract_history': _persistence,
    '_fetch_and_cache_futures_contract_history': _persistence,
    'refresh_futures_current_contract_histories': _persistence,
    'find_pending_futures_current_contract_index_names': _persistence,
    'build_futures_current_contract_report': _persistence,
    'sync_index_long_history': _persistence,
    'enrich_index_report_indicators': _persistence,
    'refresh_cached_eastmoney_index_report': _persistence,
    'persist_confirmed_index_report_row': _persistence,
    'run_index_ma20_update': _orchestration,
    'fetch_index_report': _orchestration,
}
_IMPLEMENTATIONS = {name: getattr(module, name) for name, module in _OWNER.items()}
_SYNC_NAMES = {
    *list(_IMPLEMENTATIONS),
    "ThreadPoolExecutor", "as_completed", "date", "datetime", "timedelta",
    "time", "ZoneInfo", "pd", "load_dataset", "save_dataset",
    "finish_job", "start_job", "MARKET_WINDOWS", "latest_settled_trade_date",
    "INDEX_CONFIG", "INDEX_FINAL_HISTORY_SOURCE", "INDEX_LONG_HISTORY_SOURCE",
    "INDEX_REPORT_DISPLAY_DAYS", "INDEX_SOURCE_CORRECTION_SOURCE",
    "append_eastmoney_latest_index_row", "build_export_df",
    "extract_raw_from_export_df", "extract_source_correction_rows",
    "fetch_index_from_source", "fetch_one_index", "filter_completed_market_dates",
    "get_index_data_from_yahoo", "get_index_raw_from_tickflow", "merge_by_date",
    "merge_raw_index_data", "missing_recent_market_trade_dates",
    "overlay_finalized_index_rows", "raw_cache_symbol",
    "sanitize_index_report_market_dates", "source_correction_fetch_days",
    "FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE", "FUTURES_MAIN_CONTRACT_CACHE_SOURCE",
    "FUTURES_MAIN_CONTRACT_CACHE_SYMBOL", "FUTURES_MAIN_INDEX_NAMES",
    "INDEX_HISTORY_BOOTSTRAP_BARS", "INDEX_HISTORY_BOOTSTRAP_DAYS",
    "INDEX_HISTORY_INCREMENTAL_DAYS", "INDEX_HISTORY_MIN_ROWS",
    "INDEX_VERIFICATION_TOLERANCE_PCT", "ProgressCallback", "UpdateResult",
}

def _dispatch(name: str, /, *args, **kwargs):
    namespace = globals()
    for module in _MODULES:
        module_namespace = module.__dict__
        for dependency in _SYNC_NAMES:
            if dependency == name:
                continue
            if dependency in namespace:
                module_namespace[dependency] = namespace[dependency]
    owner = _OWNER[name]
    implementation = _IMPLEMENTATIONS[name]
    owner.__dict__[name] = implementation
    return implementation(*args, **kwargs)

def _verification_report(index_name: str, index_config: dict, *, api_key: str, days: int) -> tuple[pd.DataFrame | None, str]:
    return _dispatch('_verification_report', index_name, index_config, api_key=api_key, days=days)

def verify_updated_index_data(index_names: set[str] | list[str] | tuple[str, ...], target_dates: dict[str, date | str], *, api_key: str = '', max_workers: int = 4, tolerance_pct: float = INDEX_VERIFICATION_TOLERANCE_PCT) -> pd.DataFrame:
    return _dispatch('verify_updated_index_data', index_names, target_dates, api_key=api_key, max_workers=max_workers, tolerance_pct=tolerance_pct)

def run_index_ma20_update(api_key: str = '', days: int = INDEX_REPORT_DISPLAY_DAYS, cache_source: str = 'auto', use_fresh_cache: bool = True, progress_callback: ProgressCallback | None = None, market_names: set[str] | list[str] | tuple[str, ...] | None = None, index_names: set[str] | list[str] | tuple[str, ...] | None = None, max_workers: int = 4) -> UpdateResult:
    return _dispatch('run_index_ma20_update', api_key, days, cache_source, use_fresh_cache, progress_callback, market_names, index_names, max_workers)

def build_index_update_message(*, selected_indexes: set[str], selected_markets: set[str], timings: list[dict], errors: list[str]) -> str:
    return _dispatch('build_index_update_message', selected_indexes=selected_indexes, selected_markets=selected_markets, timings=timings, errors=errors)

def build_timing_row(index_name: str, index_config: dict, status: str, elapsed_seconds: float, message: str = '') -> dict:
    return _dispatch('build_timing_row', index_name, index_config, status, elapsed_seconds, message)

def cached_report_satisfies_current_quotes(cached_df: pd.DataFrame | None, selected_items: list[tuple[str, dict]]) -> bool:
    return _dispatch('cached_report_satisfies_current_quotes', cached_df, selected_items)

def merge_index_report(existing_df: pd.DataFrame, update_df: pd.DataFrame, prefer_update_index_names: set[str] | None = None) -> pd.DataFrame:
    return _dispatch('merge_index_report', existing_df, update_df, prefer_update_index_names)

def trim_index_report(report_df: pd.DataFrame, days: int = INDEX_REPORT_DISPLAY_DAYS) -> pd.DataFrame:
    return _dispatch('trim_index_report', report_df, days)

def append_cached_index_rows(old_df: pd.DataFrame | None, new_df: pd.DataFrame) -> pd.DataFrame:
    return _dispatch('append_cached_index_rows', old_df, new_df)

def futures_contract_history_cache_symbol(contract: str) -> str:
    return _dispatch('futures_contract_history_cache_symbol', contract)

def load_futures_main_contract_mapping() -> dict[str, str]:
    return _dispatch('load_futures_main_contract_mapping')

def load_futures_current_contract_history(index_name: str) -> tuple[str | None, pd.DataFrame | None]:
    return _dispatch('load_futures_current_contract_history', index_name)

def _fetch_and_cache_futures_contract_history(index_name: str, contract: str, *, market_now: datetime | None = None) -> pd.DataFrame:
    return _dispatch('_fetch_and_cache_futures_contract_history', index_name, contract, market_now=market_now)

def refresh_futures_current_contract_histories(contract_names: dict[str, str], max_workers: int = 4, *, market_now: datetime | None = None) -> list[str]:
    return _dispatch('refresh_futures_current_contract_histories', contract_names, max_workers, market_now=market_now)

def find_pending_futures_current_contract_index_names(*, market_now: datetime | None = None, index_names: set[str] | list[str] | tuple[str, ...] | None = None) -> set[str]:
    return _dispatch('find_pending_futures_current_contract_index_names', market_now=market_now, index_names=index_names)

def build_futures_current_contract_report(index_name: str, report_raw: pd.DataFrame | None, *, days: int = INDEX_REPORT_DISPLAY_DAYS) -> pd.DataFrame | None:
    return _dispatch('build_futures_current_contract_report', index_name, report_raw, days=days)

def sync_index_long_history(cache_symbol: str, index_name: str, new_df: pd.DataFrame) -> None:
    return _dispatch('sync_index_long_history', cache_symbol, index_name, new_df)

def enrich_index_report_indicators(report_df: pd.DataFrame) -> pd.DataFrame:
    return _dispatch('enrich_index_report_indicators', report_df)

def extract_cached_index_report(cached_df: pd.DataFrame | None, index_name: str) -> pd.DataFrame | None:
    return _dispatch('extract_cached_index_report', cached_df, index_name)

def refresh_cached_eastmoney_index_report(cached_index_df: pd.DataFrame, index_name: str, index_config: dict, days: int) -> pd.DataFrame:
    return _dispatch('refresh_cached_eastmoney_index_report', cached_index_df, index_name, index_config, days)

def persist_confirmed_index_report_row(index_name: str, index_config: dict, report_df: pd.DataFrame) -> None:
    return _dispatch('persist_confirmed_index_report_row', index_name, index_config, report_df)

def latest_index_trade_date(df: pd.DataFrame | None, index_name: str) -> pd.Timestamp | None:
    return _dispatch('latest_index_trade_date', df, index_name)

def build_stale_quote_message(index_name: str, index_config: dict, df: pd.DataFrame, action_text: str) -> str:
    return _dispatch('build_stale_quote_message', index_name, index_config, df, action_text)

def has_current_index_quote(df: pd.DataFrame, index_name: str, index_config: dict) -> bool:
    return _dispatch('has_current_index_quote', df, index_name, index_config)

def fetch_index_report(index_name: str, index_config: dict, api_key: str, days: int, cached_report: pd.DataFrame | None = None) -> pd.DataFrame | None:
    return _dispatch('fetch_index_report', index_name, index_config, api_key, days, cached_report)

__all__ = ['ProgressCallback', 'INDEX_HISTORY_BOOTSTRAP_DAYS', 'INDEX_HISTORY_BOOTSTRAP_BARS', 'INDEX_HISTORY_MIN_ROWS', 'INDEX_HISTORY_INCREMENTAL_DAYS', 'INDEX_VERIFICATION_TOLERANCE_PCT', 'FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE', 'FUTURES_MAIN_CONTRACT_CACHE_SYMBOL', 'FUTURES_MAIN_CONTRACT_CACHE_SOURCE', 'FUTURES_MAIN_INDEX_NAMES', 'UpdateResult', '_verification_report', 'verify_updated_index_data', 'run_index_ma20_update', 'build_index_update_message', 'build_timing_row', 'cached_report_satisfies_current_quotes', 'merge_index_report', 'trim_index_report', 'append_cached_index_rows', 'futures_contract_history_cache_symbol', 'load_futures_main_contract_mapping', 'load_futures_current_contract_history', '_fetch_and_cache_futures_contract_history', 'refresh_futures_current_contract_histories', 'find_pending_futures_current_contract_index_names', 'build_futures_current_contract_report', 'sync_index_long_history', 'enrich_index_report_indicators', 'extract_cached_index_report', 'refresh_cached_eastmoney_index_report', 'persist_confirmed_index_report_row', 'latest_index_trade_date', 'build_stale_quote_message', 'has_current_index_quote', 'fetch_index_report']
