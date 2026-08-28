from __future__ import annotations

# Compatibility facade: implementations live in focused sibling modules.
from datetime import datetime, time, timedelta, timezone
from threading import BoundedSemaphore
from zoneinfo import ZoneInfo

import pandas as pd

from services import index_frames as _frames
from services import index_history as _history
from services import index_signals as _signals
from services import index_source_router as _source_router
from services import index_sources_akshare as _akshare
from services import index_sources_eastmoney as _eastmoney
from services import index_sources_tickflow as _tickflow
from services import index_sources_yahoo as _yahoo
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

_MODULES = (
    _frames,
    _signals,
    _yahoo,
    _eastmoney,
    _akshare,
    _tickflow,
    _source_router,
    _history,
)
_OWNER = {
    'overlay_finalized_index_rows': _frames,
    'missing_recent_market_trade_dates': _frames,
    'source_correction_start': _frames,
    'extract_source_correction_rows': _frames,
    'source_correction_fetch_days': _frames,
    'filter_market_trading_dates': _frames,
    'filter_completed_market_dates': _frames,
    'sanitize_index_report_market_dates': _signals,
    'build_export_df': _frames,
    'normalize_akshare_index_df': _frames,
    'extract_raw_from_export_df': _frames,
    'merge_newer_index_rows': _frames,
    'is_sparse_daily_history': _frames,
    '_append_unseen_raw_history': _frames,
    '_latest_completed_date_for_market': _frames,
    '_latest_raw_date': _frames,
    'merge_raw_index_data': _frames,
    'raw_cache_symbol': _frames,
    'display_index_symbol': _frames,
    'merge_by_date': _frames,
    'build_summary': _signals,
    'calculate_ma20_transition': _signals,
    'calculate_ma20_transition_snapshot': _signals,
    'calculate_ma20_transition_history': _signals,
    'fetch_yahoo_chart_payload': _yahoo,
    'fetch_yahoo_latest_index_row': _yahoo,
    'supplement_stale_yahoo_history': _yahoo,
    'append_akshare_latest_index_row': _akshare,
    'append_eastmoney_quote_row': _eastmoney,
    'fetch_eastmoney_clist_latest_index_row': _eastmoney,
    'append_eastmoney_clist_latest_index_row': _eastmoney,
    'append_eastmoney_latest_index_row': _eastmoney,
    'append_hk_index_spot_row': _akshare,
    'append_futures_spot_row': _akshare,
    'get_index_data_from_akshare_csindex': _akshare,
    'get_index_data_from_akshare_cn': _akshare,
    'get_index_data_from_akshare_cni': _akshare,
    'get_index_data_from_akshare_us': _akshare,
    'get_index_data_from_akshare_hk': _akshare,
    'get_index_data_from_yahoo': _yahoo,
    'get_index_data_from_eastmoney_kline': _eastmoney,
    'get_index_data_from_akshare_eastmoney_fallback': _eastmoney,
    'fetch_eastmoney_completed_global_row': _eastmoney,
    'get_index_data_from_cboe_vix': _akshare,
    'get_index_data_from_akshare_global': _akshare,
    'get_index_data_from_akshare_futures_main': _akshare,
    'fetch_index_from_source': _source_router,
    'get_index_data_from_tickflow': _tickflow,
    'tickflow_quote_date': _tickflow,
    'append_tickflow_quote_row': _tickflow,
    'normalize_tickflow_index_df': _tickflow,
    'get_index_raw_from_tickflow': _tickflow,
    'fetch_index_history': _history,
    'generate_index_ma20_report': _history,
    'fetch_one_index': _history,
}
_IMPLEMENTATIONS = {name: getattr(module, name) for name, module in _OWNER.items()}
_SYNC_NAMES = {
    *list(_IMPLEMENTATIONS),
    "datetime", "time", "timedelta", "timezone", "ZoneInfo", "pd",
    "expected_latest_trade_date", "get_market_window", "is_market_holiday",
    "is_market_trading_day", "latest_completed_trade_date",
    "latest_settled_trade_date", "INDEX_CONFIG", "INDEX_FINAL_HISTORY_SOURCE",
    "INDEX_LONG_HISTORY_BARS", "INDEX_LONG_HISTORY_SOURCE",
    "INDEX_RECENT_GAP_LOOKBACK_SESSIONS", "INDEX_REPORT_DISPLAY_DAYS",
    "INDEX_SOURCE_CORRECTION_SOURCE", "CFFEX_FUTURES_MAIN_PRODUCTS",
    "YAHOO_CHART_HOSTS", "YAHOO_REQUEST_GATE",
}

def _dispatch(name: str, /, *args, **kwargs):
    # Keep legacy patch targets effective while normal calls use the new owners.
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

