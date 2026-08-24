from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import floor
from typing import Iterable

import numpy as np
import pandas as pd

from services.portfolio_audit_data import (
    _normalize_price_frame,
    normalize_audit_market_data as _normalize_audit_market_data_impl,
    validate_audit_inputs as _validate_audit_inputs_impl,
)
from services.portfolio_audit_engine import (
    _cash_series,
    _combine_sleeves,
    _common_dates,
    run_portfolio_audit_engine,
)
from services.portfolio_audit_execution import (
    _execute_order,
    _pending_due,
    _run_sleeve,
)
from services.portfolio_audit_metrics import (
    _max_consecutive_losses,
    calculate_performance_summary as _calculate_performance_summary_impl,
    position_statistics as _position_statistics_impl,
)
from services.portfolio_audit_models import (
    AuditAllocation,
    AuditRunResult,
    AuditSettings,
    EXECUTION_AFTER_CLOSE,
    EXECUTION_MODES,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_NEXT_OPEN,
    EXECUTION_T2_OPEN,
    MISSED_ORDER_BOTH,
    MISSED_ORDER_BUY,
    MISSED_ORDER_NONE,
    MISSED_ORDER_SELL,
    MISSED_ORDER_SIDES,
    _SleeveResult,
)

__all__ = [
    "AuditAllocation",
    "AuditRunResult",
    "AuditSettings",
    "EXECUTION_AFTER_CLOSE",
    "EXECUTION_MODES",
    "EXECUTION_NEXT_CLOSE",
    "EXECUTION_NEXT_OPEN",
    "EXECUTION_T2_OPEN",
    "MISSED_ORDER_BOTH",
    "MISSED_ORDER_BUY",
    "MISSED_ORDER_NONE",
    "MISSED_ORDER_SELL",
    "MISSED_ORDER_SIDES",
    "calculate_performance_summary",
    "normalize_audit_market_data",
    "position_statistics",
    "run_portfolio_audit",
    "validate_audit_inputs",
]


for _compatibility_type in (AuditAllocation, AuditRunResult, AuditSettings):
    _compatibility_type.__module__ = __name__
del _compatibility_type


def normalize_audit_market_data(
    raw_df: pd.DataFrame,
    adjusted_df: pd.DataFrame,
    dividend_df: pd.DataFrame | None = None,
    share_split_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compatibility entry point for audit market-data normalization."""
    return _normalize_audit_market_data_impl(
        raw_df,
        adjusted_df,
        dividend_df,
        share_split_df,
    )


def validate_audit_inputs(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
) -> None:
    """Compatibility entry point for audit input validation."""
    return _validate_audit_inputs_impl(market_data, allocations, settings)


def run_portfolio_audit(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings | None = None,
    *,
    blocked_entries: set[tuple[str, pd.Timestamp]] | None = None,
) -> AuditRunResult:
    """Compatibility entry point for the split audit engine."""
    return run_portfolio_audit_engine(
        market_data,
        allocations,
        settings,
        blocked_entries=blocked_entries,
        _validate_inputs=validate_audit_inputs,
        _performance_summary=calculate_performance_summary,
    )


def calculate_performance_summary(
    daily: pd.DataFrame, initial_capital: float
) -> dict[str, object]:
    """Compatibility entry point for portfolio performance metrics."""
    return _calculate_performance_summary_impl(daily, initial_capital)


def position_statistics(daily: pd.DataFrame) -> pd.DataFrame:
    """Compatibility entry point for exposure statistics."""
    return _position_statistics_impl(daily)
