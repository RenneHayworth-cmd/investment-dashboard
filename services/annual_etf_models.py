from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd


REGISTRY_VERSION = "annual-etf-registry-20260817-v1"
WHITELIST_VERSION = "annual-etf-index-families-20260817-v1"
ANNUAL_CACHE_SOURCE = "annual_etf"
ANNUAL_RAW_DATA_TYPE = "raw_history"
ANNUAL_DIVIDEND_DATA_TYPE = "corporate_actions"
ANNUAL_CACHE_PERIOD = "full_1d"
ANNUAL_DIVIDEND_CACHE_KEY = "annual_etf_dividends_v1"
DEFAULT_MA_PERIODS = (10, 15, 20, 25, 30)
DEFAULT_THRESHOLDS = (0.0, 0.5, 1.0, 1.5, 2.0)
SCORE_WEIGHTS = {
    "longest_underwater_days": 0.45,
    "max_drawdown_pct": 0.25,
    "annual_volatility_pct": 0.15,
    "annual_return_pct": 0.10,
    "sharpe_ratio": 0.05,
}
EXECUTION_SAME_CLOSE = "same_close"
EXECUTION_NEXT_CLOSE = "next_close"
PARKING_SYMBOL = "512890"
PARKING_LISTING_DATE = pd.Timestamp("2019-01-18")
US_SLOTS = ("us_sp500", "us_nasdaq")
NON_US_SLOTS = (
    "a_large",
    "a_mid_small",
    "a_growth",
    "smart_beta",
    "other_overseas",
    "gold",
)
ALL_SLOTS = (*US_SLOTS, *NON_US_SLOTS)
PARKING_SLOTS = {"a_mid_small", "a_growth"}


@dataclass(frozen=True)
class HistoricalEtfRecord:
    symbol: str
    name: str
    exchange: str
    listing_date: pd.Timestamp
    tracked_index: str
    index_family: str
    direction: str
    source_url: str
    source_as_of: str = ""
    proxy_symbol: str = ""
    proxy_path: str = ""
    proxy_type: str = ""
    proxy_available_date: pd.Timestamp | None = None
    proxy_source_url: str = ""
    active: bool = True
    product_type: str = "ETF"
    registry_version: str = ""
    snapshot_date: str = ""

    @property
    def tickflow_symbol(self) -> str:
        suffix = str(self.exchange).strip().upper()
        return f"{self.symbol}.{suffix}"


AnnualEtfRegistryEntry = HistoricalEtfRecord


@dataclass(frozen=True)
class AnnualBacktestSettings:
    start_year: int = 2019
    end_date: str | pd.Timestamp | None = None
    initial_capital: float = 500000.0
    commission_rate: float = 0.00006
    lot_size: int = 100
    cash_annual_rate: float = 0.015
    min_listing_days: int = 120
    turnover_window: int = 60
    min_turnover_days: int = 40
    max_history_years: int = 5
    train_ratio: float = 0.70
    min_train_days: int = 504
    min_validation_days: int = 252
    annual_return_gate_pct: float = 10.0
    ma_periods: tuple[int, ...] = DEFAULT_MA_PERIODS
    threshold_pcts: tuple[float, ...] = DEFAULT_THRESHOLDS
    registry_version: str = REGISTRY_VERSION
    whitelist_version: str = WHITELIST_VERSION


@dataclass(frozen=True)
class AnnualSelection:
    year: int
    slot: str
    symbol: str
    name: str
    ma_period: int
    threshold_pct: float
    strategy: str
    validation_score: float
    validation_annual_return_pct: float
    validation_sharpe: float
    return_gate_relaxed: bool
    proxy_ratio_pct: float
    decision_date: pd.Timestamp


@dataclass
class AnnualQualificationResult:
    qualification: pd.DataFrame
    research_data: dict[tuple[int, str], pd.DataFrame] = field(default_factory=dict)
    errors: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class AnnualPortfolioResult:
    summary: pd.DataFrame
    daily: pd.DataFrame
    yearly: pd.DataFrame
    selections: pd.DataFrame
    qualification: pd.DataFrame
    parameters: pd.DataFrame
    trades: pd.DataFrame
    migrations: pd.DataFrame
    contribution: pd.DataFrame
    errors: pd.DataFrame
    report_markdown: str
    fingerprint: str


@dataclass
class DirectionSleeveState:
    slot: str
    initial_capital: float
    cash: float
    current_symbol: str = ""
    current_name: str = ""
    current_strategy: str = "timing"
    ma_period: int = 20
    threshold_pct: float = 1.0
    parameter_effective_date: pd.Timestamp | None = None
    pending: AnnualSelection | None = None
    long_shares: float = 0.0
    timing_shares: float = 0.0
    parking_shares: float = 0.0
    long_cost: float = 0.0
    timing_cost: float = 0.0
    parking_cost: float = 0.0
    last_price: float = np.nan
    last_parking_price: float = np.nan

    @property
    def shares(self) -> float:
        return self.long_shares + self.timing_shares


def _empty_frame(columns: Iterable[str] = ()) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


def _normalize_symbol(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value or "").strip().upper()
    return text.split(".", 1)[0]


def _optional_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()
