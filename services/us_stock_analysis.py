from __future__ import annotations

import re

import pandas as pd

from services.fund_analysis import (
    FUND_ADJUST_FORWARD_ADDITIVE,
    normalize_fund_adjustment,
)


def parse_us_symbols(text: str) -> list[str]:
    symbols = [item.strip().upper() for item in re.split(r"[\s,，;；]+", text) if item.strip()]
    return list(dict.fromkeys(symbols))


def infer_us_symbol(code: str) -> str:
    code = code.strip().upper()
    if not code:
        raise ValueError("美股代码不能为空。")
    if code.endswith(".US"):
        return code
    if "." in code:
        return f"{code}.US"
    if "-" in code:
        return f"{code.replace('-', '.', 1)}.US"
    if "_" in code:
        return f"{code.replace('_', '.', 1)}.US"
    return f"{code}.US"


def fetch_tickflow_us_daily(
    symbol: str,
    api_key: str = "",
    count: int = 1500,
    adjust: str | None = FUND_ADJUST_FORWARD_ADDITIVE,
) -> pd.DataFrame:
    """TickFlow with compatible unadjusted AkShare backups (legacy entry point)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from services.market_calendar import get_market_window, latest_settled_trade_date
    from services.market_fallback import MarketSource, fetch_market_fallback, normalize_daily_prices

    adjustment = normalize_fund_adjustment(adjust)
    sources = [MarketSource("TickFlow", lambda: _fetch_tickflow_us_daily(symbol, api_key, count, adjustment))]
    if adjustment == "none":
        ticker = symbol.removesuffix(".US")

        def convert(raw):
            frame = normalize_daily_prices(raw).tail(count).rename(columns={
                "date": "日期", "close": "收盘价", "open": "开盘价", "high": "最高价", "low": "最低价", "volume": "成交量", "amount": "成交额",
            })
            frame["symbol"] = symbol
            frame["name"] = symbol
            return frame

        def sina():
            import akshare as ak
            return convert(ak.stock_us_daily(symbol=ticker, adjust=""))

        def eastmoney():
            import akshare as ak
            spot = ak.stock_us_spot_em()
            codes = spot["代码"].astype(str)
            matched = codes[codes.str.split(".", n=1).str[-1].str.upper().eq(ticker.upper())]
            if len(matched) != 1:
                raise ValueError(f"无法唯一确认 {ticker} 的东方财富证券代码")
            return convert(ak.stock_us_hist(symbol=matched.iloc[0], period="daily", adjust=""))

        sources += [MarketSource("stock_us_daily/新浪", sina), MarketSource("stock_us_hist/东方财富", eastmoney)]
    target = latest_settled_trade_date(get_market_window("美股"), datetime.now(ZoneInfo("America/New_York")))
    return fetch_market_fallback(sources, lambda frame: frame, date_column="日期", price_column="收盘价", target_date=target)


def _fetch_tickflow_us_daily(
    symbol: str,
    api_key: str = "",
    count: int = 1500,
    adjust: str | None = FUND_ADJUST_FORWARD_ADDITIVE,
) -> pd.DataFrame:
    from tickflow import TickFlow

    adjustment = normalize_fund_adjustment(adjust)
    client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    name = fetch_tickflow_us_name(symbol, api_key=api_key)
    kwargs = {
        "period": "1d",
        "count": count,
        "as_dataframe": True,
        "adjust": adjustment,
    }
    df = client.klines.get(symbol, **kwargs)
    if df is None or df.empty:
        raise ValueError(f"TickFlow 未返回 {symbol} 的日线数据。")

    normalized = df.copy()
    normalized.columns = [str(col).strip() for col in normalized.columns]
    if "trade_date" not in normalized.columns or "close" not in normalized.columns:
        raise ValueError(f"TickFlow 返回列无法识别：{list(normalized.columns)}")

    keep_columns = [col for col in ("trade_date", "open", "high", "low", "close", "volume", "amount") if col in normalized.columns]
    result = normalized[keep_columns].copy()
    rename_map = {
        "trade_date": "日期",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "volume": "成交量",
        "amount": "成交额",
    }
    result = result.rename(columns=rename_map)
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce")
    result["收盘价"] = pd.to_numeric(result["收盘价"], errors="coerce")
    result = result.dropna(subset=["日期", "收盘价"])
    result = result.sort_values("日期").drop_duplicates("日期").reset_index(drop=True)
    result["symbol"] = symbol
    result["name"] = name or symbol
    return result


def fetch_tickflow_us_name(symbol: str, api_key: str = "") -> str:
    try:
        from tickflow import TickFlow

        client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
        instruments = client.instruments.batch(symbols=[symbol])
        if instruments:
            name = str(instruments[0].get("name", "")).strip()
            if name:
                return name
    except Exception:
        pass
    return symbol