def overlay_finalized_index_rows(history_df: pd.DataFrame | None, finalized_df: pd.DataFrame | None) -> pd.DataFrame | None:
    return _dispatch('overlay_finalized_index_rows', history_df, finalized_df)

def missing_recent_market_trade_dates(history_df: pd.DataFrame | None, market_name: str, target_date, *, lookback_sessions: int = INDEX_RECENT_GAP_LOOKBACK_SESSIONS) -> list:
    return _dispatch('missing_recent_market_trade_dates', history_df, market_name, target_date, lookback_sessions=lookback_sessions)

def source_correction_start(index_config: dict | None) -> pd.Timestamp | None:
    return _dispatch('source_correction_start', index_config)

def extract_source_correction_rows(df: pd.DataFrame | None, index_config: dict | None) -> pd.DataFrame | None:
    return _dispatch('extract_source_correction_rows', df, index_config)

def source_correction_fetch_days(index_config: dict | None, correction_df: pd.DataFrame | None, market_name: str, minimum_days: int) -> int:
    return _dispatch('source_correction_fetch_days', index_config, correction_df, market_name, minimum_days)

def filter_market_trading_dates(df: pd.DataFrame | None, market_name: str, date_column: str = 'trade_date') -> pd.DataFrame | None:
    return _dispatch('filter_market_trading_dates', df, market_name, date_column)

def filter_completed_market_dates(df: pd.DataFrame | None, market_name: str, date_column: str = 'trade_date') -> pd.DataFrame | None:
    return _dispatch('filter_completed_market_dates', df, market_name, date_column)

def sanitize_index_report_market_dates(report_df: pd.DataFrame | None) -> pd.DataFrame | None:
    return _dispatch('sanitize_index_report_market_dates', report_df)

def build_export_df(df: pd.DataFrame, index_name: str, days: int = INDEX_REPORT_DISPLAY_DAYS) -> pd.DataFrame | None:
    return _dispatch('build_export_df', df, index_name, days)

def normalize_akshare_index_df(df: pd.DataFrame) -> pd.DataFrame:
    return _dispatch('normalize_akshare_index_df', df)

def fetch_yahoo_chart_payload(symbol: str, params: dict[str, object], *, timeout: float = 10) -> dict:
    return _dispatch('fetch_yahoo_chart_payload', symbol, params, timeout=timeout)

def fetch_yahoo_latest_index_row(symbol: str) -> pd.DataFrame | None:
    return _dispatch('fetch_yahoo_latest_index_row', symbol)

def supplement_stale_yahoo_history(df: pd.DataFrame, symbol: str, *, now: datetime | None = None) -> pd.DataFrame:
    return _dispatch('supplement_stale_yahoo_history', df, symbol, now=now)

