from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time
import logging
import re

import pandas as pd

from services.futures_options_analysis import DATA_TYPE_AUTO, DATA_TYPE_OPTIONS, normalize_option_symbol
from services.futures_spread import (
    SPREAD_CALCULATION_VERSION,
    contract_name,
    parse_contracts,
    spread_respects_contract_cutoffs,
)

logger = logging.getLogger("services.position_analysis")

DEFAULT_ETF_CODES = [
    "512890",
    "159201",
    "159545",
    "513260",
    "159655",
    "159501",
    "161128",
    "518850",
    "588000",
    "159915",
    "510500",
    "159967",
    "159552",
    "513310",
    "513880",
]


DEFAULT_SPREAD_CONTRACTS = ["I2701", "I2705"]


DEFAULT_SPREAD_GROUPS = [
    DEFAULT_SPREAD_CONTRACTS.copy(),
    ["IM2609", "IM2703"],
]


DEFAULT_FUTURES_CONTRACTS = ["I2701"]


DEFAULT_OPTION_CODES: list[str] = []


ETF_FINAL_CLOSE_READY_TIME = datetime_time(15, 5)


ETF_MORNING_TIMING_START_TIME = datetime_time(9, 30)


ETF_MORNING_FAST_REFRESH_END_TIME = datetime_time(10, 0)


ETF_MORNING_TIMING_PREVIEW_END_TIME = datetime_time(11, 30)


ETF_MORNING_TIMING_REFRESH_SECONDS = 600


ETF_MIDSESSION_TIMING_REFRESH_SECONDS = 1800


ETF_LUNCH_TIMING_START_TIME = datetime_time(11, 30)


ETF_LUNCH_TIMING_FETCH_END_TIME = datetime_time(13, 0)


ETF_AFTERNOON_TIMING_START_TIME = datetime_time(13, 0)


ETF_REALTIME_TIMING_START_TIME = datetime_time(14, 50)


ETF_REALTIME_TIMING_END_TIME = datetime_time(15, 0)


ETF_REALTIME_TIMING_REFRESH_SECONDS = 120


ETF_DISPLAY_NAMES = {
    "512890": "红利低波ETF华泰柏瑞",
    "159201": "自由现金流ETF华夏",
    "159545": "恒生红利低波ETF易方达",
    "513260": "恒生科技ETF汇添富",
    "159655": "标普500ETF华夏",
    "159501": "纳指ETF嘉实",
    "161128": "标普信息科技LOF易方达",
    "518850": "黄金ETF华夏",
    "588000": "科创50ETF华夏",
    "159915": "创业板ETF易方达",
    "510500": "中证500ETF南方",
    "159967": "创业板成长ETF华夏",
    "159552": "中证2000增强ETF招商",
    "513310": "中韩半导体ETF华泰柏瑞",
    "513880": "日经225ETF华安",
}


ETF_TIMING_STRATEGIES = {
    "513260": (20, 1.0),
    "159915": (20, 1.0),
    "588000": (20, 1.0),
    "510500": (15, 1.0),
    "159201": (20, 0.5),
    "159655": (25, 2.0),
    "159501": (25, 2.0),
    "161128": (25, 1.5),
    "159545": (10, 1.0),
    "159967": (25, 2.0),
    "518850": (30, 1.5),
    "159552": (10, 2.5),
    "513310": (15, 0.5),
    "513880": (10, 2.0),
}


ETF_TIMING_TABLE_EXCLUDED_CODES = {"512890"}


ETF_PORTFOLIO_WEIGHTS_PCT = {
    "159201": 5,
    "159545": 10,
    "159655": 10,
    "159501": 15,
    "518850": 10,
    "510500": 10,
    "159967": 10,
    "159552": 10,
    "513310": 10,
    "513880": 10,
}


ETF_512890_TRANSFER_SOURCE_CODES = {
    "588000",
    "159915",
    "510500",
    "159967",
    "159552",
}


ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES = (
    "510500",
    "159967",
    "159552",
)


ETF_POSITION_STRATEGIES = {
    "159655": "半仓持有半仓择时",
    "159501": "半仓持有半仓择时",
    "161128": "纯择时",
    "159201": "纯择时",
    "159545": "纯择时",
    "518850": "纯择时",
    "513260": "纯择时",
    "588000": "纯择时",
    "159915": "纯择时",
    "510500": "纯择时",
    "159967": "纯择时",
    "159552": "纯择时",
    "513310": "纯择时",
    "513880": "纯择时",
}


ETF_AKSHARE_HISTORY_CODES = {"161128"}


ETF_SINA_REALTIME_FALLBACK_CODES = {"161128"}


SINA_REQUEST_TIMEOUT_SECONDS = 15


