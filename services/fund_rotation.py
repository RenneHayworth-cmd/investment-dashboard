from __future__ import annotations

import pandas as pd

from services import fund_rotation_data as _data
from services import fund_rotation_momentum as _momentum
from services import fund_rotation_timing as _timing
from services.fund_rotation_data import (
    _find_column,
    _first_text,
    _normalize_date_range,
    _prepare_merged_data,
)
from services.fund_rotation_metrics import (
    _calculate_drawdown,
    _calculate_individual_nav_data,
    _calculate_individual_results,
    _calculate_nav_returns,
    _calculate_sharpe_ratio,
    _calculate_trade_win_stats,
    _calculate_yearly_stats,
    _round_lot_shares,
)
from services.fund_rotation_models import (
    BUY_SLIPPAGE,
    DATE_COLUMNS,
    EXECUTION_AFTER_CLOSE,
    EXECUTION_MODES,
    EXECUTION_NEXT_OPEN,
    LOT_SIZE,
    OPEN_COLUMNS,
    PORTFOLIO_STRATEGIES,
    PORTFOLIO_EMPTY_ACTIVATION_AFTER_PRIMARY_ENTRY,
    PORTFOLIO_EMPTY_ACTIVATION_IMMEDIATE,
    PORTFOLIO_EMPTY_ACTIVATIONS,
    PORTFOLIO_INITIAL_ENTRY_FOLLOW_STATE,
    PORTFOLIO_INITIAL_ENTRY_FRESH_BUY,
    PORTFOLIO_INITIAL_ENTRY_POLICIES,
    PORTFOLIO_STRATEGY_CASH,
    PORTFOLIO_STRATEGY_HALF_TIMING,
    PORTFOLIO_STRATEGY_HOLD,
    PORTFOLIO_STRATEGY_TIMING,
    PRICE_COLUMNS,
    SELL_SLIPPAGE,
    STANDARD_BACKTEST_PERIODS,
    PortfolioTimingAllocation,
    PortfolioTimingResult,
    RotationInput,
    RotationResult,
    TimingBacktestResult,
)
from services.fund_rotation_momentum import (
    _align_rebalance_start,
    _build_rebalance_dates,
    _build_rotation_plan,
    _calculate_momentum,
    _find_tradeable_date,
    _find_valid_date,
    _get_start_date,
    _has_execution_price,
    _holding_amount_detail,
    _next_rebalance_date,
    _portfolio_value,
    _row_price,
    _select_top_symbols,
    _trade_lot_size,
    _trade_price,
    _trade_uses_slippage,
)
from services.fund_rotation_summary import (
    _build_summary,
    _build_timing_summary,
    _execution_mode_label,
)

# Keep dataclass identity and pickle paths compatible with the historical module.
for _compat_type in (
    RotationInput,
    RotationResult,
    TimingBacktestResult,
    PortfolioTimingAllocation,
    PortfolioTimingResult,
):
    _compat_type.__module__ = __name__


def normalize_rotation_dataframe(df: pd.DataFrame, fallback_name: str) -> RotationInput:
    """将上传或缓存行情标准化为轮动回测输入。"""

    return _data.normalize_rotation_dataframe(df, fallback_name)


def build_standard_backtest_periods(
    end_date: str | pd.Timestamp,
) -> list[tuple[str, pd.Timestamp | None]]:
    """返回以所选结束日为锚点的标准回测区间。"""

    return _data.build_standard_backtest_periods(end_date)


def run_ma20_timing_backtest(
    fund: RotationInput,
    ma_period: int = 20,
    threshold_pct: float = 0.0,
    initial_capital: float = 100000.0,
    transaction_cost: float = 0.00006,
    lot_size: int = 100,
    start_date: str | pd.Timestamp | None = None,
    end_date: str | pd.Timestamp | None = None,
) -> TimingBacktestResult:
    """运行单标的均线择时回测。"""

    return _timing.run_ma20_timing_backtest(
        fund=fund,
        ma_period=ma_period,
        threshold_pct=threshold_pct,
        initial_capital=initial_capital,
        transaction_cost=transaction_cost,
        lot_size=lot_size,
        start_date=start_date,
        end_date=end_date,
    )


