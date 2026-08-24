from __future__ import annotations

from datetime import datetime, time as datetime_time, timedelta
import json
import re
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from core.cache import load_dataset, save_dataset
from services.fund_analysis import (
    FUND_ADJUST_BACKWARD_ADDITIVE,
    FUND_ADJUST_BACKWARD_RATIO,
    FUND_ADJUST_FORWARD_ADDITIVE,
    FUND_ADJUST_FORWARD_RATIO,
    FUND_ADJUST_NONE,
    FUND_CACHE_SCHEMA_VERSION,
    analyze_fund_nav,
    build_fund_cache_symbol,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
    normalize_fund_adjustment,
    normalize_nav_dataframe,
    stamp_fund_history_metadata,
)
from services.market_calendar import get_market_window, previous_trading_day
from services.position_models import (
    ETF_AKSHARE_HISTORY_CODES,
    ETF_TIMING_STRATEGIES,
    SINA_REQUEST_TIMEOUT_SECONDS,
    PositionItem,
    _current_cache_time_text,
    _missing_item,
    _round_metric,
    display_etf_name,
    format_cache_time,
    logger,
    normalize_etf_base_code,
)
from services.position_sessions import (
    etf_cache_has_latest_final_close,
    etf_final_close_ready,
    filter_final_etf_rows,
    latest_final_etf_trade_date,
)
from services.position_timing import calculate_etf_timing_snapshot

def _load_dataset_if_ready(symbol: str, source: str, data_type: str, period: str = "1d"):
    cached_df, meta = load_dataset(symbol, source, data_type, period=period)
    if cached_df is None or cached_df.empty:
        return None, meta
    return cached_df, meta


def _merge_by_date(old_df: pd.DataFrame | None, new_df: pd.DataFrame, date_column: str) -> pd.DataFrame:
    normalized_new = new_df.copy()
    if date_column not in normalized_new.columns:
        return old_df.copy() if old_df is not None and not old_df.empty else normalized_new
    normalized_new[date_column] = pd.to_datetime(normalized_new[date_column], errors="coerce")
    normalized_new = normalized_new.dropna(subset=[date_column])
    normalized_new = normalized_new.sort_values(date_column).drop_duplicates(date_column, keep="last")
    if old_df is None or old_df.empty:
        return normalized_new.reset_index(drop=True)

    normalized_old = old_df.copy()
    normalized_old[date_column] = pd.to_datetime(normalized_old[date_column], errors="coerce")
    normalized_old = normalized_old.dropna(subset=[date_column])
    normalized_old = normalized_old.sort_values(date_column).drop_duplicates(date_column, keep="first")
    unseen = normalized_new[~normalized_new[date_column].isin(normalized_old[date_column])]
    return (
        pd.concat([normalized_old, unseen], ignore_index=True)
        .sort_values(date_column)
        .reset_index(drop=True)
    )


def _fund_history_validation_error(
    df: pd.DataFrame | None,
    *,
    adjust: str | None,
    min_rows: int,
    market_now: datetime | None = None,
    require_latest: bool = False,
) -> str:
    if df is None or df.empty:
        return "正式历史为空。"
    if not {"日期", "收盘价"}.issubset(df.columns):
        return f"正式历史缺少必要列：{list(df.columns)}。"

    dates = pd.to_datetime(df["日期"], errors="coerce")
    closes = pd.to_numeric(df["收盘价"], errors="coerce")
    if dates.isna().any():
        return "正式历史包含无法识别的日期。"
    if dates.dt.normalize().duplicated().any():
        return "正式历史包含重复交易日期。"
    if closes.isna().any() or (closes <= 0).any():
        return "正式历史包含空值或非正收盘价。"
    if len(df) < max(int(min_rows), 2):
        return f"正式历史仅有{len(df)}条，不足以计算至少{max(int(min_rows), 2)}条的策略。"

    adjustment = normalize_fund_adjustment(adjust)
    if "_adjust_mode" not in df.columns:
        return "缓存缺少复权方式标记。"
    modes = df["_adjust_mode"].dropna().astype(str).str.lower().unique().tolist()
    if modes != [adjustment]:
        return f"缓存复权标签为{modes}，预期为{adjustment}。"
    if "_cache_schema_version" not in df.columns:
        return "缓存缺少结构版本标记。"
    versions = pd.to_numeric(df["_cache_schema_version"], errors="coerce").dropna().unique()
    if len(versions) != 1 or not np.all(versions == FUND_CACHE_SCHEMA_VERSION):
        return "缓存结构版本与当前版本不一致。"

    if require_latest:
        target_date = latest_final_etf_trade_date(market_now)
        if dates.max().date() < target_date:
            return (
                f"正式历史最新到{dates.max():%Y-%m-%d}，"
                f"尚未覆盖最新完成交易日{target_date:%Y-%m-%d}。"
            )
    return ""


