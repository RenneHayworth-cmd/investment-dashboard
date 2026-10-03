"""Process-local US card quotes; no formal history updates or persistence."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from time import monotonic
from zoneinfo import ZoneInfo

import pandas as pd
from services import index_realtime as realtime

US_AUTO_REFRESH_INDEX_NAMES = frozenset({"标普500", "纳斯达克综合", "纳斯达克100"})
US_QUOTE_REFRESH_SECONDS = 120
US_QUOTE_RETRY_SECONDS = 600
US_QUOTE_MAX_AGE_SECONDS = 180
_LOCK = Lock()
_NEXT_ATTEMPT: dict[tuple[str, object], datetime] = {}
_INFLIGHT: set[tuple[str, object]] = set()
_ERRORS: dict[tuple[str, object], str] = {}


@dataclass
class USQuoteRefreshResult:
    attempted_names: set[str] = field(default_factory=set)
    quotes: dict[str, dict] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def current_us_quote(quote: dict, now: datetime) -> bool:
    """Require the NY session date and the source timestamp, not fetch time."""
    stamp = pd.to_datetime(quote.get("quote_time"), errors="coerce")
    price = pd.to_numeric(quote.get("price"), errors="coerce")
    if pd.isna(stamp) or pd.isna(price) or not 0 < float(price) < float("inf"):
        return False
    stamp = stamp.tz_localize("America/New_York") if stamp.tzinfo is None else stamp.tz_convert("America/New_York")
    local = now.astimezone(ZoneInfo("America/New_York"))
    age = (pd.Timestamp(local) - stamp).total_seconds()
    return stamp.date() == local.date() and -5 <= age <= US_QUOTE_MAX_AGE_SECONDS


def remember_us_manual_refresh(quotes: dict[str, dict], *, now: datetime) -> None:
    """A successful explicit refresh also satisfies the next automatic slot."""
    local = now.astimezone(ZoneInfo("America/New_York"))
    with _LOCK:
        for name in US_AUTO_REFRESH_INDEX_NAMES:
            if current_us_quote(quotes.get(name, {}), local):
                key = (name, local.date())
                _NEXT_ATTEMPT[key] = local + timedelta(seconds=US_QUOTE_REFRESH_SECONDS)
                _ERRORS.pop(key, None)


def refresh_us_index_quotes(*, now: datetime | None = None) -> USQuoteRefreshResult:
    """Only verified US instruments, while their cash market is trading.

    Browser sessions share reservations and retry deadlines. Invalid or partial
    responses retain the last successful process-local quotes and remain retryable.
    """
    local = (now or datetime.now(ZoneInfo("America/New_York"))).astimezone(ZoneInfo("America/New_York"))
    result = USQuoteRefreshResult()
    if not realtime._market_is_open("美股", now=local):
        return result
    cached = realtime.load_runtime_realtime_quotes()
    keys = {name: (name, local.date()) for name in US_AUTO_REFRESH_INDEX_NAMES}
    with _LOCK:
        cutoff = local.date() - timedelta(days=2)
        for key in list(_NEXT_ATTEMPT):
            if key[1] < cutoff and key not in _INFLIGHT:
                _NEXT_ATTEMPT.pop(key, None)
                _ERRORS.pop(key, None)
        for name, key in keys.items():
            if key not in _NEXT_ATTEMPT and current_us_quote(cached.get(name, {}), local):
                stamp = pd.Timestamp(cached[name]["quote_time"])
                stamp = stamp.tz_localize("America/New_York") if stamp.tzinfo is None else stamp.tz_convert("America/New_York")
                _NEXT_ATTEMPT[key] = stamp.to_pydatetime() + timedelta(seconds=US_QUOTE_REFRESH_SECONDS)
            if key in _INFLIGHT or local < _NEXT_ATTEMPT.get(key, local):
                continue
            _INFLIGHT.add(key)
            _NEXT_ATTEMPT[key] = local + timedelta(seconds=US_QUOTE_RETRY_SECONDS)
            result.attempted_names.add(name)
    if result.attempted_names:
        started = monotonic()
        source_errors: dict[str, str] = {}
        try:
            fetched = realtime.fetch_realtime_index_quotes(
                now=local, max_workers=4, force_index_names=result.attempted_names, errors=source_errors,
            )
            received = local + timedelta(seconds=monotonic() - started)
            for name in result.attempted_names:
                quote = fetched.get(name, {})
                if current_us_quote(quote, received):
                    result.quotes[name] = quote
                else:
                    source_errors[name] = source_errors.get(name) or "未取得当前美股交易日三分钟内的有效报价，保留上次数据"
            realtime.remember_runtime_realtime_quotes(result.quotes)
        except Exception as exc:
            source_errors = {name: f"美股报价获取失败：{str(exc)[:240]}" for name in result.attempted_names}
        finally:
            with _LOCK:
                for name in result.attempted_names:
                    key = keys[name]
                    _INFLIGHT.discard(key)
                    if name in result.quotes:
                        _NEXT_ATTEMPT[key] = local + timedelta(seconds=US_QUOTE_REFRESH_SECONDS)
                        _ERRORS.pop(key, None)
                    else:
                        _ERRORS[key] = source_errors.get(name) or "美股报价暂不可用，保留上次数据"
    with _LOCK:
        result.errors = {name: _ERRORS[key] for name, key in keys.items() if key in _ERRORS}
    return result
