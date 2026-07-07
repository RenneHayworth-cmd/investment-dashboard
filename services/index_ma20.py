from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd


INDEX_CONFIG = {
    "创业板指": {
        "source": "akshare_cn",
        "code": "399006",
        "market": "sz",
        "market_group": "A股",
        "tickflow_symbol": "399006.SZ",
    },
    "沪深300": {
        "source": "akshare_cn",
        "code": "000300",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000300.SH",
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
    "中证2000": {
        "source": "akshare_cn",
        "code": "932000",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "932000.SH",
        "eastmoney_quote_secid": "2.932000",
    },
    "微盘股": {
        "source": "eastmoney_kline",
        "code": "90.BK1158",
        "display_symbol": "BK1158",
        "market_group": "A股",
        "fqt": "1",
        "akshare_board_symbol": "BK1158",
        "require_current_quote": True,
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
        "eastmoney_quote_secid": "124.HSTECH",
        "require_current_quote": True,
    },
    "恒生港股通高息低波": {
        "source": "eastmoney_kline",
        "code": "124.HSHYLV",
        "display_symbol": "HSHYLV",
        "market_group": "港股",
        "akshare_hk_em_symbol": "HSHYLV",
        "optional": True,
        "require_current_quote": True,
    },
    "标普500": {
        "source": "akshare_us",
        "code": ".INX",
        "tickflow_symbol": ".INX.US",
        "yahoo_symbol": "^GSPC",
        "display_symbol": "SPX",
        "market_group": "美股",
        "require_current_quote": True,
    },
    "纳斯达克综合": {
        "source": "akshare_us",
        "code": ".IXIC",
        "yahoo_symbol": "^IXIC",
        "display_symbol": "IXIC",
        "market_group": "美股",
        "require_current_quote": True,
    },
    "纳斯达克100": {
        "source": "akshare_us",
        "code": ".NDX",
        "yahoo_symbol": "^NDX",
        "display_symbol": "NDX",
        "market_group": "美股",
        "require_current_quote": True,
    },
    "VIX恐慌指数": {
        "source": "yahoo",
        "code": "^VIX",
        "display_symbol": "VIX",
        "market_group": "美股",
        "require_current_quote": True,
        "show_ma20_deviation": False,
    },
    "日经225": {
        "source": "akshare_global",
        "code": "日经225",
        "yahoo_symbol": "^N225",
        "display_symbol": "N225",
        "market_group": "日本",
        "require_current_quote": True,
    },
    "韩国KOSPI": {
        "source": "akshare_global",
        "code": "韩国KOSPI",
        "yahoo_symbol": "^KS11",
        "display_symbol": "KOSPI",
        "market_group": "韩国",
        "require_current_quote": True,
    },
    "铁矿石主连": {
        "source": "akshare_futures_main",
        "code": "I0",
        "display_symbol": "I0",
        "market_group": "A股",
    },
    "沪金主连": {
        "source": "akshare_futures_main",
        "code": "AU0",
        "display_symbol": "AU0",
        "market_group": "A股",
    },
    "沪银主连": {
        "source": "akshare_futures_main",
        "code": "AG0",
        "display_symbol": "AG0",
        "market_group": "A股",
    },
    "原油主连": {
        "source": "akshare_futures_main",
        "code": "SC0",
        "display_symbol": "SC0",
        "market_group": "A股",
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
    transition_date, interval_return_pct = calculate_ma20_transition(result, "close", "MA20", date_col="trade_date")

    start_date = datetime.now() - timedelta(days=days)
    recent_data = result[result["trade_date"] >= start_date].copy()
    if recent_data.empty:
        return None

    export_df = recent_data[["trade_date", "close", "MA20", "偏离率"]].copy()
    export_df["trade_date"] = export_df["trade_date"].dt.strftime("%Y-%m-%d")
    export_df["close"] = export_df["close"].round(2)
    export_df["MA20"] = export_df["MA20"].round(2)
    export_df["偏离率"] = export_df["偏离率"].round(2)
    export_df["状态转变时间"] = transition_date
    export_df["区间涨幅"] = round(interval_return_pct, 2) if not pd.isna(interval_return_pct) else pd.NA
    export_df.columns = [
        "日期",
        f"{index_name}_收盘价",
        f"{index_name}_MA20",
        f"{index_name}_偏离率(%)",
        f"{index_name}_状态转变时间",
        f"{index_name}_区间涨幅(%)",
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


def append_eastmoney_quote_row(df: pd.DataFrame, secid: str, replace_same_day: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    latest_history_date = normalized["trade_date"].max().date()

    try:
        import requests

        quote = None
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
        for trust_env in (False, True):
            session = requests.Session()
            session.trust_env = trust_env
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
            if quote:
                break
        if not quote:
            return normalized
        latest_price = pd.to_numeric(quote.get("f43"), errors="coerce")
        quote_timestamp = pd.to_numeric(quote.get("f86"), errors="coerce")
        if pd.isna(latest_price) or pd.isna(quote_timestamp):
            return normalized
        quote_timestamp_value = float(quote_timestamp)
        if quote_timestamp_value > 10_000_000_000:
            quote_timestamp_value = quote_timestamp_value / 1000
        quote_date = datetime.fromtimestamp(quote_timestamp_value, tz=ZoneInfo("Asia/Shanghai")).date()
        if quote_date < latest_history_date:
            return normalized
        if quote_date == latest_history_date:
            if not replace_same_day:
                return normalized
            normalized.loc[normalized["trade_date"].dt.date == quote_date, "close"] = float(latest_price)
            return normalized
        supplement = pd.DataFrame([{"trade_date": pd.Timestamp(quote_date), "close": float(latest_price)}])
        return pd.concat([normalized, supplement], ignore_index=True)
    except Exception:
        return normalized


def append_eastmoney_latest_index_row(
    ak,
    df: pd.DataFrame,
    secid: str,
    board_symbol: str | None = None,
    hk_em_symbol: str | None = None,
) -> pd.DataFrame:
    """Append a same-day EastMoney spot quote when daily history is delayed."""
    normalized = append_eastmoney_quote_row(df, secid)
    if normalized is None or normalized.empty:
        return normalized

    market_timezone = "Asia/Hong_Kong" if hk_em_symbol else "Asia/Shanghai"
    today = datetime.now(ZoneInfo(market_timezone)).date()
    latest_history_date = pd.to_datetime(normalized["trade_date"], errors="coerce").max().date()
    if latest_history_date >= today:
        return normalized

    latest_price = None
    try:
        if board_symbol:
            spot_df = ak.stock_board_concept_spot_em(symbol=board_symbol)
            matched = spot_df[spot_df["item"].astype(str) == "最新"]
            if not matched.empty:
                latest_price = matched.iloc[0].get("value")
        elif hk_em_symbol:
            spot_df = ak.stock_hk_index_spot_em()
            matched = spot_df[spot_df["代码"].astype(str).str.upper() == hk_em_symbol.upper()]
            if not matched.empty:
                latest_price = matched.iloc[0].get("最新价")
    except Exception:
        return normalized

    latest_price = pd.to_numeric(latest_price, errors="coerce")
    if pd.isna(latest_price):
        return normalized
    supplement = pd.DataFrame([{"trade_date": pd.Timestamp(today), "close": float(latest_price)}])
    return pd.concat([normalized, supplement], ignore_index=True)


def append_hk_index_spot_row(
    ak,
    df: pd.DataFrame,
    index_code: str,
    eastmoney_quote_secid: str | None = None,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    if eastmoney_quote_secid:
        quoted = append_eastmoney_quote_row(normalized, eastmoney_quote_secid, replace_same_day=True)
        if quoted is not None and not quoted.empty:
            normalized = quoted

    latest_history_date = normalized["trade_date"].max().date()
    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
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
    today = datetime.now().date()
    if latest_history_date >= today:
        return normalized

    try:
        spot_df = ak.futures_zh_spot(symbol=index_code.upper(), market="CF", adjust="0")
        if spot_df is None or spot_df.empty:
            return normalized
        price_col = "current_price" if "current_price" in spot_df.columns else "last_close"
        if price_col not in spot_df.columns and "最新价" in spot_df.columns:
            price_col = "最新价"
        if price_col not in spot_df.columns and "price" in spot_df.columns:
            price_col = "price"
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

    start_date = (datetime.now() - timedelta(days=max(days * 2, 365))).strftime("%Y%m%d")
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
                shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai"))
                latest_date = pd.to_datetime(df["trade_date"]).max().date()
                if (
                    shanghai_now.weekday() < 5
                    and shanghai_now.time() >= time(11, 30)
                    and latest_date < shanghai_now.date()
                ):
                    raise RuntimeError("东方财富实时报价未返回今日数据，保留缓存等待重试")
            return build_export_df(df, index_name, days=days)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"{index_name} AkShare 获取失败：{last_error}")


def get_index_data_from_akshare_cn(
    index_code: str,
    market: str,
    index_name: str,
    days: int = 30,
    eastmoney_quote_secid: str | None = None,
):
    import akshare as ak

    start_date = (datetime.now() - timedelta(days=max(days * 2, 365))).strftime("%Y%m%d")
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
            df = append_akshare_latest_index_row(ak, df, index_code)
            if eastmoney_quote_secid:
                df = append_eastmoney_quote_row(df, eastmoney_quote_secid)
            return build_export_df(df, index_name, days=days)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"{index_name} AkShare 获取失败：{last_error}")


def extract_raw_from_export_df(export_df: pd.DataFrame, index_name: str) -> pd.DataFrame | None:
    if export_df is None or export_df.empty:
        return None
    close_col = f"{index_name}_收盘价"
    if "日期" not in export_df.columns or close_col not in export_df.columns:
        return None
    raw_df = export_df[["日期", close_col]].rename(columns={"日期": "trade_date", close_col: "close"}).copy()
    raw_df["trade_date"] = pd.to_datetime(raw_df["trade_date"], errors="coerce")
    raw_df["close"] = pd.to_numeric(raw_df["close"], errors="coerce")
    raw_df = raw_df.dropna(subset=["trade_date", "close"])
    if raw_df.empty:
        return None
    return raw_df


def merge_newer_index_rows(df: pd.DataFrame, newer_df: pd.DataFrame | None) -> pd.DataFrame:
    if newer_df is None or newer_df.empty:
        return df
    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
    newer = newer_df.copy()
    newer["trade_date"] = pd.to_datetime(newer["trade_date"], errors="coerce")
    latest_history_date = normalized["trade_date"].max()
    newer_rows = newer[newer["trade_date"] > latest_history_date]
    if newer_rows.empty:
        return normalized
    return pd.concat([normalized, newer_rows], ignore_index=True)


def get_index_data_from_akshare_us(
    index_code: str,
    index_name: str,
    days: int = 30,
    yahoo_symbol: str | None = None,
):
    import akshare as ak

    try:
        raw_df = ak.index_us_stock_sina(symbol=index_code)
        df = normalize_akshare_index_df(raw_df)
        if yahoo_symbol:
            yahoo_df = get_index_data_from_yahoo(yahoo_symbol, index_name, days=min(max(days, 60), 365))
            df = merge_newer_index_rows(df, extract_raw_from_export_df(yahoo_df, index_name))
        return build_export_df(df, index_name, days=days)
    except Exception:
        if yahoo_symbol:
            yahoo_df = get_index_data_from_yahoo(yahoo_symbol, index_name, days=days)
            if yahoo_df is not None and not yahoo_df.empty:
                return yahoo_df
        raise


def get_index_data_from_akshare_hk(
    index_code: str,
    index_name: str,
    days: int = 30,
    eastmoney_quote_secid: str | None = None,
):
    import akshare as ak

    raw_df = ak.stock_hk_index_daily_sina(symbol=index_code)
    df = normalize_akshare_index_df(raw_df)
    if index_code.upper() == "HSTECH":
        df = append_hk_index_spot_row(
            ak,
            df,
            index_code,
            eastmoney_quote_secid=eastmoney_quote_secid,
        )
    return build_export_df(df, index_name, days=days)


def get_index_data_from_yahoo(symbol: str, index_name: str, days: int = 30):
    import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
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
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()
    payload = response.json()
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
    return build_export_df(pd.DataFrame(rows), index_name, days=days)


def is_sparse_daily_history(df: pd.DataFrame) -> bool:
    if df is None or df.empty or "trade_date" not in df.columns:
        return True
    dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna().sort_values()
    if len(dates) < 20:
        return True
    gaps = dates.diff().dt.days.dropna()
    if gaps.empty:
        return True
    return bool(gaps.median() > 7 or (gaps > 10).sum() > len(gaps) * 0.3)


def get_index_data_from_eastmoney_kline(
    secid: str,
    index_name: str,
    days: int = 30,
    fqt: str = "0",
    akshare_board_symbol: str | None = None,
    akshare_hk_em_symbol: str | None = None,
) -> pd.DataFrame | None:
    import requests

    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": fqt,
        "end": "20500101",
        "lmt": str(max(days * 3, 120)),
    }
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }
    last_error = None
    payload = None
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        for host in (
            "push2his.eastmoney.com",
            "91.push2his.eastmoney.com",
            "45.push2his.eastmoney.com",
            "7.push2his.eastmoney.com",
        ):
            try:
                response = session.get(
                    f"https://{host}/api/qt/stock/kline/get",
                    params=params,
                    headers=headers,
                    timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("data"):
                    break
            except Exception as exc:
                last_error = exc
        if payload is not None and payload.get("data"):
            break
    if payload is None:
        return get_index_data_from_akshare_eastmoney_fallback(
            secid,
            index_name,
            days=days,
            fqt=fqt,
            board_symbol=akshare_board_symbol,
            hk_em_symbol=akshare_hk_em_symbol,
            last_error=last_error,
        )
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    rows = []
    for item in klines:
        fields = str(item).split(",")
        if len(fields) < 3:
            continue
        rows.append(
            {
                "trade_date": fields[0],
                "close": fields[2],
            }
        )
    if not rows:
        return get_index_data_from_akshare_eastmoney_fallback(
            secid,
            index_name,
            days=days,
            fqt=fqt,
            board_symbol=akshare_board_symbol,
            hk_em_symbol=akshare_hk_em_symbol,
            last_error=last_error,
        )
    df = normalize_akshare_index_df(pd.DataFrame(rows))
    import akshare as ak

    df = append_eastmoney_latest_index_row(
        ak,
        df,
        secid,
        board_symbol=akshare_board_symbol,
        hk_em_symbol=akshare_hk_em_symbol,
    )
    return build_export_df(df, index_name, days=days)


def get_index_data_from_akshare_eastmoney_fallback(
    secid: str,
    index_name: str,
    days: int,
    fqt: str,
    board_symbol: str | None,
    hk_em_symbol: str | None,
    last_error: Exception | None,
) -> pd.DataFrame | None:
    import akshare as ak

    start_date = (datetime.now() - timedelta(days=max(days * 2, 365))).strftime("%Y%m%d")
    end_date = "20500101"
    try:
        if board_symbol:
            adjust = {"0": "", "1": "qfq", "2": "hfq"}.get(str(fqt), "")
            raw_df = ak.stock_board_concept_hist_em(
                symbol=board_symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            df = normalize_akshare_index_df(raw_df)
            df = append_eastmoney_latest_index_row(
                ak,
                df,
                secid,
                board_symbol=board_symbol,
                hk_em_symbol=hk_em_symbol,
            )
            return build_export_df(df, index_name, days=days)
        if hk_em_symbol:
            raw_df = ak.stock_hk_index_daily_em(symbol=hk_em_symbol)
            df = normalize_akshare_index_df(raw_df)
            df = append_eastmoney_latest_index_row(
                ak,
                df,
                secid,
                board_symbol=board_symbol,
                hk_em_symbol=hk_em_symbol,
            )
            return build_export_df(df, index_name, days=days)
    except Exception as exc:
        raise RuntimeError(f"东方财富K线失败：{last_error}；AkShare兜底失败：{exc}") from exc

    raise RuntimeError(f"东方财富K线获取失败：{last_error}")


def get_index_data_from_akshare_global(index_code: str, index_name: str, days: int = 30, yahoo_symbol: str | None = None):
    import akshare as ak

    try:
        raw_df = ak.index_global_hist_em(symbol=index_code)
        df = normalize_akshare_index_df(raw_df)
        latest_history_date = pd.to_datetime(df["trade_date"], errors="coerce").max().date()
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if yahoo_symbol and (is_sparse_daily_history(df) or latest_history_date < today):
            yahoo_df = get_index_data_from_yahoo(yahoo_symbol, index_name, days=days)
            if yahoo_df is not None and not yahoo_df.empty:
                if is_sparse_daily_history(df):
                    return yahoo_df
                close_col = f"{index_name}_收盘价"
                yahoo_raw = yahoo_df[["日期", close_col]].rename(
                    columns={"日期": "trade_date", close_col: "close"}
                )
                yahoo_raw["trade_date"] = pd.to_datetime(yahoo_raw["trade_date"], errors="coerce")
                yahoo_latest_date = yahoo_raw["trade_date"].max().date()
                if yahoo_latest_date > latest_history_date:
                    df = pd.concat(
                        [df, yahoo_raw[yahoo_raw["trade_date"] > pd.Timestamp(latest_history_date)]],
                        ignore_index=True,
                    )
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
    df = append_futures_spot_row(ak, df, index_code)
    return build_export_df(df, index_name, days=days)


def fetch_index_from_source(index_name: str, index_config: dict, days: int = 30) -> pd.DataFrame | None:
    source = index_config.get("source")
    code = index_config.get("code")

    if source == "akshare_cn":
        return get_index_data_from_akshare_cn(
            code,
            index_config.get("market", "sh"),
            index_name,
            days=days,
            eastmoney_quote_secid=index_config.get("eastmoney_quote_secid"),
        )
    if source == "akshare_csindex":
        return get_index_data_from_akshare_csindex(code, index_name, days=days)
    if source == "akshare_us":
        return get_index_data_from_akshare_us(
            code,
            index_name,
            days=days,
            yahoo_symbol=index_config.get("yahoo_symbol"),
        )
    if source == "akshare_hk":
        return get_index_data_from_akshare_hk(
            code,
            index_name,
            days=days,
            eastmoney_quote_secid=index_config.get("eastmoney_quote_secid"),
        )
    if source == "akshare_global":
        return get_index_data_from_akshare_global(
            code,
            index_name,
            days=days,
            yahoo_symbol=index_config.get("yahoo_symbol"),
        )
    if source == "akshare_futures_main":
        return get_index_data_from_akshare_futures_main(code, index_name, days=days)
    if source == "eastmoney_kline":
        return get_index_data_from_eastmoney_kline(
            code,
            index_name,
            days=days,
            fqt=str(index_config.get("fqt", "0")),
            akshare_board_symbol=index_config.get("akshare_board_symbol"),
            akshare_hk_em_symbol=index_config.get("akshare_hk_em_symbol"),
        )
    if source == "yahoo":
        return get_index_data_from_yahoo(code, index_name, days=days)
    raise ValueError(f"未知数据源：{source}")


def fetch_index_history(index_name: str, index_config, days: int = 10000) -> pd.DataFrame | None:
    if not isinstance(index_config, dict):
        return None

    tickflow_symbol = index_config.get("tickflow_symbol")
    if tickflow_symbol:
        try:
            df = get_index_data_from_tickflow("", tickflow_symbol, index_name, days=days)
            if df is not None and not df.empty:
                eastmoney_quote_secid = index_config.get("eastmoney_quote_secid")
                if eastmoney_quote_secid:
                    close_col = f"{index_name}_收盘价"
                    raw_df = df[["日期", close_col]].rename(
                        columns={"日期": "trade_date", close_col: "close"}
                    )
                    raw_df = append_eastmoney_quote_row(raw_df, eastmoney_quote_secid)
                    return build_export_df(raw_df, index_name, days=days)
                return df
        except Exception:
            pass

    return fetch_index_from_source(index_name, index_config, days=days)


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
                    eastmoney_quote_secid = index_config.get("eastmoney_quote_secid")
                    if eastmoney_quote_secid:
                        close_col = f"{index_name}_收盘价"
                        raw_df = df[["日期", close_col]].rename(
                            columns={"日期": "trade_date", close_col: "close"}
                        )
                        raw_df = append_eastmoney_quote_row(raw_df, eastmoney_quote_secid)
                        return build_export_df(raw_df, index_name, days=days)
                    return df
            except Exception as exc:
                tickflow_error = exc

        try:
            return fetch_index_from_source(index_name, index_config, days=days)
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
        valid_rows = report_df.dropna(subset=[close_col, ma20_col])
        if valid_rows.empty:
            continue
        latest = valid_rows.iloc[-1]
        show_deviation = index_config.get("show_ma20_deviation", True)
        transition_col = f"{index_name}_状态转变时间"
        interval_col = f"{index_name}_区间涨幅(%)"
        transition_date, interval_return_pct = (pd.NA, pd.NA)
        if show_deviation:
            if transition_col in latest and interval_col in latest and not pd.isna(latest[transition_col]):
                transition_date = latest[transition_col]
                interval_return_pct = latest[interval_col]
            else:
                transition_date, interval_return_pct = calculate_ma20_transition(
                    valid_rows,
                    close_col,
                    ma20_col,
                    date_col="日期",
                )
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
                "偏离率(%)": latest[deviation_col] if show_deviation and deviation_col in latest else pd.NA,
                "状态转变时间": transition_date,
                "区间涨幅(%)": interval_return_pct,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).reset_index(drop=True)


def calculate_ma20_transition(
    valid_rows: pd.DataFrame,
    close_col: str,
    ma20_col: str,
    date_col: str = "日期",
) -> tuple[object, object]:
    if len(valid_rows) < 2 or date_col not in valid_rows.columns:
        return pd.NA, pd.NA

    data = valid_rows[[date_col, close_col, ma20_col]].copy()
    data[close_col] = pd.to_numeric(data[close_col], errors="coerce")
    data[ma20_col] = pd.to_numeric(data[ma20_col], errors="coerce")
    data = data.dropna(subset=[close_col, ma20_col]).reset_index(drop=True)
    if len(data) < 2:
        return pd.NA, pd.NA

    latest = data.iloc[-1]
    latest_close = latest[close_col]
    latest_above = latest_close >= latest[ma20_col]

    for idx in range(len(data) - 1, 0, -1):
        current = data.iloc[idx]
        previous = data.iloc[idx - 1]
        current_above = current[close_col] >= current[ma20_col]
        previous_above = previous[close_col] >= previous[ma20_col]
        if current_above == latest_above and previous_above != current_above:
            transition_close = current[close_col]
            if pd.isna(transition_close) or transition_close == 0:
                return pd.NA, pd.NA
            transition_date = pd.Timestamp(current[date_col]).strftime("%Y-%m-%d")
            interval_return = (latest_close / transition_close - 1) * 100
            return transition_date, interval_return
    return pd.NA, pd.NA
