from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timedelta
import json
import logging
import re
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from core.cache import load_dataset, save_dataset
from services.fund_analysis import (
    analyze_fund_nav,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
    normalize_nav_dataframe,
)
from services.futures_options_analysis import (
    DATA_TYPE_AUTO,
    DATA_TYPE_OPTIONS,
    FUTURES_OPTION_DATA_VERSION,
    add_indicators,
    append_option_spot_row,
    build_summary as build_futures_option_summary,
    fetch_futures_option_data,
    normalize_option_symbol,
)
from services.futures_spread import (
    SPREAD_CALCULATION_VERSION,
    append_futures_spot_row,
    build_spread_summary,
    calculate_spreads,
    contract_name,
    fetch_contracts,
    parse_contracts,
    spread_respects_contract_cutoffs,
)
from services.market_calendar import (
    expected_latest_trade_date,
    get_market_window,
    is_market_trading_day,
    previous_trading_day,
)


logger = logging.getLogger(__name__)


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
]
DEFAULT_SPREAD_CONTRACTS = ["I2609", "I2705"]
DEFAULT_SPREAD_GROUPS = [
    DEFAULT_SPREAD_CONTRACTS.copy(),
    ["IM2609", "IM2703"],
]
DEFAULT_OPTION_CODES = ["I2609P730", "I2609P740", "I2609P750", "I2609P760"]
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
}
ETF_TIMING_TABLE_EXCLUDED_CODES = {"512890"}
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
}
ETF_AKSHARE_HISTORY_CODES = {"161128"}
ETF_SINA_REALTIME_FALLBACK_CODES = {"161128"}
SINA_REQUEST_TIMEOUT_SECONDS = 15
_RUNTIME_ETF_QUOTE_CACHE: dict[str, dict[str, object]] = {}
_RUNTIME_ETF_QUOTE_CACHE_LOCK = Lock()

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


def normalize_etf_base_code(code: str) -> str:
    match = re.search(r"\d{6}", str(code))
    return match.group(0) if match else str(code).strip().upper()


def display_etf_name(code: str, fallback: str) -> str:
    return ETF_DISPLAY_NAMES.get(normalize_etf_base_code(code), str(fallback))


def etf_final_close_ready(market_now: datetime | None = None) -> bool:
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return bool(
        market is not None
        and is_market_trading_day(market, market_now)
        and market_now.time() >= ETF_FINAL_CLOSE_READY_TIME
    )


def etf_intraday_quote_ready(market_now: datetime | None = None) -> bool:
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return bool(
        market is not None
        and is_market_trading_day(market, market_now)
        and market.sessions[0][0] <= market_now.time() < ETF_FINAL_CLOSE_READY_TIME
    )


def etf_realtime_timing_ready(market_now: datetime | None = None) -> bool:
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return bool(
        market is not None
        and is_market_trading_day(market, market_now)
        and ETF_REALTIME_TIMING_START_TIME
        <= market_now.time()
        < ETF_REALTIME_TIMING_END_TIME
    )


def etf_morning_timing_fetch_ready(market_now: datetime | None = None) -> bool:
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return bool(
        market is not None
        and is_market_trading_day(market, market_now)
        and ETF_MORNING_TIMING_START_TIME
        <= market_now.time()
        < ETF_MORNING_TIMING_PREVIEW_END_TIME
    )


def etf_morning_timing_preview_ready(market_now: datetime | None = None) -> bool:
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return bool(
        market is not None
        and is_market_trading_day(market, market_now)
        and ETF_MORNING_TIMING_START_TIME
        <= market_now.time()
        < ETF_MORNING_TIMING_PREVIEW_END_TIME
    )


def etf_lunch_timing_fetch_ready(market_now: datetime | None = None) -> bool:
    """Allow one lunch-close quote fetch while the A-share market is paused."""
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return bool(
        market is not None
        and is_market_trading_day(market, market_now)
        and ETF_LUNCH_TIMING_START_TIME
        <= market_now.time()
        < ETF_LUNCH_TIMING_FETCH_END_TIME
    )


def etf_lunch_timing_preview_ready(market_now: datetime | None = None) -> bool:
    """Keep the captured lunch-close preview visible until the closing preview starts."""
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return bool(
        market is not None
        and is_market_trading_day(market, market_now)
        and ETF_LUNCH_TIMING_START_TIME
        <= market_now.time()
        < ETF_REALTIME_TIMING_START_TIME
    )


def etf_afternoon_timing_fetch_ready(market_now: datetime | None = None) -> bool:
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return bool(
        market is not None
        and is_market_trading_day(market, market_now)
        and ETF_AFTERNOON_TIMING_START_TIME
        <= market_now.time()
        < ETF_REALTIME_TIMING_START_TIME
    )


