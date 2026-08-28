from __future__ import annotations

from datetime import datetime
import re
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_frames import filter_completed_market_dates, normalize_akshare_index_df
from services.market_calendar import (
    get_market_window,
    latest_settled_trade_date,
    previous_trading_day,
)


def get_sina_hk_index_history(
    symbol: str,
    *,
    now: datetime | None = None,
) -> pd.DataFrame | None:
    """Return only settled Hong Kong daily rows from Sina through AkShare."""
    import akshare as ak

    raw_df = ak.stock_hk_index_daily_sina(symbol=symbol)
    if raw_df is None or raw_df.empty:
        return None
    normalized = normalize_akshare_index_df(raw_df)
    market = get_market_window("港股")
    if market is None:
        return filter_completed_market_dates(normalized, "港股")
    market_now = (
        now.astimezone(ZoneInfo(market.timezone))
        if now is not None
        else datetime.now(ZoneInfo(market.timezone))
    )
    target_date = latest_settled_trade_date(market, market_now)
    dates = pd.to_datetime(normalized["trade_date"], errors="coerce")
    normalized = normalized.loc[dates.dt.date <= target_date].copy()
    return filter_completed_market_dates(normalized, "港股")


def fetch_hsi_official_completed_close(
    series_code: str,
    *,
    now: datetime | None = None,
) -> pd.DataFrame | None:
    """Fetch the latest settled close from Hang Seng Indexes' public feed."""
    import requests

    market = get_market_window("港股")
    if market is None:
        return None
    market_now = (
        now.astimezone(ZoneInfo(market.timezone))
        if now is not None
        else datetime.now(ZoneInfo(market.timezone))
    )
    target_date = latest_settled_trade_date(market, market_now)
    close_at = datetime.combine(
        target_date,
        market.sessions[-1][1],
        tzinfo=ZoneInfo(market.timezone),
    )
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"https://www.hsi.com.hk/eng/indexes/all-indexes/{series_code}",
        "User-Agent": "Mozilla/5.0",
    }
    last_error: Exception | None = None
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        try:
            response = session.get(
                "https://www.hsi.com.hk/"
                f"data/eng/rt/index-series/{series_code}/performance.do",
                headers=headers,
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            last_error = exc
            continue
        finally:
            session.close()

        series = payload.get("indexSeriesList") or []
        indexes = (series[0].get("indexList") or []) if series else []
        if not indexes:
            continue
        latest = indexes[0]
        quote_time = pd.to_datetime(latest.get("lastUpdate"), errors="coerce")
        if pd.isna(quote_time):
            continue
        quote_dt = quote_time.to_pydatetime().replace(tzinfo=ZoneInfo(market.timezone))
        if quote_dt.date() == target_date and quote_dt >= close_at:
            close = pd.to_numeric(latest.get("indexValue"), errors="coerce")
        elif (
            quote_dt.date() > target_date
            and previous_trading_day(market, quote_dt.date()) == target_date
        ):
            close = pd.to_numeric(latest.get("previousClose"), errors="coerce")
        else:
            return None
        if pd.isna(close):
            return None
        return pd.DataFrame(
            [{"trade_date": pd.Timestamp(target_date), "close": float(close)}]
        )
    if last_error is not None:
        raise RuntimeError(f"恒生指数官网请求失败：{last_error}") from last_error
    return None


def fetch_sina_hk_realtime_quote(
    symbol: str,
) -> dict[str, object] | None:
    """Fetch a Hong Kong index quote from Sina without persisting it."""
    import requests

    headers = {
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": "Mozilla/5.0",
    }
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        try:
            response = session.get(
                f"https://hq.sinajs.cn/list=rt_hk{symbol}",
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()
            content = response.content.decode("gb18030", errors="replace")
        except Exception:
            continue
        finally:
            session.close()

        matched = re.search(r'=\"(.*)\";?', content)
        if matched is None:
            continue
        fields = matched.group(1).split(",")
        if len(fields) < 19 or fields[0].strip().upper() != symbol.strip().upper():
            continue
        price = pd.to_numeric(fields[6], errors="coerce")
        previous_close = pd.to_numeric(fields[3], errors="coerce")
        change_pct = pd.to_numeric(fields[8], errors="coerce")
        quote_time = pd.to_datetime(f"{fields[17]} {fields[18]}", errors="coerce")
        if pd.isna(price) or pd.isna(quote_time):
            continue
        quote_dt = quote_time.to_pydatetime().replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
        return {
            "price": float(price),
            "previous_close": None if pd.isna(previous_close) else float(previous_close),
            "change_pct": None if pd.isna(change_pct) else float(change_pct),
            "quote_time": quote_dt,
            "source": "新浪财经",
        }
    return None


__all__ = [
    "get_sina_hk_index_history",
    "fetch_hsi_official_completed_close",
    "fetch_sina_hk_realtime_quote",
]
