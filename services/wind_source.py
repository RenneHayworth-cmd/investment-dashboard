"""万得 Wind MCP backup adapter.

Plain HTTPS JSON-RPC ``tools/call`` requests to Wind's stateless streamable-HTTP
MCP servers. Callers use it only after their primary sources failed or lagged,
and keep their own append-only / transient-quote rules; nothing here writes a
cache. Wind resolves unknown codes fuzzily (``SA2701.CZC`` came back as the US
stock ``SA.N``), so every returned row must echo the exact requested code.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import logging
import os
import re
from threading import Lock

import pandas as pd
import requests

logger = logging.getLogger(__name__)

WIND_API_KEY_ENV = "WIND_API_KEY"
WIND_MCP_URL = "https://mcp.wind.com.cn/vserver_{server}/mcp/"
WIND_BATCH_LIMIT = 50
WIND_REQUEST_TIMEOUT_SECONDS = 20
# Network, auth and quota failures pause Wind for the process so a broken backup
# does not add a timeout (or spend credits) on every later fallback attempt.
WIND_FAILURE_COOLDOWN = timedelta(minutes=5)
WIND_SOURCE_LABEL = "万得Wind"

_QUOTE_FIELDS = "最新交易日,交易时间,最新成交价,前收盘价,今日开盘价,今日最高价,今日最低价,成交量,成交额"
_UNKNOWN_CODE_PATTERN = re.compile(r"未识别到有效的金融标的[:：]\s*([^\s;；,，]+)")

_cooldown_lock = Lock()
_cooldown_until: datetime | None = None
_cooldown_reason = ""


class WindSourceError(RuntimeError):
    """A Wind request failed; ``unknown_code`` is set when Wind rejected one code."""

    def __init__(self, message: str, *, unknown_code: str | None = None):
        super().__init__(message)
        self.unknown_code = unknown_code


def wind_api_key() -> str:
    return str(os.getenv(WIND_API_KEY_ENV) or "").strip()


def _cooldown_error() -> WindSourceError | None:
    with _cooldown_lock:
        if _cooldown_until is None or datetime.now() >= _cooldown_until:
            return None
        return WindSourceError(
            f"{WIND_SOURCE_LABEL}暂停调用至{_cooldown_until:%H:%M:%S}（上次失败：{_cooldown_reason}）"
        )


def _start_cooldown(reason: str) -> None:
    global _cooldown_until, _cooldown_reason
    with _cooldown_lock:
        _cooldown_until = datetime.now() + WIND_FAILURE_COOLDOWN
        _cooldown_reason = reason[:120]


def reset_wind_cooldown() -> None:
    global _cooldown_until, _cooldown_reason
    with _cooldown_lock:
        _cooldown_until = None
        _cooldown_reason = ""


def wind_available() -> bool:
    return bool(wind_api_key()) and _cooldown_error() is None


def _decode_rpc_body(response: requests.Response) -> dict:
    text = response.content.decode("utf-8", errors="replace")
    if "event-stream" in str(response.headers.get("content-type") or ""):
        text = "".join(line[5:].lstrip() for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


def _call_tool(server: str, tool: str, arguments: dict) -> dict:
    """Return the tool's ``data`` object; raise ``WindSourceError`` otherwise."""
    key = wind_api_key()
    if not key:
        raise WindSourceError(f"未配置 {WIND_API_KEY_ENV}，{WIND_SOURCE_LABEL}备用源不可用")
    cooling = _cooldown_error()
    if cooling is not None:
        raise cooling
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    last_error: Exception | None = None
    body = None
    # Wind is a domestic endpoint: direct first, then the environment proxy.
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        try:
            response = session.post(
                WIND_MCP_URL.format(server=server), json=payload, headers=headers,
                timeout=(5, WIND_REQUEST_TIMEOUT_SECONDS),
            )
            if response.status_code in {401, 403}:
                _start_cooldown(f"HTTP {response.status_code} 认证失败")
                raise WindSourceError(f"{WIND_SOURCE_LABEL}认证失败（HTTP {response.status_code}），请检查 {WIND_API_KEY_ENV}")
            if response.status_code == 429:
                _start_cooldown("HTTP 429 请求过于频繁")
                raise WindSourceError(f"{WIND_SOURCE_LABEL}请求过于频繁（HTTP 429）")
            response.raise_for_status()
            body = _decode_rpc_body(response)
            break
        except WindSourceError:
            raise
        except Exception as exc:
            last_error = exc
        finally:
            session.close()
    if body is None:
        reason = f"{type(last_error).__name__}: {str(last_error)[:120]}"
        _start_cooldown(reason)
        raise WindSourceError(f"{WIND_SOURCE_LABEL}请求失败：{reason}") from last_error
    if body.get("error"):
        error = body["error"]
        raise WindSourceError(f"{WIND_SOURCE_LABEL}接口错误：{error.get('message') if isinstance(error, dict) else error}")
    result = body.get("result") or {}
    text = "\n".join(
        str(item.get("text") or "") for item in result.get("content") or [] if isinstance(item, dict)
    ).strip()
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or result.get("isError"):
        message = text or "返回为空"
        unknown = _UNKNOWN_CODE_PATTERN.search(message)
        raise WindSourceError(
            f"{WIND_SOURCE_LABEL}：{message[:160]}",
            unknown_code=unknown.group(1).strip().upper() if unknown else None,
        )
    if parsed.get("error"):
        raise WindSourceError(f"{WIND_SOURCE_LABEL}：{str(parsed['error'])[:160]}")
    data = parsed.get("data")
    if not isinstance(data, dict):
        raise WindSourceError(f"{WIND_SOURCE_LABEL}未返回数据表")
    return data


