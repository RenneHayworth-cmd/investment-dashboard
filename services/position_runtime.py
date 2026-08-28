from __future__ import annotations

from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd

from services.fund_analysis import infer_tickflow_symbol
from services.market_calendar import get_market_window, is_market_trading_day
from services.position_market import _fetch_sina_exchange_fund_quote
from services.position_models import (
    ETF_AFTERNOON_TIMING_START_TIME,
    ETF_FINAL_CLOSE_READY_TIME,
    ETF_LUNCH_TIMING_FETCH_END_TIME,
    ETF_LUNCH_TIMING_START_TIME,
    ETF_MIDSESSION_TIMING_REFRESH_SECONDS,
    ETF_MORNING_FAST_REFRESH_END_TIME,
    ETF_MORNING_TIMING_PREVIEW_END_TIME,
    ETF_MORNING_TIMING_REFRESH_SECONDS,
    ETF_MORNING_TIMING_START_TIME,
    ETF_REALTIME_TIMING_END_TIME,
    ETF_REALTIME_TIMING_REFRESH_SECONDS,
    ETF_REALTIME_TIMING_START_TIME,
    ETF_SINA_REALTIME_FALLBACK_CODES,
    ETF_TIMING_STRATEGIES,
    PositionItem,
    _round_metric,
    display_etf_name,
    logger,
    normalize_etf_base_code,
)
from services.position_sessions import (
    etf_intraday_quote_ready,
    etf_lunch_timing_preview_ready,
    etf_morning_timing_preview_ready,
    etf_realtime_timing_ready,
)
from services.position_timing import calculate_etf_timing_snapshot

_RUNTIME_ETF_QUOTE_CACHE: dict[str, dict[str, object]] = {}
_RUNTIME_ETF_QUOTE_CACHE_LOCK = Lock()
_RUNTIME_ETF_QUOTE_FETCH_STATE: dict[str, object] = {}


def _runtime_quote_refresh_band(market_now: datetime) -> tuple[str, int] | None:
    market = get_market_window("A股")
    if market is None or not is_market_trading_day(market, market_now):
        return None
    current_time = market_now.time()
    if ETF_MORNING_TIMING_START_TIME <= current_time < ETF_MORNING_FAST_REFRESH_END_TIME:
        return "早盘", ETF_MORNING_TIMING_REFRESH_SECONDS
    if ETF_MORNING_FAST_REFRESH_END_TIME <= current_time < ETF_MORNING_TIMING_PREVIEW_END_TIME:
        return "上午", ETF_MIDSESSION_TIMING_REFRESH_SECONDS
    if ETF_LUNCH_TIMING_START_TIME <= current_time < ETF_LUNCH_TIMING_FETCH_END_TIME:
        return "午间", 600
    if ETF_AFTERNOON_TIMING_START_TIME <= current_time < ETF_REALTIME_TIMING_START_TIME:
        return "下午", ETF_MIDSESSION_TIMING_REFRESH_SECONDS
    if ETF_REALTIME_TIMING_START_TIME <= current_time < ETF_REALTIME_TIMING_END_TIME:
        return "尾盘", ETF_REALTIME_TIMING_REFRESH_SECONDS
    return None


