from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd


INDEX_CONFIG = {
    "沪深300": {
        "source": "akshare_cn",
        "code": "000300",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000300.SH",
    },
    "创业板指": {
        "source": "akshare_cn",
        "code": "399006",
        "market": "sz",
        "market_group": "A股",
        "tickflow_symbol": "399006.SZ",
    },
    "中证500": {
        "source": "akshare_cn",
        "code": "000905",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000905.SH",
    },
    "中证1000": {
        "source": "akshare_cn",
        "code": "000852",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000852.SH",
    },
    "中证红利低波": {
        "source": "akshare_csindex",
        "code": "H30269",
        "market_group": "A股",
    },
    "国证自由现金流": {
        "source": "akshare_cn",
        "code": "980092",
        "market": "sz",
        "market_group": "A股",
        "tickflow_symbol": "980092.SZ",
    },
    "恒生科技": {
        "source": "akshare_hk",
        "code": "HSTECH",
        "display_symbol": "HSTECH",
        "market_group": "港股",
    },
    "铁矿石主连": {
        "source": "akshare_futures_main",
        "code": "I0",
        "display_symbol": "I0",
        "market_group": "A股",
    },
    "标普500": {
        "source": "akshare_us",
        "code": ".INX",
        "tickflow_symbol": ".INX.US",
        "display_symbol": "SPX",
        "market_group": "美股",
    },
    "纳斯达克综合": {
        "source": "akshare_us",
        "code": ".IXIC",
        "tickflow_symbol": ".IXIC.US",
        "market_group": "美股",
    },
    "日经225": {
        "source": "akshare_global",
        "code": "日经225",
        "yahoo_symbol": "^N225",
        "display_symbol": "N225",
        "market_group": "日本",
    },
    "韩国KOSPI": {
        "source": "akshare_global",
        "code": "韩国KOSPI",
        "yahoo_symbol": "^KS11",
        "display_symbol": "KOSPI",
        "market_group": "韩国",
    },
}


def build_export_df(df: pd.DataFrame, index_name: str, days: int = 30) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None

    result = df.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result = result.sort_values("trade_date").reset_index(drop=True)
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result = result.dropna(subset=["trade_date", "close"])

    if result.empty:
        return None

    result["MA20"] = result["close"].rolling(window=20).mean()
    result["偏离率"] = (result["close"] - result["MA20"]) / result["MA20"] * 100

    start_date = datetime.now() - timedelta(days=days)
    recent_data = result[result["trade_date"] >= start_date].copy()
    if recent_data.empty:
        return None

    export_df = recent_data[["trade_date", "close", "MA20", "偏离率"]].copy()
    export_df["trade_date"] = export_df["trade_date"].dt.strftime("%Y-%m-%d")
    export_df["close"] = export_df["close"].round(2)
    export_df["MA20"] = export_df["MA20"].round(2)
    export_df["偏离率"] = export_df["偏离率"].round(2)
    export_df.columns = [
        "日期",
        f"{index_name}_收盘价",
        f"{index_name}_MA20",
        f"{index_name}_偏离率(%)",
    ]
    return export_df


def normalize_akshare_index_df(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for column in df.columns:
        column_text = str(column).strip()
        if column_text in {"日期", "date", "trade_date", "交易日期"}:
            rename_map[column] = "trade_date"
        elif column_text in {"收盘", "收盘价", "最新价", "close", "Close"}:
            rename_map[column] = "close"

    normalized = df.rename(columns=rename_map)
    if "trade_date" not in normalized.columns or "close" not in normalized.columns:
        raise ValueError(f"AkShare返回列无法识别：{list(df.columns)}")
    return normalized[["trade_date", "close"]].copy()


def fetch_yahoo_latest_index_row(symbol: str) -> pd.DataFrame | None:
    try:
        import requests

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"range": "10d", "interval": "1d"}
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        payload = response.json()
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


