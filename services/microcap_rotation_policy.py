"""ABCD fixed-capital policy, trading calendar and Decimal accounting."""
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from services.market_calendar import (get_market_window, is_market_trading_day,
                                      latest_settled_trade_date, uncovered_calendar_years)

VERSION = "microcap20-abcd-v2"
INITIAL = Decimal("220000.00")
FEE = Decimal("2.00")
ETF = "512890"
ETF_FEE_RATE = Decimal("0.00006")  # 512890 commission: 0.6 per ten-thousand
POLICY = dict(version=VERSION, initial=220000, stock_target=10000, fee=2,
              etf_fee_rate="0.00006",
              universe="BK1158", count=20, ma=15, band=0.025,
              execution="next_session_unadjusted_close", reinvest=False,
              missing_data="skip_or_mark_provisional", launch="today_close_from_previous_snapshot")
NAMES = {"A": "BK1158指数基准", "B": "微盘20周度轮动",
         "C": "微盘20择时·现金", "D": "微盘20择时·512890"}

class DataGap(ValueError):
    """Inputs are incomplete; never advance the ledger through this date."""

def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def transaction_fee(code, amount):
    """Return the simulated commission for a filled order.

    Stocks keep the fixed 2 yuan fee.  512890 is charged at 0.6 per
    ten-thousand of turnover, rounded to cents for ledger accounting.
    """
    amount = money(amount)
    if code == ETF:
        return money(amount * ETF_FEE_RATE)
    return FEE

def now():
    return datetime.now(ZoneInfo("Asia/Shanghai"))

def settled():
    return latest_settled_trade_date(get_market_window("A股"), now()).isoformat()

def trading(day):
    day = date.fromisoformat(str(day))
    market = get_market_window("A股")
    if uncovered_calendar_years(market, day, day):
        raise DataGap(f"{day.year} 交易日历未覆盖")
    return is_market_trading_day(market, datetime.combine(day, datetime.min.time()))

def next_day(day):
    value = date.fromisoformat(day) + timedelta(days=1)
    while not trading(value.isoformat()):
        value += timedelta(days=1)
    return value.isoformat()

def sessions(start, end):
    value = start
    while value <= end:
        if trading(value):
            yield value
        value = (date.fromisoformat(value) + timedelta(days=1)).isoformat()

def weekly_turn(day, following):
    return date.fromisoformat(day).isocalendar()[:2] != date.fromisoformat(following).isocalendar()[:2]
