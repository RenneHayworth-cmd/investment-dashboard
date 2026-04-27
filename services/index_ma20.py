from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pandas as pd


TICKFLOW_SITE_PACKAGES = "/home/renne/.hermes/python/.venv/lib/python3.11/site-packages"
if TICKFLOW_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, TICKFLOW_SITE_PACKAGES)


INDEX_CONFIG = {
    "沪深300": {"source": "akshare_cn", "code": "000300", "market": "sh"},
    "创业板指": {"source": "akshare_cn", "code": "399006", "market": "sz"},
    "中证500": {"source": "akshare_cn", "code": "000905", "market": "sh"},
    "中证1000": {"source": "akshare_cn", "code": "000852", "market": "sh"},
    "中证红利低波": {"source": "akshare_csindex", "code": "H30269"},
    "国证自由现金流": {"source": "akshare_cn", "code": "980092", "market": "sz"},
    "标普500": {"source": "akshare_us", "code": ".INX"},
    "纳斯达克综合": {"source": "akshare_us", "code": ".IXIC"},
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
        elif column_text in {"收盘", "收盘价", "close", "Close"}:
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
            df = append_akshare_latest_index_row(ak, df, index_code)
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


def get_index_data_from_tickflow(api_key: str, index_code: str, index_name: str, days: int = 30):
    from tickflow import TickFlow

    client = TickFlow(api_key=api_key)
    df = client.klines.get(index_code, as_dataframe=True)
    return build_export_df(df, index_name, days=days)


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
        source = index_config.get("source")
        code = index_config.get("code")
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
        raise ValueError(f"未知数据源：{source}")

    return get_index_data_from_tickflow(api_key, index_config, index_name, days=days)


def build_summary(report_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for index_name in INDEX_CONFIG:
        close_col = f"{index_name}_收盘价"
        ma20_col = f"{index_name}_MA20"
        deviation_col = f"{index_name}_偏离率(%)"
        if close_col not in report_df.columns:
            continue
        valid_rows = report_df.dropna(subset=[close_col, ma20_col, deviation_col])
        if valid_rows.empty:
            continue
        latest = valid_rows.iloc[-1]
        rows.append(
            {
                "指数": index_name,
                "日期": latest["日期"],
                "收盘价": latest[close_col],
                "MA20": latest[ma20_col],
                "偏离率(%)": latest[deviation_col],
            }
        )
    return pd.DataFrame(rows)
