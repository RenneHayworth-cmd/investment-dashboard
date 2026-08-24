from __future__ import annotations

# Long-term compatibility facade. Implementations live in prefixed sibling modules.
from datetime import datetime
import os
from pathlib import Path

import pandas as pd

from core.db import get_conn, init_db
from services.futures_options_analysis import fetch_option_from_akshare, normalize_option_symbol
from services.futures_spread import completed_futures_daily_cutoff, fetch_futures_daily
from services.market_calendar import get_market_window, is_market_holiday
from services import futures_live_calendar as _calendar
from services import futures_live_daily_pnl as _daily_pnl
from services import futures_live_models as _models
from services import futures_live_pnl as _pnl
from services import futures_live_positions as _positions
from services import futures_live_prices as _prices
from services import futures_live_repository as _repository
from services import futures_live_settlements as _settlements
from services import futures_live_statement_parser as _statement_parser

DEFAULT_FUTURES_STATEMENT_DIR = _models.DEFAULT_FUTURES_STATEMENT_DIR
STATEMENT_FILE_PATTERN = _models.STATEMENT_FILE_PATTERN
ASSET_TYPES = _models.ASSET_TYPES
BUY_SELL_VALUES = _models.BUY_SELL_VALUES
OPEN_CLOSE_VALUES = _models.OPEN_CLOSE_VALUES
CASH_FLOW_TYPES = _models.CASH_FLOW_TYPES
OPTION_EXPIRY_OUTCOMES = _models.OPTION_EXPIRY_OUTCOMES
DAILY_PNL_RESOLUTIONS = _models.DAILY_PNL_RESOLUTIONS
RECONCILIATION_TOLERANCE = _models.RECONCILIATION_TOLERANCE
StatementPayload = _models.StatementPayload
StatementSyncResult = _models.StatementSyncResult
# Keep the historical pickle/import identity while sharing the same class object
# with the implementation modules.
StatementPayload.__module__ = __name__
StatementSyncResult.__module__ = __name__

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

def configured_statement_dir(value: str | os.PathLike[str] | None=None) -> Path:
    return _call(_models, 'configured_statement_dir', value)


def discover_statement_files(directory: str | os.PathLike[str] | None=None) -> list[Path]:
    return _call(_models, 'discover_statement_files', directory)


def normalize_contract(value: object, asset_type: str | None=None) -> str:
    return _call(_models, 'normalize_contract', value, asset_type)


def parse_statement(path: str | os.PathLike[str]) -> StatementPayload:
    return _call(_statement_parser, 'parse_statement', path)


def sync_statements(directory: str | os.PathLike[str] | None=None, *, force: bool=False) -> StatementSyncResult:
    return _call(_repository, 'sync_statements', directory, force=force)


def list_statement_imports() -> pd.DataFrame:
    return _call(_repository, 'list_statement_imports')


def list_monthly_accounts() -> pd.DataFrame:
    return _call(_repository, 'list_monthly_accounts')


def latest_monthly_account() -> dict[str, object] | None:
    return _call(_repository, 'latest_monthly_account')


def list_futures_cash_flows(*, include_taken_over: bool=True) -> pd.DataFrame:
    return _call(_repository, 'list_futures_cash_flows', include_taken_over=include_taken_over)


def add_manual_cash_flow(*, flow_date: object, entry_type: str, amount: float, notes: str='') -> int:
    return _call(_repository, 'add_manual_cash_flow', flow_date=flow_date, entry_type=entry_type, amount=amount, notes=notes)


def delete_manual_cash_flow(flow_id: int) -> bool:
    return _call(_repository, 'delete_manual_cash_flow', flow_id)


def list_futures_daily_pnl_overrides() -> pd.DataFrame:
    return _call(_repository, 'list_futures_daily_pnl_overrides')


def add_manual_daily_pnl(*, trade_date: object, pnl_amount: float, notes: str='') -> int:
    return _call(_repository, 'add_manual_daily_pnl', trade_date=trade_date, pnl_amount=pnl_amount, notes=notes)


def delete_manual_daily_pnl(record_id: int) -> bool:
    return _call(_repository, 'delete_manual_daily_pnl', record_id)


def resolve_manual_daily_pnl(record_id: int, resolution: str) -> bool:
    return _call(_repository, 'resolve_manual_daily_pnl', record_id, resolution)


def list_month_end_positions(statement_month: str | None=None) -> pd.DataFrame:
    return _call(_repository, 'list_month_end_positions', statement_month)


def list_futures_live_trades(*, include_taken_over: bool=True) -> pd.DataFrame:
    return _call(_repository, 'list_futures_live_trades', include_taken_over=include_taken_over)


def iron_ore_option_expiry_date(contract: str) -> str | None:
    return _call(_positions, 'iron_ore_option_expiry_date', contract)


def list_option_expiry_events() -> pd.DataFrame:
    return _call(_positions, 'list_option_expiry_events')


