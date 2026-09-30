"""Unadjusted A-share history and transient quote adapter for the microcap ledger."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import math
import re

import pandas as pd
import requests

from core.cache import load_dataset, save_dataset
from services.fund_analysis import infer_tickflow_symbol
from services.market_calendar import get_market_window, latest_settled_trade_date, previous_trading_day

DATA_TYPE = "microcap_live_close_v1"
TZ = ZoneInfo("Asia/Shanghai")
# The current session's close is expected from 15:05; until daily bars publish,
# it comes from a post-close quote snapshot (see load_microcap_histories).
SETTLEMENT_DELAY = timedelta(minutes=5)
# Merge priority: official daily bars win over post-close snapshots for the same date.
HISTORY_SOURCES = (
    "tickflow", "akshare", "akshare_tx",
    "tickflow_close_snapshot", "eastmoney_close_snapshot", "tencent_close_snapshot",
    "bk1158_close_snapshot",
)


def microcap_target_date(market_now: datetime) -> str:
    """Latest A-share session whose formal stock close should already be published."""
    return latest_settled_trade_date(
        get_market_window("A股"), market_now, settlement_delay=SETTLEMENT_DELAY,
    ).isoformat()


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
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    target_date = microcap_target_date(current)
    normalized_codes = sorted({str(code).zfill(6) for code in symbols if str(code).strip()})
    histories: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}
    # core.cache keeps legacy filenames keyed by symbol/source/period; namespace
    # the on-disk symbol so this raw A-share history cannot overwrite ETF data.
    frames = {
        code: {
            source: _normalize_history(load_dataset(f"microcap_live_{code}", source, DATA_TYPE, period="1d")[0])
            for source in HISTORY_SOURCES
        }
        for code in normalized_codes
    }

    def merged_of(code: str) -> pd.DataFrame:
        merged = pd.DataFrame(columns=["date", "close"])
        for source in HISTORY_SOURCES:
            merged = _append_only(merged, frames[code][source], target_date)
        return merged

    def last_date(code: str) -> str:
        merged = merged_of(code)
        return "" if merged.empty else str(merged["date"].max())

    def fetch_daily(code: str) -> None:
        cache_symbol = f"microcap_live_{code}"
        own = frames[code]
        merged = merged_of(code)
        requested = (
            (pd.Timestamp(merged["date"].max()) - pd.Timedelta(days=3)).date().isoformat()
            if not merged.empty else (start_date or "2000-01-01")
        )
        try:
            own["tickflow"] = _append_only(own["tickflow"], _tickflow_history(code, api_key, 10000), target_date)
            if not own["tickflow"].empty:
                save_dataset(cache_symbol, code, "tickflow", DATA_TYPE, own["tickflow"], period="1d")
        except Exception as exc:
            failures[code] = f"TickFlow正式日线：{type(exc).__name__}: {exc}"
        if last_date(code) >= target_date:
            if not own["tickflow"].empty:
                failures.pop(code, None)
            return
        try:
            fresh_ak = _akshare_history(code, requested, target_date)
            source = "akshare_tx" if fresh_ak.attrs.get("source") == "akshare_tx" else "akshare"
            own[source] = _append_only(own[source], fresh_ak, target_date)
            if not own[source].empty:
                save_dataset(cache_symbol, code, source, DATA_TYPE, own[source], period="1d")
            failures.pop(code, None)
        except Exception as exc:
            previous = failures.get(code, "")
            failures[code] = (previous + "；" if previous else "") + f"AkShare正式日线：{type(exc).__name__}: {exc}"

    if allow_fetch:
        market = get_market_window("A股")
        # Daily-bar vendors publish the current session long after the close, but a
        # post-close snapshot already carries the final price. Use it only for the
        # current session and only when the cache is otherwise complete, so the
        # snapshot can never paper over an older missing date.
        snapshot_day = target_date if target_date == current.date().isoformat() else None
        daily_goal = previous_trading_day(market, current.date()).isoformat() if snapshot_day else target_date
        for code in normalized_codes:
            if last_date(code) < daily_goal:
                fetch_daily(code)
        if snapshot_day:
            ready = [code for code in normalized_codes if last_date(code) == daily_goal]
            if ready:
                close_at = datetime.combine(current.date(), market.sessions[-1][1], tzinfo=TZ)
                snapshots, snapshot_error = _close_snapshots(ready, api_key, current, close_at)
                for code, row in snapshots.items():
                    source = row["cache_source"]
                    fresh = pd.DataFrame({"date": [snapshot_day], "close": [row["price"]]})
                    frames[code][source] = _append_only(frames[code][source], fresh, target_date)
                    save_dataset(f"microcap_live_{code}", code, source, DATA_TYPE, frames[code][source], period="1d")
                    failures.pop(code, None)
                for code in ready:
                    if code not in snapshots:
                        fetch_daily(code)
                        if last_date(code) < target_date and snapshot_error:
                            failures[code] = "；".join(p for p in (failures.get(code), snapshot_error) if p)

    for code in normalized_codes:
        merged = merged_of(code)
        if not merged.empty:
            histories[code] = merged
            if merged["date"].max() < target_date:
                failures.setdefault(code, f"正式收盘数据截止{merged['date'].max()}，缺少目标日{target_date}")
        else:
            histories[code] = pd.DataFrame(columns=["date", "close"])
            failures.setdefault(code, "本地无未复权正式收盘缓存")
    return histories, failures


def _close_snapshots(
    codes: list[str], api_key: str, current: datetime, close_at: datetime,
) -> tuple[dict[str, dict], str]:
    """Post-close quotes stamped at or after today's close: TickFlow, EastMoney, Tencent, then the local BK1158 snapshot."""
    result: dict[str, dict] = {}
    errors = []
    for label, cache_source, fetch in (
        ("TickFlow收盘快照", "tickflow_close_snapshot", lambda missing: _tickflow_quotes(missing, api_key.strip(), current)),
        ("东方财富收盘快照", "eastmoney_close_snapshot", lambda missing: _eastmoney_quotes(missing, current)),
        ("腾讯收盘快照", "tencent_close_snapshot", lambda missing: _tencent_quotes(missing, current)),
    ):
        missing = [code for code in codes if code not in result]
        if not missing:
            break
        try:
            quotes = fetch(missing)
        except Exception as exc:
            errors.append(_brief(label, exc))
            continue
        for code, quote in quotes.items():
            quote_time = pd.Timestamp(quote.get("quote_time"))
            quote_time = quote_time.tz_localize(TZ) if quote_time.tzinfo is None else quote_time.tz_convert(TZ)
            price = pd.to_numeric(quote.get("price"), errors="coerce")
            if code in missing and quote_time >= close_at and pd.notna(price) and float(price) > 0:
                result[code] = {"price": float(price), "cache_source": cache_source}
    missing = [code for code in codes if code not in result]
    if missing:
        try:
            result.update(_bk1158_snapshot_closes(missing, close_at))
        except Exception as exc:
            errors.append(_brief("BK1158成分快照", exc))
    return result, "; ".join(errors)