def append_akshare_latest_index_row(ak, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    return _dispatch('append_akshare_latest_index_row', ak, df, index_code)

def append_eastmoney_quote_row(df: pd.DataFrame, secid: str, replace_same_day: bool = False) -> pd.DataFrame:
    return _dispatch('append_eastmoney_quote_row', df, secid, replace_same_day)

def fetch_eastmoney_clist_latest_index_row(*, board_symbol: str | None = None, hk_em_symbol: str | None = None) -> pd.DataFrame | None:
    return _dispatch('fetch_eastmoney_clist_latest_index_row', board_symbol=board_symbol, hk_em_symbol=hk_em_symbol)

def append_eastmoney_clist_latest_index_row(df: pd.DataFrame, *, board_symbol: str | None = None, hk_em_symbol: str | None = None) -> pd.DataFrame:
    return _dispatch('append_eastmoney_clist_latest_index_row', df, board_symbol=board_symbol, hk_em_symbol=hk_em_symbol)

def append_eastmoney_latest_index_row(ak, df: pd.DataFrame, secid: str, board_symbol: str | None = None, hk_em_symbol: str | None = None) -> pd.DataFrame:
    return _dispatch('append_eastmoney_latest_index_row', ak, df, secid, board_symbol, hk_em_symbol)

def append_hk_index_spot_row(ak, df: pd.DataFrame, index_code: str, eastmoney_quote_secid: str | None = None) -> pd.DataFrame:
    return _dispatch('append_hk_index_spot_row', ak, df, index_code, eastmoney_quote_secid)

def append_futures_spot_row(ak, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    return _dispatch('append_futures_spot_row', ak, df, index_code)

def get_index_data_from_akshare_csindex(index_code: str, index_name: str, days: int = 30):
    return _dispatch('get_index_data_from_akshare_csindex', index_code, index_name, days)

def get_index_data_from_akshare_cn(index_code: str, market: str, index_name: str, days: int = 30, eastmoney_quote_secid: str | None = None):
    return _dispatch('get_index_data_from_akshare_cn', index_code, market, index_name, days, eastmoney_quote_secid)

def get_index_data_from_akshare_cni(index_code: str, index_name: str, days: int = 30):
    return _dispatch('get_index_data_from_akshare_cni', index_code, index_name, days)

def extract_raw_from_export_df(export_df: pd.DataFrame, index_name: str) -> pd.DataFrame | None:
    return _dispatch('extract_raw_from_export_df', export_df, index_name)

def merge_newer_index_rows(df: pd.DataFrame, newer_df: pd.DataFrame | None) -> pd.DataFrame:
    return _dispatch('merge_newer_index_rows', df, newer_df)

def get_index_data_from_akshare_us(index_code: str, index_name: str, days: int = 30, yahoo_symbol: str | None = None):
    return _dispatch('get_index_data_from_akshare_us', index_code, index_name, days, yahoo_symbol)

def get_index_data_from_akshare_hk(index_code: str, index_name: str, days: int = 30, eastmoney_quote_secid: str | None = None):
    return _dispatch('get_index_data_from_akshare_hk', index_code, index_name, days, eastmoney_quote_secid)

def get_index_data_from_yahoo(symbol: str, index_name: str, days: int = 30):
    return _dispatch('get_index_data_from_yahoo', symbol, index_name, days)

def is_sparse_daily_history(df: pd.DataFrame) -> bool:
    return _dispatch('is_sparse_daily_history', df)

def get_index_data_from_eastmoney_kline(secid: str, index_name: str, days: int = 30, fqt: str = '0', akshare_board_symbol: str | None = None, akshare_hk_em_symbol: str | None = None, sina_hk_symbol: str | None = None, hsi_official_series: str | None = None, mx_query_name: str | None = None, mx_expected_code: str | None = None) -> pd.DataFrame | None:
    return _dispatch('get_index_data_from_eastmoney_kline', secid, index_name, days, fqt, akshare_board_symbol, akshare_hk_em_symbol, sina_hk_symbol, hsi_official_series, mx_query_name, mx_expected_code)

def get_index_data_from_akshare_eastmoney_fallback(secid: str, index_name: str, days: int, fqt: str, board_symbol: str | None, hk_em_symbol: str | None, last_error: Exception | None, sina_hk_symbol: str | None = None, hsi_official_series: str | None = None, mx_query_name: str | None = None, mx_expected_code: str | None = None) -> pd.DataFrame | None:
    return _dispatch('get_index_data_from_akshare_eastmoney_fallback', secid, index_name, days, fqt, board_symbol, hk_em_symbol, last_error, sina_hk_symbol, hsi_official_series, mx_query_name, mx_expected_code)

def fetch_eastmoney_completed_global_row(secid: str, market_name: str, *, now: datetime | None = None) -> pd.DataFrame | None:
    return _dispatch('fetch_eastmoney_completed_global_row', secid, market_name, now=now)

def get_index_data_from_cboe_vix(index_name: str, days: int = 30) -> pd.DataFrame:
    return _dispatch('get_index_data_from_cboe_vix', index_name, days)

def get_index_data_from_akshare_global(index_code: str, index_name: str, days: int = 30, yahoo_symbol: str | None = None, eastmoney_quote_secid: str | None = None, market_name: str = ''):
    return _dispatch('get_index_data_from_akshare_global', index_code, index_name, days, yahoo_symbol, eastmoney_quote_secid, market_name)

def get_index_data_from_akshare_futures_main(index_code: str, index_name: str, days: int = 30):
    return _dispatch('get_index_data_from_akshare_futures_main', index_code, index_name, days)

def fetch_index_from_source(index_name: str, index_config: dict, days: int = 30) -> pd.DataFrame | None:
    return _dispatch('fetch_index_from_source', index_name, index_config, days)

def _append_unseen_raw_history(old_df: pd.DataFrame | None, new_df: pd.DataFrame | None) -> pd.DataFrame | None:
    return _dispatch('_append_unseen_raw_history', old_df, new_df)

def _latest_completed_date_for_market(market_name: str):
    return _dispatch('_latest_completed_date_for_market', market_name)

def _latest_raw_date(df: pd.DataFrame | None):
    return _dispatch('_latest_raw_date', df)

def fetch_index_history(index_name: str, index_config, days: int = 10000) -> pd.DataFrame | None:
    return _dispatch('fetch_index_history', index_name, index_config, days)

def get_index_data_from_tickflow(api_key: str, index_code: str, index_name: str, days: int = 30):
    return _dispatch('get_index_data_from_tickflow', api_key, index_code, index_name, days)

def tickflow_quote_date(symbol: str, timestamp) -> pd.Timestamp:
    return _dispatch('tickflow_quote_date', symbol, timestamp)

def append_tickflow_quote_row(client, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    return _dispatch('append_tickflow_quote_row', client, df, index_code)

def normalize_tickflow_index_df(df: pd.DataFrame) -> pd.DataFrame:
    return _dispatch('normalize_tickflow_index_df', df)

def get_index_raw_from_tickflow(api_key: str, index_code: str, count: int = 80) -> pd.DataFrame | None:
    return _dispatch('get_index_raw_from_tickflow', api_key, index_code, count)

def merge_raw_index_data(old_df: pd.DataFrame | None, new_df: pd.DataFrame) -> pd.DataFrame:
    return _dispatch('merge_raw_index_data', old_df, new_df)

def raw_cache_symbol(index_name: str, index_config) -> str:
    return _dispatch('raw_cache_symbol', index_name, index_config)

def display_index_symbol(index_config) -> str:
    return _dispatch('display_index_symbol', index_config)

def merge_by_date(all_data: list[pd.DataFrame]) -> pd.DataFrame:
    return _dispatch('merge_by_date', all_data)

def generate_index_ma20_report(api_key: str, days: int = INDEX_REPORT_DISPLAY_DAYS) -> pd.DataFrame:
    return _dispatch('generate_index_ma20_report', api_key, days)

def fetch_one_index(index_name: str, index_config, api_key: str, days: int = 30) -> pd.DataFrame | None:
    return _dispatch('fetch_one_index', index_name, index_config, api_key, days)

def build_summary(report_df: pd.DataFrame) -> pd.DataFrame:
    return _dispatch('build_summary', report_df)

def calculate_ma20_transition(valid_rows: pd.DataFrame, close_col: str, ma20_col: str, date_col: str = '日期') -> tuple[object, object]:
    return _dispatch('calculate_ma20_transition', valid_rows, close_col, ma20_col, date_col)

def calculate_ma20_transition_snapshot(valid_rows: pd.DataFrame, close_col: str, ma20_col: str, date_col: str = '日期') -> tuple[object, object, object, object]:
    return _dispatch('calculate_ma20_transition_snapshot', valid_rows, close_col, ma20_col, date_col)

def calculate_ma20_transition_history(valid_rows: pd.DataFrame, close_col: str, ma20_col: str, date_col: str = '日期') -> pd.DataFrame:
    return _dispatch('calculate_ma20_transition_history', valid_rows, close_col, ma20_col, date_col)

__all__ = ['YAHOO_CHART_HOSTS', 'YAHOO_REQUEST_GATE', 'INDEX_CONFIG', 'INDEX_LONG_HISTORY_SOURCE', 'INDEX_FINAL_HISTORY_SOURCE', 'INDEX_SOURCE_CORRECTION_SOURCE', 'INDEX_LONG_HISTORY_BARS', 'INDEX_REPORT_DISPLAY_DAYS', 'INDEX_RECENT_GAP_LOOKBACK_SESSIONS', 'CFFEX_FUTURES_MAIN_PRODUCTS', 'overlay_finalized_index_rows', 'missing_recent_market_trade_dates', 'source_correction_start', 'extract_source_correction_rows', 'source_correction_fetch_days', 'filter_market_trading_dates', 'filter_completed_market_dates', 'sanitize_index_report_market_dates', 'build_export_df', 'normalize_akshare_index_df', 'fetch_yahoo_chart_payload', 'fetch_yahoo_latest_index_row', 'supplement_stale_yahoo_history', 'append_akshare_latest_index_row', 'append_eastmoney_quote_row', 'fetch_eastmoney_clist_latest_index_row', 'append_eastmoney_clist_latest_index_row', 'append_eastmoney_latest_index_row', 'append_hk_index_spot_row', 'append_futures_spot_row', 'get_index_data_from_akshare_csindex', 'get_index_data_from_akshare_cn', 'get_index_data_from_akshare_cni', 'extract_raw_from_export_df', 'merge_newer_index_rows', 'get_index_data_from_akshare_us', 'get_index_data_from_akshare_hk', 'get_index_data_from_yahoo', 'is_sparse_daily_history', 'get_index_data_from_eastmoney_kline', 'get_index_data_from_akshare_eastmoney_fallback', 'fetch_eastmoney_completed_global_row', 'get_index_data_from_cboe_vix', 'get_index_data_from_akshare_global', 'get_index_data_from_akshare_futures_main', 'fetch_index_from_source', '_append_unseen_raw_history', '_latest_completed_date_for_market', '_latest_raw_date', 'fetch_index_history', 'get_index_data_from_tickflow', 'tickflow_quote_date', 'append_tickflow_quote_row', 'normalize_tickflow_index_df', 'get_index_raw_from_tickflow', 'merge_raw_index_data', 'raw_cache_symbol', 'display_index_symbol', 'merge_by_date', 'generate_index_ma20_report', 'fetch_one_index', 'build_summary', 'calculate_ma20_transition', 'calculate_ma20_transition_snapshot', 'calculate_ma20_transition_history']
