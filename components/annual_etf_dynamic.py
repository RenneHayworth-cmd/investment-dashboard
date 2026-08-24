"""年度动态组合页面兼容门面。

数据准备、联网补齐、结果展示和页面协调分别位于 ``components.backtest``；
本模块保留原导入路径，并在调用时注入依赖，使旧路径 mock 继续生效。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from components.backtest import annual_data as _data
from components.backtest.annual_config import (
    AUDIT_MARKET_DIR,
    DIRECTION_LABELS,
    REGISTRY_PATH,
    RESULT_TABLES,
    ROOT,
    RUNTIME_DIR,
    WHITELIST_PATH,
)
from components.backtest.annual_dynamic import (
    render_annual_dynamic_mode as _render_annual_dynamic_mode_impl,
)
from components.backtest.annual_results import render_result as _render_result_impl
from core.cache import load_dataset, save_dataset
from services.annual_etf_portfolio import (
    ALL_SLOTS,
    ANNUAL_CACHE_PERIOD,
    ANNUAL_CACHE_SOURCE,
    ANNUAL_DIVIDEND_CACHE_KEY,
    ANNUAL_DIVIDEND_DATA_TYPE,
    ANNUAL_RAW_DATA_TYPE,
    AnnualBacktestSettings,
    annual_raw_cache_key,
    dividends_for_symbol,
    fetch_annual_dividends,
    fetch_annual_etf_raw_history,
    load_index_family_config,
    load_registry,
    normalize_annual_market_data,
    preflight_annual_candidates,
    registry_frame,
    run_annual_etf_backtest,
    share_splits_for_symbol,
    validate_registry_against_whitelist,
)
from services.market_calendar import get_market_window, latest_completed_trade_date


def _date_column(frame: pd.DataFrame) -> str | None:
    return _data.date_column(frame)


def _completed_a_share_date():
    return _data.completed_a_share_date(globals())


def _filter_completed_rows(frame: pd.DataFrame, completed_date) -> pd.DataFrame:
    return _data.filter_completed_rows(
        frame,
        completed_date,
        date_column_fn=_date_column,
    )


def _append_unseen_dates(
    existing: pd.DataFrame | None,
    fetched: pd.DataFrame,
) -> pd.DataFrame:
    return _data.append_unseen_dates(
        existing,
        fetched,
        date_column_fn=_date_column,
    )


def _append_dividends(
    existing: pd.DataFrame | None,
    fetched: pd.DataFrame,
) -> pd.DataFrame:
    return _data.append_dividends(existing, fetched)


def _read_raw_fallback(record) -> tuple[pd.DataFrame | None, str]:
    return _data.read_raw_fallback(
        record,
        audit_market_dir=AUDIT_MARKET_DIR,
        root=ROOT,
    )


def _load_dividends() -> tuple[pd.DataFrame, str]:
    return _data.load_dividends(globals())


def _load_proxy_data(records, completed_date):
    return _data.load_proxy_data(records, completed_date, globals())


def _load_market_bundle(records, whitelist, completed_date):
    return _data.load_market_bundle(records, whitelist, completed_date, globals())


def _network_fill(
    records,
    completed_date,
    start_year: int,
    refresh: bool,
    batch_size: int,
):
    return _data.network_fill(
        records,
        completed_date,
        start_year,
        refresh,
        batch_size,
        globals(),
    )


def _qualification_summary(frame: pd.DataFrame) -> pd.DataFrame:
    return _data.qualification_summary(frame, direction_labels=DIRECTION_LABELS)


def _render_result(result, initial_capital: float) -> None:
    return _render_result_impl(
        result,
        initial_capital,
        direction_labels=DIRECTION_LABELS,
        result_tables=RESULT_TABLES,
    )


def render_annual_dynamic_mode() -> None:
    return _render_annual_dynamic_mode_impl(globals())


__all__ = [
    "AUDIT_MARKET_DIR",
    "DIRECTION_LABELS",
    "REGISTRY_PATH",
    "RESULT_TABLES",
    "ROOT",
    "RUNTIME_DIR",
    "WHITELIST_PATH",
    "_append_dividends",
    "_append_unseen_dates",
    "_completed_a_share_date",
    "_date_column",
    "_filter_completed_rows",
    "_load_dividends",
    "_load_market_bundle",
    "_load_proxy_data",
    "_network_fill",
    "_qualification_summary",
    "_read_raw_fallback",
    "_render_result",
    "render_annual_dynamic_mode",
]