def _recent_etf_gap_warning(
    df: pd.DataFrame | None,
    *,
    market_now: datetime | None = None,
    sessions: int = 20,
) -> str:
    if df is None or df.empty or "日期" not in df.columns:
        return ""
    market = get_market_window("A股")
    if market is None:
        return ""
    dates = pd.to_datetime(df["日期"], errors="coerce").dropna().dt.date
    if dates.empty:
        return ""
    available = set(dates)
    first_date = min(available)
    expected_date = latest_final_etf_trade_date(market_now)
    expected: list[object] = []
    cursor = expected_date
    for _ in range(max(int(sessions), 1)):
        if cursor >= first_date:
            expected.append(cursor)
        cursor = previous_trading_day(market, cursor)
    missing = sorted(day for day in expected if day not in available)
    if not missing:
        return ""
    preview = "、".join(day.strftime("%Y-%m-%d") for day in missing[:5])
    suffix = "等" if len(missing) > 5 else ""
    return f"最近交易日存在{len(missing)}个缺口（{preview}{suffix}）；可能为停牌，请核对。"


def _adjusted_history_has_overlap_changes(
    old_df: pd.DataFrame | None,
    new_df: pd.DataFrame | None,
    *,
    date_column: str = "日期",
) -> bool:
    if old_df is None or old_df.empty or new_df is None or new_df.empty:
        return False
    if date_column not in old_df.columns or date_column not in new_df.columns:
        return True

    old = old_df.copy()
    new = new_df.copy()
    old[date_column] = pd.to_datetime(old[date_column], errors="coerce").dt.normalize()
    new[date_column] = pd.to_datetime(new[date_column], errors="coerce").dt.normalize()
    old = old.dropna(subset=[date_column]).drop_duplicates(date_column, keep="first")
    new = new.dropna(subset=[date_column]).drop_duplicates(date_column, keep="last")
    overlap = old.merge(new, on=date_column, how="inner", suffixes=("_old", "_new"))
    if overlap.empty:
        return True

    compared = False
    for column in ("收盘价", "开盘价"):
        old_column = f"{column}_old"
        new_column = f"{column}_new"
        if old_column not in overlap.columns or new_column not in overlap.columns:
            continue
        compared = True
        old_values = pd.to_numeric(overlap[old_column], errors="coerce")
        new_values = pd.to_numeric(overlap[new_column], errors="coerce")
        both_missing = old_values.isna() & new_values.isna()
        equal = np.isclose(
            old_values.fillna(0.0),
            new_values.fillna(0.0),
            rtol=1e-9,
            atol=1e-9,
        ) | both_missing
        if not bool(equal.all()):
            return True
    return not compared


def _append_position_error(current: str, message: str) -> str:
    parts = [str(value).strip() for value in (current, message) if str(value).strip()]
    return "；".join(dict.fromkeys(parts))


def _prepare_fetched_etf_history(
    df: pd.DataFrame,
    *,
    adjust: str,
    market_now: datetime | None,
    allow_unfinished_session: bool,
) -> pd.DataFrame:
    result = stamp_fund_history_metadata(df, adjust)
    if allow_unfinished_session:
        return result
    filtered = filter_final_etf_rows(result, market_now=market_now)
    if filtered is None:
        return pd.DataFrame()
    filtered = stamp_fund_history_metadata(filtered, adjust)
    filtered["_final_close_confirmed"] = True
    return filtered