def _tickflow_quote_datetime(row: pd.Series) -> datetime | None:
    timestamp = pd.to_numeric(row.get("timestamp"), errors="coerce")
    if not pd.isna(timestamp):
        unit = "ms" if float(timestamp) > 10_000_000_000 else "s"
        parsed = pd.to_datetime(float(timestamp), unit=unit, utc=True, errors="coerce")
        if not pd.isna(parsed):
            return parsed.tz_convert("Asia/Shanghai").to_pydatetime()
    trade_date = pd.to_datetime(row.get("trade_date"), errors="coerce")
    if pd.isna(trade_date):
        return None
    trade_time_value = row.get("trade_time")
    trade_time = "00:00:00" if pd.isna(trade_time_value) else str(trade_time_value)
    parsed = pd.to_datetime(f"{trade_date:%Y-%m-%d} {trade_time}", errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.tz_localize("Asia/Shanghai").to_pydatetime()


def fetch_tickflow_etf_quotes(
    codes: list[str],
    *,
    api_key: str,
    market_now: datetime | None = None,
) -> dict[str, dict[str, object]]:
    if not api_key.strip():
        raise ValueError("TickFlow实时行情需要填写API Key。")

    from tickflow import TickFlow

    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    symbols = [infer_tickflow_symbol(code) for code in codes]
    client = TickFlow(api_key=api_key)
    quote_frames = []
    for start in range(0, len(symbols), 5):
        batch = symbols[start:start + 5]
        batch_df = client.quotes.get(symbols=batch, as_dataframe=True)
        if batch_df is not None and not batch_df.empty:
            quote_frames.append(batch_df)
    if not quote_frames:
        raise ValueError("TickFlow未返回ETF实时行情。")
    quote_df = pd.concat(quote_frames, ignore_index=True)
    if "symbol" not in quote_df.columns:
        quote_df = quote_df.reset_index()

    quotes: dict[str, dict[str, object]] = {}
    for _, row in quote_df.iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        latest_price = pd.to_numeric(row.get("last_price"), errors="coerce")
        quote_time = _tickflow_quote_datetime(row)
        if not symbol or pd.isna(latest_price) or quote_time is None:
            continue
        if quote_time.date() != market_now.date():
            continue
        previous_close = pd.to_numeric(row.get("prev_close"), errors="coerce")
        if not pd.isna(previous_close) and float(previous_close) != 0:
            change_pct = (float(latest_price) / float(previous_close) - 1) * 100
        else:
            change_pct = pd.to_numeric(row.get("ext.change_pct"), errors="coerce")
        quotes[normalize_etf_base_code(symbol)] = {
            "symbol": symbol,
            "price": float(latest_price),
            "previous_close": None if pd.isna(previous_close) else float(previous_close),
            "change_pct": None if pd.isna(change_pct) else float(change_pct),
            "quote_time": quote_time,
        }

    for symbol in symbols:
        base_code = normalize_etf_base_code(symbol)
        if base_code not in ETF_SINA_REALTIME_FALLBACK_CODES or base_code in quotes:
            continue
        try:
            quotes[base_code] = _fetch_sina_exchange_fund_quote(
                symbol=symbol,
                market_now=market_now,
            )
        except Exception as exc:
            logger.warning("%s TickFlow实时行情缺失，新浪备用源也失败：%s", symbol, exc)

    if not quotes:
        raise ValueError("TickFlow未返回当天ETF实时行情。")
    return quotes


def remember_runtime_etf_quotes(quotes: dict[str, dict[str, object]]) -> None:
    if not quotes:
        return
    with _RUNTIME_ETF_QUOTE_CACHE_LOCK:
        for code, quote in quotes.items():
            _RUNTIME_ETF_QUOTE_CACHE[normalize_etf_base_code(code)] = dict(quote)


def load_runtime_etf_quotes() -> dict[str, dict[str, object]]:
    with _RUNTIME_ETF_QUOTE_CACHE_LOCK:
        return {
            code: dict(quote)
            for code, quote in _RUNTIME_ETF_QUOTE_CACHE.items()
        }


def filter_current_etf_realtime_quotes(
    quotes: dict[str, dict[str, object]] | None,
    *,
    market_now: datetime | None = None,
) -> dict[str, dict[str, object]]:
    """Keep session quotes only during their same-day intraday display window."""
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if not etf_intraday_quote_ready(market_now):
        return {}

    current: dict[str, dict[str, object]] = {}
    for code, quote in (quotes or {}).items():
        quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
        if pd.isna(quote_time) or quote_time.date() != market_now.date():
            continue
        current[normalize_etf_base_code(code)] = dict(quote)
    return current


def apply_etf_realtime_quote(item: PositionItem, quote: dict[str, object]) -> PositionItem:
    symbol = str(quote.get("symbol") or item.code).strip().upper()
    base_code = normalize_etf_base_code(symbol)
    latest_price = pd.to_numeric(quote.get("price"), errors="coerce")
    if pd.isna(latest_price):
        return item

    metrics = dict(item.metrics)
    previous_close = pd.to_numeric(quote.get("previous_close"), errors="coerce")
    if pd.isna(previous_close):
        previous_close = pd.to_numeric(metrics.get("最新价"), errors="coerce")
    if not pd.isna(previous_close) and float(previous_close) != 0:
        change_pct = (float(latest_price) / float(previous_close) - 1) * 100
    else:
        change_pct = pd.to_numeric(quote.get("change_pct"), errors="coerce")
    metrics["最新价"] = _round_metric(latest_price, 4)
    metrics["日涨跌(%)"] = _round_metric(change_pct)

    quote_time = quote.get("quote_time")
    quote_date = pd.to_datetime(quote_time, errors="coerce")
    return PositionItem(
        category="ETF",
        code=symbol,
        name=display_etf_name(base_code, item.name),
        status="盘中",
        source="TickFlow实时行情（不写入缓存）",
        latest_date="" if pd.isna(quote_date) else quote_date.strftime("%Y-%m-%d"),
        cache_time=item.cache_time,
        metrics=metrics,
        dataframe=item.dataframe,
    )


def apply_etf_realtime_quotes_to_items(
    items: list[PositionItem],
    quotes: dict[str, dict[str, object]],
) -> list[PositionItem]:
    updated_items: list[PositionItem] = []
    for item in items:
        if item.category != "ETF":
            updated_items.append(item)
            continue
        quote = quotes.get(normalize_etf_base_code(item.code))
        updated_items.append(
            apply_etf_realtime_quote(item, quote) if quote is not None else item
        )
    return updated_items


def apply_etf_realtime_quote_to_timing(
    item: PositionItem,
    quote: dict[str, object],
    *,
    market_now: datetime | None = None,
) -> PositionItem:
    """Build an intraday timing preview without changing formal history."""
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    is_morning_preview = etf_morning_timing_preview_ready(market_now)
    is_closing_preview = etf_realtime_timing_ready(market_now)
    is_lunch_preview = etf_lunch_timing_preview_ready(market_now)
    if not is_morning_preview and not is_closing_preview and not is_lunch_preview:
        return item

    base_code = normalize_etf_base_code(item.code)
    strategy = ETF_TIMING_STRATEGIES.get(base_code)
    quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
    quote_price = pd.to_numeric(quote.get("price"), errors="coerce")
    if (
        strategy is None
        or pd.isna(quote_time)
        or quote_time.date() != market_now.date()
        or pd.isna(quote_price)
        or float(quote_price) <= 0
    ):
        return item

    quoted_item = apply_etf_realtime_quote(item, quote)
    timing_data = (
        item.dataframe[["date", "price"]].copy()
        if item.dataframe is not None
        and not item.dataframe.empty
        and {"date", "price"}.issubset(item.dataframe.columns)
        else pd.DataFrame(columns=["date", "price"])
    )
    realtime_date = pd.Timestamp(quote_time.date())
    if not timing_data.empty:
        timing_dates = pd.to_datetime(timing_data["date"], errors="coerce")
        timing_data = timing_data.loc[timing_dates.dt.date != realtime_date.date()].copy()
    timing_data = pd.concat(
        [
            timing_data,
            pd.DataFrame(
                {
                    "date": [realtime_date],
                    "price": [float(quote_price)],
                }
            ),
        ],
        ignore_index=True,
    )
    timing_snapshot = calculate_etf_timing_snapshot(
        timing_data,
        ma_period=int(strategy[0]),
        threshold_pct=float(strategy[1]),
    )
    metrics = dict(quoted_item.metrics)
    metrics.update(
        {
            key: _round_metric(value, 6)
            if key == "策略均线"
            else _round_metric(value)
            if key in {"策略偏离(%)", "策略区间涨幅(%)", "策略上一区间涨幅(%)"}
            else value
            for key, value in timing_snapshot.items()
        }
    )
    return PositionItem(
        category=item.category,
        code=quoted_item.code,
        name=quoted_item.name,
        status=(
            "实时预判"
            if is_closing_preview
            else "午间预判"
            if is_lunch_preview
            else "早盘预判"
        ),
        source=(
            "TickFlow实时行情（14:50-15:00择时预判，不写入缓存）"
            if is_closing_preview
            else "TickFlow午间收盘行情（择时预判，不写入缓存）"
            if is_lunch_preview
            else "TickFlow早盘实时行情（择时预判，不写入缓存）"
        ),
        latest_date=realtime_date.strftime("%Y-%m-%d"),
        cache_time=item.cache_time,
        metrics=metrics,
        dataframe=item.dataframe,
        error=item.error,
    )


def latest_final_etf_trade_date(market_now: datetime | None = None):
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if market is None:
        return market_now.date()
    if etf_final_close_ready(market_now):
        return market_now.date()
    return previous_trading_day(market, market_now.date())


def filter_final_etf_rows(
    df: pd.DataFrame | None,
    *,
    date_column: str = "日期",
    market_now: datetime | None = None,
    require_current_confirmation: bool = False,
) -> pd.DataFrame | None:
    if df is None or df.empty or date_column not in df.columns:
        return None if df is None else df.copy()
    result = df.copy()
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    target_date = latest_final_etf_trade_date(market_now)
    dates = pd.to_datetime(result[date_column], errors="coerce")
    keep = dates.dt.date <= target_date
    if require_current_confirmation and target_date == market_now.date():
        if "_final_close_confirmed" in result.columns:
            confirmation_values = result["_final_close_confirmed"]
            confirmed = confirmation_values.eq(True) | confirmation_values.astype(str).str.lower().isin(
                {"true", "1"}
            )
        else:
            confirmed = pd.Series(False, index=result.index)
        keep &= (dates.dt.date < target_date) | confirmed
    result = result.loc[keep].copy()
    return result.reset_index(drop=True)


def etf_cache_has_latest_final_close(
    df: pd.DataFrame | None,
    *,
    date_column: str = "日期",
    market_now: datetime | None = None,
) -> bool:
    if df is None or df.empty or date_column not in df.columns:
        return False
    dates = pd.to_datetime(df[date_column], errors="coerce").dropna()
    if dates.empty:
        return False
    return dates.max().date() >= latest_final_etf_trade_date(market_now)


def calculate_etf_timing_snapshot(
    df: pd.DataFrame,
    *,
    ma_period: int,
    threshold_pct: float,
) -> dict[str, object]:
    data = df[["date", "price"]].copy() if {"date", "price"}.issubset(df.columns) else pd.DataFrame()
    if data.empty:
        return {}
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["price"] = pd.to_numeric(data["price"], errors="coerce")
    data = data.dropna(subset=["date", "price"]).sort_values("date").reset_index(drop=True)
    if data.empty:
        return {}

    ma_col = f"ma_{int(ma_period)}"
    data[ma_col] = data["price"].rolling(window=int(ma_period)).mean()
    threshold = float(threshold_pct) / 100
    position = 0
    latest_action = "等待均线"
    transition_date = None
    transition_price = None
    previous_transition_date = None
    previous_transition_price = None
    previous_interval_return_pct = pd.NA
    for _, row in data.iterrows():
        ma_value = pd.to_numeric(row[ma_col], errors="coerce")
        if pd.isna(ma_value):
            continue
        price = float(row["price"])
        desired_position = (
            1
            if price > float(ma_value) * (1 + threshold)
            else 0
            if price < float(ma_value) * (1 - threshold)
            else position
        )
        if desired_position != position:
            previous_transition_date = transition_date
            previous_transition_price = transition_price
            latest_action = "买入" if desired_position else "卖出"
            transition_date = pd.Timestamp(row["date"])
            transition_price = price
            previous_interval_return_pct = (
                (transition_price / previous_transition_price - 1) * 100
                if previous_transition_price is not None and previous_transition_price != 0
                else pd.NA
            )
        else:
            latest_action = "持有" if position else "空仓"
        position = desired_position

    latest = data.iloc[-1]
    latest_ma = pd.to_numeric(latest[ma_col], errors="coerce")
    latest_price = float(latest["price"])
    deviation_pct = (
        (latest_price / float(latest_ma) - 1) * 100
        if not pd.isna(latest_ma) and float(latest_ma) != 0
        else pd.NA
    )
    interval_return_pct = (
        (latest_price / transition_price - 1) * 100
        if transition_price is not None and transition_price != 0
        else pd.NA
    )
    return {
        "策略参数": f"MA{int(ma_period)} / {float(threshold_pct):.1f}%",
        "策略均线": latest_ma,
        "策略偏离(%)": deviation_pct,
        "择时判断": latest_action,
        "状态转换时间": transition_date.strftime("%Y-%m-%d") if transition_date is not None else pd.NA,
        "策略区间涨幅(%)": interval_return_pct,
        "上一状态转换时间": (
            previous_transition_date.strftime("%Y-%m-%d")
            if previous_transition_date is not None
            else pd.NA
        ),
        "策略上一区间涨幅(%)": previous_interval_return_pct,
    }


def etf_position_decision(code: str, timing_action: object) -> object:
    if timing_action is None or pd.isna(timing_action):
        return pd.NA
    action = str(timing_action)
    if ETF_POSITION_STRATEGIES.get(normalize_etf_base_code(code)) != "半仓持有半仓择时":
        return action
    return {
        "买入": "加至满仓",
        "持有": "持有",
        "卖出": "降至半仓",
        "空仓": "半仓",
        "等待均线": "半仓（等待均线）",
    }.get(action, action)


def calculate_etf_timing_transitions(
    df: pd.DataFrame,
    *,
    ma_period: int,
    threshold_pct: float,
) -> pd.DataFrame:
    columns = ["日期", "收盘价", "均线", "原始信号"]
    if df is None or df.empty or not {"date", "price"}.issubset(df.columns):
        return pd.DataFrame(columns=columns)

    data = df[["date", "price"]].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["price"] = pd.to_numeric(data["price"], errors="coerce")
    data = data.dropna(subset=["date", "price"]).sort_values("date").reset_index(drop=True)
    if data.empty:
        return pd.DataFrame(columns=columns)

    ma_col = f"ma_{int(ma_period)}"
    data[ma_col] = data["price"].rolling(window=int(ma_period)).mean()
    threshold = float(threshold_pct) / 100
    position = 0
    rows = []
    for _, row in data.iterrows():
        ma_value = pd.to_numeric(row[ma_col], errors="coerce")
        if pd.isna(ma_value):
            continue
        price = float(row["price"])
        desired_position = (
            1
            if price > float(ma_value) * (1 + threshold)
            else 0
            if price < float(ma_value) * (1 - threshold)
            else position
        )
        if desired_position != position:
            rows.append(
                {
                    "日期": pd.Timestamp(row["date"]),
                    "收盘价": price,
                    "均线": float(ma_value),
                    "原始信号": "买入" if desired_position else "卖出",
                }
            )
        position = desired_position
    return pd.DataFrame(rows, columns=columns)


def build_recent_etf_operation_guidance(
    items: list[PositionItem],
    *,
    days: int = 7,
) -> pd.DataFrame:
    columns = ["日期", "ETF名称", "代码", "策略参数", "操作指引", "操作后仓位", "触发收盘价"]
    latest_dates = []
    for item in items:
        if item.category != "ETF" or item.dataframe is None or item.dataframe.empty:
            continue
        dates = pd.to_datetime(item.dataframe.get("date"), errors="coerce").dropna()
        if not dates.empty:
            latest_dates.append(dates.max())
    if not latest_dates:
        return pd.DataFrame(columns=columns)

    end_date = max(latest_dates).normalize()
    start_date = end_date - pd.Timedelta(days=max(int(days), 1) - 1)
    rows = []
    for item in items:
        if item.category != "ETF":
            continue
        base_code = normalize_etf_base_code(item.code)
        strategy = ETF_TIMING_STRATEGIES.get(base_code)
        if strategy is None or base_code in ETF_TIMING_TABLE_EXCLUDED_CODES:
            continue
        ma_period, threshold_pct = strategy
        transitions = calculate_etf_timing_transitions(
            item.dataframe,
            ma_period=ma_period,
            threshold_pct=threshold_pct,
        )
        if transitions.empty:
            continue
        transitions = transitions[
            (transitions["日期"] >= start_date) & (transitions["日期"] <= end_date)
        ]
        for _, transition in transitions.iterrows():
            raw_action = str(transition["原始信号"])
            action = etf_position_decision(base_code, raw_action)
            half_timing = ETF_POSITION_STRATEGIES.get(base_code) == "半仓持有半仓择时"
            post_position = (
                "持有"
                if raw_action == "买入" and half_timing
                else "半仓"
                if raw_action == "卖出" and half_timing
                else "持有"
                if raw_action == "买入"
                else "空仓"
            )
            rows.append(
                {
                    "日期": pd.Timestamp(transition["日期"]).strftime("%Y-%m-%d"),
                    "ETF名称": display_etf_name(base_code, item.name),
                    "代码": base_code,
                    "策略参数": f"MA{int(ma_period)} / {float(threshold_pct):.1f}%",
                    "操作指引": action,
                    "操作后仓位": post_position,
                    "触发收盘价": round(float(transition["收盘价"]), 3),
                }
            )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["日期", "代码"], ascending=[False, True]
    ).reset_index(drop=True)