def _bk1158_snapshot_closes(codes: list[str], close_at: datetime) -> dict[str, dict]:
    """Last resort from the local 15:05 BK1158 constituent snapshot (no network).

    Its time is when the 400-stock list was taken, not a per-stock trade time, so only a
    snapshot taken on the same day at or after the close counts, and suspended rows are
    left to the daily bars. Held stocks that have left BK1158 are simply absent.
    """
    from services.microcap import load_microcap_constituent_snapshots

    snapshots, _ = load_microcap_constituent_snapshots()
    if snapshots is None or snapshots.empty:
        return {}
    rows = snapshots[snapshots["快照日期"].astype(str) == close_at.date().isoformat()]
    result: dict[str, dict] = {}
    for _, row in rows.iterrows():
        code = str(row.get("代码") or "").zfill(6)
        taken = pd.Timestamp(row.get("快照时间"))
        if code not in codes or pd.isna(taken) or bool(row.get("是否停牌")):
            continue
        taken = taken.tz_localize(TZ) if taken.tzinfo is None else taken.tz_convert(TZ)
        price = pd.to_numeric(row.get("最新价"), errors="coerce")
        if taken >= close_at and taken.date() == close_at.date() and pd.notna(price) and float(price) > 0:
            result[code] = {"price": float(price), "cache_source": "bk1158_close_snapshot"}
    return result