def _fetch_position_etf_history(
    *,
    symbol: str,
    base_code: str,
    api_key: str,
    count: int,
    adjust: str,
    market_now: datetime | None,
) -> pd.DataFrame:
    if base_code in ETF_AKSHARE_HISTORY_CODES:
        return _fetch_exchange_fund_close(
            symbol=symbol,
            count=int(count),
            adjust=adjust,
            market_now=market_now,
        )
    return fetch_tickflow_fund_close(
        symbol=symbol,
        api_key=api_key,
        count=int(count),
        adjust=adjust,
    )


def _merge_current_day_refresh(
    old_df: pd.DataFrame | None,
    new_df: pd.DataFrame,
    date_column: str,
) -> pd.DataFrame:
    merged = _merge_by_date(old_df, new_df, date_column)
    if new_df is None or new_df.empty or date_column not in new_df.columns:
        return merged

    today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
    refreshed = new_df.copy()
    refreshed[date_column] = pd.to_datetime(refreshed[date_column], errors="coerce")
    today_rows = refreshed[refreshed[date_column].dt.normalize() == today]
    if today_rows.empty:
        return merged

    merged_dates = pd.to_datetime(merged[date_column], errors="coerce")
    historical = merged[merged_dates.dt.normalize() != today]
    cached_today = merged[merged_dates.dt.normalize() == today]
    combined_today = (
        today_rows.sort_values(date_column)
        .drop_duplicates(date_column, keep="last")
        .set_index(date_column)
        .combine_first(cached_today.set_index(date_column))
        .reset_index()
    )
    return (
        pd.concat([historical, combined_today], ignore_index=True)
        .sort_values(date_column)
        .drop_duplicates(date_column, keep="last")
        .reset_index(drop=True)
    )


def _fetch_eastmoney_exchange_fund_close(
    *,
    symbol: str,
    count: int,
    adjust: str | None,
) -> pd.DataFrame:
    import akshare as ak

    adjustment = normalize_fund_adjustment(adjust)
    base_code = normalize_etf_base_code(symbol)
    end_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    calendar_days = max(int(count) * 2, 365)
    start_date = end_date - timedelta(days=calendar_days)
    adjust_value = {
        FUND_ADJUST_FORWARD_ADDITIVE: "qfq",
        FUND_ADJUST_BACKWARD_ADDITIVE: "hfq",
        FUND_ADJUST_NONE: "",
    }.get(adjustment)
    if adjust_value is None:
        raise ValueError("东方财富/AkShare不提供与TickFlow比例复权等价的口径。")
    raw = ak.fund_etf_hist_em(
        symbol=base_code,
        period="daily",
        start_date=start_date.strftime("%Y%m%d"),
        end_date=end_date.strftime("%Y%m%d"),
        adjust=adjust_value,
    )
    if raw is None or raw.empty:
        raise ValueError(f"东方财富未返回 {base_code} 的场内日线数据。")
    if not {"日期", "收盘"}.issubset(raw.columns):
        raise ValueError(f"东方财富返回列无法识别：{list(raw.columns)}")

    columns = ["日期", "收盘"]
    if "开盘" in raw.columns:
        columns.insert(1, "开盘")
    result = raw[columns].copy()
    result.columns = (
        ["日期", "开盘价", "收盘价"]
        if "开盘" in raw.columns
        else ["日期", "收盘价"]
    )
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce")
    if "开盘价" in result.columns:
        result["开盘价"] = pd.to_numeric(result["开盘价"], errors="coerce")
    result["收盘价"] = pd.to_numeric(result["收盘价"], errors="coerce")
    result = result.dropna(subset=["日期", "收盘价"])
    result = (
        result.sort_values("日期")
        .drop_duplicates("日期")
        .tail(int(count))
        .reset_index(drop=True)
    )
    result["symbol"] = symbol
    result["name"] = display_etf_name(base_code, symbol)
    return stamp_fund_history_metadata(result, adjustment)


def _sina_exchange_symbol(symbol: str) -> str:
    base_code = normalize_etf_base_code(symbol)
    exchange = "sh" if str(symbol).strip().upper().endswith(".SH") else "sz"
    return f"{exchange}{base_code}"


