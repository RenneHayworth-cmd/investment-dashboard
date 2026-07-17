from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from functools import lru_cache
import logging

import pandas as pd


logger = logging.getLogger(__name__)

try:
    import exchange_calendars as xcals
except ImportError:  # Optional enhancement when the package is available locally.
    xcals = None


@dataclass(frozen=True)
class MarketWindow:
    name: str
    timezone: str
    sessions: tuple[tuple[datetime_time, datetime_time], ...]
    calendar_name: str | None = None


MARKET_WINDOWS = (
    MarketWindow(
        name="A股",
        timezone="Asia/Shanghai",
        sessions=(
            (datetime_time(9, 30), datetime_time(11, 30)),
            (datetime_time(13, 0), datetime_time(15, 0)),
        ),
        calendar_name="XSHG",
    ),
    MarketWindow(
        name="港股",
        timezone="Asia/Hong_Kong",
        sessions=(
            (datetime_time(9, 30), datetime_time(12, 0)),
            (datetime_time(13, 0), datetime_time(16, 0)),
        ),
        calendar_name="XHKG",
    ),
    MarketWindow(
        name="日本",
        timezone="Asia/Tokyo",
        sessions=(
            (datetime_time(9, 0), datetime_time(11, 30)),
            (datetime_time(12, 30), datetime_time(15, 30)),
        ),
        calendar_name="XTKS",
    ),
    MarketWindow(
        name="韩国",
        timezone="Asia/Seoul",
        sessions=((datetime_time(9, 0), datetime_time(15, 30)),),
        calendar_name="XKRX",
    ),
    MarketWindow(
        name="美股",
        timezone="America/New_York",
        sessions=((datetime_time(9, 30), datetime_time(16, 0)),),
        calendar_name="XNYS",
    ),
)


def _date_set(*values: str) -> set[date]:
    return {date.fromisoformat(value) for value in values}


# Published 2026 cash-market closures. Weekends are handled separately.
STATIC_MARKET_HOLIDAYS = {
    "A股": _date_set(
        "2026-01-01", "2026-01-02",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
        "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    ),
    "港股": _date_set(
        "2026-01-01",
        "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-04-03", "2026-04-06", "2026-04-07",
        "2026-05-01", "2026-05-25",
        "2026-06-19",
        "2026-07-01",
        "2026-10-01", "2026-10-19",
        "2026-12-25",
    ),
    "日本": _date_set(
        "2026-01-01", "2026-01-02", "2026-01-12",
        "2026-02-11", "2026-02-23",
        "2026-03-20",
        "2026-04-29",
        "2026-05-04", "2026-05-05", "2026-05-06",
        "2026-07-20",
        "2026-08-11",
        "2026-09-21", "2026-09-22", "2026-09-23",
        "2026-10-12",
        "2026-11-03", "2026-11-23",
        "2026-12-31",
    ),
    "韩国": _date_set(
        "2026-01-01",
        "2026-02-16", "2026-02-17", "2026-02-18",
        "2026-03-02",
        "2026-05-01", "2026-05-05", "2026-05-25",
        "2026-06-03",
        "2026-07-17",
        "2026-08-17",
        "2026-09-24", "2026-09-25", "2026-09-28",
        "2026-10-05", "2026-10-09",
        "2026-12-25", "2026-12-31",
    ),
}


def _observed_date(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_market_holidays(year: int) -> set[date]:
    return {
        _observed_date(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_date(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_date(date(year, 6, 19)),
        _observed_date(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_date(date(year, 12, 25)),
    }


@lru_cache(maxsize=None)
def _get_exchange_calendar(calendar_name: str):
    if xcals is None:
        return None
    try:
        return xcals.get_calendar(calendar_name)
    except Exception as exc:
        logger.warning("交易所日历 %s 加载失败，将使用静态休市日兜底：%s", calendar_name, exc)
        return None


@lru_cache(maxsize=None)
def _warn_static_calendar_coverage(market_name: str, year: int) -> None:
    covered_years = sorted({day.year for day in STATIC_MARKET_HOLIDAYS.get(market_name, set())})
    if not covered_years or year <= max(covered_years):
        return
    coverage = "、".join(str(value) for value in covered_years)
    logger.warning(
        "%s 交易所日历不可用；静态休市日仅覆盖 %s 年，%s 年休市判断可能不完整。",
        market_name,
        coverage,
        year,
    )


def get_market_window(name: str) -> MarketWindow | None:
    return next((market for market in MARKET_WINDOWS if market.name == name), None)


def is_market_holiday(market: MarketWindow, day: date) -> bool:
    if market.calendar_name:
        calendar = _get_exchange_calendar(market.calendar_name)
        if calendar is not None:
            try:
                return not bool(calendar.is_session(pd.Timestamp(day)))
            except Exception as exc:
                logger.warning("%s 交易所日历查询 %s 失败，将使用静态休市日兜底：%s", market.name, day, exc)
    if market.name != "美股":
        _warn_static_calendar_coverage(market.name, day.year)
    if day in STATIC_MARKET_HOLIDAYS.get(market.name, set()):
        return True
    if market.name == "美股":
        holidays = set().union(*(us_market_holidays(year) for year in (day.year - 1, day.year, day.year + 1)))
        return day in holidays
    return False


def is_market_trading_day(market: MarketWindow, market_now: datetime) -> bool:
    market_date = market_now.date()
    return market_date.weekday() < 5 and not is_market_holiday(market, market_date)


def previous_weekday(day: date) -> date:
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def previous_trading_day(market: MarketWindow, day: date) -> date:
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5 or is_market_holiday(market, previous):
        previous -= timedelta(days=1)
    return previous


def expected_latest_trade_date(market: MarketWindow, market_now: datetime) -> date:
    """Return the newest trading date the dashboard should expect for a market."""
    if not is_market_trading_day(market, market_now):
        return previous_trading_day(market, market_now.date())
    if market_now.time() < market.sessions[0][0]:
        return previous_trading_day(market, market_now.date())
    return market_now.date()


def latest_completed_trade_date(market: MarketWindow, market_now: datetime) -> date:
    """Return the latest session whose regular market close has passed."""
    if not is_market_trading_day(market, market_now):
        return previous_trading_day(market, market_now.date())
    if market_now.time() <= market.sessions[-1][1]:
        return previous_trading_day(market, market_now.date())
    return market_now.date()


def latest_settled_trade_date(
    market: MarketWindow,
    market_now: datetime,
    *,
    settlement_delay: timedelta = timedelta(minutes=10),
) -> date:
    """Return the latest session safe to persist as a formal daily close."""
    if not is_market_trading_day(market, market_now):
        return previous_trading_day(market, market_now.date())
    close_at = datetime.combine(
        market_now.date(),
        market.sessions[-1][1],
        tzinfo=market_now.tzinfo,
    )
    if market_now < close_at + settlement_delay:
        return previous_trading_day(market, market_now.date())
    return market_now.date()