def build_etf_timing_table(items: list[PositionItem]) -> pd.DataFrame:
    columns = [
        "ETF名称",
        "代码",
        "最新价",
        "当日涨跌幅(%)",
        "策略参数",
        "对应均线",
        "偏离率(%)",
        "择时判断",
        "状态转换时间",
        "区间涨幅(%)",
        "上一状态转换时间",
        "上一区间涨幅(%)",
    ]
    rows = []
    for item in items:
        if item.category != "ETF":
            continue
        base_code = normalize_etf_base_code(item.code)
        if base_code in ETF_TIMING_TABLE_EXCLUDED_CODES:
            continue
        row = {
            "ETF名称": display_etf_name(base_code, item.name),
            "代码": base_code,
            "最新价": item.metrics.get("最新价"),
            "当日涨跌幅(%)": item.metrics.get("日涨跌(%)"),
            "策略参数": pd.NA,
            "对应均线": pd.NA,
            "偏离率(%)": pd.NA,
            "择时判断": pd.NA,
            "状态转换时间": pd.NA,
            "区间涨幅(%)": pd.NA,
            "上一状态转换时间": pd.NA,
            "上一区间涨幅(%)": pd.NA,
        }
        if base_code in ETF_TIMING_STRATEGIES:
            ma_period, threshold_pct = ETF_TIMING_STRATEGIES[base_code]
            row.update(
                {
                    "策略参数": item.metrics.get(
                        "策略参数",
                        f"MA{int(ma_period)} / {float(threshold_pct):.1f}%",
                    ),
                    "对应均线": item.metrics.get("策略均线", pd.NA),
                    "偏离率(%)": item.metrics.get("策略偏离(%)", pd.NA),
                    "择时判断": etf_position_decision(
                        base_code,
                        item.metrics.get("择时判断", pd.NA),
                    ),
                    "状态转换时间": item.metrics.get("状态转换时间", pd.NA),
                    "区间涨幅(%)": item.metrics.get("策略区间涨幅(%)", pd.NA),
                    "上一状态转换时间": item.metrics.get("上一状态转换时间", pd.NA),
                    "上一区间涨幅(%)": item.metrics.get("策略上一区间涨幅(%)", pd.NA),
                }
            )
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=columns)
    result = pd.DataFrame(rows, columns=columns)
    result["_sort_deviation"] = pd.to_numeric(result["偏离率(%)"], errors="coerce")
    return result.sort_values("_sort_deviation", ascending=False, na_position="last").drop(
        columns="_sort_deviation"
    ).reset_index(drop=True)


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