def append_akshare_latest_index_row(ak, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    latest_history_date = normalized["trade_date"].max().date()
    today = datetime.now().date()
    if latest_history_date >= today:
        return normalized

    latest_price = None
    try:
        spot_df = ak.stock_zh_index_spot_em(symbol="中证系列指数")
        matched = spot_df[spot_df["代码"].astype(str).str.upper() == index_code.upper()]
        if not matched.empty:
            latest_price = matched.iloc[0].get("最新价")
    except Exception:
        pass

    if latest_price is None:
        try:
            spot_df = ak.stock_zh_index_spot_sina()
            code_values = spot_df["代码"].astype(str).str.upper()
            matched = spot_df[
                (code_values == index_code.upper())
                | (code_values == f"SH{index_code.upper()}")
                | (code_values == f"SZ{index_code.upper()}")
                | (code_values == f"CSI{index_code.upper()}")
            ]
            if not matched.empty:
                latest_price = matched.iloc[0].get("最新价")
        except Exception:
            pass

    latest_price = pd.to_numeric(latest_price, errors="coerce")
    if not pd.isna(latest_price):
        supplement = pd.DataFrame([{"trade_date": pd.Timestamp(today), "close": float(latest_price)}])
        return pd.concat([normalized, supplement], ignore_index=True)

    yahoo_df = fetch_yahoo_latest_index_row(f"{index_code}.SS")
    if yahoo_df is not None and not yahoo_df.empty:
        yahoo_date = pd.to_datetime(yahoo_df.iloc[0]["trade_date"]).date()
        if yahoo_date > latest_history_date:
            return pd.concat([normalized, yahoo_df], ignore_index=True)

    return normalized


def append_eastmoney_quote_row(df: pd.DataFrame, secid: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    latest_history_date = normalized["trade_date"].max().date()

    try:
        import requests

        quote = None
        session = requests.Session()
        session.trust_env = False
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
            "User-Agent": "Mozilla/5.0",
        }
        params = {
            "secid": secid,
            "fields": "f43,f57,f58,f86",
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "fltt": "2",
            "invt": "2",
        }
        for host in (
            "push2.eastmoney.com",
            "36.push2.eastmoney.com",
            "48.push2.eastmoney.com",
            "push2delay.eastmoney.com",
        ):
            try:
                response = session.get(
                    f"https://{host}/api/qt/stock/get",
                    params=params,
                    headers=headers,
                    timeout=3,
                )
                response.raise_for_status()
                quote = response.json().get("data") or {}
                if quote:
                    break
            except Exception:
                continue
        if not quote:
            return normalized
        latest_price = pd.to_numeric(quote.get("f43"), errors="coerce")
        quote_timestamp = pd.to_numeric(quote.get("f86"), errors="coerce")
        if pd.isna(latest_price) or pd.isna(quote_timestamp):
            return normalized
        quote_date = datetime.fromtimestamp(float(quote_timestamp)).date()
        if quote_date <= latest_history_date:
            return normalized
        supplement = pd.DataFrame([{"trade_date": pd.Timestamp(quote_date), "close": float(latest_price)}])
        return pd.concat([normalized, supplement], ignore_index=True)
    except Exception:
        return normalized


def append_hk_index_spot_row(ak, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    latest_history_date = normalized["trade_date"].max().date()
    today = datetime.now().date()
    if latest_history_date >= today:
        return normalized

    try:
        spot_df = ak.stock_hk_index_spot_sina()
        code_col = spot_df.columns[0]
        price_col = "最新价" if "最新价" in spot_df.columns else spot_df.columns[2]
        matched = spot_df[spot_df[code_col].astype(str).str.upper() == index_code.upper()]
        if matched.empty:
            return normalized
        latest_price = pd.to_numeric(matched.iloc[0][price_col], errors="coerce")
        if pd.isna(latest_price):
            return normalized
        supplement = pd.DataFrame([{"trade_date": pd.Timestamp(today), "close": float(latest_price)}])
        return pd.concat([normalized, supplement], ignore_index=True)
    except Exception:
        return normalized


def append_futures_spot_row(ak, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    latest_history_date = normalized["trade_date"].max().date()
    now = datetime.now()
    today = now.date()
    if latest_history_date >= today or now.time() < time(15, 0):
        return normalized

    try:
        spot_df = ak.futures_zh_spot(symbol=index_code.upper(), market="CF", adjust="0")
        if spot_df is None or spot_df.empty:
            return normalized
        price_col = "current_price" if "current_price" in spot_df.columns else "last_close"
        if price_col not in spot_df.columns:
            return normalized
        latest_price = pd.to_numeric(spot_df.iloc[0][price_col], errors="coerce")
        if pd.isna(latest_price):
            return normalized
        supplement = pd.DataFrame([{"trade_date": pd.Timestamp(today), "close": float(latest_price)}])
        return pd.concat([normalized, supplement], ignore_index=True)
    except Exception:
        return normalized


def get_index_data_from_akshare_csindex(index_code: str, index_name: str, days: int = 30):
    import akshare as ak

    start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    attempts = [
        lambda: ak.stock_zh_index_hist_csindex(
            symbol=index_code,
            start_date=start_date,
            end_date=end_date,
        ),
        lambda: ak.stock_zh_index_daily(symbol=index_code.lower()),
        lambda: ak.stock_zh_index_daily_em(symbol=f"csi{index_code}"),
        lambda: ak.stock_zh_index_daily_em(symbol=index_code.lower()),
    ]

    last_error = None
    for fetcher in attempts:
        try:
            raw_df = fetcher()
            if raw_df is None or raw_df.empty:
                continue
            df = normalize_akshare_index_df(raw_df)
            if index_code.upper() == "H30269":
                df = append_eastmoney_quote_row(df, "2.H30269")
            return build_export_df(df, index_name, days=days)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"{index_name} AkShare 获取失败：{last_error}")


def get_index_data_from_akshare_cn(index_code: str, market: str, index_name: str, days: int = 30):
    import akshare as ak

    start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    market_symbol = f"{market}{index_code}".lower()
    attempts = [
        lambda: ak.index_zh_a_hist(
            symbol=index_code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
        ),
        lambda: ak.stock_zh_index_daily_em(symbol=market_symbol),
        lambda: ak.stock_zh_index_daily(symbol=market_symbol),
    ]

    last_error = None
    for fetcher in attempts:
        try:
            raw_df = fetcher()
            if raw_df is None or raw_df.empty:
                continue
            df = normalize_akshare_index_df(raw_df)
            return build_export_df(df, index_name, days=days)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"{index_name} AkShare 获取失败：{last_error}")


def get_index_data_from_akshare_us(index_code: str, index_name: str, days: int = 30):
    import akshare as ak

    raw_df = ak.index_us_stock_sina(symbol=index_code)
    df = normalize_akshare_index_df(raw_df)
    return build_export_df(df, index_name, days=days)


def get_index_data_from_akshare_hk(index_code: str, index_name: str, days: int = 30):
    import akshare as ak

    raw_df = ak.stock_hk_index_daily_sina(symbol=index_code)
    df = normalize_akshare_index_df(raw_df)
    if index_code.upper() == "HSTECH":
        df = append_hk_index_spot_row(ak, df, index_code)
    return build_export_df(df, index_name, days=days)


def get_index_data_from_yahoo(symbol: str, index_name: str, days: int = 30):
    import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "1y", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("chart", {}).get("result", [])
    if not result:
        return None

    item = result[0]
    timestamps = item.get("timestamp") or []
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
    if not rows:
        return None
    return build_export_df(pd.DataFrame(rows), index_name, days=days)


def get_index_data_from_akshare_global(index_code: str, index_name: str, days: int = 30, yahoo_symbol: str | None = None):
    import akshare as ak

    try:
        raw_df = ak.index_global_hist_em(symbol=index_code)
        df = normalize_akshare_index_df(raw_df)
        return build_export_df(df, index_name, days=days)
    except Exception:
        if yahoo_symbol:
            yahoo_df = get_index_data_from_yahoo(yahoo_symbol, index_name, days=days)
            if yahoo_df is not None and not yahoo_df.empty:
                return yahoo_df
        raise


def get_index_data_from_akshare_futures_main(index_code: str, index_name: str, days: int = 30):
    import akshare as ak

    raw_df = ak.futures_zh_daily_sina(symbol=index_code)
    df = normalize_akshare_index_df(raw_df)
    if index_code.upper() == "I0":
        df = append_futures_spot_row(ak, df, index_code)
    return build_export_df(df, index_name, days=days)


def get_index_data_from_tickflow(api_key: str, index_code: str, index_name: str, days: int = 30):
    from tickflow import TickFlow

    client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    count = max(days * 2, 80)
    df = client.klines.get(index_code, period="1d", count=count, as_dataframe=True)
    if df is None or df.empty:
        return None
    if api_key:
        df = append_tickflow_quote_row(client, df, index_code)
    return build_export_df(df, index_name, days=days)


def tickflow_quote_date(symbol: str, timestamp) -> pd.Timestamp:
    quote_time = pd.to_numeric(timestamp, errors="coerce")
    if pd.isna(quote_time):
        if str(symbol).upper().endswith(".US"):
            return pd.Timestamp(datetime.now(ZoneInfo("America/New_York")).date())
        return pd.Timestamp(datetime.now().date())

    timestamp_value = float(quote_time)
    if timestamp_value > 10_000_000_000:
        timestamp_value = timestamp_value / 1000

    market_zone = ZoneInfo("America/New_York") if str(symbol).upper().endswith(".US") else ZoneInfo("Asia/Shanghai")
    quote_dt = datetime.fromtimestamp(timestamp_value, tz=timezone.utc).astimezone(market_zone)
    return pd.Timestamp(quote_dt.date())


def append_tickflow_quote_row(client, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = normalize_tickflow_index_df(df)
    try:
        quote_df = client.quotes.get_by_symbols([index_code], as_dataframe=True)
        if quote_df is None or quote_df.empty:
            return normalized
        quote_row = quote_df.iloc[0]
        latest_price = pd.to_numeric(quote_row.get("last_price"), errors="coerce")
        if pd.isna(latest_price):
            return normalized

        quote_timestamp = quote_row.get("timestamp")
        quote_date = tickflow_quote_date(index_code, quote_timestamp)
        latest_history_date = normalized["trade_date"].max()
        if quote_date < latest_history_date:
            return normalized

        supplement = pd.DataFrame([{"trade_date": quote_date, "close": float(latest_price)}])
        return merge_raw_index_data(normalized, supplement)
    except Exception:
        return normalized


def normalize_tickflow_index_df(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    normalized.columns = [str(col).strip() for col in normalized.columns]
    if "trade_date" not in normalized.columns or "close" not in normalized.columns:
        raise ValueError(f"TickFlow返回列无法识别：{list(df.columns)}")
    result = normalized[["trade_date", "close"]].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result = result.dropna(subset=["trade_date", "close"])
    return result.sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)


def get_index_raw_from_tickflow(api_key: str, index_code: str, count: int = 80) -> pd.DataFrame | None:
    from tickflow import TickFlow

    client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    df = client.klines.get(index_code, period="1d", count=count, as_dataframe=True)
    if df is None or df.empty:
        return None
    return normalize_tickflow_index_df(df)


def merge_raw_index_data(old_df: pd.DataFrame | None, new_df: pd.DataFrame) -> pd.DataFrame:
    if old_df is None or old_df.empty:
        merged = new_df.copy()
    else:
        merged = pd.concat([old_df, new_df], ignore_index=True)
    merged["trade_date"] = pd.to_datetime(merged["trade_date"], errors="coerce")
    merged["close"] = pd.to_numeric(merged["close"], errors="coerce")
    merged = merged.dropna(subset=["trade_date", "close"])
    return merged.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)


def raw_cache_symbol(index_name: str, index_config) -> str:
    if isinstance(index_config, dict) and index_config.get("tickflow_symbol"):
        return f"index_raw_{index_config['tickflow_symbol']}"
    return f"index_raw_{index_name}"


def display_index_symbol(index_config) -> str:
    if not isinstance(index_config, dict):
        return str(index_config).split(".", 1)[0]
    symbol = index_config.get(
        "display_symbol",
        index_config.get("tickflow_symbol", index_config.get("code", "")),
    )
    symbol_text = str(symbol).strip()
    if symbol_text.startswith("."):
        return symbol_text.rsplit(".", 1)[0].lstrip(".")
    return symbol_text.split(".", 1)[0]


def merge_by_date(all_data: list[pd.DataFrame]) -> pd.DataFrame:
    merged_df = all_data[0]
    for df in all_data[1:]:
        merged_df = pd.merge(merged_df, df, on="日期", how="outer")
    return merged_df.sort_values("日期").reset_index(drop=True)


def generate_index_ma20_report(api_key: str, days: int = 30) -> pd.DataFrame:
    all_data = []
    errors = []
    for index_name, index_config in INDEX_CONFIG.items():
        try:
            df = fetch_one_index(index_name, index_config, api_key=api_key, days=days)

            if df is not None and not df.empty:
                all_data.append(df)
        except Exception as exc:
            errors.append(f"{index_name}: {exc}")

    if not all_data:
        raise RuntimeError("未获取到任何指数数据。" + " | ".join(errors))

    merged_df = merge_by_date(all_data)
    if errors:
        merged_df.attrs["errors"] = errors
    return merged_df


def fetch_one_index(index_name: str, index_config, api_key: str, days: int = 30) -> pd.DataFrame | None:
    if isinstance(index_config, dict):
        tickflow_symbol = index_config.get("tickflow_symbol")
        tickflow_error = None
        if tickflow_symbol:
            try:
                df = get_index_data_from_tickflow(
                    api_key,
                    tickflow_symbol,
                    index_name,
                    days=days,
                )
                if df is not None and not df.empty:
                    return df
            except Exception as exc:
                tickflow_error = exc

        source = index_config.get("source")
        code = index_config.get("code")
        try:
            if source == "akshare_cn":
                return get_index_data_from_akshare_cn(
                    code,
                    index_config.get("market", "sh"),
                    index_name,
                    days=days,
                )
            if source == "akshare_csindex":
                return get_index_data_from_akshare_csindex(code, index_name, days=days)
            if source == "akshare_us":
                return get_index_data_from_akshare_us(code, index_name, days=days)
            if source == "akshare_hk":
                return get_index_data_from_akshare_hk(code, index_name, days=days)
            if source == "akshare_global":
                return get_index_data_from_akshare_global(
                    code,
                    index_name,
                    days=days,
                    yahoo_symbol=index_config.get("yahoo_symbol"),
                )
            if source == "akshare_futures_main":
                return get_index_data_from_akshare_futures_main(code, index_name, days=days)
            raise ValueError(f"未知数据源：{source}")
        except Exception as exc:
            if tickflow_error:
                raise RuntimeError(f"TickFlow失败：{tickflow_error}；AkShare失败：{exc}") from exc
            raise

    return get_index_data_from_tickflow(api_key, index_config, index_name, days=days)


def build_summary(report_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for index_name, index_config in INDEX_CONFIG.items():
        close_col = f"{index_name}_收盘价"
        ma20_col = f"{index_name}_MA20"
        deviation_col = f"{index_name}_偏离率(%)"
        if close_col not in report_df.columns:
            continue
        valid_rows = report_df.dropna(subset=[close_col, ma20_col, deviation_col])
        if valid_rows.empty:
            continue
        latest = valid_rows.iloc[-1]
        previous_close = pd.NA
        daily_change_pct = pd.NA
        if len(valid_rows) >= 2:
            previous_close = valid_rows.iloc[-2][close_col]
            if previous_close:
                daily_change_pct = (latest[close_col] / previous_close - 1) * 100
        rows.append(
            {
                "指数": index_name,
                "代码": display_index_symbol(index_config),
                "日期": latest["日期"],
                "收盘价": latest[close_col],
                "前收盘价": previous_close,
                "当日涨跌幅(%)": daily_change_pct,
                "MA20": latest[ma20_col],
                "偏离率(%)": latest[deviation_col],
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).reset_index(drop=True)