def build_estimated_positions(*, as_of: object=None) -> pd.DataFrame:
    return _call(_positions, 'build_estimated_positions', as_of=as_of)


def list_option_expiry_candidates(*, as_of: object=None) -> pd.DataFrame:
    return _call(_positions, 'list_option_expiry_candidates', as_of=as_of)


def confirm_option_expiry_event(*, option_contract: str, outcome: str, quantity: int | None=None, notes: str='') -> int:
    return _call(_positions, 'confirm_option_expiry_event', option_contract=option_contract, outcome=outcome, quantity=quantity, notes=notes)


def delete_manual_option_expiry_event(event_id: int) -> bool:
    return _call(_positions, 'delete_manual_option_expiry_event', event_id)


def add_manual_trade(*, trade_date: object, trade_time: str='', asset_type: str, contract: str, buy_sell: str, open_close: str, price: float, quantity: int, turnover: float | None=None, fee: float=0, close_pnl: float | None=None, broker_trade_id: str='', strategy: str='', notes: str='') -> int:
    return _call(_positions, 'add_manual_trade', trade_date=trade_date, trade_time=trade_time, asset_type=asset_type, contract=contract, buy_sell=buy_sell, open_close=open_close, price=price, quantity=quantity, turnover=turnover, fee=fee, close_pnl=close_pnl, broker_trade_id=broker_trade_id, strategy=strategy, notes=notes)


def delete_manual_trade(trade_id: int) -> bool:
    return _call(_positions, 'delete_manual_trade', trade_id)


def load_daily_closes(asset_type: str | None=None, contract: str | None=None) -> pd.DataFrame:
    return _call(_repository, 'load_daily_closes', asset_type, contract)


def _save_daily_close_frame(asset_type: str, contract: str, data: pd.DataFrame, source: str, *, max_trade_date: str | None=None) -> int:
    return _call(_prices, '_save_daily_close_frame', asset_type, contract, data, source, max_trade_date=max_trade_date)


def _infer_previous_settlement(latest_price: object, change_pct: object, *, tick_size: float=0.1) -> float | None:
    return _call(_settlements, '_infer_previous_settlement', latest_price, change_pct, tick_size=tick_size)


def _save_daily_settlement_frame(asset_type: str, contract: str, data: pd.DataFrame, source: str, *, min_trade_date: str | None=None, max_trade_date: str | None=None) -> dict[str, object]:
    return _call(_settlements, '_save_daily_settlement_frame', asset_type, contract, data, source, min_trade_date=min_trade_date, max_trade_date=max_trade_date)


def _update_position_settlements(positions: pd.DataFrame, target_date: str, *, force: bool=False, market_now: datetime | None=None) -> dict[str, object]:
    return _call(_settlements, '_update_position_settlements', positions, target_date, force=force, market_now=market_now)


def update_position_daily_closes(*, api_key: str='', force: bool=False, market_now: datetime | None=None) -> dict[str, object]:
    return _call(_prices, 'update_position_daily_closes', api_key=api_key, force=force, market_now=market_now)


def _historical_contract_requirements(*, market_now: datetime | None=None) -> pd.DataFrame:
    return _call(_prices, '_historical_contract_requirements', market_now=market_now)


def update_traded_contract_daily_closes(*, api_key: str='', force: bool=False, market_now: datetime | None=None) -> dict[str, object]:
    return _call(_prices, 'update_traded_contract_daily_closes', api_key=api_key, force=force, market_now=market_now)


