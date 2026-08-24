from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


EXECUTION_AFTER_CLOSE = "after_close"
EXECUTION_NEXT_OPEN = "next_open"
EXECUTION_NEXT_CLOSE = "next_close"
EXECUTION_T2_OPEN = "t2_open"
EXECUTION_MODES = (
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_OPEN,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_T2_OPEN,
)
MISSED_ORDER_BOTH = "both"
MISSED_ORDER_BUY = "buy"
MISSED_ORDER_SELL = "sell"
MISSED_ORDER_NONE = "none"
MISSED_ORDER_SIDES = (
    MISSED_ORDER_BOTH,
    MISSED_ORDER_BUY,
    MISSED_ORDER_SELL,
    MISSED_ORDER_NONE,
)


@dataclass(frozen=True)
class AuditAllocation:
    symbol: str
    name: str
    weight_pct: float
    strategy: str
    ma_period: int = 20
    threshold_pct: float = 1.0
    signal_rule: str = "percent"
    atr_k: float = 0.0
    sigma_period: int = 60
    buy_k: float = 0.0
    sell_k: float = 0.0
    buy_alpha_pct: float = 0.0
    sell_alpha_pct: float = 0.0


@dataclass(frozen=True)
class AuditSettings:
    initial_capital: float = 100000.0
    commission_rate: float = 0.00006
    lot_size: int = 100
    execution_mode: str = EXECUTION_AFTER_CLOSE
    after_hours_fill_rate: float = 1.0
    slippage_bp: float = 0.0
    cash_annual_rate: float = 0.0
    random_seed: int = 20260727
    missed_signal_rate: float = 0.0
    missed_order_side: str = MISSED_ORDER_BOTH
    start_date: str | pd.Timestamp | None = None
    end_date: str | pd.Timestamp | None = None


@dataclass
class AuditRunResult:
    summary: dict[str, object]
    daily: pd.DataFrame
    trades: pd.DataFrame
    contribution: pd.DataFrame
    component_daily: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class _SleeveResult:
    daily: pd.DataFrame
    trades: pd.DataFrame
    summary: dict[str, float]