def _request_sina_realtime_snapshot(sina_symbol: str):
    url = f"https://hq.sinajs.cn/list={sina_symbol}"
    request_kwargs = {
        "headers": {
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0",
        },
        "timeout": SINA_REQUEST_TIMEOUT_SECONDS,
    }
    try:
        return requests.get(url, **request_kwargs)
    except requests.exceptions.ProxyError:
        with requests.Session() as session:
            session.trust_env = False
            return session.get(url, **request_kwargs)


def _fetch_sina_exchange_fund_quote(
    *,
    symbol: str,
    market_now: datetime | None = None,
) -> dict[str, object]:
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    base_code = normalize_etf_base_code(symbol)
    sina_symbol = _sina_exchange_symbol(symbol)
    response = _request_sina_realtime_snapshot(sina_symbol)
    response.raise_for_status()
    payload = response.content.decode("gb18030", errors="replace")
    match = re.search(
        rf'var\s+hq_str_{re.escape(sina_symbol)}="([^"]*)"',
        payload,
    )
    fields = match.group(1).split(",") if match else []
    if len(fields) < 32:
        raise ValueError(f"新浪财经未返回 {base_code} 的可识别实时快照。")

    latest_price = pd.to_numeric(fields[3], errors="coerce")
    previous_close = pd.to_numeric(fields[2], errors="coerce")
    quote_timestamp = pd.to_datetime(
        f"{fields[30]} {fields[31]}",
        errors="coerce",
    )
    if (
        pd.isna(latest_price)
        or float(latest_price) <= 0
        or pd.isna(quote_timestamp)
    ):
        raise ValueError(f"新浪财经返回的 {base_code} 实时快照无效。")
    if quote_timestamp.date() != market_now.date():
        raise ValueError(
            f"新浪财经 {base_code} 实时快照日期为 {quote_timestamp:%Y-%m-%d}，"
            f"当前日期为 {market_now:%Y-%m-%d}。"
        )

    quote_time = quote_timestamp.tz_localize("Asia/Shanghai").to_pydatetime()
    change_pct = None
    if not pd.isna(previous_close) and float(previous_close) > 0:
        change_pct = (float(latest_price) / float(previous_close) - 1) * 100
    return {
        "symbol": symbol.strip().upper(),
        "price": float(latest_price),
        "previous_close": (
            None if pd.isna(previous_close) else float(previous_close)
        ),
        "change_pct": change_pct,
        "quote_time": quote_time,
    }


