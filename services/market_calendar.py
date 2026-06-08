from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


def is_market_trading_day(market: MarketWindow, market_now: datetime) -> bool:
    return market_now.weekday() < 5
