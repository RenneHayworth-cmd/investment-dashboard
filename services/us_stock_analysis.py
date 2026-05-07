from __future__ import annotations

import re

import pandas as pd


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
    adjust: str | None = "forward",
) -> pd.DataFrame:
    from tickflow import TickFlow

    client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    name = fetch_tickflow_us_name(symbol, api_key=api_key)
    kwargs = {
        "period": "1d",
        "count": count,
        "as_dataframe": True,
    }
    if adjust:
        kwargs["adjust"] = adjust
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