def refresh_runtime_etf_quotes(
    codes: list[str],
    *,
    api_key: str,
    market_now: datetime | None = None,
) -> dict[str, dict[str, object]]:
    """Refresh one process-wide ETF quote batch and reuse it across pages."""
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    requested_scope = {
        normalize_etf_base_code(code) for code in codes if str(code or "").strip()
    }
    if not requested_scope:
        return {}
    band = _runtime_quote_refresh_band(market_now)
    if band is None:
        return filter_current_etf_realtime_quotes(
            load_runtime_etf_quotes(),
            market_now=market_now,
            retain_after_close=True,
        )

    band_name, refresh_seconds = band
    market_now_naive = market_now.replace(tzinfo=None)
    trade_date = market_now.date().isoformat()
    with _RUNTIME_ETF_QUOTE_CACHE_LOCK:
        same_date = _RUNTIME_ETF_QUOTE_FETCH_STATE.get("trade_date") == trade_date
        existing_scope = (
            set(_RUNTIME_ETF_QUOTE_FETCH_STATE.get("scope") or []) if same_date else set()
        )
        last_attempt = pd.to_datetime(
            _RUNTIME_ETF_QUOTE_FETCH_STATE.get("last_attempt"), errors="coerce"
        )
        same_band = _RUNTIME_ETF_QUOTE_FETCH_STATE.get("band") == band_name
        scope_covered = requested_scope.issubset(existing_scope)
        successful_scope = set(
            _RUNTIME_ETF_QUOTE_FETCH_STATE.get("last_success_scope") or []
        )
        lunch_already_succeeded = bool(
            band_name == "午间"
            and _RUNTIME_ETF_QUOTE_FETCH_STATE.get("last_success_trade_date")
            == trade_date
            and _RUNTIME_ETF_QUOTE_FETCH_STATE.get("last_success_band")
            == band_name
            and requested_scope.issubset(successful_scope)
        )
        due = bool(
            not same_date
            or not same_band
            or not scope_covered
            or pd.isna(last_attempt)
            or (
                not lunch_already_succeeded
                and (market_now_naive - pd.Timestamp(last_attempt)).total_seconds()
                >= refresh_seconds
            )
        )
        if not due:
            return {
                code: dict(quote)
                for code, quote in _RUNTIME_ETF_QUOTE_CACHE.items()
                if code in requested_scope
            }
        fetch_scope = sorted(requested_scope | existing_scope)
        _RUNTIME_ETF_QUOTE_FETCH_STATE.update(
            {
                "trade_date": trade_date,
                "scope": fetch_scope,
                "band": band_name,
                "last_attempt": market_now_naive.isoformat(),
            }
        )

    try:
        quotes = fetch_tickflow_etf_quotes(
            fetch_scope,
            api_key=api_key,
            market_now=market_now,
        )
    except Exception as exc:
        with _RUNTIME_ETF_QUOTE_CACHE_LOCK:
            _RUNTIME_ETF_QUOTE_FETCH_STATE["error"] = str(exc)
        raise
    remember_runtime_etf_quotes(quotes)
    with _RUNTIME_ETF_QUOTE_CACHE_LOCK:
        _RUNTIME_ETF_QUOTE_FETCH_STATE.update(
            {
                "last_success": market_now_naive.isoformat(),
                "last_success_trade_date": trade_date,
                "last_success_band": band_name,
                "last_success_scope": fetch_scope,
                "error": "",
            }
        )
        return {
            code: dict(quote)
            for code, quote in _RUNTIME_ETF_QUOTE_CACHE.items()
            if code in requested_scope
        }


def load_runtime_etf_quote_state() -> dict[str, object]:
    with _RUNTIME_ETF_QUOTE_CACHE_LOCK:
        return dict(_RUNTIME_ETF_QUOTE_FETCH_STATE)