OPTION_PRODUCT_NAMES = {
    "i": "铁矿石",
    "io": "沪深300股指",
    "ho": "上证50股指",
    "mo": "中证1000股指",
}


@dataclass
class PositionItem:
    category: str
    code: str
    name: str
    status: str
    source: str = ""
    latest_date: str = ""
    cache_time: str = ""
    metrics: dict[str, object] = field(default_factory=dict)
    dataframe: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: str = ""
    formal_history_valid: bool = True


def normalize_etf_base_code(code: str) -> str:
    match = re.search(r"\d{6}", str(code))
    return match.group(0) if match else str(code).strip().upper()


def display_etf_name(code: str, fallback: str) -> str:
    return ETF_DISPLAY_NAMES.get(normalize_etf_base_code(code), str(fallback))


def parse_position_codes(text: str) -> list[str]:
    return parse_contracts(text)


def parse_spread_groups(text: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for line in str(text or "").replace(";", "\n").splitlines():
        contracts = parse_position_codes(line)
        if contracts:
            groups.append(contracts)
    return groups


def format_spread_position_name(base_contract: str, other_contract: str) -> str:
    base_contract = base_contract.strip().upper()
    other_contract = other_contract.strip().upper()
    if not other_contract:
        return contract_name(base_contract)
    product_match = re.search(r"\(([^()]*)\)$", contract_name(base_contract))
    product_name = product_match.group(1) if product_match else ""
    suffix = f" ({product_name})" if product_name else ""
    return f"{base_contract} - {other_contract}{suffix}"


def format_futures_position_name(contract: str) -> str:
    contract = contract.strip().upper()
    product_match = re.search(r"\(([^()]*)\)$", contract_name(contract))
    product_name = product_match.group(1) if product_match else ""
    return f"{contract} ({product_name}期货)" if product_name else f"{contract} 期货"


def format_cache_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def _round_metric(value: object, digits: int = 2) -> object:
    if value is None or pd.isna(value):
        return float("nan")
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def _current_cache_time_text() -> str:
    return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")


def _futures_option_cache_key(symbol: str, data_type: str, period: str, count: int) -> str:
    safe_symbol = symbol.strip().replace(".", "_").replace("/", "_")
    return f"futures_option_{safe_symbol}_{data_type}_{period}_{int(count)}"


def _futures_contract_cache_key(contract: str) -> str:
    safe_contract = contract.strip().upper().replace(".", "_").replace("/", "_")
    return f"futures_contract_{safe_contract}"


def _futures_option_cache_candidates(symbol: str, period: str, count: int) -> list[str]:
    symbols = []
    for candidate in (symbol.strip(), normalize_option_symbol(symbol), symbol.strip().upper()):
        if candidate and candidate not in symbols:
            symbols.append(candidate)

    keys = []
    for candidate in symbols:
        for data_type in (DATA_TYPE_OPTIONS, DATA_TYPE_AUTO):
            key = _futures_option_cache_key(candidate, data_type, period, count)
            if key not in keys:
                keys.append(key)
    return keys


def option_display_name(symbol: str) -> str:
    display_code = normalize_option_symbol(symbol)
    match = re.match(r"^([a-z]+)\d{4}([CP])?(\d+)?$", display_code)
    if not match:
        return f"{display_code} 期权"
    product, option_type, _strike = match.groups()
    product_name = OPTION_PRODUCT_NAMES.get(product, product.upper())
    side_name = "看涨" if option_type == "C" else "看跌" if option_type == "P" else ""
    return f"{display_code} {product_name}{side_name}期权"


def _market_cache_is_usable(df: pd.DataFrame | None) -> bool:
    return df is not None and not df.empty and "date" in df.columns and "close" in df.columns


def _spread_cache_matches_contracts(df: pd.DataFrame | None, contracts: list[str], base_contract: str) -> bool:
    if df is None or df.empty or "date" not in df.columns:
        return False
    required_columns = [f"{base_contract}_close"]
    required_columns.extend(
        f"spread_{base_contract}_vs_{contract}"
        for contract in contracts
        if contract != base_contract
    )
    if not all(column in df.columns for column in required_columns):
        return False
    if "_calculation_version" not in df.columns:
        return False
    if not df["_calculation_version"].eq(SPREAD_CALCULATION_VERSION).all():
        return False
    return spread_respects_contract_cutoffs(df, contracts, base_contract)


def _missing_item(category: str, code: str, name: str = "") -> PositionItem:
    return PositionItem(
        category=category,
        code=code,
        name=name or code,
        status="无缓存",
        error="本地暂无缓存；点击「加载持仓信息」可联网补齐。",
        formal_history_valid=False if category == "ETF" else True,
    )
