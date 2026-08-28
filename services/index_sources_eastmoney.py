from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_config import (
    CFFEX_FUTURES_MAIN_PRODUCTS, INDEX_CONFIG, INDEX_REPORT_DISPLAY_DAYS,
    YAHOO_CHART_HOSTS, YAHOO_REQUEST_GATE,
)
from services.index_frames import (
    build_export_df, extract_raw_from_export_df, filter_completed_market_dates,
    filter_market_trading_dates, is_sparse_daily_history, merge_newer_index_rows,
    merge_raw_index_data, normalize_akshare_index_df,
)
from services.market_calendar import (
    expected_latest_trade_date, get_market_window, is_market_holiday,
    is_market_trading_day, latest_completed_trade_date, latest_settled_trade_date,
)
from services.index_sources_sina import (
    fetch_hsi_official_completed_close,
    get_sina_hk_index_history,
)
from services.index_sources_mx import get_mx_index_history

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
        market_name = "港股" if str(secid).startswith(("124.", "125.", "305.")) else "A股"
        quote_frame = pd.DataFrame([{"trade_date": pd.Timestamp(quote_date), "close": float(latest_price)}])
        filtered_quote = filter_market_trading_dates(quote_frame, market_name)
        if filtered_quote is None or filtered_quote.empty:
            return normalized
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

def fetch_eastmoney_clist_latest_index_row(
    *,
    board_symbol: str | None = None,
    hk_em_symbol: str | None = None,
) -> pd.DataFrame | None:
    """Fetch a latest EastMoney list quote when stock/get or kline hosts fail."""
    target_symbol = (board_symbol or hk_em_symbol or "").strip().upper()
    if not target_symbol:
        return None

    if board_symbol:
        fs = "m:90 t:3 f:!50"
        market_timezone = "Asia/Shanghai"
        market_name = "A股"
    elif hk_em_symbol:
        fs = "m:124,m:125,m:305"
        market_timezone = "Asia/Hong_Kong"
        market_name = "港股"
    else:
        return None

    try:
        import requests

        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        hosts = (
            "push2delay.eastmoney.com",
            "push2.eastmoney.com",
            "36.push2.eastmoney.com",
            "48.push2.eastmoney.com",
        )
        fields = "f12,f13,f14,f2,f3,f4,f5,f6,f20,f21,f124,f152"
        for trust_env in (False, True):
            session = requests.Session()
            session.trust_env = trust_env
            for host in hosts:
                for page in range(1, 10):
                    params = {
                        "pn": page,
                        "pz": 100,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": "f3",
                        "fs": fs,
                        "fields": fields,
                    }
                    try:
                        response = session.get(
                            f"https://{host}/api/qt/clist/get",
                            params=params,
                            headers=headers,
                            timeout=8,
                        )
                        response.raise_for_status()
                        payload = response.json()
                    except Exception:
                        break

                    rows = (payload.get("data") or {}).get("diff") or []
                    if not rows:
                        break
                    for row in rows:
                        if str(row.get("f12", "")).strip().upper() != target_symbol:
                            continue
                        latest_price = pd.to_numeric(row.get("f2"), errors="coerce")
                        if pd.isna(latest_price):
                            return None
                        quote_timestamp = pd.to_numeric(row.get("f124"), errors="coerce")
                        if pd.isna(quote_timestamp):
                            return None
                        timestamp_value = float(quote_timestamp)
                        if timestamp_value > 10_000_000_000:
                            timestamp_value = timestamp_value / 1000
                        quote_date = datetime.fromtimestamp(
                            timestamp_value,
                            tz=ZoneInfo(market_timezone),
                        ).date()
                        market = get_market_window(market_name)
                        quote_noon = datetime.combine(
                            quote_date,
                            time(12, 0),
                            tzinfo=ZoneInfo(market_timezone),
                        )
                        if market is not None and not is_market_trading_day(market, quote_noon):
                            return None
                        return pd.DataFrame(
                            [{"trade_date": pd.Timestamp(quote_date), "close": float(latest_price)}]
                        )
        return None
    except Exception:
        return None

def append_eastmoney_clist_latest_index_row(
    df: pd.DataFrame,
    *,
    board_symbol: str | None = None,
    hk_em_symbol: str | None = None,
) -> pd.DataFrame:
    latest_row = fetch_eastmoney_clist_latest_index_row(
        board_symbol=board_symbol,
        hk_em_symbol=hk_em_symbol,
    )
    if latest_row is None or latest_row.empty:
        return df
    return merge_raw_index_data(df, latest_row)

