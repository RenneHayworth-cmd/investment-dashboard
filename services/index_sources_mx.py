from __future__ import annotations

from datetime import datetime
import os
import re
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_frames import filter_completed_market_dates, normalize_akshare_index_df
from services.market_calendar import get_market_window, is_market_trading_day


MX_API_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
MX_HISTORY_MAX_BARS = 500


def _request_mx_data(query: str, *, api_key: str | None = None) -> list[dict]:
    import requests

    key = str(api_key or os.getenv("MX_APIKEY") or "").strip()
    if not key:
        raise RuntimeError("未配置 MX_APIKEY")
    last_error: Exception | None = None
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        try:
            response = session.post(
                MX_API_URL,
                headers={"Content-Type": "application/json", "apikey": key},
                json={"toolQuery": query},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != 0:
                raise RuntimeError(f"妙想接口返回错误：{payload.get('message') or payload.get('status')}")
            search_result = (
                ((payload.get("data") or {}).get("data") or {}).get("searchDataResultDTO")
                or {}
            )
            dto_list = search_result.get("dataTableDTOList") or []
            if not dto_list:
                raise RuntimeError("妙想接口未返回数据表")
            return [item for item in dto_list if isinstance(item, dict)]
        except Exception as exc:
            last_error = exc
        finally:
            session.close()
    raise RuntimeError(f"妙想金融数据请求失败：{last_error}") from last_error


def _number(value) -> float | None:
    text = ("" if value is None else str(value)).strip().replace(",", "")
    matched = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if matched is None:
        return None
    parsed = pd.to_numeric(matched.group(0), errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def _matching_dtos(dto_list: list[dict], expected_code: str) -> list[dict]:
    expected = expected_code.strip().upper()
    return [
        item
        for item in dto_list
        if str(item.get("code") or "").strip().upper() == expected
    ]


def get_mx_index_history(
    query_name: str,
    expected_code: str,
    market_name: str,
    *,
    days: int = 30,
    now: datetime | None = None,
    api_key: str | None = None,
) -> pd.DataFrame | None:
    """Fetch settled closes only when Miaoxiang resolves the exact expected entity."""
    bars = min(max(int(days), 30), MX_HISTORY_MAX_BARS)
    dto_list = _request_mx_data(
        f"{query_name}近{bars}个交易日的日期和收盘价",
        api_key=api_key,
    )
    candidates: list[pd.DataFrame] = []
    for dto in _matching_dtos(dto_list, expected_code):
        table = dto.get("table") or {}
        headers = table.get("headName") or []
        if len(headers) < 2:
            continue
        name_map = dto.get("nameMap") or {}
        order = dto.get("indicatorOrder") or []
        close_key = next((key for key in order if name_map.get(key) == "收盘价"), None)
        closes = table.get(close_key) if close_key is not None else None
        if not isinstance(closes, list) or len(closes) != len(headers):
            continue
        rows = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(
                    pd.Series(headers).astype(str).str.replace("(日)", "", regex=False),
                    errors="coerce",
                ),
                "close": [_number(value) for value in closes],
            }
        ).dropna(subset=["trade_date", "close"])
        if not rows.empty:
            candidates.append(rows)
    if not candidates:
        raise RuntimeError(f"妙想未返回预期实体 {expected_code} 的日线收盘价")
    normalized = normalize_akshare_index_df(pd.concat(candidates, ignore_index=True))
    normalized = (
        normalized.sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )
    if now is not None:
        market = get_market_window(market_name)
        if market is not None:
            market_now = now.astimezone(ZoneInfo(market.timezone))
            dates = pd.to_datetime(normalized["trade_date"], errors="coerce")
            normalized = normalized.loc[dates.dt.date <= market_now.date()].copy()
    return filter_completed_market_dates(normalized, market_name)


def fetch_mx_realtime_quote(
    query_name: str,
    expected_code: str,
    market_name: str,
    *,
    now: datetime | None = None,
    api_key: str | None = None,
) -> dict[str, object] | None:
    """Fetch one transient quote after exact entity and timestamp validation."""
    dto_list = _request_mx_data(
        f"{query_name}最新点位、涨跌幅、今开、最高、最低、昨收、更新时间",
        api_key=api_key,
    )
    values: dict[str, float] = {}
    quote_times: list[pd.Timestamp] = []
    for dto in _matching_dtos(dto_list, expected_code):
        table = dto.get("table") or {}
        headers = table.get("headName") or []
        if len(headers) != 1:
            continue
        quote_time = pd.to_datetime(headers[0], errors="coerce")
        if not pd.isna(quote_time):
            quote_times.append(quote_time)
        name_map = dto.get("nameMap") or {}
        for key in dto.get("indicatorOrder") or []:
            label = str(name_map.get(key) or "")
            raw_values = table.get(key) or []
            value = raw_values[0] if isinstance(raw_values, list) and raw_values else raw_values
            parsed = _number(value)
            if parsed is not None:
                values[label] = parsed
    price = values.get("最新价", values.get("最新点位"))
    if price is None or not quote_times:
        raise RuntimeError(f"妙想未返回预期实体 {expected_code} 的实时点位")
    market = get_market_window(market_name)
    timezone_name = market.timezone if market is not None else "Asia/Shanghai"
    quote_dt = max(quote_times).to_pydatetime()
    if quote_dt.tzinfo is None:
        quote_dt = quote_dt.replace(tzinfo=ZoneInfo(timezone_name))
    else:
        quote_dt = quote_dt.astimezone(ZoneInfo(timezone_name))
    market_now = now.astimezone(ZoneInfo(timezone_name)) if now else datetime.now(ZoneInfo(timezone_name))
    if market is not None and is_market_trading_day(market, market_now):
        if quote_dt.date() != market_now.date():
            return None
    change_pct = values.get("涨跌幅")
    previous_close = values.get("昨收", values.get("昨收价"))
    if previous_close is None and change_pct is not None and change_pct > -100:
        previous_close = price / (1 + change_pct / 100)
    return {
        "price": price,
        "previous_close": previous_close,
        "change_pct": change_pct,
        "quote_time": quote_dt,
        "source": "东方财富妙想",
    }


__all__ = ["get_mx_index_history", "fetch_mx_realtime_quote"]