def format_cache_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def _load_dataset_if_ready(symbol: str, source: str, data_type: str, period: str = "1d"):
    cached_df, meta = load_dataset(symbol, source, data_type, period=period)
    if cached_df is None or cached_df.empty:
        return None, meta
    return cached_df, meta


def _merge_by_date(old_df: pd.DataFrame | None, new_df: pd.DataFrame, date_column: str) -> pd.DataFrame:
    normalized_new = new_df.copy()
    if date_column not in normalized_new.columns:
        return old_df.copy() if old_df is not None and not old_df.empty else normalized_new
    normalized_new[date_column] = pd.to_datetime(normalized_new[date_column], errors="coerce")
    normalized_new = normalized_new.dropna(subset=[date_column])
    normalized_new = normalized_new.sort_values(date_column).drop_duplicates(date_column, keep="last")
    if old_df is None or old_df.empty:
        return normalized_new.reset_index(drop=True)

    normalized_old = old_df.copy()
    normalized_old[date_column] = pd.to_datetime(normalized_old[date_column], errors="coerce")
    normalized_old = normalized_old.dropna(subset=[date_column])
    normalized_old = normalized_old.sort_values(date_column).drop_duplicates(date_column, keep="first")
    unseen = normalized_new[~normalized_new[date_column].isin(normalized_old[date_column])]
    return (
        pd.concat([normalized_old, unseen], ignore_index=True)
        .sort_values(date_column)
        .reset_index(drop=True)
    )


def _merge_current_day_refresh(
    old_df: pd.DataFrame | None,
    new_df: pd.DataFrame,
    date_column: str,
) -> pd.DataFrame:
    merged = _merge_by_date(old_df, new_df, date_column)
    if new_df is None or new_df.empty or date_column not in new_df.columns:
        return merged

    today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
    refreshed = new_df.copy()
    refreshed[date_column] = pd.to_datetime(refreshed[date_column], errors="coerce")
    today_rows = refreshed[refreshed[date_column].dt.normalize() == today]
    if today_rows.empty:
        return merged

    merged_dates = pd.to_datetime(merged[date_column], errors="coerce")
    historical = merged[merged_dates.dt.normalize() != today]
    cached_today = merged[merged_dates.dt.normalize() == today]
    combined_today = (
        today_rows.sort_values(date_column)
        .drop_duplicates(date_column, keep="last")
        .set_index(date_column)
        .combine_first(cached_today.set_index(date_column))
        .reset_index()
    )
    return (
        pd.concat([historical, combined_today], ignore_index=True)
        .sort_values(date_column)
        .drop_duplicates(date_column, keep="last")
        .reset_index(drop=True)
    )


def _round_metric(value: object, digits: int = 2) -> object:
    if value is None or pd.isna(value):
        return float("nan")
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def _current_cache_time_text() -> str:
    return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")


def _cache_has_expected_trade_date(
    df: pd.DataFrame | None,
    date_column: str = "date",
    market_now: datetime | None = None,
) -> bool:
    if df is None or df.empty or date_column not in df.columns:
        return False
    dates = pd.to_datetime(df[date_column], errors="coerce").dropna()
    if dates.empty:
        return False
    market = get_market_window("A股")
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    expected_date = expected_latest_trade_date(market, market_now) if market is not None else market_now.date()
    return dates.max().date() >= expected_date


def _futures_option_cache_key(symbol: str, data_type: str, period: str, count: int) -> str:
    safe_symbol = symbol.strip().replace(".", "_").replace("/", "_")
    return f"futures_option_{safe_symbol}_{data_type}_{period}_{int(count)}"


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
    )


def _fetch_eastmoney_exchange_fund_close(
    *,
    symbol: str,
    count: int,
    adjust: str | None,
) -> pd.DataFrame:
    import akshare as ak

    base_code = normalize_etf_base_code(symbol)
    end_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    calendar_days = max(int(count) * 2, 365)
    start_date = end_date - timedelta(days=calendar_days)
    adjust_value = {
        "forward": "qfq",
        "backward": "hfq",
        None: "",
    }.get(adjust, "")
    raw = ak.fund_etf_hist_em(
        symbol=base_code,
        period="daily",
        start_date=start_date.strftime("%Y%m%d"),
        end_date=end_date.strftime("%Y%m%d"),
        adjust=adjust_value,
    )
    if raw is None or raw.empty:
        raise ValueError(f"东方财富未返回 {base_code} 的场内日线数据。")
    if not {"日期", "收盘"}.issubset(raw.columns):
        raise ValueError(f"东方财富返回列无法识别：{list(raw.columns)}")

    columns = ["日期", "收盘"]
    if "开盘" in raw.columns:
        columns.insert(1, "开盘")
    result = raw[columns].copy()
    result.columns = (
        ["日期", "开盘价", "收盘价"]
        if "开盘" in raw.columns
        else ["日期", "收盘价"]
    )
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce")
    if "开盘价" in result.columns:
        result["开盘价"] = pd.to_numeric(result["开盘价"], errors="coerce")
    result["收盘价"] = pd.to_numeric(result["收盘价"], errors="coerce")
    result = result.dropna(subset=["日期", "收盘价"])
    result = (
        result.sort_values("日期")
        .drop_duplicates("日期")
        .tail(int(count))
        .reset_index(drop=True)
    )
    result["symbol"] = symbol
    result["name"] = display_etf_name(base_code, symbol)
    return result