def _table(data: dict) -> pd.DataFrame:
    columns = [str(column.get("name")) for column in data.get("columns") or [] if isinstance(column, dict)]
    rows = data.get("rows") or []
    if not columns or not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame([list(row)[: len(columns)] for row in rows], columns=columns)


def _number(series: pd.Series | None) -> pd.Series | None:
    return None if series is None else pd.to_numeric(series, errors="coerce")


def fetch_wind_quotes(server: str, codes: list[str], *, indexes: str = _QUOTE_FIELDS) -> pd.DataFrame:
    """Latest snapshot for exact Wind codes, batched 50 at a time.

    Columns: wind_code, trade_date (date), quote_time (tz-aware Timestamp), price,
    previous_close, open, high, low, volume, amount. A code Wind does not
    recognise is dropped from its batch and the rest retried; a row whose
    ``Wind代码`` differs from every requested code is discarded.
    """
    wanted = list(dict.fromkeys(str(code).strip().upper() for code in codes if str(code).strip()))
    frames = []
    for start in range(0, len(wanted), WIND_BATCH_LIMIT):
        batch = wanted[start:start + WIND_BATCH_LIMIT]
        while batch:
            try:
                data = _call_tool(server, f"get_{server.split('_')[0]}_price_indicators",
                                  {"windcode": ",".join(batch), "indexes": indexes})
            except WindSourceError as exc:
                if exc.unknown_code and exc.unknown_code in batch:
                    logger.warning("%s不识别代码 %s，已从批次中剔除", WIND_SOURCE_LABEL, exc.unknown_code)
                    batch = [code for code in batch if code != exc.unknown_code]
                    continue
                raise
            frames.append(_table(data))
            break
    if not frames:
        return pd.DataFrame(columns=["wind_code", "trade_date", "quote_time", "price"])
    raw = pd.concat(frames, ignore_index=True)
    if "Wind代码" not in raw.columns:
        raise WindSourceError(f"{WIND_SOURCE_LABEL}行情缺少Wind代码列")
    result = pd.DataFrame({"wind_code": raw["Wind代码"].astype(str).str.strip().str.upper()})
    result["trade_date"] = pd.to_datetime(raw.get("最新交易日"), format="%Y%m%d", errors="coerce").dt.date
    result["quote_time"] = [
        pd.Timestamp(value) if pd.notna(pd.to_datetime(value, errors="coerce")) else pd.NaT
        for value in raw.get("交易时间", pd.Series([None] * len(raw)))
    ]
    for target, source in (("price", "最新成交价"), ("previous_close", "前收盘价"), ("open", "今日开盘价"),
                           ("high", "今日最高价"), ("low", "今日最低价"), ("volume", "成交量"), ("amount", "成交额")):
        result[target] = _number(raw[source]) if source in raw.columns else pd.NA
    result = result.loc[result["wind_code"].isin(set(wanted))]
    return result.drop_duplicates("wind_code", keep="first").reset_index(drop=True)