def append_eastmoney_latest_index_row(
    ak,
    df: pd.DataFrame,
    secid: str,
    board_symbol: str | None = None,
    hk_em_symbol: str | None = None,
) -> pd.DataFrame:
    """Append a same-day EastMoney spot quote when daily history is delayed."""
    market_name = "港股" if hk_em_symbol else "A股"
    market = get_market_window(market_name)
    market_timezone = market.timezone if market is not None else ("Asia/Hong_Kong" if hk_em_symbol else "Asia/Shanghai")
    market_now = datetime.now(ZoneInfo(market_timezone))
    expected_date = expected_latest_trade_date(market, market_now) if market is not None else market_now.date()

    normalized = append_eastmoney_quote_row(df, secid)
    if normalized is None or normalized.empty:
        return normalized
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
    valid_market_date = normalized["trade_date"].dt.date.map(
        lambda day: market is None or (day.weekday() < 5 and not is_market_holiday(market, day))
    )
    normalized = normalized[
        normalized["trade_date"].notna()
        & (normalized["trade_date"].dt.date <= expected_date)
        & valid_market_date
    ].reset_index(drop=True)
    if normalized.empty:
        return normalized

    latest_history_date = pd.to_datetime(normalized["trade_date"], errors="coerce").max().date()
    if latest_history_date >= expected_date:
        return normalized

    normalized = append_eastmoney_clist_latest_index_row(
        normalized,
        board_symbol=board_symbol,
        hk_em_symbol=hk_em_symbol,
    )
    latest_history_date = pd.to_datetime(normalized["trade_date"], errors="coerce").max().date()
    if latest_history_date >= expected_date or ak is None:
        return normalized

    latest_price = None
    try:
        if board_symbol:
            spot_df = ak.stock_board_concept_spot_em(symbol=board_symbol)
            matched = spot_df[spot_df["item"].astype(str) == "\u6700\u65b0"]
            if not matched.empty:
                latest_price = matched.iloc[0].get("value")
        elif hk_em_symbol:
            spot_df = ak.stock_hk_index_spot_em()
            matched = spot_df[spot_df["\u4ee3\u7801"].astype(str).str.upper() == hk_em_symbol.upper()]
            if not matched.empty:
                latest_price = matched.iloc[0].get("\u6700\u65b0\u4ef7")
    except Exception:
        return normalized

    latest_price = pd.to_numeric(latest_price, errors="coerce")
    if pd.isna(latest_price):
        return normalized
    supplement = pd.DataFrame([{"trade_date": pd.Timestamp(expected_date), "close": float(latest_price)}])
    return pd.concat([normalized, supplement], ignore_index=True)