def quote_phase(market_now: datetime) -> str | None:
    """Where a trading day stands for transient valuation quotes.

    ``session`` while trading, ``lunch`` between the two sessions, ``after_close``
    from 15:00 on; ``None`` before the open and on non-trading days. Quotes stay
    useful at lunch (the morning close) and after 15:00 until the formal close
    lands, so neither phase should fall back to the previous day's close.
    """
    market = get_market_window("A股")
    if not market_now.tzinfo:
        market_now = market_now.replace(tzinfo=TZ)
    from services.market_calendar import is_market_trading_day
    if not is_market_trading_day(market, market_now):
        return None
    now_time = market_now.time().replace(tzinfo=None)
    if any(start <= now_time < end for start, end in market.sessions):
        return "session"
    if now_time < market.sessions[0][0]:
        return None
    if now_time >= market.sessions[-1][1]:
        return "after_close"
    return "lunch"


def _is_open(market_now: datetime) -> bool:
    return quote_phase(market_now) == "session"


def _code_from_tickflow(value) -> str | None:
    text = str(value or "").strip().upper()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else None


TICKFLOW_QUOTE_BATCH = 5  # TickFlow quotes endpoint rejects more than 5 symbols per request


def _tickflow_quotes(codes: list[str], api_key: str, current: datetime) -> dict[str, dict]:
    """Batch TickFlow quotes 5 at a time; a failed batch only drops its own symbols.

    Raises the last error only when every batch failed, so callers can report it.
    """
    if not api_key:
        return {}
    from tickflow import TickFlow
    from services.position_analysis import _tickflow_quote_datetime
    client = TickFlow(api_key=api_key, timeout=10, max_retries=0)
    frames, last_error, batches = [], None, 0
    try:
        for start in range(0, len(codes), TICKFLOW_QUOTE_BATCH):
            batches += 1
            batch = [infer_tickflow_symbol(code) for code in codes[start:start + TICKFLOW_QUOTE_BATCH]]
            try:
                frame = client.quotes.get(symbols=batch, as_dataframe=True)
            except Exception as exc:  # keep other batches
                last_error = exc
                continue
            if frame is not None and not frame.empty:
                frames.append(frame if "symbol" in frame.columns else frame.reset_index())
    finally:
        client.close()
    if not frames:
        if last_error is not None and batches:
            raise last_error
        return {}
    frame = pd.concat(frames, ignore_index=True)
    result = {}
    for _, row in frame.iterrows():
        code = _code_from_tickflow(row.get("symbol"))
        price = pd.to_numeric(row.get("last_price"), errors="coerce")
        quote_time = _tickflow_quote_datetime(row)
        if code in codes and pd.notna(price) and math.isfinite(float(price)) and float(price) > 0 and quote_time and quote_time.date() == current.date():
            result[code] = {"price": float(price), "quote_time": quote_time,
                            "source": "TickFlow实时行情", "status": "实时"}
    return result


EASTMONEY_ULIST_URLS = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get",
    "https://36.push2.eastmoney.com/api/qt/ulist.np/get",
    "https://pushguest.eastmoney.com/api/qt/ulist.np/get",
)


def _eastmoney_secid(code: str) -> str:
    # Shanghai A (6xxxxx) and B (900xxx) use market 1; Shenzhen and Beijing use market 0.
    return ("1." if code.startswith(("6", "900")) else "0.") + code


def _brief(label: str, exc: Exception) -> str:
    return f"{label}：{type(exc).__name__}: {str(exc)[:80]}"


def _eastmoney_quotes(codes: list[str], current: datetime) -> dict[str, dict]:
    """One batched EastMoney request for all codes; direct connection first, then the system proxy.

    AkShare only goes through the system proxy, so a flaky local proxy used to knock out every
    fallback quote. Raises the last error when no host/route answered.
    """
    params = {"fltt": 2, "invt": 2, "fields": "f2,f12,f124",
              "secids": ",".join(_eastmoney_secid(code) for code in codes)}
    headers = {"Accept": "application/json,text/plain,*/*", "Referer": "https://quote.eastmoney.com/",
               "User-Agent": "Mozilla/5.0"}
    last_error: Exception | None = None
    for trust_env, route in ((False, "直连"), (True, "代理")):
        session = requests.Session()
        session.trust_env = trust_env
        try:
            for url in EASTMONEY_ULIST_URLS:
                try:
                    response = session.get(url, params=params, headers=headers, timeout=8)
                    response.raise_for_status()
                    rows = ((response.json().get("data") or {}).get("diff") or [])
                except Exception as exc:
                    last_error = exc
                    continue
                result = {}
                for row in rows:
                    code = str(row.get("f12") or "").zfill(6)
                    price = pd.to_numeric(row.get("f2"), errors="coerce")
                    stamp = pd.to_numeric(row.get("f124"), errors="coerce")
                    if code not in codes or pd.isna(price) or not math.isfinite(float(price)) or float(price) <= 0 or pd.isna(stamp):
                        continue
                    quote_time = datetime.fromtimestamp(float(stamp), TZ)
                    if quote_time.date() != current.date():
                        continue
                    result[code] = {"price": float(price), "quote_time": quote_time,
                                    "source": f"东方财富实时行情（{route}）", "status": "实时"}
                if result:
                    return result
                last_error = ValueError("东方财富未返回当日有效价格")
        finally:
            session.close()
    if last_error is not None:
        raise last_error
    return {}


TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_QUOTE_BATCH = 60


def _tencent_symbol(code: str) -> str:
    if code.startswith(("6", "900")):
        return "sh" + code
    return ("bj" if code.startswith(("4", "8", "9")) else "sz") + code


def _tencent_rows(content: bytes, codes: list[str], current: datetime, route: str) -> dict[str, dict]:
    """Parse one Tencent batch; keep only stocks that traded today (a suspended row repeats the previous close)."""
    result = {}
    for prefix, code, payload in re.findall(r'v_(sh|sz|bj)(\d{6})="([^"]*)"', content.decode("gbk", errors="replace")):
        fields = payload.split("~")
        if code not in codes or len(fields) < 38 or fields[2] != code or _tencent_symbol(code) != prefix + code:
            continue
        price, volume = (pd.to_numeric(fields[index], errors="coerce") for index in (3, 6))
        try:
            quote_time = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        except ValueError:
            continue
        if (pd.isna(price) or not math.isfinite(float(price)) or float(price) <= 0
                or pd.isna(volume) or float(volume) <= 0 or quote_time.date() != current.date()):
            continue
        result[code] = {"price": float(price), "quote_time": quote_time,
                        "source": f"腾讯实时行情（{route}）", "status": "实时"}
    return result


def _tencent_quotes(codes: list[str], current: datetime) -> dict[str, dict]:
    """Batched Tencent quotes, a vendor independent of EastMoney; direct connection first, then the system proxy.

    Raises the last error only when no batch returned a usable price.
    """
    headers = {"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"}
    result: dict[str, dict] = {}
    last_error: Exception | None = None
    for start in range(0, len(codes), TENCENT_QUOTE_BATCH):
        batch = codes[start:start + TENCENT_QUOTE_BATCH]
        url = TENCENT_QUOTE_URL + ",".join(_tencent_symbol(code) for code in batch)
        for trust_env, route in ((False, "直连"), (True, "代理")):
            session = requests.Session()
            session.trust_env = trust_env
            try:
                response = session.get(url, headers=headers, timeout=8)
                response.raise_for_status()
                rows = _tencent_rows(response.content, batch, current, route)
            except Exception as exc:
                last_error = exc
                continue
            finally:
                session.close()
            if rows:
                result.update(rows)
                break
            last_error = ValueError("腾讯未返回当日有效价格")
    if not result and last_error is not None:
        raise last_error
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
    if not codes or quote_phase(current) is None:
        return {}, {}
    quotes, failures = {}, {}
    errors = []
    try:
        quotes.update(_tickflow_quotes(codes, api_key.strip(), current))
    except Exception as exc:
        errors.append(_brief("TickFlow", exc))
    missing = [code for code in codes if code not in quotes]
    if missing:
        try:
            quotes.update(_eastmoney_quotes(missing, current))
        except Exception as exc:
            errors.append(_brief("东方财富", exc))
    missing = [code for code in codes if code not in quotes]
    if missing:
        try:
            quotes.update(_tencent_quotes(missing, current))
        except Exception as exc:
            errors.append(_brief("腾讯", exc))
    tickflow_error = "; ".join(errors)
    missing = [code for code in codes if code not in quotes]
    bj_missing = [code for code in missing if code.startswith(("4", "8", "9"))]
    if bj_missing:
        try:
            quotes.update(_akshare_bj_quotes(bj_missing, current))
        except Exception as exc:
            bj_error = _brief("AkShare全市场快照", exc)
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
                    ak_error = _brief("AkShare实时快照", exc)
                    failures[code] = "; ".join(part for part in (tickflow_error, ak_error) if part)
    for code in bj_missing:
        if code not in quotes and code not in failures:
            failures[code] = "; ".join(part for part in (tickflow_error, "AkShare全市场快照未返回有效实时价格") if part)
    return quotes, failures


__all__ = ["DATA_TYPE", "load_microcap_histories", "fetch_microcap_realtime_quotes"]
