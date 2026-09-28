"""Unadjusted A-share history and transient quote adapter for the microcap ledger."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
import math

import pandas as pd
import requests

from core.cache import load_dataset, save_dataset
from services.fund_analysis import infer_tickflow_symbol
from services.market_calendar import get_market_window, latest_settled_trade_date

DATA_TYPE = "microcap_live_close_v1"
TZ = ZoneInfo("Asia/Shanghai")


def _normalize_history(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date", "close"])
    date_col = next((c for c in ("date", "日期", "trade_date") if c in frame.columns), None)
    price_col = next((c for c in ("close", "收盘价", "price") if c in frame.columns), None)
    if date_col is None or price_col is None:
        raise ValueError(f"行情字段无法识别：{list(frame.columns)}")
    result = pd.DataFrame({
        "date": pd.to_datetime(frame[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
        "close": pd.to_numeric(frame[price_col], errors="coerce"),
    })
    result = result.dropna(subset=["date", "close"])
    result = result.loc[result["close"].map(lambda value: math.isfinite(float(value)) and float(value) > 0)]
    result = result.drop_duplicates("date", keep="first")
    return result.sort_values("date").reset_index(drop=True)


def _append_only(old: pd.DataFrame, fresh: pd.DataFrame, target_date: str) -> pd.DataFrame:
    old = _normalize_history(old)
    fresh = _normalize_history(fresh)
    fresh = fresh.loc[fresh["date"] <= target_date]
    merged = pd.concat([old, fresh], ignore_index=True).drop_duplicates("date", keep="first")
    return merged.sort_values("date").reset_index(drop=True)


def _tickflow_history(symbol: str, api_key: str, count: int) -> pd.DataFrame:
    from tickflow import TickFlow
    client = TickFlow(api_key=api_key, timeout=15, max_retries=0) if api_key else TickFlow.free()
    try:
        frame = client.klines.get(
            infer_tickflow_symbol(symbol), period="1d", count=count,
            adjust="none", as_dataframe=True,
        )
    finally:
        client.close()
    return _normalize_history(frame)


def _akshare_history(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak
    try:
        frame = ak.stock_zh_a_hist(
            symbol=symbol, period="daily", start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""), adjust="",
        )
    except requests.exceptions.ProxyError as proxy_error:
        # AkShare's A-share history wrapper uses the same EastMoney endpoint,
        # but requests inherits the system proxy by default. If that proxy
        # drops the connection, try AkShare's Tencent-backed daily history,
        # then retry the EastMoney kline query on alternate routes.
        tencent_error = None
        try:
            code = str(symbol).zfill(6)
            market_prefix = "sh" if code.startswith(("6", "9")) else (
                "bj" if code.startswith(("4", "8")) else "sz"
            )
            tencent_frame = ak.stock_zh_a_hist_tx(
                symbol=f"{market_prefix}{code}",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="",
                timeout=12,
            )
            tencent_history = _normalize_history(tencent_frame)
            if not tencent_history.empty:
                tencent_history.attrs["source"] = "akshare_tx"
                return tencent_history
            tencent_error = ValueError("AkShare腾讯日线未返回有效记录")
        except Exception as exc:
            tencent_error = exc
        try:
            fallback_history = _akshare_history_fallback(symbol, start_date, end_date)
            fallback_history.attrs["source"] = "akshare"
            return fallback_history
        except Exception as route_error:
            raise RuntimeError(
                f"AkShare东方财富主源失败（{proxy_error}）；"
                f"AkShare腾讯日线失败（{tencent_error}）；"
                f"东方财富备用线路失败（{route_error}）"
            ) from route_error
    return _normalize_history(frame)


def _akshare_history_fallback(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Retry AkShare's unadjusted EastMoney kline request on alternate routes."""
    code = str(symbol).zfill(6)
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101",
        "fqt": "0",
        "secid": f"{1 if code.startswith('6') else 0}.{code}",
        "beg": start_date.replace("-", ""),
        "end": end_date.replace("-", ""),
    }
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }
    last_error = None
    hosts = (
        "push2his.eastmoney.com",
        "91.push2his.eastmoney.com",
        "45.push2his.eastmoney.com",
        "7.push2his.eastmoney.com",
    )
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        try:
            for host in hosts:
                try:
                    response = session.get(
                        f"https://{host}/api/qt/stock/kline/get",
                        params=params, headers=headers, timeout=12,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    klines = ((payload.get("data") or {}).get("klines") or [])
                    if klines:
                        rows = []
                        for item in klines:
                            fields = str(item).split(",")
                            if len(fields) >= 3:
                                rows.append({"date": fields[0], "close": fields[2]})
                        if rows:
                            return _normalize_history(pd.DataFrame(rows))
                    last_error = ValueError("东方财富未返回有效日线记录")
                except Exception as exc:
                    last_error = exc
        finally:
            session.close()
    raise RuntimeError(f"东方财富无代理日线请求失败：{last_error}") from last_error


def load_microcap_histories(
    symbols, *, start_date: str | None = None, api_key: str = "",
    allow_fetch: bool = False, market_now: datetime | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Load unadjusted formal closes; append only unseen completed-session rows."""
    current = market_now or datetime.now(TZ)
    target_date = latest_settled_trade_date(get_market_window("A股"), current).isoformat()
    normalized_codes = sorted({str(code).zfill(6) for code in symbols if str(code).strip()})
    histories: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}
    for code in normalized_codes:
        # core.cache keeps legacy filenames keyed by symbol/source/period; namespace
        # the on-disk symbol so this raw A-share history cannot overwrite ETF data.
        cache_symbol = f"microcap_live_{code}"
        tick_cache, _ = load_dataset(cache_symbol, "tickflow", DATA_TYPE, period="1d")
        ak_cache, _ = load_dataset(cache_symbol, "akshare", DATA_TYPE, period="1d")
        tx_cache, _ = load_dataset(cache_symbol, "akshare_tx", DATA_TYPE, period="1d")
        tick = _normalize_history(tick_cache)
        ak = _normalize_history(ak_cache)
        tx = _normalize_history(tx_cache)
        merged = _append_only(_append_only(tick, ak, target_date), tx, target_date)
        if allow_fetch and (merged.empty or merged["date"].max() < target_date):
            requested = (
                (pd.Timestamp(merged["date"].max()) - pd.Timedelta(days=3)).date().isoformat()
                if not merged.empty else (start_date or "2000-01-01")
            )
            try:
                fresh_tick = _tickflow_history(code, api_key, 10000)
                tick = _append_only(tick, fresh_tick, target_date)
                if not tick.empty:
                    save_dataset(cache_symbol, code, "tickflow", DATA_TYPE, tick, period="1d")
            except Exception as exc:
                failures[code] = f"TickFlow正式日线：{type(exc).__name__}: {exc}"
            merged = _append_only(_append_only(tick, ak, target_date), tx, target_date)
            if merged.empty or merged["date"].max() < target_date:
                try:
                    fresh_ak = _akshare_history(code, requested, target_date)
                    source = fresh_ak.attrs.get("source", "akshare")
                    if source == "akshare_tx":
                        tx = _append_only(tx, fresh_ak, target_date)
                        if not tx.empty:
                            save_dataset(cache_symbol, code, "akshare_tx", DATA_TYPE, tx, period="1d")
                    else:
                        ak = _append_only(ak, fresh_ak, target_date)
                        if not ak.empty:
                            save_dataset(cache_symbol, code, "akshare", DATA_TYPE, ak, period="1d")
                    failures.pop(code, None)
                except Exception as exc:
                    previous = failures.get(code, "")
                    failures[code] = (previous + "；" if previous else "") + f"AkShare正式日线：{type(exc).__name__}: {exc}"
                merged = _append_only(_append_only(tick, ak, target_date), tx, target_date)
            elif not tick.empty:
                failures.pop(code, None)
        if not merged.empty:
            histories[code] = merged
            if merged["date"].max() < target_date:
                failures.setdefault(code, f"正式收盘数据截止{merged['date'].max()}，缺少目标日{target_date}")
        else:
            histories[code] = pd.DataFrame(columns=["date", "close"])
            failures.setdefault(code, "本地无未复权正式收盘缓存")
    return histories, failures


def _is_open(market_now: datetime) -> bool:
    market = get_market_window("A股")
    if not market_now.tzinfo:
        market_now = market_now.replace(tzinfo=TZ)
    if not any(start <= market_now.time().replace(tzinfo=None) < end for start, end in market.sessions):
        return False
    from services.market_calendar import is_market_trading_day
    return is_market_trading_day(market, market_now)


def _code_from_tickflow(value) -> str | None:
    text = str(value or "").strip().upper()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else None


def _tickflow_quotes(codes: list[str], api_key: str, current: datetime) -> dict[str, dict]:
    if not api_key:
        return {}
    from tickflow import TickFlow
    from services.position_analysis import _tickflow_quote_datetime
    client = TickFlow(api_key=api_key, timeout=10, max_retries=0)
    try:
        frame = client.quotes.get(
            symbols=[infer_tickflow_symbol(code) for code in codes], as_dataframe=True,
        )
    finally:
        client.close()
    if frame is None or frame.empty:
        return {}
    if "symbol" not in frame.columns:
        frame = frame.reset_index()
    result = {}
    for _, row in frame.iterrows():
        code = _code_from_tickflow(row.get("symbol"))
        price = pd.to_numeric(row.get("last_price"), errors="coerce")
        quote_time = _tickflow_quote_datetime(row)
        if code in codes and pd.notna(price) and math.isfinite(float(price)) and float(price) > 0 and quote_time and quote_time.date() == current.date():
            result[code] = {"price": float(price), "quote_time": quote_time,
                            "source": "TickFlow实时行情", "status": "实时"}
    return result


def _akshare_quote(code: str, current: datetime) -> dict | None:
    import akshare as ak
    frame = ak.stock_bid_ask_em(symbol=code)
    if frame is None or frame.empty or not {"item", "value"}.issubset(frame.columns):
        return None
    values = dict(zip(frame["item"].astype(str), frame["value"]))
    price = pd.to_numeric(values.get("最新"), errors="coerce")
    if pd.isna(price) or not math.isfinite(float(price)) or float(price) <= 0:
        return None
    return {"price": float(price), "quote_time": current,
            "source": "AkShare东方财富实时快照", "status": "实时"}


def _akshare_bj_quotes(codes: list[str], current: datetime) -> dict[str, dict]:
    """Use AkShare's all-A-share snapshot for Beijing listings unsupported by bid_ask_em."""
    import akshare as ak
    frame = ak.stock_zh_a_spot_em()
    if frame is None or frame.empty or not {"代码", "最新价"}.issubset(frame.columns):
        return {}
    frame = frame.copy()
    frame["代码"] = frame["代码"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    frame["最新价"] = pd.to_numeric(frame["最新价"], errors="coerce")
    wanted = set(codes)
    result = {}
    for _, row in frame.loc[frame["代码"].isin(wanted)].iterrows():
        price = row["最新价"]
        if pd.isna(price) or not math.isfinite(float(price)) or float(price) <= 0:
            continue
        result[str(row["代码"])] = {
            "price": float(price), "quote_time": current,
            "source": "AkShare全市场实时快照（请求时间）", "status": "实时",
        }
    return result


def fetch_microcap_realtime_quotes(
    symbols, *, api_key: str = "", market_now: datetime | None = None,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Fetch transient intraday marks. This function never writes them to cache."""
    current = market_now or datetime.now(TZ)
    codes = sorted({str(code).zfill(6) for code in symbols if str(code).strip()})
    if not codes or not _is_open(current):
        return {}, {}
    quotes, failures = {}, {}
    tickflow_error = ""
    try:
        quotes.update(_tickflow_quotes(codes, api_key.strip(), current))
    except Exception as exc:
        tickflow_error = f"TickFlow：{type(exc).__name__}: {exc}"
    missing = [code for code in codes if code not in quotes]
    bj_missing = [code for code in missing if code.startswith(("4", "8", "9"))]
    if bj_missing:
        try:
            quotes.update(_akshare_bj_quotes(bj_missing, current))
        except Exception as exc:
            bj_error = f"AkShare全市场快照：{type(exc).__name__}: {exc}"
            for code in bj_missing:
                failures[code] = "; ".join(part for part in (tickflow_error, bj_error) if part)
    ordinary_missing = [code for code in missing if code not in bj_missing]
    if ordinary_missing:
        with ThreadPoolExecutor(max_workers=min(4, len(ordinary_missing))) as pool:
            futures = {pool.submit(_akshare_quote, code, current): code for code in ordinary_missing}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    item = future.result()
                    if item:
                        quotes[code] = item
                    else:
                        failures[code] = "; ".join(part for part in (tickflow_error, "AkShare未返回有效实时价格") if part)
                except Exception as exc:
                    ak_error = f"AkShare实时快照：{type(exc).__name__}: {exc}"
                    failures[code] = "; ".join(part for part in (tickflow_error, ak_error) if part)
    for code in bj_missing:
        if code not in quotes and code not in failures:
            failures[code] = "; ".join(part for part in (tickflow_error, "AkShare全市场快照未返回有效实时价格") if part)
    return quotes, failures


__all__ = ["DATA_TYPE", "load_microcap_histories", "fetch_microcap_realtime_quotes"]
