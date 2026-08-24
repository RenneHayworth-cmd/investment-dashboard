from __future__ import annotations

from datetime import date, timedelta

from services.market_calendar import get_market_window, is_market_holiday

def _futures_trading_dates(start: date, end: date) -> list[str]:
    market = get_market_window("A股")
    days: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and (market is None or not is_market_holiday(market, current)):
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days
