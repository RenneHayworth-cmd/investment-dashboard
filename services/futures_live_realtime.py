"""期货实盘盘中估算：按持仓合约取实时价，相对最近正式结算价估算当日盯市盈亏。

报价只在页面会话内临时使用，不写缓存、不进入正式收益曲线、日历或策略分析。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from services.market_calendar import get_market_window, is_market_holiday, is_market_trading_day

MARKET_TZ = ZoneInfo("Asia/Shanghai")
QUOTE_INTERVAL_SECONDS = 110
FAILED_RETRY_SECONDS = 600

CFFEX_PRODUCTS = {"IF", "IH", "IC", "IM", "T", "TF", "TS", "TL", "IO", "HO", "MO"}
CFFEX_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
COMMODITY_DAY_SESSIONS = ((time(9, 0), time(10, 15)), (time(10, 30), time(11, 30)), (time(13, 30), time(15, 0)))
# 夜盘收盘时间：未列出的商品期货默认 23:00；列入 NO_NIGHT 的品种没有夜盘。
NIGHT_SESSION_END = {
    "AU": time(2, 30), "AG": time(2, 30), "SC": time(2, 30),
    "CU": time(1, 0), "AL": time(1, 0), "ZN": time(1, 0), "PB": time(1, 0), "NI": time(1, 0),
    "SN": time(1, 0), "SS": time(1, 0), "BC": time(1, 0), "AO": time(1, 0),
}
NO_NIGHT_PRODUCTS = {"AP", "CJ", "JD", "LH", "PK", "UR", "SF", "SM", "WH", "PM", "RI", "LR", "JR", "RS", "BB", "FB", "WR"}


def contract_product(contract: str) -> str:
    """合约品种代码（期权取标的品种），如 I2701P700 → I，MO2610-P-6000 → MO。"""
    match = re.match(r"[A-Za-z]+", str(contract or "").strip())
    return match.group(0).upper() if match else ""


def _night_enabled(evening_day: date) -> bool:
    """当晚有夜盘：当天是交易日，且下一个工作日不是节假日（节前夜盘停盘）。"""
    market = get_market_window("A股")
    if market is None:
        return False
    following = evening_day + timedelta(days=1)
    while following.weekday() >= 5:
        following += timedelta(days=1)
    return is_market_trading_day(market, datetime.combine(evening_day, time(21))) and not is_market_holiday(
        market, following
    )


def _next_trading_day(day: date) -> date:
    market = get_market_window("A股")
    day += timedelta(days=1)
    while day.weekday() >= 5 or (market is not None and is_market_holiday(market, day)):
        day += timedelta(days=1)
    return day


def session_trade_date(contract: str, market_now: datetime) -> date | None:
    """当前时刻若处于该合约交易时段，返回所属期货交易日；夜盘归属下一交易日。"""
    product = contract_product(contract)
    now = market_now.astimezone(MARKET_TZ) if market_now.tzinfo else market_now.replace(tzinfo=MARKET_TZ)
    clock = now.time()
    market = get_market_window("A股")
    if market is None:
        return None
    if product in CFFEX_PRODUCTS:
        sessions = CFFEX_SESSIONS
    else:
        sessions = COMMODITY_DAY_SESSIONS
        if product not in NO_NIGHT_PRODUCTS:
            night_end = NIGHT_SESSION_END.get(product, time(23, 0))
            if clock >= time(21, 0) and (night_end < time(21, 0) or clock <= night_end):
                return _next_trading_day(now.date()) if _night_enabled(now.date()) else None
            if night_end < time(21, 0) and clock <= night_end:
                evening = now.date() - timedelta(days=1)
                return _next_trading_day(evening) if _night_enabled(evening) else None
    if not is_market_trading_day(market, now):
        return None
    if any(start <= clock <= end for start, end in sessions):
        return now.date()
    return None


def quote_refresh_due(
    contracts: list[str],
    market_now: datetime,
    state: dict | None,
) -> bool:
    """任一持仓合约在交易时段内，且距上次成功取价满两分钟（失败最多每十分钟重试）。"""
    if not any(session_trade_date(contract, market_now) for contract in contracts):
        return False
    state = state or {}
    fetched_at = pd.to_datetime(state.get("fetched_at"), errors="coerce")
    if pd.isna(fetched_at):
        return True
    fetched_at = fetched_at.tz_localize(MARKET_TZ) if fetched_at.tzinfo is None else fetched_at
    age = (market_now - fetched_at.to_pydatetime()).total_seconds()
    if state.get("failures") and not state.get("succeeded"):
        # 本轮全部失败：最多每十分钟重试；部分成功时按两分钟节奏继续刷新。
        return age >= FAILED_RETRY_SECONDS
    return age >= QUOTE_INTERVAL_SECONDS


def _wind_quote(wind_code: str | None, label: str) -> dict[str, object]:
    from services.wind_source import WIND_SOURCE_LABEL, fetch_wind_quotes, wind_api_key

    if not wind_code or not wind_api_key():
        raise ValueError(f"{label}没有可用的万得Wind代码或未配置Key")
    frame = fetch_wind_quotes("index_data", [wind_code])
    if frame.empty:
        raise ValueError(f"万得Wind未返回 {wind_code} 的实时行情")
    row = frame.iloc[0]
    price = pd.to_numeric(row.get("price"), errors="coerce")
    if pd.isna(price) or float(price) <= 0:
        raise ValueError(f"万得Wind {wind_code} 实时价无效")
    stamp = pd.to_datetime(row.get("quote_time"), errors="coerce")
    return {
        "price": float(price),
        "quote_date": pd.Timestamp(row.get("trade_date")) if pd.notna(row.get("trade_date")) else pd.NaT,
        "quote_time": "" if pd.isna(stamp) else stamp.strftime("%H:%M:%S"),
        "source": WIND_SOURCE_LABEL,
    }


def _spot_frame_quote(spot: pd.DataFrame, source: str) -> dict[str, object]:
    if spot is None or spot.empty:
        raise ValueError("实时接口未返回数据")
    row = spot.iloc[0]
    price = next(
        (pd.to_numeric(row.get(column), errors="coerce") for column in ("current_price", "最新价", "price")
         if column in spot.columns),
        float("nan"),
    )
    if pd.isna(price) or float(price) <= 0:
        raise ValueError("实时价无效")
    quote_date = pd.to_datetime(row.get("quote_date"), errors="coerce") if "quote_date" in spot.columns else pd.NaT
    quote_time = str(row.get("quote_time") or "").strip() if "quote_time" in spot.columns else ""
    return {"price": float(price), "quote_date": quote_date, "quote_time": quote_time, "source": source}


def _fetch_futures_quote(contract: str) -> dict[str, object]:
    """期货合约实时价：新浪直连 → AkShare → 万得Wind，依次尝试直到拿到有效价格。"""
    from services.futures_spread import _fetch_futures_spot_from_sina_direct, _spot_market_for_contract
    from services.wind_source import wind_futures_contract_code

    def akshare_spot() -> pd.DataFrame:
        import akshare as ak

        return ak.futures_zh_spot(symbol=contract, market=_spot_market_for_contract(contract), adjust="0")

    errors = []
    for source, fetch in (("新浪期货实时", lambda: _fetch_futures_spot_from_sina_direct(contract)),
                          ("AkShare期货实时", akshare_spot)):
        try:
            return _spot_frame_quote(fetch(), source)
        except Exception as exc:
            errors.append(f"{source}：{exc}")
    try:
        return _wind_quote(wind_futures_contract_code(contract), contract)
    except Exception as exc:
        errors.append(f"万得Wind：{exc}")
    raise ValueError("；".join(errors))


def _fetch_option_quote(contract: str, market_now: datetime) -> dict[str, object]:
    """期权实时价：新浪期权链 → 万得Wind。"""
    from services.futures_options_analysis import append_option_spot_row
    from services.wind_source import wind_option_contract_code

    errors = []
    try:
        yesterday = pd.Timestamp(market_now.date()) - pd.Timedelta(days=1)
        seed = pd.DataFrame({"date": [yesterday], "close": [float("nan")]})
        result = append_option_spot_row(seed, contract, replace_current_day=True, market_now=market_now)
        today = result[
            pd.to_datetime(result["date"], errors="coerce").dt.normalize().eq(pd.Timestamp(market_now.date()))
        ]
        price = pd.to_numeric(today["close"], errors="coerce").dropna() if not today.empty else pd.Series(dtype=float)
        if price.empty or float(price.iloc[-1]) <= 0:
            raise ValueError("新浪期权链未返回当日价格")
        return {"price": float(price.iloc[-1]), "quote_date": pd.Timestamp(market_now.date()), "quote_time": "",
                "source": "新浪期权实时"}
    except Exception as exc:
        errors.append(f"新浪期权链：{exc}")
    try:
        return _wind_quote(wind_option_contract_code(contract), contract)
    except Exception as exc:
        errors.append(f"万得Wind：{exc}")
    raise ValueError("；".join(errors))


def fetch_futures_live_quotes(
    positions: pd.DataFrame,
    market_now: datetime,
) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    """为交易时段内的持仓合约取实时价；日期不属于当前交易日的报价视为无效。"""
    quotes: dict[str, dict[str, object]] = {}
    failures: dict[str, str] = {}
    if positions is None or positions.empty:
        return quotes, failures
    active = positions[pd.to_numeric(positions["estimated_quantity"], errors="coerce").fillna(0).gt(0)]
    for asset_type, contract in active[["asset_type", "contract"]].drop_duplicates().itertuples(index=False):
        trade_date = session_trade_date(contract, market_now)
        if trade_date is None:
            continue
        try:
            quote = _fetch_option_quote(contract, market_now) if asset_type == "期权" else _fetch_futures_quote(contract)
            quote_date = quote.get("quote_date")
            if pd.notna(quote_date) and pd.Timestamp(quote_date).date() not in {trade_date, market_now.date()}:
                raise ValueError(f"报价日期 {pd.Timestamp(quote_date).date()} 不是当前交易日")
            quotes[contract] = {**quote, "trade_date": trade_date, "fetched_at": market_now.isoformat(timespec="seconds")}
        except Exception as exc:
            failures[contract] = str(exc)
    return quotes, failures


def build_futures_intraday_preview(
    positions: pd.DataFrame,
    quotes: dict[str, dict[str, object]],
) -> pd.DataFrame:
    """当日盯市估算 =（实时价 − 最近结算价）× 持仓 × 合约乘数 × 方向。"""
    columns = [
        "asset_type", "contract", "side", "estimated_quantity", "multiplier", "base_price", "base_date",
        "live_price", "quote_time", "source", "intraday_pnl", "floating_pnl",
    ]
    if positions is None or positions.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for position in positions.to_dict("records"):
        quantity = int(pd.to_numeric(position.get("estimated_quantity"), errors="coerce") or 0)
        quote = quotes.get(str(position["contract"]))
        if quantity <= 0 or not quote:
            continue
        direction = 1 if position["side"] == "多" else -1
        multiplier = pd.to_numeric(position.get("multiplier"), errors="coerce")
        base = pd.to_numeric(position.get("valuation_price"), errors="coerce")
        average = pd.to_numeric(position.get("average_price"), errors="coerce")
        live = float(quote["price"])
        intraday = (live - base) * quantity * multiplier * direction if pd.notna(base) and pd.notna(multiplier) else None
        floating = (live - average) * quantity * multiplier * direction if pd.notna(average) and pd.notna(multiplier) else None
        rows.append(
            {
                "asset_type": position["asset_type"],
                "contract": position["contract"],
                "side": position["side"],
                "estimated_quantity": quantity,
                "multiplier": multiplier,
                "base_price": base,
                "base_date": position.get("valuation_date"),
                "live_price": live,
                "quote_time": quote.get("quote_time") or str(quote.get("fetched_at", ""))[11:19],
                "source": quote.get("source", ""),
                "intraday_pnl": intraday,
                "floating_pnl": floating,
            }
        )
    return pd.DataFrame(rows, columns=columns)


__all__ = [
    "build_futures_intraday_preview",
    "contract_product",
    "fetch_futures_live_quotes",
    "quote_refresh_due",
    "session_trade_date",
]