def _ensure_sina_adjustment_is_identity(sina_symbol: str, adjust: str | None) -> None:
    adjustment = normalize_fund_adjustment(adjust)
    if adjustment == FUND_ADJUST_NONE:
        return
    if adjustment in {FUND_ADJUST_FORWARD_RATIO, FUND_ADJUST_BACKWARD_RATIO}:
        raise ValueError("新浪备用源不提供与TickFlow比例复权等价的口径。")

    adjustment_name = (
        "qfq" if adjustment == FUND_ADJUST_FORWARD_ADDITIVE else "hfq"
    )
    response = requests.get(
        f"https://finance.sina.com.cn/realstock/company/{sina_symbol}/{adjustment_name}.js",
        timeout=SINA_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    _prefix, separator, remainder = response.text.partition("=")
    payload_text = remainder.splitlines()[0].strip().rstrip(";") if separator else ""
    if not payload_text:
        raise ValueError("新浪备用源未返回可识别的复权因子。")
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("新浪备用源复权因子格式无法识别。") from exc

    factor_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(factor_rows, list) or not factor_rows:
        raise ValueError("新浪备用源未返回复权因子。")
    for row in factor_rows:
        try:
            factor = float(row.get("f", 1))
            split = float(row.get("s", 1))
            dividend = float(row.get("u", 0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("新浪备用源复权因子字段无法识别。") from exc
        if (
            abs(factor - 1.0) > 1e-12
            or abs(split - 1.0) > 1e-12
            or abs(dividend) > 1e-12
        ):
            raise ValueError("新浪备用源存在复权事件，不能用未复权日线替代当前复权口径。")


def _fetch_sina_exchange_fund_close(
    *,
    symbol: str,
    count: int,
    adjust: str | None,
) -> pd.DataFrame:
    from akshare.stock.cons import hk_js_decode
    import py_mini_racer

    base_code = normalize_etf_base_code(symbol)
    sina_symbol = _sina_exchange_symbol(symbol)
    _ensure_sina_adjustment_is_identity(sina_symbol, adjust)

    response = requests.get(
        f"https://finance.sina.com.cn/realstock/company/{sina_symbol}/hisdata_klc2/klc_kl.js",
        timeout=SINA_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    _prefix, separator, remainder = response.text.partition("=")
    encoded = remainder.split(";", 1)[0].replace('"', "").strip() if separator else ""
    if not encoded:
        raise ValueError(f"新浪财经未返回 {base_code} 的场内日线数据。")

    decoder = py_mini_racer.MiniRacer()
    decoder.eval(hk_js_decode)
    decoded_rows = decoder.call("d", encoded)
    raw = pd.DataFrame(decoded_rows)
    required_columns = {"date", "close"}
    if raw.empty or not required_columns.issubset(raw.columns):
        raise ValueError(f"新浪财经返回列无法识别：{list(raw.columns)}")

    columns = ["date", "close"]
    if "open" in raw.columns:
        columns.insert(1, "open")
    result = raw[columns].copy()
    result.columns = (
        ["日期", "开盘价", "收盘价"]
        if "open" in raw.columns
        else ["日期", "收盘价"]
    )
    result["日期"] = pd.to_datetime(
        result["日期"], errors="coerce", utc=True
    ).dt.tz_localize(None)
    if "开盘价" in result.columns:
        result["开盘价"] = pd.to_numeric(result["开盘价"], errors="coerce")
    result["收盘价"] = pd.to_numeric(result["收盘价"], errors="coerce")
    today = pd.Timestamp(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    result = result.dropna(subset=["日期", "收盘价"])
    result = (
        result[result["日期"] <= today]
        .sort_values("日期")
        .drop_duplicates("日期")
        .tail(int(count))
        .reset_index(drop=True)
    )
    if result.empty:
        raise ValueError(f"新浪财经未返回 {base_code} 的有效场内日线数据。")
    result["symbol"] = symbol
    result["name"] = display_etf_name(base_code, symbol)
    return stamp_fund_history_metadata(result, adjust)


def _fetch_sina_exchange_fund_final_close(
    *,
    symbol: str,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if not etf_final_close_ready(market_now):
        raise ValueError("A股当日正式收盘价需在交易日15:05后确认。")

    base_code = normalize_etf_base_code(symbol)
    sina_symbol = _sina_exchange_symbol(symbol)
    response = _request_sina_realtime_snapshot(sina_symbol)
    response.raise_for_status()
    payload = response.content.decode("gb18030", errors="replace")
    match = re.search(
        rf'var\s+hq_str_{re.escape(sina_symbol)}="([^"]*)"',
        payload,
    )
    fields = match.group(1).split(",") if match else []
    if len(fields) < 32:
        raise ValueError(f"新浪财经未返回 {base_code} 的可识别收盘快照。")

    open_price = pd.to_numeric(fields[1], errors="coerce")
    close_price = pd.to_numeric(fields[3], errors="coerce")
    quote_timestamp = pd.to_datetime(
        f"{fields[30]} {fields[31]}",
        errors="coerce",
    )
    if pd.isna(close_price) or float(close_price) <= 0 or pd.isna(quote_timestamp):
        raise ValueError(f"新浪财经返回的 {base_code} 收盘快照无效。")

    target_date = latest_final_etf_trade_date(market_now)
    market = get_market_window("A股")
    session_close = market.sessions[-1][1] if market is not None else datetime_time(15, 0)
    if quote_timestamp.date() != target_date:
        raise ValueError(
            f"新浪财经 {base_code} 收盘快照日期为 {quote_timestamp:%Y-%m-%d}，"
            f"最新完成交易日应为 {target_date:%Y-%m-%d}。"
        )
    if quote_timestamp.time() < session_close:
        raise ValueError(
            f"新浪财经 {base_code} 快照时间为 {quote_timestamp:%H:%M:%S}，尚未收盘。"
        )

    return pd.DataFrame(
        {
            "日期": [quote_timestamp.normalize()],
            "开盘价": [None if pd.isna(open_price) else float(open_price)],
            "收盘价": [float(close_price)],
            "symbol": [symbol],
            "name": [display_etf_name(base_code, symbol)],
            "_final_close_confirmed": [True],
        }
    )


def _append_sina_final_close(
    history: pd.DataFrame,
    *,
    symbol: str,
    adjust: str | None,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    result = history.copy()
    result.attrs.update(history.attrs)
    if result.empty or "日期" not in result.columns or not etf_final_close_ready(market_now):
        return result

    target_date = latest_final_etf_trade_date(market_now)
    history_dates = pd.to_datetime(result["日期"], errors="coerce")
    if history_dates.dt.date.eq(target_date).any():
        return result

    adjustment = normalize_fund_adjustment(adjust)
    if adjustment == FUND_ADJUST_BACKWARD_ADDITIVE:
        warning = "新浪当日收盘快照为原始价格，不能追加到后复权正式历史。"
        result.attrs["position_history_warning"] = warning
        logger.warning("%s %s", symbol, warning)
        return result

    try:
        final_close = _fetch_sina_exchange_fund_final_close(
            symbol=symbol,
            market_now=market_now,
        )
    except Exception as exc:
        warning = f"新浪当日收盘快照获取失败：{exc}"
        result.attrs["position_history_warning"] = warning
        logger.warning("%s %s", symbol, warning)
        return result

    history_attrs = dict(result.attrs)
    result = _merge_by_date(result, final_close, "日期")
    result.attrs.update(history_attrs)
    history_source = str(result.attrs.get("position_history_source") or "").strip()
    if history_source and "新浪收盘快照" not in history_source:
        result.attrs["position_history_source"] = f"{history_source} + 新浪收盘快照"
    return result


def _fetch_exchange_fund_close(
    *,
    symbol: str,
    count: int,
    adjust: str | None,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    adjustment = normalize_fund_adjustment(adjust)
    if adjustment in {FUND_ADJUST_FORWARD_RATIO, FUND_ADJUST_BACKWARD_RATIO}:
        raise ValueError("161128的东方财富/AkShare正式历史不支持比例复权。")
    eastmoney_error = ""
    try:
        result = _fetch_eastmoney_exchange_fund_close(
            symbol=symbol,
            count=count,
            adjust=adjustment,
        )
        result.attrs["position_history_source"] = "东方财富/AkShare"
        return _append_sina_final_close(
            result,
            symbol=symbol,
            adjust=adjustment,
            market_now=market_now,
        )
    except Exception as exc:
        eastmoney_error = str(exc)
        logger.warning("%s 东方财富场内日线获取失败，尝试新浪备用源：%s", symbol, exc)

    try:
        result = _fetch_sina_exchange_fund_close(
            symbol=symbol,
            count=count,
            adjust=adjustment,
        )
        result.attrs["position_history_source"] = "新浪财经备用源"
        return _append_sina_final_close(
            result,
            symbol=symbol,
            adjust=adjustment,
            market_now=market_now,
        )
    except Exception as sina_exc:
        raise RuntimeError(
            f"东方财富场内日线获取失败：{eastmoney_error}；新浪备用源也失败：{sina_exc}"
        ) from sina_exc


def load_or_fetch_etf(
    code: str,
    *,
    api_key: str = "",
    count: int = 5000,
    adjust: str | None = FUND_ADJUST_FORWARD_ADDITIVE,
    ma_periods: list[int] | tuple[int, ...] = (20, 60, 120, 250),
    rsi_period: int = 14,
    base_date: str = "2024-09-24",
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
    allow_unfinished_session: bool = False,
    market_now: datetime | None = None,
) -> PositionItem:
    raw_code = code.strip()
    base_code = normalize_etf_base_code(raw_code)
    strategy = ETF_TIMING_STRATEGIES.get(base_code)
    try:
        adjustment = normalize_fund_adjustment(adjust)
        symbol = infer_tickflow_symbol(raw_code)
    except Exception as exc:
        return PositionItem("ETF", raw_code, display_etf_name(base_code, raw_code), "失败", error=str(exc))

    use_akshare_history = base_code in ETF_AKSHARE_HISTORY_CODES
    cache_source = "akshare" if use_akshare_history else "tickflow"
    fetch_source = "东方财富/AkShare" if use_akshare_history else "TickFlow"
    cache_symbol = build_fund_cache_symbol("fund_close", symbol, adjustment)
    period = f"{int(count)}_1d"
    cached_df, cache_meta = _load_dataset_if_ready(
        cache_symbol,
        cache_source,
        "fund_close_raw",
        period=period,
    )
    cached_df = filter_final_etf_rows(
        cached_df,
        market_now=market_now,
        require_current_confirmation=True,
    )
    minimum_rows = int(strategy[0]) if strategy is not None else 2
    cache_validation_error = _fund_history_validation_error(
        cached_df,
        adjust=adjustment,
        min_rows=minimum_rows,
        market_now=market_now,
    )
    cached_history_valid = not cache_validation_error
    cache_is_current = etf_cache_has_latest_final_close(
        cached_df,
        date_column="日期",
        market_now=market_now,
    )
    should_refresh = force_refresh or (
        allow_fetch and (not cache_is_current or not cached_history_valid)
    )
    used_cache = cached_df is not None and not should_refresh
    source_df = cached_df.copy() if used_cache else None
    source = "本地缓存" if used_cache else fetch_source
    status = "缓存"
    error = cache_validation_error if cached_df is not None and not cached_history_valid else ""
    formal_history_valid = bool(cached_df is not None and cached_history_valid)

    if source_df is None:
        if not allow_fetch:
            if cached_df is None:
                return _missing_item("ETF", raw_code, display_etf_name(base_code, symbol))
            source_df = cached_df.copy()
            source = "本地缓存（待校验）"
            status = "缓存待校验"
            formal_history_valid = False
        try:
            if source_df is None and cached_df is not None and cached_history_valid:
                incremental_count = min(max(120, int(count) // 20), int(count))
                latest_df = _fetch_position_etf_history(
                    symbol=symbol,
                    base_code=base_code,
                    api_key=api_key,
                    count=incremental_count,
                    adjust=adjustment,
                    market_now=market_now,
                )
                if use_akshare_history:
                    source = str(latest_df.attrs.get("position_history_source") or fetch_source)
                    error = str(latest_df.attrs.get("position_history_warning") or "")
                latest_df = _prepare_fetched_etf_history(
                    latest_df,
                    adjust=adjustment,
                    market_now=market_now,
                    allow_unfinished_session=allow_unfinished_session,
                )
                rebuild_required = bool(
                    adjustment != FUND_ADJUST_NONE
                    and _adjusted_history_has_overlap_changes(cached_df, latest_df)
                )
                if rebuild_required:
                    try:
                        rebuilt_df = _fetch_position_etf_history(
                            symbol=symbol,
                            base_code=base_code,
                            api_key=api_key,
                            count=int(count),
                            adjust=adjustment,
                            market_now=market_now,
                        )
                        if use_akshare_history:
                            source = str(
                                rebuilt_df.attrs.get("position_history_source") or fetch_source
                            )
                            error = _append_position_error(
                                error,
                                str(rebuilt_df.attrs.get("position_history_warning") or ""),
                            )
                        source_df = _prepare_fetched_etf_history(
                            rebuilt_df,
                            adjust=adjustment,
                            market_now=market_now,
                            allow_unfinished_session=allow_unfinished_session,
                        )
                        validation_error = _fund_history_validation_error(
                            source_df,
                            adjust=adjustment,
                            min_rows=minimum_rows,
                            market_now=market_now,
                            require_latest=not allow_unfinished_session,
                        )
                        if validation_error:
                            raise ValueError(validation_error)
                        status = "已重建"
                        formal_history_valid = not allow_unfinished_session
                    except Exception as rebuild_exc:
                        source_df = cached_df.copy()
                        source = "本地缓存（复权重建失败）"
                        status = "缓存待校验"
                        formal_history_valid = False
                        error = _append_position_error(
                            error,
                            f"复权历史发生回溯变化，但全量重建失败：{rebuild_exc}",
                        )
                else:
                    source_df = stamp_fund_history_metadata(
                        _merge_by_date(cached_df, latest_df, "日期"),
                        adjustment,
                    )
                    validation_error = _fund_history_validation_error(
                        source_df,
                        adjust=adjustment,
                        min_rows=minimum_rows,
                        market_now=market_now,
                        require_latest=not allow_unfinished_session,
                    )
                    if validation_error:
                        raise ValueError(validation_error)
                    status = "已增量更新"
                    formal_history_valid = not allow_unfinished_session
            elif source_df is None:
                source_df = _fetch_position_etf_history(
                    symbol=symbol,
                    base_code=base_code,
                    api_key=api_key,
                    count=int(count),
                    adjust=adjustment,
                    market_now=market_now,
                )
                if use_akshare_history:
                    source = str(source_df.attrs.get("position_history_source") or fetch_source)
                    error = str(source_df.attrs.get("position_history_warning") or "")
                source_df = _prepare_fetched_etf_history(
                    source_df,
                    adjust=adjustment,
                    market_now=market_now,
                    allow_unfinished_session=allow_unfinished_session,
                )
                validation_error = _fund_history_validation_error(
                    source_df,
                    adjust=adjustment,
                    min_rows=minimum_rows,
                    market_now=market_now,
                    require_latest=not allow_unfinished_session,
                )
                if validation_error:
                    raise ValueError(validation_error)
                status = "已更新"
                formal_history_valid = not allow_unfinished_session

            if (
                formal_history_valid
                and save_to_cache
                and not allow_unfinished_session
                and status in {"已更新", "已增量更新", "已重建"}
            ):
                save_dataset(
                    symbol=cache_symbol,
                    name=f"{symbol} 场内基金/股票原始收盘价",
                    source=cache_source,
                    data_type="fund_close_raw",
                    period=period,
                    df=source_df,
                )
        except Exception as exc:
            if cached_df is None:
                return PositionItem(
                    "ETF",
                    raw_code,
                    display_etf_name(base_code, symbol),
                    "失败",
                    source=fetch_source,
                    error=str(exc),
                    formal_history_valid=False,
                )
            source_df = cached_df.copy()
            source = "本地缓存（刷新失败）"
            status = "缓存" if cached_history_valid else "缓存待校验"
            formal_history_valid = cached_history_valid
            error = _append_position_error(error, str(exc))

    gap_warning = _recent_etf_gap_warning(source_df, market_now=market_now)
    error = _append_position_error(error, gap_warning)

    try:
        analysis_source_df = source_df.drop(
            columns=[column for column in source_df.columns if str(column).startswith("_")],
            errors="ignore",
        )
        fund_name, nav_df = normalize_nav_dataframe(analysis_source_df, fallback_name=f"{symbol} ETF")
        effective_ma_periods = set(int(period) for period in ma_periods)
        if strategy is not None:
            effective_ma_periods.add(int(strategy[0]))
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=tuple(sorted(effective_ma_periods)),
            rsi_period=int(rsi_period),
            base_date=base_date,
        )
    except Exception as exc:
        return PositionItem("ETF", raw_code, symbol, "失败", source=source, error=str(exc))

    summary = result.summary
    latest_row = result.dataframe.iloc[-1]
    metrics = {
        "最新价": _round_metric(summary.get("最新价格"), 4),
        "日涨跌(%)": _round_metric(latest_row.get("daily_return_pct")),
        "20日涨跌(%)": _round_metric(summary.get("20日涨幅(%)")),
        "60日涨跌(%)": _round_metric(summary.get("60日涨幅(%)")),
        "MA20偏离(%)": _round_metric(summary.get("MA20偏离(%)")),
        "价格百分位": _round_metric(summary.get("价格百分位")),
        "年化波动(%)": _round_metric(summary.get("年化波动率(%)")),
    }
    if strategy is not None and formal_history_valid:
        timing_snapshot = calculate_etf_timing_snapshot(
            result.dataframe,
            ma_period=int(strategy[0]),
            threshold_pct=float(strategy[1]),
        )
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
        category="ETF",
        code=symbol,
        name=display_etf_name(base_code, str(summary.get("基金名称") or fund_name)),
        status=status,
        source=source,
        latest_date=str(summary.get("最新日期") or ""),
        cache_time=(
            _current_cache_time_text()
            if status in {"已更新", "已增量更新", "已重建"} and save_to_cache
            else format_cache_time(cache_meta.get("last_update_time") if cache_meta else "")
        ),
        metrics=metrics,
        dataframe=result.dataframe,
        error=error,
        formal_history_valid=formal_history_valid,
    )
