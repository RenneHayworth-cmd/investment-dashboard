from __future__ import annotations

# Long-term compatibility facade. Implementations live in prefixed sibling modules.
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timedelta
import json
import logging
import re
from threading import Lock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from core.cache import load_dataset, save_dataset
from services.fund_analysis import (
    FUND_ADJUST_BACKWARD_ADDITIVE, FUND_ADJUST_BACKWARD_RATIO,
    FUND_ADJUST_FORWARD_ADDITIVE, FUND_ADJUST_FORWARD_RATIO, FUND_ADJUST_NONE,
    FUND_CACHE_SCHEMA_VERSION, analyze_fund_nav, build_fund_cache_symbol,
    fetch_tickflow_fund_close, infer_tickflow_symbol, normalize_fund_adjustment,
    normalize_nav_dataframe, stamp_fund_history_metadata,
)
from services.futures_options_analysis import (
    DATA_TYPE_AUTO, DATA_TYPE_OPTIONS, FUTURES_OPTION_DATA_VERSION, add_indicators,
    append_option_spot_row, build_summary as build_futures_option_summary,
    fetch_futures_option_data, normalize_option_symbol,
)
from services.futures_spread import (
    SPREAD_CALCULATION_VERSION, append_futures_spot_row, build_spread_summary,
    calculate_spreads, contract_name, fetch_contracts, fetch_futures_daily,
    parse_contracts, spread_respects_contract_cutoffs,
)
from services.market_calendar import (
    expected_latest_trade_date, get_market_window, is_market_trading_day,
    previous_trading_day,
)
from services import position_derivatives as _derivatives
from services import position_market as _market
from services import position_models as _models
from services import position_performance as _performance
from services import position_runtime as _runtime
from services import position_sessions as _sessions
from services import position_timing as _timing

logger = _models.logger
DEFAULT_ETF_CODES = _models.DEFAULT_ETF_CODES
DEFAULT_SPREAD_CONTRACTS = _models.DEFAULT_SPREAD_CONTRACTS
DEFAULT_SPREAD_GROUPS = _models.DEFAULT_SPREAD_GROUPS
DEFAULT_FUTURES_CONTRACTS = _models.DEFAULT_FUTURES_CONTRACTS
DEFAULT_OPTION_CODES = _models.DEFAULT_OPTION_CODES
ETF_FINAL_CLOSE_READY_TIME = _models.ETF_FINAL_CLOSE_READY_TIME
ETF_MORNING_TIMING_START_TIME = _models.ETF_MORNING_TIMING_START_TIME
ETF_MORNING_FAST_REFRESH_END_TIME = _models.ETF_MORNING_FAST_REFRESH_END_TIME
ETF_MORNING_TIMING_PREVIEW_END_TIME = _models.ETF_MORNING_TIMING_PREVIEW_END_TIME
ETF_MORNING_TIMING_REFRESH_SECONDS = _models.ETF_MORNING_TIMING_REFRESH_SECONDS
ETF_MIDSESSION_TIMING_REFRESH_SECONDS = _models.ETF_MIDSESSION_TIMING_REFRESH_SECONDS
ETF_LUNCH_TIMING_START_TIME = _models.ETF_LUNCH_TIMING_START_TIME
ETF_LUNCH_TIMING_FETCH_END_TIME = _models.ETF_LUNCH_TIMING_FETCH_END_TIME
ETF_AFTERNOON_TIMING_START_TIME = _models.ETF_AFTERNOON_TIMING_START_TIME
ETF_REALTIME_TIMING_START_TIME = _models.ETF_REALTIME_TIMING_START_TIME
ETF_REALTIME_TIMING_END_TIME = _models.ETF_REALTIME_TIMING_END_TIME
ETF_REALTIME_TIMING_REFRESH_SECONDS = _models.ETF_REALTIME_TIMING_REFRESH_SECONDS
ETF_DISPLAY_NAMES = _models.ETF_DISPLAY_NAMES
ETF_TIMING_STRATEGIES = _models.ETF_TIMING_STRATEGIES
ETF_TIMING_TABLE_EXCLUDED_CODES = _models.ETF_TIMING_TABLE_EXCLUDED_CODES
ETF_PORTFOLIO_WEIGHTS_PCT = _models.ETF_PORTFOLIO_WEIGHTS_PCT
ETF_512890_TRANSFER_SOURCE_CODES = _models.ETF_512890_TRANSFER_SOURCE_CODES
ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES = _models.ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES
ETF_POSITION_STRATEGIES = _models.ETF_POSITION_STRATEGIES
ETF_AKSHARE_HISTORY_CODES = _models.ETF_AKSHARE_HISTORY_CODES
ETF_SINA_REALTIME_FALLBACK_CODES = _models.ETF_SINA_REALTIME_FALLBACK_CODES
SINA_REQUEST_TIMEOUT_SECONDS = _models.SINA_REQUEST_TIMEOUT_SECONDS
OPTION_PRODUCT_NAMES = _models.OPTION_PRODUCT_NAMES
PositionItem = _models.PositionItem
PositionItem.__module__ = __name__
POSITION_TIMING_START_DATE = _performance.POSITION_TIMING_START_DATE
POSITION_TIMING_INITIAL_CAPITAL = _performance.POSITION_TIMING_INITIAL_CAPITAL
POSITION_TIMING_TRANSACTION_COST = _performance.POSITION_TIMING_TRANSACTION_COST
POSITION_TIMING_LOT_SIZE = _performance.POSITION_TIMING_LOT_SIZE
POSITION_TIMING_PARKING_SYMBOL = _performance.POSITION_TIMING_PARKING_SYMBOL
PositionTimingPerformanceResult = _performance.PositionTimingPerformanceResult
PositionTimingPerformanceResult.__module__ = __name__
_RUNTIME_ETF_QUOTE_CACHE = _runtime._RUNTIME_ETF_QUOTE_CACHE
_RUNTIME_ETF_QUOTE_CACHE_LOCK = _runtime._RUNTIME_ETF_QUOTE_CACHE_LOCK
_RUNTIME_ETF_QUOTE_FETCH_STATE = _runtime._RUNTIME_ETF_QUOTE_FETCH_STATE