def _sina_exchange_symbol(symbol: str) -> str:
    base_code = normalize_etf_base_code(symbol)
    exchange = "sh" if str(symbol).strip().upper().endswith(".SH") else "sz"
    return f"{exchange}{base_code}"


def _request_sina_realtime_snapshot(sina_symbol: str):
    url = f"https://hq.sinajs.cn/list={sina_symbol}"
    request_kwargs = {
        "headers": {
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0",
        },
        "timeout": SINA_REQUEST_TIMEOUT_SECONDS,
    }
    try:
        return requests.get(url, **request_kwargs)
    except requests.exceptions.ProxyError:
        with requests.Session() as session:
            session.trust_env = False
            return session.get(url, **request_kwargs)


def _fetch_sina_exchange_fund_quote(
    *,
    symbol: str,
    market_now: datetime | None = None,
) -> dict[str, object]:
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    base_code = normalize_etf_base_code(symbol)
    sina_symbol = _sina_exchange_symbol(symbol)
    response = _request_sina_realtime_snapshot(sina_symbol)
    response.raise_for_status()
    payload = response.content.decode("gb18030", errors="replace")
    match = re.search(
        rf'var\s+hq_str_{re.escape(sina_symbol)}="([^"]*)"',
        payload,
    )
    fields = match.group(1).split(",") if match else []
    if len(fields) < 32:
        raise ValueError(f"新浪财经未返回 {base_code} 的可识别实时快照。")

    latest_price = pd.to_numeric(fields[3], errors="coerce")
    previous_close = pd.to_numeric(fields[2], errors="coerce")
    quote_timestamp = pd.to_datetime(
        f"{fields[30]} {fields[31]}",
        errors="coerce",
    )
    if (
        pd.isna(latest_price)
        or float(latest_price) <= 0
        or pd.isna(quote_timestamp)
    ):
        raise ValueError(f"新浪财经返回的 {base_code} 实时快照无效。")
    if quote_timestamp.date() != market_now.date():
        raise ValueError(
            f"新浪财经 {base_code} 实时快照日期为 {quote_timestamp:%Y-%m-%d}，"
            f"当前日期为 {market_now:%Y-%m-%d}。"
        )

    quote_time = quote_timestamp.tz_localize("Asia/Shanghai").to_pydatetime()
    change_pct = None
    if not pd.isna(previous_close) and float(previous_close) > 0:
        change_pct = (float(latest_price) / float(previous_close) - 1) * 100
    return {
        "symbol": symbol.strip().upper(),
        "price": float(latest_price),
        "previous_close": (
            None if pd.isna(previous_close) else float(previous_close)
        ),
        "change_pct": change_pct,
        "quote_time": quote_time,
    }


