from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as datetime_time


@dataclass(frozen=True)
class MarketWindow:
    name: str
    timezone: str
    sessions: tuple[tuple[datetime_time, datetime_time], ...]


MARKET_WINDOWS = (
    MarketWindow(
        name="A股",
        timezone="Asia/Shanghai",
        sessions=(
            (datetime_time(9, 30), datetime_time(11, 30)),
            (datetime_time(13, 0), datetime_time(15, 0)),
        ),
    ),
    MarketWindow(
        name="港股",
        timezone="Asia/Hong_Kong",
        sessions=(
            (datetime_time(9, 30), datetime_time(12, 0)),
            (datetime_time(13, 0), datetime_time(16, 0)),
        ),
    ),
    MarketWindow(
        name="日本",
        timezone="Asia/Tokyo",
        sessions=(
            (datetime_time(9, 0), datetime_time(11, 30)),
            (datetime_time(12, 30), datetime_time(15, 30)),
        ),
    ),
    MarketWindow(
        name="韩国",
        timezone="Asia/Seoul",
        sessions=((datetime_time(9, 0), datetime_time(15, 30)),),
    ),
    MarketWindow(
        name="美股",
        timezone="America/New_York",
        sessions=((datetime_time(9, 30), datetime_time(16, 0)),),
    ),
)


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


def is_market_holiday(market: MarketWindow, day: date) -> bool:
    if market.name == "美股":
        return day in us_market_holidays(day.year)
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
