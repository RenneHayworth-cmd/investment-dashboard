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

from services.index_sources_eastmoney import (
    append_eastmoney_quote_row, fetch_eastmoney_completed_global_row,
)
from services.index_sources_yahoo import (
    fetch_yahoo_latest_index_row, get_index_data_from_yahoo,
)

def append_akshare_latest_index_row(ak, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    market = get_market_window("A股")
    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    expected_date = expected_latest_trade_date(market, market_now) if market is not None else market_now.date()
    normalized = filter_market_trading_dates(normalized, "A股")
    if normalized is None or normalized.empty:
        return normalized
    latest_history_date = normalized["trade_date"].max().date()
    if latest_history_date >= expected_date:
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
        supplement = pd.DataFrame([{"trade_date": pd.Timestamp(expected_date), "close": float(latest_price)}])
        return pd.concat([normalized, supplement], ignore_index=True)

    yahoo_df = fetch_yahoo_latest_index_row(f"{index_code}.SS")
    if yahoo_df is not None and not yahoo_df.empty:
        yahoo_date = pd.to_datetime(yahoo_df.iloc[0]["trade_date"]).date()
        if yahoo_date > latest_history_date:
            return pd.concat([normalized, yahoo_df], ignore_index=True)

    return normalized

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

    normalized = filter_market_trading_dates(normalized, "港股")
    if normalized is None or normalized.empty:
        return normalized
    market = get_market_window("港股")
    market_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    expected_date = expected_latest_trade_date(market, market_now) if market is not None else market_now.date()
    latest_history_date = normalized["trade_date"].max().date()
    if latest_history_date >= expected_date:
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
        supplement = pd.DataFrame([{"trade_date": pd.Timestamp(expected_date), "close": float(latest_price)}])
        return pd.concat([normalized, supplement], ignore_index=True)
    except Exception:
        return normalized

def append_futures_spot_row(ak, df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = filter_market_trading_dates(normalized, "A股")
    if normalized is None or normalized.empty:
        return normalized
    market = get_market_window("A股")
    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    expected_date = expected_latest_trade_date(market, market_now) if market is not None else market_now.date()
    latest_history_date = normalized["trade_date"].max().date()
    if latest_history_date >= expected_date:
        return normalized

    try:
        normalized_code = index_code.upper()
        cffex_product = CFFEX_FUTURES_MAIN_PRODUCTS.get(normalized_code)
        if cffex_product:
            spot_df = ak.futures_zh_realtime(symbol=cffex_product)
            if spot_df is None or spot_df.empty or "symbol" not in spot_df.columns:
                return normalized
            matched = spot_df[
                spot_df["symbol"].astype(str).str.strip().str.upper() == normalized_code
            ]
            if matched.empty:
                return normalized
            latest = matched.iloc[0]
            quote_date = pd.to_datetime(latest.get("tradedate"), errors="coerce")
            quote_time = pd.to_datetime(latest.get("ticktime"), errors="coerce")
            if (
                pd.isna(quote_date)
                or quote_date.date() != expected_date
                or pd.isna(quote_time)
                or quote_time.time() < time(15, 0)
            ):
                return normalized
            latest_price = pd.to_numeric(
                latest.get("close", latest.get("trade")),
                errors="coerce",
            )
            if pd.isna(latest_price):
                return normalized
            supplement = pd.DataFrame(
                [{"trade_date": pd.Timestamp(expected_date), "close": float(latest_price)}]
            )
            return pd.concat([normalized, supplement], ignore_index=True)

        spot_df = ak.futures_zh_spot(symbol=normalized_code, market="CF", adjust="0")
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
        supplement = pd.DataFrame([{"trade_date": pd.Timestamp(expected_date), "close": float(latest_price)}])
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

def get_index_data_from_akshare_cni(index_code: str, index_name: str, days: int = 30):
    import akshare as ak

    lookback_days = max(int(days) + 30, 120)
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    raw_df = ak.index_hist_cni(
        symbol=index_code,
        start_date=start_date,
        end_date=end_date,
    )
    df = normalize_akshare_index_df(raw_df)
    return build_export_df(df, index_name, days=days)

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
            try:
                yahoo_df = get_index_data_from_yahoo(
                    yahoo_symbol,
                    index_name,
                    days=min(max(days, 60), 365),
                )
            except Exception:
                yahoo_df = None
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

def get_index_data_from_cboe_vix(index_name: str, days: int = 30) -> pd.DataFrame:
    """Fetch VIX formal daily closes from CBOE's official history file."""
    from io import StringIO

    import requests

    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    errors: list[str] = []
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        try:
            response = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            response.raise_for_status()
            raw_df = pd.read_csv(StringIO(response.text))
            normalized = normalize_akshare_index_df(raw_df.rename(columns={"DATE": "trade_date", "CLOSE": "close"}))
            normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
            normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
            normalized = normalized.dropna(subset=["trade_date", "close"])
            if normalized.empty:
                raise ValueError("CBOE VIX历史文件没有有效日线")
            return build_export_df(normalized, index_name, days=days)
        except Exception as exc:
            errors.append(str(exc).strip() or type(exc).__name__)
        finally:
            session.close()
    raise RuntimeError(f"CBOE VIX日线获取失败：{errors[-1] if errors else '未返回数据'}")

def get_index_data_from_akshare_global(
    index_code: str,
    index_name: str,
    days: int = 30,
    yahoo_symbol: str | None = None,
    eastmoney_quote_secid: str | None = None,
    market_name: str = "",
):
    import akshare as ak

    try:
        raw_df = ak.index_global_hist_em(symbol=index_code)
        df = normalize_akshare_index_df(raw_df)
        latest_history_date = pd.to_datetime(df["trade_date"], errors="coerce").max().date()
        market = get_market_window(market_name)
        market_now = datetime.now(ZoneInfo(market.timezone)) if market is not None else datetime.now(ZoneInfo("Asia/Shanghai"))
        target_date = latest_settled_trade_date(market, market_now) if market is not None else market_now.date()
        if eastmoney_quote_secid and latest_history_date < target_date:
            completed_row = fetch_eastmoney_completed_global_row(
                eastmoney_quote_secid,
                market_name,
                now=market_now,
            )
            df = merge_newer_index_rows(df, completed_row)
            latest_history_date = pd.to_datetime(df["trade_date"], errors="coerce").max().date()
        if yahoo_symbol and (is_sparse_daily_history(df) or latest_history_date < target_date):
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
        completed_row = None
        if eastmoney_quote_secid:
            completed_row = fetch_eastmoney_completed_global_row(
                eastmoney_quote_secid,
                market_name,
            )
        if yahoo_symbol:
            yahoo_df = get_index_data_from_yahoo(yahoo_symbol, index_name, days=days)
            if yahoo_df is not None and not yahoo_df.empty:
                yahoo_raw = extract_raw_from_export_df(yahoo_df, index_name)
                merged = merge_newer_index_rows(yahoo_raw, completed_row)
                return build_export_df(merged, index_name, days=days)
        if completed_row is not None and not completed_row.empty:
            return build_export_df(completed_row, index_name, days=days)
        raise

def get_index_data_from_akshare_futures_main(index_code: str, index_name: str, days: int = 30):
    import akshare as ak

    raw_df = ak.futures_zh_daily_sina(symbol=index_code)
    df = normalize_akshare_index_df(raw_df)
    df = append_futures_spot_row(ak, df, index_code)
    return build_export_df(df, index_name, days=days)

__all__ = ['append_akshare_latest_index_row', 'append_hk_index_spot_row', 'append_futures_spot_row', 'get_index_data_from_akshare_csindex', 'get_index_data_from_akshare_cn', 'get_index_data_from_akshare_cni', 'get_index_data_from_akshare_us', 'get_index_data_from_akshare_hk', 'get_index_data_from_cboe_vix', 'get_index_data_from_akshare_global', 'get_index_data_from_akshare_futures_main']