def _fetch_cffex_settlement_history(contracts: set[str], start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    return _call(_settlements, '_fetch_cffex_settlement_history', contracts, start_date, end_date)


def _parse_dce_option_settlement_payload(payload: object, trade_date: str, contracts: set[str]) -> pd.DataFrame:
    return _call(_settlements, '_parse_dce_option_settlement_payload', payload, trade_date, contracts)


def update_traded_contract_daily_settlements(*, force: bool=False, market_now: datetime | None=None) -> dict[str, object]:
    return _call(_settlements, 'update_traded_contract_daily_settlements', force=force, market_now=market_now)


def build_current_position_pnl(*, as_of: object=None, valuation_mode: str='close') -> pd.DataFrame:
    return _call(_pnl, 'build_current_position_pnl', as_of=as_of, valuation_mode=valuation_mode)


def build_contract_pnl_history(*, as_of: object=None, valuation_mode: str='close') -> pd.DataFrame:
    return _call(_pnl, 'build_contract_pnl_history', as_of=as_of, valuation_mode=valuation_mode)


def summarize_futures_live_pnl(*, as_of: object=None, valuation_mode: str='close', include_declaration_fee: bool=True) -> dict[str, object]:
    return _call(_pnl, 'summarize_futures_live_pnl', as_of=as_of, valuation_mode=valuation_mode, include_declaration_fee=include_declaration_fee)


def build_daily_account_pnl(*, as_of: object=None, valuation_mode: str='close') -> pd.DataFrame:
    return _call(_daily_pnl, 'build_daily_account_pnl', as_of=as_of, valuation_mode=valuation_mode)


def build_futures_daily_returns(daily_pnl: pd.DataFrame) -> pd.DataFrame:
    return _call(_daily_pnl, 'build_futures_daily_returns', daily_pnl)


_DIRECT_EXPORTS = {
    _statement_parser: ('_clean_label', '_number', '_integer', '_text', '_date_text', '_time_text', '_read_sheet', '_value_after_label', '_deduplicate_headers', '_table_after_header', '_section_table', '_statement_end_date', '_parse_account', '_parse_declaration_fee', '_parse_statement_cash_flows', '_position_multiplier', '_parse_positions', '_trade_multiplier', '_parse_futures_trades', '_parse_option_trades'),
    _repository: ('_file_hash', '_json_list', '_insert_statement_payload', '_backfill_position_multipliers', '_manual_match_candidates', '_reconcile_manual_trades', '_reconcile_manual_cash_flows', '_reconcile_option_expiry_events', '_effective_cash_flows', '_effective_manual_trades'),
    _positions: ('_known_multiplier', '_apply_manual_to_positions', '_build_estimated_positions_base', '_option_contract_parts', '_effective_option_expiry_events', '_apply_option_expiry_events'),
    _settlements: ('_save_settlement_price', '_fetch_futures_settlement_history', '_fetch_dce_option_settlements_for_date'),
    _pnl: ('_position_prices_for_date', '_option_cashflow', '_trades_with_calculated_manual_close_pnl'),
    _calendar: ('_futures_trading_dates',),
    _daily_pnl: ('_daily_fee_adjustments', '_apply_manual_daily_pnl_overrides'),
}
for _module, _names in _DIRECT_EXPORTS.items():
    globals().update({name: getattr(_module, name) for name in _names})

__all__ = [
    *['DEFAULT_FUTURES_STATEMENT_DIR', 'STATEMENT_FILE_PATTERN', 'ASSET_TYPES', 'BUY_SELL_VALUES', 'OPEN_CLOSE_VALUES', 'CASH_FLOW_TYPES', 'OPTION_EXPIRY_OUTCOMES', 'DAILY_PNL_RESOLUTIONS', 'RECONCILIATION_TOLERANCE', 'StatementPayload', 'StatementSyncResult'],
    *['configured_statement_dir', 'discover_statement_files', 'normalize_contract', '_clean_label', '_number', '_integer', '_text', '_date_text', '_time_text', '_read_sheet', '_value_after_label', '_deduplicate_headers', '_table_after_header', '_section_table', '_statement_end_date', '_parse_account', '_parse_declaration_fee', '_parse_statement_cash_flows', '_position_multiplier', '_parse_positions', '_trade_multiplier', '_parse_futures_trades', '_parse_option_trades', 'parse_statement', '_file_hash', '_json_list', '_insert_statement_payload', '_backfill_position_multipliers', '_manual_match_candidates', '_reconcile_manual_trades', '_reconcile_manual_cash_flows', '_reconcile_option_expiry_events', 'sync_statements', 'list_statement_imports', 'list_monthly_accounts', 'latest_monthly_account', 'list_futures_cash_flows', '_effective_cash_flows', 'add_manual_cash_flow', 'delete_manual_cash_flow', 'list_futures_daily_pnl_overrides', 'add_manual_daily_pnl', 'delete_manual_daily_pnl', 'resolve_manual_daily_pnl', 'list_month_end_positions', 'list_futures_live_trades', '_effective_manual_trades', '_known_multiplier', '_apply_manual_to_positions', '_build_estimated_positions_base', '_option_contract_parts', 'iron_ore_option_expiry_date', 'list_option_expiry_events', '_effective_option_expiry_events', '_apply_option_expiry_events', 'build_estimated_positions', 'list_option_expiry_candidates', 'confirm_option_expiry_event', 'delete_manual_option_expiry_event', 'add_manual_trade', 'delete_manual_trade', 'load_daily_closes', '_save_daily_close_frame', '_infer_previous_settlement', '_save_settlement_price', '_save_daily_settlement_frame', '_update_position_settlements', 'update_position_daily_closes', '_historical_contract_requirements', 'update_traded_contract_daily_closes', '_fetch_futures_settlement_history', '_fetch_cffex_settlement_history', '_parse_dce_option_settlement_payload', '_fetch_dce_option_settlements_for_date', 'update_traded_contract_daily_settlements', '_position_prices_for_date', '_option_cashflow', '_trades_with_calculated_manual_close_pnl', 'build_current_position_pnl', 'build_contract_pnl_history', 'summarize_futures_live_pnl', '_futures_trading_dates', '_daily_fee_adjustments', 'build_daily_account_pnl', '_apply_manual_daily_pnl_overrides', 'build_futures_daily_returns'],
]
_FACADE_DEFAULTS.update({name: globals()[name] for name in __all__})
