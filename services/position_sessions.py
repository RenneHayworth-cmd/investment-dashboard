from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from services.market_calendar import (
    expected_latest_trade_date,
    get_market_window,
    is_market_trading_day,
    previous_trading_day,
)
from services.position_models import (
    ETF_AFTERNOON_TIMING_START_TIME,
    ETF_FINAL_CLOSE_READY_TIME,
    ETF_LUNCH_TIMING_FETCH_END_TIME,
    ETF_LUNCH_TIMING_START_TIME,
    ETF_MORNING_TIMING_PREVIEW_END_TIME,
    ETF_MORNING_TIMING_START_TIME,
    ETF_REALTIME_TIMING_END_TIME,
    ETF_REALTIME_TIMING_START_TIME,
)

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
