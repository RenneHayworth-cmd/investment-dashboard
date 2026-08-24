from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd


ProgressCallback = Callable[[str, int, int, str, float | None], None]
INDEX_HISTORY_BOOTSTRAP_DAYS = 1000
INDEX_HISTORY_BOOTSTRAP_BARS = 1000
INDEX_HISTORY_MIN_ROWS = 252
INDEX_HISTORY_INCREMENTAL_DAYS = 30
INDEX_VERIFICATION_TOLERANCE_PCT = 0.20
FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE = "index_futures_current_contract_history"
FUTURES_MAIN_CONTRACT_CACHE_SYMBOL = "index_futures_main_contracts"
FUTURES_MAIN_CONTRACT_CACHE_SOURCE = "index_metadata"
FUTURES_MAIN_INDEX_NAMES = {
    "中证500期货主连",
    "中证1000期货主连",
    "铁矿石主连",
    "沪金主连",
    "沪银主连",
    "原油主连",
}



@dataclass
class UpdateResult:
    status: str
    message: str
    dataframe: pd.DataFrame | None = None
    errors: list[str] = field(default_factory=list)
    timings: list[dict] = field(default_factory=list)

__all__ = ["ProgressCallback", "INDEX_HISTORY_BOOTSTRAP_DAYS", "INDEX_HISTORY_BOOTSTRAP_BARS", "INDEX_HISTORY_MIN_ROWS", "INDEX_HISTORY_INCREMENTAL_DAYS", "INDEX_VERIFICATION_TOLERANCE_PCT", "FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE", "FUTURES_MAIN_CONTRACT_CACHE_SYMBOL", "FUTURES_MAIN_CONTRACT_CACHE_SOURCE", "FUTURES_MAIN_INDEX_NAMES", "UpdateResult"]