def _tickflow_quote_datetime(row: pd.Series) -> datetime | None:
    timestamp = pd.to_numeric(row.get("timestamp"), errors="coerce")
    if not pd.isna(timestamp):
        unit = "ms" if float(timestamp) > 10_000_000_000 else "s"
        parsed = pd.to_datetime(float(timestamp), unit=unit, utc=True, errors="coerce")
        if not pd.isna(parsed):
            return parsed.tz_convert("Asia/Shanghai").to_pydatetime()
    trade_date = pd.to_datetime(row.get("trade_date"), errors="coerce")
    if pd.isna(trade_date):
        return None
    trade_time_value = row.get("trade_time")
    trade_time = "00:00:00" if pd.isna(trade_time_value) else str(trade_time_value)
    parsed = pd.to_datetime(f"{trade_date:%Y-%m-%d} {trade_time}", errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.tz_localize("Asia/Shanghai").to_pydatetime()


def fetch_tickflow_etf_quotes(
    codes: list[str],
    *,
    api_key: str,
    market_now: datetime | None = None,
) -> dict[str, dict[str, object]]:
    if not api_key.strip():
        raise ValueError("TickFlow实时行情需要填写API Key。")

    from tickflow import TickFlow

    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    symbols = [infer_tickflow_symbol(code) for code in codes]
    client = TickFlow(api_key=api_key)
    quote_frames = []
    for start in range(0, len(symbols), 5):
        batch = symbols[start:start + 5]
        batch_df = client.quotes.get(symbols=batch, as_dataframe=True)
        if batch_df is not None and not batch_df.empty:
            quote_frames.append(batch_df)
    if not quote_frames:
        raise ValueError("TickFlow未返回ETF实时行情。")
    quote_df = pd.concat(quote_frames, ignore_index=True)
    if "symbol" not in quote_df.columns:
        quote_df = quote_df.reset_index()

    quotes: dict[str, dict[str, object]] = {}
    for _, row in quote_df.iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        latest_price = pd.to_numeric(row.get("last_price"), errors="coerce")
        quote_time = _tickflow_quote_datetime(row)
        if not symbol or pd.isna(latest_price) or quote_time is None:
            continue
        if quote_time.date() != market_now.date():
            continue
        previous_close = pd.to_numeric(row.get("prev_close"), errors="coerce")
        if not pd.isna(previous_close) and float(previous_close) != 0:
            change_pct = (float(latest_price) / float(previous_close) - 1) * 100
        else:
            change_pct = pd.to_numeric(row.get("ext.change_pct"), errors="coerce")
        quotes[normalize_etf_base_code(symbol)] = {
            "symbol": symbol,
            "price": float(latest_price),
            "previous_close": None if pd.isna(previous_close) else float(previous_close),
            "change_pct": None if pd.isna(change_pct) else float(change_pct),
            "quote_time": quote_time,
        }

    for symbol in symbols:
        base_code = normalize_etf_base_code(symbol)
        if base_code not in ETF_SINA_REALTIME_FALLBACK_CODES or base_code in quotes:
            continue
        try:
            quotes[base_code] = _fetch_sina_exchange_fund_quote(
                symbol=symbol,
                market_now=market_now,
            )
        except Exception as exc:
            logger.warning("%s TickFlow实时行情缺失，新浪备用源也失败：%s", symbol, exc)

    if not quotes:
        raise ValueError("TickFlow未返回当天ETF实时行情。")
    return quotes


def remember_runtime_etf_quotes(quotes: dict[str, dict[str, object]]) -> None:
    if not quotes:
        return
    with _RUNTIME_ETF_QUOTE_CACHE_LOCK:
        for code, quote in quotes.items():
            _RUNTIME_ETF_QUOTE_CACHE[normalize_etf_base_code(code)] = dict(quote)


def load_runtime_etf_quotes() -> dict[str, dict[str, object]]:
    with _RUNTIME_ETF_QUOTE_CACHE_LOCK:
        return {
            code: dict(quote)
            for code, quote in _RUNTIME_ETF_QUOTE_CACHE.items()
        }


def filter_current_etf_realtime_quotes(
    quotes: dict[str, dict[str, object]] | None,
    *,
    market_now: datetime | None = None,
    retain_after_close: bool = False,
) -> dict[str, dict[str, object]]:
    """Keep same-day quotes intraday, optionally until the formal close is ready."""
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    market = get_market_window("A股")
    after_close_retention = bool(
        retain_after_close
        and market is not None
        and is_market_trading_day(market, market_now)
        and market_now.time() >= ETF_FINAL_CLOSE_READY_TIME
    )
    if not etf_intraday_quote_ready(market_now) and not after_close_retention:
        return {}

    current: dict[str, dict[str, object]] = {}
    for code, quote in (quotes or {}).items():
        quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
        if pd.isna(quote_time) or quote_time.date() != market_now.date():
            continue
        current[normalize_etf_base_code(code)] = dict(quote)
    return current


def apply_etf_realtime_quote(item: PositionItem, quote: dict[str, object]) -> PositionItem:
    symbol = str(quote.get("symbol") or item.code).strip().upper()
    base_code = normalize_etf_base_code(symbol)
    latest_price = pd.to_numeric(quote.get("price"), errors="coerce")
    if pd.isna(latest_price):
        return item

    metrics = dict(item.metrics)
    previous_close = pd.to_numeric(quote.get("previous_close"), errors="coerce")
    if pd.isna(previous_close):
        previous_close = pd.to_numeric(metrics.get("最新价"), errors="coerce")
    if not pd.isna(previous_close) and float(previous_close) != 0:
        change_pct = (float(latest_price) / float(previous_close) - 1) * 100
    else:
        change_pct = pd.to_numeric(quote.get("change_pct"), errors="coerce")
    metrics["最新价"] = _round_metric(latest_price, 4)
    metrics["日涨跌(%)"] = _round_metric(change_pct)

    quote_time = quote.get("quote_time")
    quote_date = pd.to_datetime(quote_time, errors="coerce")
    return PositionItem(
        category="ETF",
        code=symbol,
        name=display_etf_name(base_code, item.name),
        status="盘中",
        source="TickFlow实时行情（不写入缓存）",
        latest_date="" if pd.isna(quote_date) else quote_date.strftime("%Y-%m-%d"),
        cache_time=item.cache_time,
        metrics=metrics,
        dataframe=item.dataframe,
        error=item.error,
        formal_history_valid=item.formal_history_valid,
    )


def apply_etf_realtime_quotes_to_items(
    items: list[PositionItem],
    quotes: dict[str, dict[str, object]],
) -> list[PositionItem]:
    updated_items: list[PositionItem] = []
    for item in items:
        if item.category != "ETF":
            updated_items.append(item)
            continue
        quote = quotes.get(normalize_etf_base_code(item.code))
        updated_items.append(
            apply_etf_realtime_quote(item, quote) if quote is not None else item
        )
    return updated_items


def apply_etf_realtime_quote_to_timing(
    item: PositionItem,
    quote: dict[str, object],
    *,
    market_now: datetime | None = None,
    allow_close_retention: bool = False,
) -> PositionItem:
    """Build an intraday timing preview without changing formal history."""
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    is_morning_preview = etf_morning_timing_preview_ready(market_now)
    is_closing_preview = etf_realtime_timing_ready(market_now)
    is_lunch_preview = etf_lunch_timing_preview_ready(market_now)
    is_close_retention = bool(
        allow_close_retention
        and market_now.time() >= ETF_REALTIME_TIMING_END_TIME
    )
    if (
        not is_morning_preview
        and not is_closing_preview
        and not is_lunch_preview
        and not is_close_retention
    ):
        return item

    base_code = normalize_etf_base_code(item.code)
    strategy = ETF_TIMING_STRATEGIES.get(base_code)
    quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
    quote_price = pd.to_numeric(quote.get("price"), errors="coerce")
    if (
        pd.isna(quote_time)
        or quote_time.date() != market_now.date()
        or pd.isna(quote_price)
        or float(quote_price) <= 0
    ):
        return item

    quoted_item = apply_etf_realtime_quote(item, quote)
    if not item.formal_history_valid:
        return quoted_item
    if strategy is None:
        # 512890 has no MA signal of its own, but its parking row should use the
        # same transient price and daily change as its card. Its aggregate
        # position state is still calculated from the three transfer sources.
        return quoted_item if base_code == "512890" else item

    timing_data = (
        item.dataframe[["date", "price"]].copy()
        if item.dataframe is not None
        and not item.dataframe.empty
        and {"date", "price"}.issubset(item.dataframe.columns)
        else pd.DataFrame(columns=["date", "price"])
    )
    realtime_date = pd.Timestamp(quote_time.date())
    if not timing_data.empty:
        timing_dates = pd.to_datetime(timing_data["date"], errors="coerce")
        timing_data = timing_data.loc[timing_dates.dt.date != realtime_date.date()].copy()
    timing_data = pd.concat(
        [
            timing_data,
            pd.DataFrame(
                {
                    "date": [realtime_date],
                    "price": [float(quote_price)],
                }
            ),
        ],
        ignore_index=True,
    )
    timing_snapshot = calculate_etf_timing_snapshot(
        timing_data,
        ma_period=int(strategy[0]),
        threshold_pct=float(strategy[1]),
    )
    metrics = dict(quoted_item.metrics)
    metrics.update(
        {
            key: _round_metric(value, 6)
            if key == "策略均线"
            else _round_metric(value)
            if key in {"策略偏离(%)", "策略区间涨幅(%)", "策略上一区间涨幅(%)"}
            else value
            for key, value in timing_snapshot.items()
        }
    )
    return PositionItem(
        category=item.category,
        code=quoted_item.code,
        name=quoted_item.name,
        status=(
            "收盘待确认"
            if is_close_retention
            else "实时预判"
            if is_closing_preview
            else "午间预判"
            if is_lunch_preview
            else "早盘预判"
        ),
        source=(
            "TickFlow当天最后行情（待正式收盘确认，不写入缓存）"
            if is_close_retention
            else "TickFlow实时行情（14:50-15:00择时预判，不写入缓存）"
            if is_closing_preview
            else "TickFlow午间收盘行情（择时预判，不写入缓存）"
            if is_lunch_preview
            else "TickFlow早盘实时行情（择时预判，不写入缓存）"
        ),
        latest_date=realtime_date.strftime("%Y-%m-%d"),
        cache_time=item.cache_time,
        metrics=metrics,
        dataframe=item.dataframe,
        error=item.error,
        formal_history_valid=item.formal_history_valid,
    )
