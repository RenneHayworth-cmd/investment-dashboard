from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

DATE_COLUMNS = ("trade_date", "日期", "date", "datetime", "time", "净值日期")
PRICE_COLUMNS = ("close", "收盘价", "收盘", "累计净值", "复权净值", "单位净值", "nav", "price")
OPEN_COLUMNS = ("open", "开盘价", "开盘")
BUY_SLIPPAGE = 0.0005
SELL_SLIPPAGE = 0.0005
LOT_SIZE = 100
STANDARD_BACKTEST_PERIODS = ("近一年", "今年来", "近三年", "近五年", "成立来")
EXECUTION_AFTER_CLOSE = "after_close"
EXECUTION_NEXT_OPEN = "next_open"
EXECUTION_MODES = (EXECUTION_AFTER_CLOSE, EXECUTION_NEXT_OPEN)
PORTFOLIO_STRATEGY_HOLD = "hold"
PORTFOLIO_STRATEGY_TIMING = "timing"
PORTFOLIO_STRATEGY_HALF_TIMING = "half_timing"
PORTFOLIO_STRATEGY_CASH = "cash"
PORTFOLIO_STRATEGIES = (
    PORTFOLIO_STRATEGY_HOLD,
    PORTFOLIO_STRATEGY_TIMING,
    PORTFOLIO_STRATEGY_HALF_TIMING,
    PORTFOLIO_STRATEGY_CASH,
)


@dataclass
class RotationInput:
    symbol: str
    name: str
    dataframe: pd.DataFrame
    trade_lot_size: int = 100
    apply_slippage: bool = True


@dataclass
class RotationResult:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    nav_data: pd.DataFrame
    trades: pd.DataFrame
    summary: dict[str, object]
    individual_results: pd.DataFrame = field(default_factory=pd.DataFrame)
    individual_nav_data: pd.DataFrame = field(default_factory=pd.DataFrame)
    drawdown: pd.DataFrame = field(default_factory=pd.DataFrame)
    yearly_stats: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class TimingBacktestResult:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    data: pd.DataFrame
    trades: pd.DataFrame
    drawdown: pd.DataFrame
    yearly_stats: pd.DataFrame
    summary: dict[str, object]


@dataclass(frozen=True)
class PortfolioTimingAllocation:
    symbol: str
    name: str
    weight_pct: float
    strategy: str
    ma_period: int = 20
    threshold_pct: float = 1.0


@dataclass
class PortfolioTimingResult:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    nav_data: pd.DataFrame
    trades: pd.DataFrame
    drawdown: pd.DataFrame
    yearly_stats: pd.DataFrame
    summary: dict[str, object]
    component_results: pd.DataFrame = field(default_factory=pd.DataFrame)

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
    "RotationInput",
    "RotationResult",
    "TimingBacktestResult",
    "PortfolioTimingAllocation",
    "PortfolioTimingResult",
]
