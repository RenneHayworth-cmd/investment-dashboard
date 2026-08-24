from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_config import (
    CFFEX_FUTURES_MAIN_PRODUCTS, INDEX_CONFIG, INDEX_REPORT_DISPLAY_DAYS,
    YAHOO_CHART_HOSTS, YAHOO_REQUEST_GATE,
)
from services.index_frames import (
    build_export_df, extract_raw_from_export_df, filter_market_trading_dates,
    is_sparse_daily_history, merge_newer_index_rows, merge_raw_index_data,
    normalize_akshare_index_df,
)
from services.market_calendar import (
    expected_latest_trade_date, get_market_window, is_market_holiday,
    is_market_trading_day, latest_completed_trade_date, latest_settled_trade_date,
)

def fetch_yahoo_chart_payload(
    symbol: str,
    params: dict[str, object],
    *,
    timeout: float = 10,
) -> dict:
    """Fetch a Yahoo chart payload with alternate hosts and proxy paths."""
    import requests

    errors: list[str] = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for trust_env in (True, False):
        session = requests.Session()
        session.trust_env = trust_env
        denied_hosts = 0
        try:
            for host in YAHOO_CHART_HOSTS:
                try:
                    with YAHOO_REQUEST_GATE:
                        response = session.get(
                            f"https://{host}/v8/finance/chart/{symbol}",
                            params=params,
                            headers=headers,
                            timeout=timeout,
                        )
                    if getattr(response, "status_code", None) in {401, 403, 429}:
                        denied_hosts += 1
                    response.raise_for_status()
                    payload = response.json()
                    result = payload.get("chart", {}).get("result") or []
                    if result:
                        return payload
                    chart_error = payload.get("chart", {}).get("error")
                    errors.append(f"{host}: {chart_error or '未返回行情数据'}")
                except Exception as exc:
                    errors.append(f"{host}: {exc}")
        finally:
            session.close()

        # A second proxy mode cannot fix an explicit access denial from both
        # Yahoo hosts and only doubles the wait and error text.
        if denied_hosts == len(YAHOO_CHART_HOSTS):
            break

    detail = " | ".join(errors[-2:]) if errors else "未返回行情数据"
    raise RuntimeError(f"Yahoo行情请求失败（{symbol}）：{detail}")

def fetch_yahoo_latest_index_row(symbol: str) -> pd.DataFrame | None:
    try:
        payload = fetch_yahoo_chart_payload(
            symbol,
            {"range": "10d", "interval": "1d"},
            timeout=10,
        )
        result = payload.get("chart", {}).get("result", [])
        if not result:
            return None

        item = result[0]
        timestamps = item.get("timestamp") or []
        closes = item.get("indicators", {}).get("quote", [{}])[0].get("close") or []
        rows = []
        for timestamp, close in zip(timestamps, closes):
            if close is None:
                continue
            rows.append(
                {
                    "trade_date": pd.Timestamp(datetime.fromtimestamp(timestamp).date()),
                    "close": float(close),
                }
            )
        if not rows:
            return None
        return pd.DataFrame([rows[-1]])
    except Exception:
        return None

def supplement_stale_yahoo_history(
    df: pd.DataFrame,
    symbol: str,
    *,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Retry Yahoo's short window only when its main history response is stale."""
    if df is None or df.empty or "trade_date" not in df.columns:
        return df

    market = get_market_window("美股")
    if market is None:
        return df
    market_now = now or datetime.now(ZoneInfo(market.timezone))
    expected_date = latest_completed_trade_date(market, market_now)
    latest_date = pd.to_datetime(df["trade_date"], errors="coerce").max()
    if not pd.isna(latest_date) and latest_date.date() >= expected_date:
        return df

    latest_row = fetch_yahoo_latest_index_row(symbol)
    return merge_newer_index_rows(df, latest_row)

def get_index_data_from_yahoo(symbol: str, index_name: str, days: int = 30):
    if days > 365:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=days + 30)
        params = {
            "period1": int(start_time.timestamp()),
            "period2": int(end_time.timestamp()),
            "interval": "1d",
        }
    else:
        params = {"range": "1y", "interval": "1d"}
    payload = fetch_yahoo_chart_payload(symbol, params, timeout=10)
    result = payload.get("chart", {}).get("result", [])
    if not result:
        return None

    item = result[0]
    timestamps = item.get("timestamp") or []
    meta = item.get("meta") or {}
    quote = item.get("indicators", {}).get("quote", [{}])[0]
    closes = quote.get("close") or []
    rows = []
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        rows.append(
            {
                "trade_date": pd.Timestamp(datetime.fromtimestamp(timestamp).date()),
                "close": float(close),
            }
        )
    latest_price = pd.to_numeric(meta.get("regularMarketPrice"), errors="coerce")
    latest_timestamp = pd.to_numeric(meta.get("regularMarketTime"), errors="coerce")
    if not pd.isna(latest_price) and not pd.isna(latest_timestamp):
        market_timezone = ZoneInfo(str(meta.get("exchangeTimezoneName") or "America/New_York"))
        latest_date = datetime.fromtimestamp(float(latest_timestamp), tz=market_timezone).date()
        if not rows or latest_date > rows[-1]["trade_date"].date():
            rows.append({"trade_date": pd.Timestamp(latest_date), "close": float(latest_price)})
    if not rows:
        return None
    raw_df = supplement_stale_yahoo_history(pd.DataFrame(rows), symbol)
    return build_export_df(raw_df, index_name, days=days)

__all__ = ['fetch_yahoo_chart_payload', 'fetch_yahoo_latest_index_row', 'supplement_stale_yahoo_history', 'get_index_data_from_yahoo']