def get_index_data_from_eastmoney_kline(
    secid: str,
    index_name: str,
    days: int = 30,
    fqt: str = "0",
    akshare_board_symbol: str | None = None,
    akshare_hk_em_symbol: str | None = None,
    sina_hk_symbol: str | None = None,
    hsi_official_series: str | None = None,
    mx_query_name: str | None = None,
    mx_expected_code: str | None = None,
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
            sina_hk_symbol=sina_hk_symbol,
            hsi_official_series=hsi_official_series,
            mx_query_name=mx_query_name,
            mx_expected_code=mx_expected_code,
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
            sina_hk_symbol=sina_hk_symbol,
            hsi_official_series=hsi_official_series,
            mx_query_name=mx_query_name,
            mx_expected_code=mx_expected_code,
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
    df = _supplement_hk_independent_rows(
        df,
        sina_hk_symbol=sina_hk_symbol,
        hsi_official_series=hsi_official_series,
        mx_query_name=mx_query_name,
        mx_expected_code=mx_expected_code,
        days=days,
    )
    return build_export_df(df, index_name, days=days)


def _supplement_hk_independent_rows(
    df: pd.DataFrame,
    *,
    sina_hk_symbol: str | None,
    hsi_official_series: str | None,
    mx_query_name: str | None = None,
    mx_expected_code: str | None = None,
    days: int = 30,
) -> pd.DataFrame:
    normalized = normalize_akshare_index_df(df)
    normalized = filter_completed_market_dates(normalized, "港股")
    market = get_market_window("港股")
    market_now = datetime.now(ZoneInfo(market.timezone)) if market is not None else None
    target_date = (
        latest_settled_trade_date(market, market_now) if market is not None else None
    )

    def needs_target() -> bool:
        dates = pd.to_datetime(normalized["trade_date"], errors="coerce").dropna()
        return target_date is not None and (dates.empty or dates.max().date() < target_date)

    if sina_hk_symbol and needs_target():
        try:
            sina_raw = get_sina_hk_index_history(sina_hk_symbol, now=market_now)
            normalized = merge_newer_index_rows(normalized, sina_raw)
        except Exception:
            pass
    if mx_query_name and mx_expected_code and needs_target():
        try:
            mx_raw = get_mx_index_history(
                mx_query_name,
                mx_expected_code,
                "港股",
                days=days,
                now=market_now,
            )
            normalized = merge_newer_index_rows(normalized, mx_raw)
        except Exception:
            pass
    if hsi_official_series and needs_target():
        try:
            official_row = fetch_hsi_official_completed_close(
                hsi_official_series,
                now=market_now,
            )
            normalized = merge_newer_index_rows(normalized, official_row)
        except Exception:
            pass
    return normalized

def get_index_data_from_akshare_eastmoney_fallback(
    secid: str,
    index_name: str,
    days: int,
    fqt: str,
    board_symbol: str | None,
    hk_em_symbol: str | None,
    last_error: Exception | None,
    sina_hk_symbol: str | None = None,
    hsi_official_series: str | None = None,
    mx_query_name: str | None = None,
    mx_expected_code: str | None = None,
) -> pd.DataFrame | None:
    import akshare as ak

    start_date = (datetime.now() - timedelta(days=max(days * 2, 365))).strftime("%Y%m%d")
    end_date = "20500101"
    independent_errors: list[str] = []
    independent_raw: pd.DataFrame | None = None
    if sina_hk_symbol:
        try:
            sina_raw = get_sina_hk_index_history(sina_hk_symbol)
            if sina_raw is not None and not sina_raw.empty:
                independent_raw = sina_raw
        except Exception as exc:
            independent_errors.append(f"新浪日线失败：{exc}")
    required_rows = min(max(int(days), 30), 252)
    if mx_query_name and mx_expected_code and (
        independent_raw is None or len(independent_raw) < required_rows
    ):
        try:
            mx_raw = get_mx_index_history(
                mx_query_name,
                mx_expected_code,
                "港股",
                days=days,
            )
            independent_raw = merge_raw_index_data(mx_raw, independent_raw) if independent_raw is not None else mx_raw
        except Exception as exc:
            independent_errors.append(f"妙想日线失败：{exc}")
    if hsi_official_series:
        try:
            official_row = fetch_hsi_official_completed_close(hsi_official_series)
            if official_row is not None and not official_row.empty:
                independent_raw = merge_raw_index_data(independent_raw, official_row)
        except Exception as exc:
            independent_errors.append(f"恒生官网失败：{exc}")
    if independent_raw is not None and not independent_raw.empty:
        return build_export_df(independent_raw, index_name, days=days)
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
        independent_text = "；".join(independent_errors)
        if independent_text:
            independent_text = f"；{independent_text}"
        raise RuntimeError(
            f"东方财富K线失败：{last_error}{independent_text}；AkShare东方财富兜底失败：{exc}"
        ) from exc

    raise RuntimeError(f"东方财富K线获取失败：{last_error}")

def fetch_eastmoney_completed_global_row(
    secid: str,
    market_name: str,
    *,
    now: datetime | None = None,
) -> pd.DataFrame | None:
    """Return one EastMoney quote only after it represents a settled close."""
    import requests

    market = get_market_window(market_name)
    if market is None:
        return None
    market_now = now.astimezone(ZoneInfo(market.timezone)) if now else datetime.now(ZoneInfo(market.timezone))
    target_date = latest_settled_trade_date(market, market_now)
    close_at = datetime.combine(
        target_date,
        market.sessions[-1][1],
        tzinfo=ZoneInfo(market.timezone),
    )
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
        try:
            for host in ("push2.eastmoney.com", "36.push2.eastmoney.com", "push2delay.eastmoney.com"):
                try:
                    response = session.get(
                        f"https://{host}/api/qt/stock/get",
                        params=params,
                        headers=headers,
                        timeout=3,
                    )
                    response.raise_for_status()
                    quote = response.json().get("data") or {}
                except Exception:
                    continue
                price = pd.to_numeric(quote.get("f43"), errors="coerce")
                timestamp = pd.to_numeric(quote.get("f86"), errors="coerce")
                if pd.isna(price) or pd.isna(timestamp):
                    continue
                timestamp_value = float(timestamp)
                if timestamp_value > 10_000_000_000:
                    timestamp_value /= 1000
                quote_time = datetime.fromtimestamp(timestamp_value, tz=timezone.utc).astimezone(
                    ZoneInfo(market.timezone)
                )
                if quote_time.date() != target_date or quote_time < close_at - timedelta(minutes=1):
                    continue
                return pd.DataFrame(
                    [{"trade_date": pd.Timestamp(target_date), "close": float(price)}]
                )
        finally:
            session.close()
    return None

__all__ = ['append_eastmoney_quote_row', 'fetch_eastmoney_clist_latest_index_row', 'append_eastmoney_clist_latest_index_row', 'append_eastmoney_latest_index_row', 'get_index_data_from_eastmoney_kline', 'get_index_data_from_akshare_eastmoney_fallback', 'fetch_eastmoney_completed_global_row']