def fetch_wind_daily_bars(
    server: str,
    code: str,
    begin_date: date | str,
    end_date: date | str,
    *,
    aftype: str = "2",
) -> pd.DataFrame:
    """Daily bars (default unadjusted, ``aftype=2``): date, open, close, high, low, volume."""
    begin = pd.Timestamp(begin_date).strftime("%Y-%m-%d")
    end = pd.Timestamp(end_date).strftime("%Y-%m-%d")
    data = _call_tool(server, f"get_{server.split('_')[0]}_kline", {
        "windcode": str(code).strip().upper(), "begin_date": begin, "end_date": end,
        "period": "1d", "aftype": str(aftype),
    })
    raw = _table(data)
    if raw.empty or "TIME" not in raw.columns or "MATCH" not in raw.columns:
        raise WindSourceError(f"{WIND_SOURCE_LABEL}未返回 {code} 的日线")
    # Wind stamps every daily bar at local midnight (+08:00) with the bar's own
    # session date, including foreign indexes; keep that calendar date as-is.
    result = pd.DataFrame({"date": pd.to_datetime(raw["TIME"].astype(str).str[:10], errors="coerce")})
    for target, source in (("open", "OPEN"), ("close", "MATCH"), ("high", "HIGH"), ("low", "LOW"), ("volume", "VOLUME")):
        result[target] = _number(raw[source]) if source in raw.columns else pd.NA
    result = result.dropna(subset=["date", "close"])
    result = result.loc[result["close"] > 0]
    if result.empty:
        raise WindSourceError(f"{WIND_SOURCE_LABEL}返回的 {code} 日线无有效收盘价")
    return result.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def wind_stock_code(code: str) -> str:
    """Six-digit A-share code to its Wind code (SH / SZ / BJ)."""
    digits = str(code).strip().split(".")[0].zfill(6)
    if digits.startswith(("6", "900")):
        return f"{digits}.SH"
    if digits.startswith(("4", "8", "92")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


_WIND_FUTURES_EXCHANGES = {"SHF": "SHF", "DCE": "DCE", "ZCE": "CZC", "GFE": "GFE", "CFX": "CFE", "INE": "INE"}


def wind_futures_contract_code(contract: str) -> str | None:
    """``I2701`` -> ``I2701.DCE``; Zhengzhou contracts use Wind's 3-digit month (``SA701.CZC``)."""
    from services.futures_spread import FUTURES_EXCHANGES

    matched = re.fullmatch(r"([A-Za-z]+)(\d{3,4})", str(contract).strip().split(".")[0])
    if matched is None:
        return None
    prefix, month = matched.group(1).upper(), matched.group(2)
    exchange = "INE" if prefix in {"SC", "LU", "NR", "BC"} else FUTURES_EXCHANGES.get(prefix)
    wind_exchange = _WIND_FUTURES_EXCHANGES.get(str(exchange or ""))
    if wind_exchange is None:
        return None
    if wind_exchange == "CZC" and len(month) == 4:
        month = month[1:]
    return f"{prefix}{month}.{wind_exchange}"


__all__ = [
    "WIND_API_KEY_ENV", "WIND_SOURCE_LABEL", "WindSourceError", "wind_api_key", "wind_available",
    "reset_wind_cooldown", "fetch_wind_quotes", "fetch_wind_daily_bars", "wind_stock_code",
    "wind_futures_contract_code",
]