_FACADE_DEFAULTS: dict[str, object] = {}

def _call(module, name: str, /, *args, **kwargs):
    restored: dict[str, object] = {}
    for dependency, default in _FACADE_DEFAULTS.items():
        current = globals().get(dependency)
        if current is default or dependency == name or not hasattr(module, dependency):
            continue
        restored[dependency] = getattr(module, dependency)
        setattr(module, dependency, current)
    try:
        return getattr(module, name)(*args, **kwargs)
    finally:
        for dependency, original in restored.items():
            setattr(module, dependency, original)

def normalize_etf_base_code(code: str) -> str:
    return _call(_models, 'normalize_etf_base_code', code)

def display_etf_name(code: str, fallback: str) -> str:
    return _call(_models, 'display_etf_name', code, fallback)

def etf_final_close_ready(market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_final_close_ready', market_now)

def etf_intraday_quote_ready(market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_intraday_quote_ready', market_now)

def etf_realtime_timing_ready(market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_realtime_timing_ready', market_now)

def etf_morning_timing_fetch_ready(market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_morning_timing_fetch_ready', market_now)

def etf_morning_timing_preview_ready(market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_morning_timing_preview_ready', market_now)

def etf_lunch_timing_fetch_ready(market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_lunch_timing_fetch_ready', market_now)

def etf_lunch_timing_preview_ready(market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_lunch_timing_preview_ready', market_now)

def etf_afternoon_timing_fetch_ready(market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_afternoon_timing_fetch_ready', market_now)

def _tickflow_quote_datetime(row: pd.Series) -> datetime | None:
    return _call(_runtime, '_tickflow_quote_datetime', row)

def fetch_tickflow_etf_quotes(codes: list[str], *, api_key: str, market_now: datetime | None=None) -> dict[str, dict[str, object]]:
    return _call(_runtime, 'fetch_tickflow_etf_quotes', codes, api_key=api_key, market_now=market_now)

def refresh_runtime_etf_quotes(codes: list[str], *, api_key: str, market_now: datetime | None=None) -> dict[str, dict[str, object]]:
    return _call(_runtime, 'refresh_runtime_etf_quotes', codes, api_key=api_key, market_now=market_now)

def load_runtime_etf_quote_state() -> dict[str, object]:
    return _call(_runtime, 'load_runtime_etf_quote_state')

def remember_runtime_etf_quotes(quotes: dict[str, dict[str, object]]) -> None:
    return _call(_runtime, 'remember_runtime_etf_quotes', quotes)

def load_runtime_etf_quotes() -> dict[str, dict[str, object]]:
    return _call(_runtime, 'load_runtime_etf_quotes')

def filter_current_etf_realtime_quotes(quotes: dict[str, dict[str, object]] | None, *, market_now: datetime | None=None, retain_after_close: bool=False) -> dict[str, dict[str, object]]:
    return _call(_runtime, 'filter_current_etf_realtime_quotes', quotes, market_now=market_now, retain_after_close=retain_after_close)

def apply_etf_realtime_quote(item: PositionItem, quote: dict[str, object]) -> PositionItem:
    return _call(_runtime, 'apply_etf_realtime_quote', item, quote)

def apply_etf_realtime_quotes_to_items(items: list[PositionItem], quotes: dict[str, dict[str, object]]) -> list[PositionItem]:
    return _call(_runtime, 'apply_etf_realtime_quotes_to_items', items, quotes)

def apply_etf_realtime_quote_to_timing(item: PositionItem, quote: dict[str, object], *, market_now: datetime | None=None, allow_close_retention: bool=False) -> PositionItem:
    return _call(_runtime, 'apply_etf_realtime_quote_to_timing', item, quote, market_now=market_now, allow_close_retention=allow_close_retention)

def latest_final_etf_trade_date(market_now: datetime | None=None):
    return _call(_sessions, 'latest_final_etf_trade_date', market_now)

def filter_final_etf_rows(df: pd.DataFrame | None, *, date_column: str='日期', market_now: datetime | None=None, require_current_confirmation: bool=False) -> pd.DataFrame | None:
    return _call(_sessions, 'filter_final_etf_rows', df, date_column=date_column, market_now=market_now, require_current_confirmation=require_current_confirmation)

def etf_cache_has_latest_final_close(df: pd.DataFrame | None, *, date_column: str='日期', market_now: datetime | None=None) -> bool:
    return _call(_sessions, 'etf_cache_has_latest_final_close', df, date_column=date_column, market_now=market_now)

def calculate_etf_timing_snapshot(df: pd.DataFrame, *, ma_period: int, threshold_pct: float) -> dict[str, object]:
    return _call(_timing, 'calculate_etf_timing_snapshot', df, ma_period=ma_period, threshold_pct=threshold_pct)

def etf_position_decision(code: str, timing_action: object) -> object:
    return _call(_timing, 'etf_position_decision', code, timing_action)

def calculate_etf_timing_transitions(df: pd.DataFrame, *, ma_period: int, threshold_pct: float) -> pd.DataFrame:
    return _call(_timing, 'calculate_etf_timing_transitions', df, ma_period=ma_period, threshold_pct=threshold_pct)

def _calculate_etf_timing_position_series(df: pd.DataFrame | None, *, ma_period: int, threshold_pct: float) -> pd.Series:
    return _call(_timing, '_calculate_etf_timing_position_series', df, ma_period=ma_period, threshold_pct=threshold_pct)

def _timing_action_position(value: object) -> int | None:
    return _call(_timing, '_timing_action_position', value)

def calculate_512890_parking_snapshot(items: list[PositionItem]) -> dict[str, object]:
    return _call(_timing, 'calculate_512890_parking_snapshot', items)

def build_recent_etf_operation_guidance(items: list[PositionItem], *, days: int=7) -> pd.DataFrame:
    return _call(_timing, 'build_recent_etf_operation_guidance', items, days=days)

def build_etf_timing_table(items: list[PositionItem]) -> pd.DataFrame:
    return _call(_timing, 'build_etf_timing_table', items)

def build_position_timing_performance(items: list[PositionItem], *, start_date: str | pd.Timestamp=POSITION_TIMING_START_DATE, initial_capital: float=POSITION_TIMING_INITIAL_CAPITAL, transaction_cost: float=POSITION_TIMING_TRANSACTION_COST, lot_size: int=POSITION_TIMING_LOT_SIZE, market_now: datetime | None=None) -> PositionTimingPerformanceResult:
    return _call(_performance, 'build_position_timing_performance', items, start_date=start_date, initial_capital=initial_capital, transaction_cost=transaction_cost, lot_size=lot_size, market_now=market_now)

def parse_position_codes(text: str) -> list[str]:
    return _call(_models, 'parse_position_codes', text)

def parse_spread_groups(text: str) -> list[list[str]]:
    return _call(_models, 'parse_spread_groups', text)

def format_spread_position_name(base_contract: str, other_contract: str) -> str:
    return _call(_models, 'format_spread_position_name', base_contract, other_contract)

def format_futures_position_name(contract: str) -> str:
    return _call(_models, 'format_futures_position_name', contract)

def format_cache_time(value: str | None) -> str:
    return _call(_models, 'format_cache_time', value)

def _load_dataset_if_ready(symbol: str, source: str, data_type: str, period: str='1d'):
    return _call(_market, '_load_dataset_if_ready', symbol, source, data_type, period)

def _merge_by_date(old_df: pd.DataFrame | None, new_df: pd.DataFrame, date_column: str) -> pd.DataFrame:
    return _call(_market, '_merge_by_date', old_df, new_df, date_column)

def _fund_history_validation_error(df: pd.DataFrame | None, *, adjust: str | None, min_rows: int, market_now: datetime | None=None, require_latest: bool=False) -> str:
    return _call(_market, '_fund_history_validation_error', df, adjust=adjust, min_rows=min_rows, market_now=market_now, require_latest=require_latest)

def _recent_etf_gap_warning(df: pd.DataFrame | None, *, market_now: datetime | None=None, sessions: int=20) -> str:
    return _call(_market, '_recent_etf_gap_warning', df, market_now=market_now, sessions=sessions)

def _adjusted_history_has_overlap_changes(old_df: pd.DataFrame | None, new_df: pd.DataFrame | None, *, date_column: str='日期') -> bool:
    return _call(_market, '_adjusted_history_has_overlap_changes', old_df, new_df, date_column=date_column)

def _append_position_error(current: str, message: str) -> str:
    return _call(_market, '_append_position_error', current, message)

def _prepare_fetched_etf_history(df: pd.DataFrame, *, adjust: str, market_now: datetime | None, allow_unfinished_session: bool) -> pd.DataFrame:
    return _call(_market, '_prepare_fetched_etf_history', df, adjust=adjust, market_now=market_now, allow_unfinished_session=allow_unfinished_session)

def _fetch_position_etf_history(*, symbol: str, base_code: str, api_key: str, count: int, adjust: str, market_now: datetime | None) -> pd.DataFrame:
    return _call(_market, '_fetch_position_etf_history', symbol=symbol, base_code=base_code, api_key=api_key, count=count, adjust=adjust, market_now=market_now)

def _merge_current_day_refresh(old_df: pd.DataFrame | None, new_df: pd.DataFrame, date_column: str) -> pd.DataFrame:
    return _call(_market, '_merge_current_day_refresh', old_df, new_df, date_column)

def _round_metric(value: object, digits: int=2) -> object:
    return _call(_models, '_round_metric', value, digits)

def _current_cache_time_text() -> str:
    return _call(_models, '_current_cache_time_text')

def _cache_has_expected_trade_date(df: pd.DataFrame | None, date_column: str='date', market_now: datetime | None=None) -> bool:
    return _call(_sessions, '_cache_has_expected_trade_date', df, date_column, market_now)

def _futures_option_cache_key(symbol: str, data_type: str, period: str, count: int) -> str:
    return _call(_models, '_futures_option_cache_key', symbol, data_type, period, count)

def _futures_contract_cache_key(contract: str) -> str:
    return _call(_models, '_futures_contract_cache_key', contract)

def _futures_option_cache_candidates(symbol: str, period: str, count: int) -> list[str]:
    return _call(_models, '_futures_option_cache_candidates', symbol, period, count)

def option_display_name(symbol: str) -> str:
    return _call(_models, 'option_display_name', symbol)

def _market_cache_is_usable(df: pd.DataFrame | None) -> bool:
    return _call(_models, '_market_cache_is_usable', df)

def _spread_cache_matches_contracts(df: pd.DataFrame | None, contracts: list[str], base_contract: str) -> bool:
    return _call(_models, '_spread_cache_matches_contracts', df, contracts, base_contract)

def _missing_item(category: str, code: str, name: str='') -> PositionItem:
    return _call(_models, '_missing_item', category, code, name)

def _fetch_eastmoney_exchange_fund_close(*, symbol: str, count: int, adjust: str | None) -> pd.DataFrame:
    return _call(_market, '_fetch_eastmoney_exchange_fund_close', symbol=symbol, count=count, adjust=adjust)

def _sina_exchange_symbol(symbol: str) -> str:
    return _call(_market, '_sina_exchange_symbol', symbol)

def _request_sina_realtime_snapshot(sina_symbol: str):
    return _call(_market, '_request_sina_realtime_snapshot', sina_symbol)

def _fetch_sina_exchange_fund_quote(*, symbol: str, market_now: datetime | None=None) -> dict[str, object]:
    return _call(_market, '_fetch_sina_exchange_fund_quote', symbol=symbol, market_now=market_now)

def _ensure_sina_adjustment_is_identity(sina_symbol: str, adjust: str | None) -> None:
    return _call(_market, '_ensure_sina_adjustment_is_identity', sina_symbol, adjust)

def _fetch_sina_exchange_fund_close(*, symbol: str, count: int, adjust: str | None) -> pd.DataFrame:
    return _call(_market, '_fetch_sina_exchange_fund_close', symbol=symbol, count=count, adjust=adjust)

def _fetch_sina_exchange_fund_final_close(*, symbol: str, market_now: datetime | None=None) -> pd.DataFrame:
    return _call(_market, '_fetch_sina_exchange_fund_final_close', symbol=symbol, market_now=market_now)

def _append_sina_final_close(history: pd.DataFrame, *, symbol: str, adjust: str | None, market_now: datetime | None=None) -> pd.DataFrame:
    return _call(_market, '_append_sina_final_close', history, symbol=symbol, adjust=adjust, market_now=market_now)

def _fetch_exchange_fund_close(*, symbol: str, count: int, adjust: str | None, market_now: datetime | None=None) -> pd.DataFrame:
    return _call(_market, '_fetch_exchange_fund_close', symbol=symbol, count=count, adjust=adjust, market_now=market_now)

def load_or_fetch_etf(code: str, *, api_key: str='', count: int=5000, adjust: str | None=FUND_ADJUST_FORWARD_ADDITIVE, ma_periods: list[int] | tuple[int, ...]=(20, 60, 120, 250), rsi_period: int=14, base_date: str='2024-09-24', allow_fetch: bool=True, force_refresh: bool=False, save_to_cache: bool=True, allow_unfinished_session: bool=False, market_now: datetime | None=None) -> PositionItem:
    return _call(_market, 'load_or_fetch_etf', code, api_key=api_key, count=count, adjust=adjust, ma_periods=ma_periods, rsi_period=rsi_period, base_date=base_date, allow_fetch=allow_fetch, force_refresh=force_refresh, save_to_cache=save_to_cache, allow_unfinished_session=allow_unfinished_session, market_now=market_now)

def load_or_fetch_futures_contract(contract: str, *, api_key: str='', count: int=500, ma_periods: list[int] | tuple[int, ...]=(5, 20, 60), allow_fetch: bool=True, force_refresh: bool=False, save_to_cache: bool=True, realtime_preview: bool=False, market_now: datetime | None=None) -> PositionItem:
    return _call(_derivatives, 'load_or_fetch_futures_contract', contract, api_key=api_key, count=count, ma_periods=ma_periods, allow_fetch=allow_fetch, force_refresh=force_refresh, save_to_cache=save_to_cache, realtime_preview=realtime_preview, market_now=market_now)

def load_or_fetch_spread(contracts: list[str], *, base_contract: str | None=None, api_key: str='', max_workers: int=2, allow_fetch: bool=True, force_refresh: bool=False, save_to_cache: bool=True, realtime_preview: bool=False, market_now: datetime | None=None) -> PositionItem:
    return _call(_derivatives, 'load_or_fetch_spread', contracts, base_contract=base_contract, api_key=api_key, max_workers=max_workers, allow_fetch=allow_fetch, force_refresh=force_refresh, save_to_cache=save_to_cache, realtime_preview=realtime_preview, market_now=market_now)

def load_or_fetch_option(code: str, *, period: str='1d', count: int=500, ma_periods: list[int] | tuple[int, ...]=(5, 20, 60), allow_fetch: bool=True, force_refresh: bool=False, save_to_cache: bool=True, realtime_preview: bool=False, market_now: datetime | None=None) -> PositionItem:
    return _call(_derivatives, 'load_or_fetch_option', code, period=period, count=count, ma_periods=ma_periods, allow_fetch=allow_fetch, force_refresh=force_refresh, save_to_cache=save_to_cache, realtime_preview=realtime_preview, market_now=market_now)

def refresh_position_derivative_items(items: list[PositionItem], *, api_key: str='', max_workers: int=2, option_count: int=500, market_now: datetime | None=None) -> tuple[list[PositionItem], list[str]]:
    return _call(_derivatives, 'refresh_position_derivative_items', items, api_key=api_key, max_workers=max_workers, option_count=option_count, market_now=market_now)

__all__ = ['DEFAULT_ETF_CODES', 'DEFAULT_SPREAD_CONTRACTS', 'DEFAULT_SPREAD_GROUPS', 'DEFAULT_FUTURES_CONTRACTS', 'DEFAULT_OPTION_CODES', 'ETF_FINAL_CLOSE_READY_TIME', 'ETF_MORNING_TIMING_START_TIME', 'ETF_MORNING_FAST_REFRESH_END_TIME', 'ETF_MORNING_TIMING_PREVIEW_END_TIME', 'ETF_MORNING_TIMING_REFRESH_SECONDS', 'ETF_MIDSESSION_TIMING_REFRESH_SECONDS', 'ETF_LUNCH_TIMING_START_TIME', 'ETF_LUNCH_TIMING_FETCH_END_TIME', 'ETF_AFTERNOON_TIMING_START_TIME', 'ETF_REALTIME_TIMING_START_TIME', 'ETF_REALTIME_TIMING_END_TIME', 'ETF_REALTIME_TIMING_REFRESH_SECONDS', 'ETF_DISPLAY_NAMES', 'ETF_TIMING_STRATEGIES', 'ETF_TIMING_TABLE_EXCLUDED_CODES', 'ETF_PORTFOLIO_WEIGHTS_PCT', 'ETF_512890_TRANSFER_SOURCE_CODES', 'ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES', 'ETF_POSITION_STRATEGIES', 'ETF_AKSHARE_HISTORY_CODES', 'ETF_SINA_REALTIME_FALLBACK_CODES', 'SINA_REQUEST_TIMEOUT_SECONDS', 'OPTION_PRODUCT_NAMES', 'PositionItem', 'POSITION_TIMING_START_DATE', 'POSITION_TIMING_INITIAL_CAPITAL', 'POSITION_TIMING_TRANSACTION_COST', 'POSITION_TIMING_LOT_SIZE', 'POSITION_TIMING_PARKING_SYMBOL', 'PositionTimingPerformanceResult', 'normalize_etf_base_code', 'display_etf_name', 'etf_final_close_ready', 'etf_intraday_quote_ready', 'etf_realtime_timing_ready', 'etf_morning_timing_fetch_ready', 'etf_morning_timing_preview_ready', 'etf_lunch_timing_fetch_ready', 'etf_lunch_timing_preview_ready', 'etf_afternoon_timing_fetch_ready', '_tickflow_quote_datetime', 'fetch_tickflow_etf_quotes', 'refresh_runtime_etf_quotes', 'load_runtime_etf_quote_state', 'remember_runtime_etf_quotes', 'load_runtime_etf_quotes', 'filter_current_etf_realtime_quotes', 'apply_etf_realtime_quote', 'apply_etf_realtime_quotes_to_items', 'apply_etf_realtime_quote_to_timing', 'latest_final_etf_trade_date', 'filter_final_etf_rows', 'etf_cache_has_latest_final_close', 'calculate_etf_timing_snapshot', 'etf_position_decision', 'calculate_etf_timing_transitions', '_calculate_etf_timing_position_series', '_timing_action_position', 'calculate_512890_parking_snapshot', 'build_recent_etf_operation_guidance', 'build_etf_timing_table', 'build_position_timing_performance', 'parse_position_codes', 'parse_spread_groups', 'format_spread_position_name', 'format_futures_position_name', 'format_cache_time', '_load_dataset_if_ready', '_merge_by_date', '_fund_history_validation_error', '_recent_etf_gap_warning', '_adjusted_history_has_overlap_changes', '_append_position_error', '_prepare_fetched_etf_history', '_merge_current_day_refresh', '_round_metric', '_current_cache_time_text', '_cache_has_expected_trade_date', '_futures_option_cache_key', '_futures_contract_cache_key', '_futures_option_cache_candidates', 'option_display_name', '_market_cache_is_usable', '_spread_cache_matches_contracts', '_missing_item', '_fetch_eastmoney_exchange_fund_close', '_sina_exchange_symbol', '_request_sina_realtime_snapshot', '_fetch_sina_exchange_fund_quote', '_ensure_sina_adjustment_is_identity', '_fetch_sina_exchange_fund_close', '_fetch_sina_exchange_fund_final_close', '_append_sina_final_close', '_fetch_exchange_fund_close', 'load_or_fetch_etf', 'load_or_fetch_futures_contract', 'load_or_fetch_spread', 'load_or_fetch_option', 'refresh_position_derivative_items']
_PATCHABLE_NAMES = ['annotations', 'dataclass', 'field', 'datetime', 'datetime_time', 'timedelta', 'json', 'logging', 're', 'Lock', 'ZoneInfo', 'np', 'pd', 'requests', 'load_dataset', 'save_dataset', 'FUND_ADJUST_BACKWARD_ADDITIVE', 'FUND_ADJUST_BACKWARD_RATIO', 'FUND_ADJUST_FORWARD_ADDITIVE', 'FUND_ADJUST_FORWARD_RATIO', 'FUND_ADJUST_NONE', 'FUND_CACHE_SCHEMA_VERSION', 'analyze_fund_nav', 'build_fund_cache_symbol', 'fetch_tickflow_fund_close', 'infer_tickflow_symbol', 'normalize_fund_adjustment', 'normalize_nav_dataframe', 'stamp_fund_history_metadata', 'DATA_TYPE_AUTO', 'DATA_TYPE_OPTIONS', 'FUTURES_OPTION_DATA_VERSION', 'add_indicators', 'append_option_spot_row', 'build_futures_option_summary', 'fetch_futures_option_data', 'normalize_option_symbol', 'SPREAD_CALCULATION_VERSION', 'append_futures_spot_row', 'build_spread_summary', 'calculate_spreads', 'contract_name', 'fetch_contracts', 'fetch_futures_daily', 'parse_contracts', 'spread_respects_contract_cutoffs', 'expected_latest_trade_date', 'get_market_window', 'is_market_trading_day', 'previous_trading_day', 'logger', '_RUNTIME_ETF_QUOTE_CACHE', '_RUNTIME_ETF_QUOTE_CACHE_LOCK', 'DEFAULT_ETF_CODES', 'DEFAULT_SPREAD_CONTRACTS', 'DEFAULT_SPREAD_GROUPS', 'DEFAULT_FUTURES_CONTRACTS', 'DEFAULT_OPTION_CODES', 'ETF_FINAL_CLOSE_READY_TIME', 'ETF_MORNING_TIMING_START_TIME', 'ETF_MORNING_FAST_REFRESH_END_TIME', 'ETF_MORNING_TIMING_PREVIEW_END_TIME', 'ETF_MORNING_TIMING_REFRESH_SECONDS', 'ETF_MIDSESSION_TIMING_REFRESH_SECONDS', 'ETF_LUNCH_TIMING_START_TIME', 'ETF_LUNCH_TIMING_FETCH_END_TIME', 'ETF_AFTERNOON_TIMING_START_TIME', 'ETF_REALTIME_TIMING_START_TIME', 'ETF_REALTIME_TIMING_END_TIME', 'ETF_REALTIME_TIMING_REFRESH_SECONDS', 'ETF_DISPLAY_NAMES', 'ETF_TIMING_STRATEGIES', 'ETF_TIMING_TABLE_EXCLUDED_CODES', 'ETF_PORTFOLIO_WEIGHTS_PCT', 'ETF_512890_TRANSFER_SOURCE_CODES', 'ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES', 'ETF_POSITION_STRATEGIES', 'ETF_AKSHARE_HISTORY_CODES', 'ETF_SINA_REALTIME_FALLBACK_CODES', 'SINA_REQUEST_TIMEOUT_SECONDS', 'OPTION_PRODUCT_NAMES', 'PositionItem', 'normalize_etf_base_code', 'display_etf_name', 'etf_final_close_ready', 'etf_intraday_quote_ready', 'etf_realtime_timing_ready', 'etf_morning_timing_fetch_ready', 'etf_morning_timing_preview_ready', 'etf_lunch_timing_fetch_ready', 'etf_lunch_timing_preview_ready', 'etf_afternoon_timing_fetch_ready', '_tickflow_quote_datetime', 'fetch_tickflow_etf_quotes', 'remember_runtime_etf_quotes', 'load_runtime_etf_quotes', 'filter_current_etf_realtime_quotes', 'apply_etf_realtime_quote', 'apply_etf_realtime_quotes_to_items', 'apply_etf_realtime_quote_to_timing', 'latest_final_etf_trade_date', 'filter_final_etf_rows', 'etf_cache_has_latest_final_close', 'calculate_etf_timing_snapshot', 'etf_position_decision', 'calculate_etf_timing_transitions', '_calculate_etf_timing_position_series', '_timing_action_position', 'calculate_512890_parking_snapshot', 'build_recent_etf_operation_guidance', 'build_etf_timing_table', 'parse_position_codes', 'parse_spread_groups', 'format_spread_position_name', 'format_futures_position_name', 'format_cache_time', '_load_dataset_if_ready', '_merge_by_date', '_fund_history_validation_error', '_recent_etf_gap_warning', '_adjusted_history_has_overlap_changes', '_append_position_error', '_prepare_fetched_etf_history', '_fetch_position_etf_history', '_merge_current_day_refresh', '_round_metric', '_current_cache_time_text', '_cache_has_expected_trade_date', '_futures_option_cache_key', '_futures_contract_cache_key', '_futures_option_cache_candidates', 'option_display_name', '_market_cache_is_usable', '_spread_cache_matches_contracts', '_missing_item', '_fetch_eastmoney_exchange_fund_close', '_sina_exchange_symbol', '_request_sina_realtime_snapshot', '_fetch_sina_exchange_fund_quote', '_ensure_sina_adjustment_is_identity', '_fetch_sina_exchange_fund_close', '_fetch_sina_exchange_fund_final_close', '_append_sina_final_close', '_fetch_exchange_fund_close', 'load_or_fetch_etf', 'load_or_fetch_futures_contract', 'load_or_fetch_spread', 'load_or_fetch_option', 'refresh_position_derivative_items']
_PATCHABLE_NAMES.extend([
    'POSITION_TIMING_START_DATE', 'POSITION_TIMING_INITIAL_CAPITAL',
    'POSITION_TIMING_TRANSACTION_COST', 'POSITION_TIMING_LOT_SIZE',
    'POSITION_TIMING_PARKING_SYMBOL', 'PositionTimingPerformanceResult',
    'build_position_timing_performance',
    'refresh_runtime_etf_quotes', 'load_runtime_etf_quote_state',
])
_FACADE_DEFAULTS.update({name: globals()[name] for name in _PATCHABLE_NAMES if name in globals()})