def run_portfolio_timing_backtest(
    funds: list[RotationInput],
    allocations: list[PortfolioTimingAllocation],
    initial_capital: float = 100000.0,
    transaction_cost: float = 0.00006,
    lot_size: int = 100,
    start_date: str | pd.Timestamp | None = None,
    end_date: str | pd.Timestamp | None = None,
) -> PortfolioTimingResult:
    """运行多标的配置择时回测。"""

    return _timing.run_portfolio_timing_backtest(
        funds=funds,
        allocations=allocations,
        initial_capital=initial_capital,
        transaction_cost=transaction_cost,
        lot_size=lot_size,
        start_date=start_date,
        end_date=end_date,
        _timing_runner=run_ma20_timing_backtest,
    )


def run_fund_rotation_backtest(
    funds: list[RotationInput],
    frequency: str = "week",
    lookback_period: int = 22,
    num_positions: int = 1,
    initial_capital: float = 100000.0,
    transaction_cost: float = 0.00006,
    start_date: str | pd.Timestamp | None = None,
    end_date: str | pd.Timestamp | None = None,
    execution_mode: str = EXECUTION_AFTER_CLOSE,
) -> RotationResult:
    """运行基金动量轮动回测。"""

    return _momentum.run_fund_rotation_backtest(
        funds=funds,
        frequency=frequency,
        lookback_period=lookback_period,
        num_positions=num_positions,
        initial_capital=initial_capital,
        transaction_cost=transaction_cost,
        start_date=start_date,
        end_date=end_date,
        execution_mode=execution_mode,
    )


__all__ = [
    "DATE_COLUMNS",
    "PRICE_COLUMNS",
    "OPEN_COLUMNS",
    "BUY_SLIPPAGE",
    "SELL_SLIPPAGE",
    "LOT_SIZE",
    "STANDARD_BACKTEST_PERIODS",
    "EXECUTION_AFTER_CLOSE",
    "EXECUTION_NEXT_OPEN",
    "EXECUTION_MODES",
    "PORTFOLIO_STRATEGY_HOLD",
    "PORTFOLIO_STRATEGY_TIMING",
    "PORTFOLIO_STRATEGY_HALF_TIMING",
    "PORTFOLIO_STRATEGY_CASH",
    "PORTFOLIO_STRATEGIES",
    "PORTFOLIO_INITIAL_ENTRY_FOLLOW_STATE",
    "PORTFOLIO_INITIAL_ENTRY_FRESH_BUY",
    "PORTFOLIO_INITIAL_ENTRY_POLICIES",
    "PORTFOLIO_EMPTY_ACTIVATION_IMMEDIATE",
    "PORTFOLIO_EMPTY_ACTIVATION_AFTER_PRIMARY_ENTRY",
    "PORTFOLIO_EMPTY_ACTIVATIONS",
    "RotationInput",
    "RotationResult",
    "TimingBacktestResult",
    "PortfolioTimingAllocation",
    "PortfolioTimingResult",
    "normalize_rotation_dataframe",
    "build_standard_backtest_periods",
    "run_ma20_timing_backtest",
    "run_portfolio_timing_backtest",
    "run_fund_rotation_backtest",
    "_normalize_date_range",
    "_find_column",
    "_first_text",
    "_prepare_merged_data",
    "_get_start_date",
    "_align_rebalance_start",
    "_find_valid_date",
    "_next_rebalance_date",
    "_build_rebalance_dates",
    "_build_rotation_plan",
    "_find_tradeable_date",
    "_calculate_momentum",
    "_select_top_symbols",
    "_row_price",
    "_trade_price",
    "_has_execution_price",
    "_execution_mode_label",
    "_trade_lot_size",
    "_trade_uses_slippage",
    "_round_lot_shares",
    "_portfolio_value",
    "_holding_amount_detail",
    "_calculate_drawdown",
    "_calculate_individual_results",
    "_calculate_individual_nav_data",
    "_calculate_yearly_stats",
    "_calculate_sharpe_ratio",
    "_calculate_nav_returns",
    "_calculate_trade_win_stats",
    "_build_summary",
    "_build_timing_summary",
]