def _ensure_sina_adjustment_is_identity(sina_symbol: str, adjust: str | None) -> None:
    if adjust not in {"forward", "backward"}:
        return

    adjustment_name = "qfq" if adjust == "forward" else "hfq"
    response = requests.get(
        f"https://finance.sina.com.cn/realstock/company/{sina_symbol}/{adjustment_name}.js",
        timeout=SINA_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    _prefix, separator, remainder = response.text.partition("=")
    payload_text = remainder.splitlines()[0].strip().rstrip(";") if separator else ""
    if not payload_text:
        raise ValueError("新浪备用源未返回可识别的复权因子。")
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("新浪备用源复权因子格式无法识别。") from exc

    factor_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(factor_rows, list) or not factor_rows:
        raise ValueError("新浪备用源未返回复权因子。")
    for row in factor_rows:
        try:
            factor = float(row.get("f", 1))
            split = float(row.get("s", 1))
            dividend = float(row.get("u", 0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("新浪备用源复权因子字段无法识别。") from exc
        if (
            abs(factor - 1.0) > 1e-12
            or abs(split - 1.0) > 1e-12
            or abs(dividend) > 1e-12
        ):
            raise ValueError("新浪备用源存在复权事件，不能用未复权日线替代当前复权口径。")


def _fetch_sina_exchange_fund_close(
    *,
    symbol: str,
    count: int,
    adjust: str | None,
) -> pd.DataFrame:
    from akshare.stock.cons import hk_js_decode
    import py_mini_racer

    base_code = normalize_etf_base_code(symbol)
    sina_symbol = _sina_exchange_symbol(symbol)
    _ensure_sina_adjustment_is_identity(sina_symbol, adjust)

    response = requests.get(
        f"https://finance.sina.com.cn/realstock/company/{sina_symbol}/hisdata_klc2/klc_kl.js",
        timeout=SINA_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    _prefix, separator, remainder = response.text.partition("=")
    encoded = remainder.split(";", 1)[0].replace('"', "").strip() if separator else ""
    if not encoded:
        raise ValueError(f"新浪财经未返回 {base_code} 的场内日线数据。")

    decoder = py_mini_racer.MiniRacer()
    decoder.eval(hk_js_decode)
    decoded_rows = decoder.call("d", encoded)
    raw = pd.DataFrame(decoded_rows)
    required_columns = {"date", "close"}
    if raw.empty or not required_columns.issubset(raw.columns):
        raise ValueError(f"新浪财经返回列无法识别：{list(raw.columns)}")

    columns = ["date", "close"]
    if "open" in raw.columns:
        columns.insert(1, "open")
    result = raw[columns].copy()
    result.columns = (
        ["日期", "开盘价", "收盘价"]
        if "open" in raw.columns
        else ["日期", "收盘价"]
    )
    result["日期"] = pd.to_datetime(
        result["日期"], errors="coerce", utc=True
    ).dt.tz_localize(None)
    if "开盘价" in result.columns:
        result["开盘价"] = pd.to_numeric(result["开盘价"], errors="coerce")
    result["收盘价"] = pd.to_numeric(result["收盘价"], errors="coerce")
    today = pd.Timestamp(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    result = result.dropna(subset=["日期", "收盘价"])
    result = (
        result[result["日期"] <= today]
        .sort_values("日期")
        .drop_duplicates("日期")
        .tail(int(count))
        .reset_index(drop=True)
    )
    if result.empty:
        raise ValueError(f"新浪财经未返回 {base_code} 的有效场内日线数据。")
    result["symbol"] = symbol
    result["name"] = display_etf_name(base_code, symbol)
    return result


def _fetch_sina_exchange_fund_final_close(
    *,
    symbol: str,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if not etf_final_close_ready(market_now):
        raise ValueError("A股当日正式收盘价需在交易日15:05后确认。")

    base_code = normalize_etf_base_code(symbol)
    sina_symbol = _sina_exchange_symbol(symbol)
    response = _request_sina_realtime_snapshot(sina_symbol)
    response.raise_for_status()
    payload = response.content.decode("gb18030", errors="replace")
    match = re.search(
        rf'var\s+hq_str_{re.escape(sina_symbol)}="([^"]*)"',
        payload,
    )
    fields = match.group(1).split(",") if match else []
    if len(fields) < 32:
        raise ValueError(f"新浪财经未返回 {base_code} 的可识别收盘快照。")

    open_price = pd.to_numeric(fields[1], errors="coerce")
    close_price = pd.to_numeric(fields[3], errors="coerce")
    quote_timestamp = pd.to_datetime(
        f"{fields[30]} {fields[31]}",
        errors="coerce",
    )
    if pd.isna(close_price) or float(close_price) <= 0 or pd.isna(quote_timestamp):
        raise ValueError(f"新浪财经返回的 {base_code} 收盘快照无效。")

    target_date = latest_final_etf_trade_date(market_now)
    market = get_market_window("A股")
    session_close = market.sessions[-1][1] if market is not None else datetime_time(15, 0)
    if quote_timestamp.date() != target_date:
        raise ValueError(
            f"新浪财经 {base_code} 收盘快照日期为 {quote_timestamp:%Y-%m-%d}，"
            f"最新完成交易日应为 {target_date:%Y-%m-%d}。"
        )
    if quote_timestamp.time() < session_close:
        raise ValueError(
            f"新浪财经 {base_code} 快照时间为 {quote_timestamp:%H:%M:%S}，尚未收盘。"
        )

    return pd.DataFrame(
        {
            "日期": [quote_timestamp.normalize()],
            "开盘价": [None if pd.isna(open_price) else float(open_price)],
            "收盘价": [float(close_price)],
            "symbol": [symbol],
            "name": [display_etf_name(base_code, symbol)],
            "_final_close_confirmed": [True],
        }
    )


def _append_sina_final_close(
    history: pd.DataFrame,
    *,
    symbol: str,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    result = history.copy()
    result.attrs.update(history.attrs)
    if result.empty or "日期" not in result.columns or not etf_final_close_ready(market_now):
        return result

    target_date = latest_final_etf_trade_date(market_now)
    history_dates = pd.to_datetime(result["日期"], errors="coerce")
    if history_dates.dt.date.eq(target_date).any():
        return result

    try:
        final_close = _fetch_sina_exchange_fund_final_close(
            symbol=symbol,
            market_now=market_now,
        )
    except Exception as exc:
        warning = f"新浪当日收盘快照获取失败：{exc}"
        result.attrs["position_history_warning"] = warning
        logger.warning("%s %s", symbol, warning)
        return result

    history_attrs = dict(result.attrs)
    result = _merge_by_date(result, final_close, "日期")
    result.attrs.update(history_attrs)
    history_source = str(result.attrs.get("position_history_source") or "").strip()
    if history_source and "新浪收盘快照" not in history_source:
        result.attrs["position_history_source"] = f"{history_source} + 新浪收盘快照"
    return result


def _fetch_exchange_fund_close(
    *,
    symbol: str,
    count: int,
    adjust: str | None,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    eastmoney_error = ""
    try:
        result = _fetch_eastmoney_exchange_fund_close(
            symbol=symbol,
            count=count,
            adjust=adjust,
        )
        result.attrs["position_history_source"] = "东方财富/AkShare"
        return _append_sina_final_close(
            result,
            symbol=symbol,
            market_now=market_now,
        )
    except Exception as exc:
        eastmoney_error = str(exc)
        logger.warning("%s 东方财富场内日线获取失败，尝试新浪备用源：%s", symbol, exc)

    try:
        result = _fetch_sina_exchange_fund_close(
            symbol=symbol,
            count=count,
            adjust=adjust,
        )
        result.attrs["position_history_source"] = "新浪财经备用源"
        return _append_sina_final_close(
            result,
            symbol=symbol,
            market_now=market_now,
        )
    except Exception as sina_exc:
        raise RuntimeError(
            f"东方财富场内日线获取失败：{eastmoney_error}；新浪备用源也失败：{sina_exc}"
        ) from sina_exc


def load_or_fetch_etf(
    code: str,
    *,
    api_key: str = "",
    count: int = 5000,
    adjust: str | None = "forward",
    ma_periods: list[int] | tuple[int, ...] = (20, 60, 120, 250),
    rsi_period: int = 14,
    base_date: str = "2024-09-24",
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
    allow_unfinished_session: bool = False,
    market_now: datetime | None = None,
) -> PositionItem:
    raw_code = code.strip()
    base_code = normalize_etf_base_code(raw_code)
    strategy = ETF_TIMING_STRATEGIES.get(base_code)
    try:
        symbol = infer_tickflow_symbol(raw_code)
    except Exception as exc:
        return PositionItem("ETF", raw_code, display_etf_name(base_code, raw_code), "失败", error=str(exc))

    use_akshare_history = base_code in ETF_AKSHARE_HISTORY_CODES
    cache_source = "akshare" if use_akshare_history else "tickflow"
    fetch_source = "东方财富/AkShare" if use_akshare_history else "TickFlow"
    cache_symbol = f"fund_close_{symbol}_{adjust or 'none'}"
    period = f"{int(count)}_1d"
    cached_df, cache_meta = _load_dataset_if_ready(
        cache_symbol,
        cache_source,
        "fund_close_raw",
        period=period,
    )
    cached_df = filter_final_etf_rows(
        cached_df,
        market_now=market_now,
        require_current_confirmation=True,
    )
    cache_is_current = etf_cache_has_latest_final_close(
        cached_df,
        date_column="日期",
        market_now=market_now,
    )
    should_refresh = force_refresh or (allow_fetch and not cache_is_current)
    used_cache = cached_df is not None and not should_refresh
    source_df = cached_df.copy() if used_cache else None
    source = "本地缓存" if used_cache else fetch_source
    status = "缓存"
    error = ""

    if source_df is None:
        if not allow_fetch:
            return _missing_item("ETF", raw_code, display_etf_name(base_code, symbol))
        try:
            if cached_df is not None:
                incremental_count = min(max(120, int(count) // 20), int(count))
                latest_df = (
                    _fetch_exchange_fund_close(
                        symbol=symbol,
                        count=incremental_count,
                        adjust=adjust,
                        market_now=market_now,
                    )
                    if use_akshare_history
                    else fetch_tickflow_fund_close(
                        symbol=symbol,
                        api_key=api_key,
                        count=incremental_count,
                        adjust=adjust,
                    )
                )
                if use_akshare_history:
                    source = str(latest_df.attrs.get("position_history_source") or fetch_source)
                    error = str(latest_df.attrs.get("position_history_warning") or "")
                if not allow_unfinished_session:
                    latest_df = filter_final_etf_rows(latest_df, market_now=market_now)
                    latest_df["_final_close_confirmed"] = True
                source_df = _merge_by_date(cached_df, latest_df, "日期")
                status = "已增量更新"
            else:
                source_df = (
                    _fetch_exchange_fund_close(
                        symbol=symbol,
                        count=int(count),
                        adjust=adjust,
                        market_now=market_now,
                    )
                    if use_akshare_history
                    else fetch_tickflow_fund_close(
                        symbol=symbol,
                        api_key=api_key,
                        count=int(count),
                        adjust=adjust,
                    )
                )
                if use_akshare_history:
                    source = str(source_df.attrs.get("position_history_source") or fetch_source)
                    error = str(source_df.attrs.get("position_history_warning") or "")
                if not allow_unfinished_session:
                    source_df = filter_final_etf_rows(source_df, market_now=market_now)
                    source_df["_final_close_confirmed"] = True
                status = "已更新"
            if save_to_cache and not allow_unfinished_session:
                save_dataset(
                    symbol=cache_symbol,
                    name=f"{symbol} 场内基金/股票原始收盘价",
                    source=cache_source,
                    data_type="fund_close_raw",
                    period=period,
                    df=source_df,
                )
        except Exception as exc:
            if cached_df is None:
                return PositionItem(
                    "ETF",
                    raw_code,
                    display_etf_name(base_code, symbol),
                    "失败",
                    source=fetch_source,
                    error=str(exc),
                )
            source_df = cached_df.copy()
            source = "本地缓存（刷新失败）"
            status = "缓存"
            error = str(exc)

    try:
        analysis_source_df = source_df.drop(
            columns=[column for column in source_df.columns if str(column).startswith("_")],
            errors="ignore",
        )
        fund_name, nav_df = normalize_nav_dataframe(analysis_source_df, fallback_name=f"{symbol} ETF")
        effective_ma_periods = set(int(period) for period in ma_periods)
        if strategy is not None:
            effective_ma_periods.add(int(strategy[0]))
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=tuple(sorted(effective_ma_periods)),
            rsi_period=int(rsi_period),
            base_date=base_date,
        )
    except Exception as exc:
        return PositionItem("ETF", raw_code, symbol, "失败", source=source, error=str(exc))

    summary = result.summary
    latest_row = result.dataframe.iloc[-1]
    metrics = {
        "最新价": _round_metric(summary.get("最新价格"), 4),
        "日涨跌(%)": _round_metric(latest_row.get("daily_return_pct")),
        "20日涨跌(%)": _round_metric(summary.get("20日涨幅(%)")),
        "60日涨跌(%)": _round_metric(summary.get("60日涨幅(%)")),
        "MA20偏离(%)": _round_metric(summary.get("MA20偏离(%)")),
        "价格百分位": _round_metric(summary.get("价格百分位")),
        "年化波动(%)": _round_metric(summary.get("年化波动率(%)")),
    }
    if strategy is not None:
        timing_snapshot = calculate_etf_timing_snapshot(
            result.dataframe,
            ma_period=int(strategy[0]),
            threshold_pct=float(strategy[1]),
        )
        metrics.update(
            {
                key: _round_metric(value, 6)
                if key == "策略均线"
                else _round_metric(value)
                if key in {"策略偏离(%)", "策略区间涨幅(%)", "策略上一区间涨幅(%)"}
                else value
                for key, value in timing_snapshot.items()
            }
        )
    return PositionItem(
        category="ETF",
        code=symbol,
        name=display_etf_name(base_code, str(summary.get("基金名称") or fund_name)),
        status=status,
        source=source,
        latest_date=str(summary.get("最新日期") or ""),
        cache_time=(
            _current_cache_time_text()
            if status != "缓存" and save_to_cache
            else format_cache_time(cache_meta.get("last_update_time") if cache_meta else "")
        ),
        metrics=metrics,
        dataframe=result.dataframe,
        error=error,
    )


def load_or_fetch_spread(
    contracts: list[str],
    *,
    base_contract: str | None = None,
    api_key: str = "",
    max_workers: int = 2,
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
    realtime_preview: bool = False,
) -> PositionItem:
    contracts = [contract.strip().upper() for contract in contracts if contract.strip()]
    if len(contracts) < 2:
        return PositionItem("期货价差", " ".join(contracts), "期货价差", "失败", error="至少需要两个合约。")

    base_contract = (base_contract or contracts[0]).strip().upper()
    configured_other_contract = next((contract for contract in contracts if contract != base_contract), "")
    configured_code = (
        f"{base_contract} - {configured_other_contract}"
        if configured_other_contract
        else base_contract
    )
    configured_name = format_spread_position_name(
        base_contract,
        configured_other_contract,
    )
    cache_symbol = f"futures_spread_{base_contract}"
    cached_df, cache_meta = _load_dataset_if_ready(cache_symbol, "akshare", "futures_spread")
    cache_ready = _spread_cache_matches_contracts(cached_df, contracts, base_contract)
    refresh_stale_cache = allow_fetch and cache_ready and not _cache_has_expected_trade_date(cached_df, "date")
    source = "本地缓存"
    status = "缓存"
    error = ""

    if cached_df is not None and cache_ready and not force_refresh and not refresh_stale_cache:
        spread_df = cached_df.copy()
        spread_df["date"] = pd.to_datetime(spread_df["date"], errors="coerce")
    else:
        if not allow_fetch:
            return _missing_item("期货价差", configured_code, configured_name)
        try:
            if realtime_preview and cached_df is not None and cache_ready:
                data = {}
                errors = []
                today = pd.Timestamp.now(tz="Asia/Shanghai").date()
                for contract in contracts:
                    close_col = f"{contract}_close"
                    contract_df = cached_df[["date", close_col]].rename(
                        columns={close_col: "close"}
                    )
                    contract_df = append_futures_spot_row(
                        contract_df,
                        contract,
                        replace_current_day=True,
                    )
                    latest_contract_date = pd.to_datetime(
                        contract_df["date"], errors="coerce"
                    ).max()
                    if (
                        pd.isna(latest_contract_date)
                        or latest_contract_date.date() != today
                    ):
                        raise RuntimeError(f"{contract} 实时源未返回今日行情")
                    data[contract] = contract_df
            else:
                data, errors = fetch_contracts(
                    contracts,
                    max_workers=max_workers,
                    api_key=api_key,
                    prefer_realtime_snapshot=realtime_preview,
                )
            latest_spread_df = calculate_spreads(data, base_contract)
            spread_df = (
                _merge_current_day_refresh(cached_df, latest_spread_df, "date")
                if cached_df is not None and cache_ready
                else latest_spread_df
            )
            source = "TickFlow/AkShare"
            status = "已增量更新" if cached_df is not None and cache_ready else "已更新"
            if errors:
                error = " | ".join(errors)
            if save_to_cache:
                save_dataset(
                    symbol=cache_symbol,
                    name=f"{contract_name(base_contract)} 期货价差",
                    source="akshare",
                    data_type="futures_spread",
                    df=spread_df,
                )
        except Exception as exc:
            if cached_df is None or not cache_ready:
                return PositionItem(
                    "期货价差",
                    configured_code,
                    configured_name,
                    "失败",
                    source="TickFlow/AkShare",
                    error=str(exc),
                )
            spread_df = cached_df.copy()
            spread_df["date"] = pd.to_datetime(spread_df["date"], errors="coerce")
            source = "本地缓存（刷新失败）"
            status = "缓存"
            error = str(exc)

    available_contracts = [contract for contract in contracts if f"{contract}_close" in spread_df.columns]
    summary_df = build_spread_summary(spread_df, available_contracts, base_contract)
    if summary_df.empty:
        return PositionItem(
            "期货价差",
            configured_code,
            configured_name,
            "失败",
            source=source,
            error="没有可展示的价差统计。",
        )

    other_contract = next((contract for contract in available_contracts if contract != base_contract), "")
    spread_col = f"spread_{base_contract}_vs_{other_contract}"
    latest_rows = spread_df.dropna(subset=[spread_col]) if spread_col in spread_df.columns else pd.DataFrame()
    latest_row = latest_rows.iloc[-1] if not latest_rows.empty else spread_df.iloc[-1]
    summary_row = summary_df.iloc[0]
    code = f"{base_contract} - {other_contract}" if other_contract else base_contract
    spread_daily_change = float("nan")
    if spread_col in spread_df.columns:
        latest_spread = pd.to_numeric(spread_df[spread_col], errors="coerce").dropna()
        if len(latest_spread) >= 2:
            spread_daily_change = latest_spread.iloc[-1] - latest_spread.iloc[-2]
    metrics = {
        "最新价差": _round_metric(summary_row.get("最新价差"), 4),
        "价差日变化": _round_metric(spread_daily_change, 4),
        "最新占比(%)": _round_metric(summary_row.get("最新占比(%)")),
        "平均价差": _round_metric(summary_row.get("平均价差"), 4),
        "最大价差": _round_metric(summary_row.get("最大价差"), 4),
        "最小价差": _round_metric(summary_row.get("最小价差"), 4),
        f"{base_contract}收盘": _round_metric(latest_row.get(f"{base_contract}_close"), 4),
    }
    if other_contract:
        metrics[f"{other_contract}收盘"] = _round_metric(latest_row.get(f"{other_contract}_close"), 4)

    latest_date = ""
    if "date" in latest_row:
        latest_date = pd.Timestamp(latest_row["date"]).strftime("%Y-%m-%d")

    return PositionItem(
        category="期货价差",
        code=code,
        name=format_spread_position_name(base_contract, other_contract),
        status=status,
        source=source,
        latest_date=latest_date,
        cache_time=(
            _current_cache_time_text()
            if status != "缓存" and save_to_cache
            else format_cache_time(cache_meta.get("last_update_time") if cache_meta else "")
        ),
        metrics=metrics,
        dataframe=spread_df,
        error=error,
    )


def load_or_fetch_option(
    code: str,
    *,
    period: str = "1d",
    count: int = 500,
    ma_periods: list[int] | tuple[int, ...] = (5, 20, 60),
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
    realtime_preview: bool = False,
) -> PositionItem:
    raw_code = code.strip()
    cache_candidates = _futures_option_cache_candidates(raw_code, period, int(count))
    cached_df = None
    cache_meta = None
    for cache_symbol in cache_candidates:
        candidate_df, candidate_meta = _load_dataset_if_ready(cache_symbol, "market", "futures_option", period=period)
        if _market_cache_is_usable(candidate_df):
            cached_df, cache_meta = candidate_df, candidate_meta
            break

    refresh_stale_cache = allow_fetch and cached_df is not None and not _cache_has_expected_trade_date(cached_df, "date")

    if cached_df is not None and not force_refresh and not refresh_stale_cache:
        result_df = cached_df.copy()
        source = "本地缓存"
        status = "缓存"
        error = ""
    else:
        if not allow_fetch:
            return _missing_item("期权", raw_code, normalize_option_symbol(raw_code))
        try:
            if realtime_preview and cached_df is not None:
                latest_result_df = append_option_spot_row(
                    cached_df,
                    raw_code,
                    replace_current_day=True,
                )
                result_source = "AkShare期权实时快照"
                result_is_chain = False
            else:
                result = fetch_futures_option_data(
                    raw_symbol=raw_code,
                    data_type=DATA_TYPE_OPTIONS,
                    period=period,
                    count=int(count),
                    api_key="",
                    use_free=True,
                    ma_periods=ma_periods,
                    prefer_realtime_snapshot=realtime_preview,
                )
                latest_result_df = result.dataframe.copy()
                result_source = result.source
                result_is_chain = result.is_chain
            result_df = (
                _merge_current_day_refresh(cached_df, latest_result_df, "date")
                if cached_df is not None
                else latest_result_df
            )
            if not result_is_chain and "_data_version" not in result_df.columns:
                result_df["_data_version"] = FUTURES_OPTION_DATA_VERSION
            source = result_source
            status = "已增量更新" if cached_df is not None else "已更新"
            error = ""
        except Exception as exc:
            if cached_df is None:
                return PositionItem("期权", raw_code, normalize_option_symbol(raw_code), "失败", source="AkShare", error=str(exc))
            result_df = cached_df.copy()
            source = "本地缓存（刷新失败）"
            status = "缓存"
            error = str(exc)

    try:
        result_df["date"] = pd.to_datetime(result_df["date"], errors="coerce")
        result_df["close"] = pd.to_numeric(result_df["close"], errors="coerce")
        result_df = result_df.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date").reset_index(drop=True)
        result_df = add_indicators(result_df, ma_periods)
        result_df["_data_version"] = FUTURES_OPTION_DATA_VERSION
        summary = build_futures_option_summary(result_df)
    except Exception as exc:
        return PositionItem("期权", raw_code, normalize_option_symbol(raw_code), "失败", source=source, error=str(exc))

    metrics = {
        "最新收盘": _round_metric(summary.get("最新收盘"), 4),
        "日涨跌(%)": _round_metric(result_df.iloc[-1].get("daily_return_pct")),
        "20日涨跌(%)": _round_metric(summary.get("20日涨跌幅(%)")),
        "20日波动(%)": _round_metric(summary.get("20日波动率(%)")),
        "价格百分位": _round_metric(summary.get("价格百分位")),
        "最新成交量": _round_metric(summary.get("最新成交量"), 0),
        "最新持仓量": _round_metric(summary.get("最新持仓量"), 0),
    }
    if status != "缓存" and save_to_cache:
        save_dataset(
            symbol=_futures_option_cache_key(raw_code, DATA_TYPE_OPTIONS, period, int(count)),
            name=f"{normalize_option_symbol(raw_code)} 期货期权数据",
            source="market",
            data_type="futures_option",
            period=period,
            df=result_df,
        )
    display_code = normalize_option_symbol(raw_code)
    return PositionItem(
        category="期权",
        code=display_code,
        name=option_display_name(display_code),
        status=status,
        source=source,
        latest_date=str(summary.get("最新日期") or ""),
        cache_time=(
            _current_cache_time_text()
            if status != "缓存" and save_to_cache
            else format_cache_time(cache_meta.get("last_update_time") if cache_meta else "")
        ),
        metrics=metrics,
        dataframe=result_df,
        error=error,
    )


def refresh_position_derivative_items(
    items: list[PositionItem],
    *,
    api_key: str = "",
    max_workers: int = 2,
    option_count: int = 500,
) -> tuple[list[PositionItem], list[str]]:
    refreshed: list[PositionItem] = []
    errors: list[str] = []
    for item in items:
        if item.category == "期货价差":
            contracts = [part.strip() for part in item.code.split(" - ") if part.strip()]
            if len(contracts) < 2:
                errors.append(f"{item.name}: 无法识别价差合约")
                continue
            latest = load_or_fetch_spread(
                contracts,
                base_contract=contracts[0],
                api_key=api_key,
                max_workers=max_workers,
                allow_fetch=True,
                force_refresh=True,
                save_to_cache=False,
                realtime_preview=True,
            )
        elif item.category == "期权":
            latest = load_or_fetch_option(
                item.code,
                count=option_count,
                allow_fetch=True,
                force_refresh=True,
                save_to_cache=False,
                realtime_preview=True,
            )
        else:
            continue

        if latest.status == "失败" or "刷新失败" in latest.source:
            errors.append(f"{item.name}: {latest.error or latest.source}")
            continue
        latest_date = pd.to_datetime(latest.latest_date, errors="coerce")
        today = pd.Timestamp.now(tz="Asia/Shanghai").date()
        if pd.isna(latest_date) or latest_date.date() != today:
            errors.append(f"{item.name}: 实时源未返回今日行情")
            continue
        refreshed.append(latest)
    return refreshed, errors
