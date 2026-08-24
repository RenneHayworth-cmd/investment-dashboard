from __future__ import annotations

from dataclasses import replace
from typing import Iterable

import numpy as np
import pandas as pd

from services.portfolio_audit import (
    AuditAllocation,
    AuditRunResult,
    AuditSettings,
    calculate_performance_summary,
    run_portfolio_audit,
)
from services.portfolio_audit_analysis_core import (
    _component_drawdown_contribution,
    _fast_training_score,
    _metric_subset,
    _off_diagonal_mean,
    _simultaneous_transition_ratio,
    add_contribution_diagnostics as _add_contribution_diagnostics_impl,
    atr_parameter_analysis as _atr_parameter_analysis_impl,
    build_benchmarks as _build_benchmarks_impl,
    custom_parameter_attribution as _custom_parameter_attribution_impl,
    drawdown_attribution as _drawdown_attribution_impl,
    parameter_grid_analysis as _parameter_grid_analysis_impl,
    parameter_robustness as _parameter_robustness_impl,
    risk_bucket_analysis as _risk_bucket_analysis_impl,
    same_exposure_equal_weight_benchmark as _same_exposure_equal_weight_benchmark_impl,
    stress_test_analysis as _stress_test_analysis_impl,
    time_split_analysis as _time_split_analysis_impl,
    walk_forward_analysis as _walk_forward_analysis_impl,
)
from services.portfolio_audit_analysis_full_history import (
    full_history_validation as _full_history_validation_impl,
)
from services.portfolio_audit_analysis_missed_orders import (
    _missed_order_sleeve_specs,
    _simulate_missed_order_paths,
    missed_order_simulation_analysis as _missed_order_simulation_analysis_impl,
)

__all__ = [
    "add_contribution_diagnostics",
    "atr_parameter_analysis",
    "build_benchmarks",
    "custom_parameter_attribution",
    "drawdown_attribution",
    "full_history_validation",
    "missed_order_simulation_analysis",
    "parameter_grid_analysis",
    "parameter_robustness",
    "risk_bucket_analysis",
    "same_exposure_equal_weight_benchmark",
    "stress_test_analysis",
    "time_split_analysis",
    "walk_forward_analysis",
]


def build_benchmarks(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    strategy_result: AuditRunResult,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    return _build_benchmarks_impl(market_data, allocations, settings, strategy_result)


def custom_parameter_attribution(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    baseline: AuditRunResult,
) -> pd.DataFrame:
    return _custom_parameter_attribution_impl(market_data, allocations, settings, baseline)


def same_exposure_equal_weight_benchmark(
    market_data: dict[str, pd.DataFrame],
    symbols: list[str],
    strategy_daily: pd.DataFrame,
    settings: AuditSettings,
) -> pd.DataFrame:
    return _same_exposure_equal_weight_benchmark_impl(market_data, symbols, strategy_daily, settings)


def parameter_grid_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    ma_periods: Iterable[int],
    threshold_pcts: Iterable[float],
) -> pd.DataFrame:
    return _parameter_grid_analysis_impl(market_data, allocations, settings, ma_periods, threshold_pcts)


def parameter_robustness(grid: pd.DataFrame) -> pd.DataFrame:
    return _parameter_robustness_impl(grid)


def atr_parameter_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    k_values: Iterable[float],
) -> pd.DataFrame:
    return _atr_parameter_analysis_impl(market_data, allocations, settings, k_values)


def time_split_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    grid: pd.DataFrame,
    splits: Iterable[float],
) -> pd.DataFrame:
    return _time_split_analysis_impl(market_data, allocations, settings, grid, splits)


def walk_forward_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    ma_periods: Iterable[int],
    threshold_pcts: Iterable[float],
    train_days: int = 180,
    test_days: int = 60,
) -> pd.DataFrame:
    return _walk_forward_analysis_impl(
        market_data, allocations, settings, ma_periods, threshold_pcts, train_days, test_days
    )


def stress_test_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    baseline: AuditRunResult,
    sensitivity: dict[str, list[float]],
) -> pd.DataFrame:
    return _stress_test_analysis_impl(market_data, allocations, settings, baseline, sensitivity)


def risk_bucket_analysis(
    result: AuditRunResult,
    market_data: dict[str, pd.DataFrame],
    risk_buckets: dict[str, list[str]],
) -> pd.DataFrame:
    return _risk_bucket_analysis_impl(result, market_data, risk_buckets)


def drawdown_attribution(
    result: AuditRunResult,
    risk_buckets: dict[str, list[str]],
    top_n: int = 5,
) -> pd.DataFrame:
    return _drawdown_attribution_impl(result, risk_buckets, top_n)


def add_contribution_diagnostics(result: AuditRunResult) -> pd.DataFrame:
    return _add_contribution_diagnostics_impl(result)


def missed_order_simulation_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    baseline: AuditRunResult,
    *,
    hold_annual_return_pct: float,
    unified_annual_return_pct: float,
    miss_rates: Iterable[float] = (0.0, 0.05, 0.10, 0.20),
    simulations: int = 1000,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    return _missed_order_simulation_analysis_impl(
        market_data,
        allocations,
        settings,
        baseline,
        hold_annual_return_pct=hold_annual_return_pct,
        unified_annual_return_pct=unified_annual_return_pct,
        miss_rates=miss_rates,
        simulations=simulations,
    )


def full_history_validation(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    *,
    research_end_date: str | pd.Timestamp,
    common_history_ratings: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return _full_history_validation_impl(
        market_data,
        allocations,
        settings,
        research_end_date=research_end_date,
        common_history_ratings=common_history_ratings,
    )
