"""ETF实盘账本的不复权正式收盘价：场内基金走 TickFlow/备用链，可转债走新浪日线。

只为账本实际需要的区间取数：已清仓标的只需覆盖到清仓日，持仓标的覆盖到最新完成交易日。
可转债中签后、上市前没有行情，估值按面值 100 元补齐，不写入缓存。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import load_dataset, save_dataset
from services.fund_analysis import FUND_ADJUST_NONE
from services.market_calendar import get_market_window, is_market_holiday

MARKET_TZ = ZoneInfo("Asia/Shanghai")
BOND_SOURCE = "akshare_sina_bond"
BOND_DATA_TYPE = "convertible_bond_close"
BOND_FACE_VALUE = 100.0


def is_convertible_bond(symbol: object) -> bool:
    """沪市 11xxxx、深市 12xxxx 为可转债。"""
    text = str(symbol or "").strip()
    return len(text) == 6 and text.isdigit() and text[:2] in {"11", "12"}


def _bond_cache_key(symbol: str) -> str:
    return f"live_bond_close_v1_{symbol}"


def required_price_windows(trades: pd.DataFrame, target_date: date) -> dict[str, tuple[date, date]]:
    """每个标的需要正式收盘价的区间：首笔成交日至清仓日（未清仓则至目标日）。"""
    if trades is None or trades.empty:
        return {}
    data = trades.copy()
    data["symbol"] = data["symbol"].astype(str).str.extract(r"(\d{6})", expand=False)
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.date
    data["signed"] = pd.to_numeric(data["quantity"], errors="coerce").fillna(0).where(
        data["side"].eq("买入"), -pd.to_numeric(data["quantity"], errors="coerce").fillna(0)
    )
    data = data.dropna(subset=["symbol", "trade_date"]).assign(
        _time=data.get("trade_time", pd.Series("", index=data.index)).fillna("").astype(str)
    )
    windows: dict[str, tuple[date, date]] = {}
    order = ["trade_date", "_time", "id"] if "id" in data.columns else ["trade_date", "_time"]
    for symbol, group in data.sort_values(order).groupby("symbol"):
        holding = group["signed"].cumsum()
        start = group["trade_date"].min()
        end = target_date if holding.iloc[-1] > 0 else group["trade_date"].iloc[-1]
        windows[str(symbol)] = (start, max(start, end))
    return windows


def _a_share_sessions(start: date, end: date) -> list[date]:
    market = get_market_window("A股")
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5 and (market is None or not is_market_holiday(market, current)):
            days.append(current)
        current += timedelta(days=1)
    return days


def _fetch_bond_daily(symbol: str) -> pd.DataFrame:
    import akshare as ak

    prefix = "sh" if symbol.startswith("11") else "sz"
    raw = ak.bond_zh_hs_cov_daily(symbol=f"{prefix}{symbol}")
    if raw is None or raw.empty or "close" not in raw.columns:
        raise ValueError(f"新浪可转债日线未返回 {symbol} 的数据")
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["date"], errors="coerce").dt.strftime("%Y-%m-%d"),
            "price": pd.to_numeric(raw["close"], errors="coerce"),
        }
    )
    return frame.dropna().drop_duplicates("date", keep="last")


def load_bond_history(
    symbol: str,
    *,
    window: tuple[date, date],
    allow_fetch: bool,
    save_to_cache: bool,
    completed_date: date,
) -> tuple[pd.DataFrame, str | None]:
    """返回 date/price 两列；缓存按日期只追加，上市前日期按面值补齐。"""
    cached, _ = load_dataset(_bond_cache_key(symbol), BOND_SOURCE, BOND_DATA_TYPE)
    history = cached[["date", "price"]].copy() if cached is not None and not cached.empty else pd.DataFrame(
        columns=["date", "price"]
    )
    error = None
    start, end = window
    covered = not history.empty and str(history["date"].max()) >= end.isoformat()
    if allow_fetch and not covered:
        try:
            fetched = _fetch_bond_daily(symbol)
            fetched = fetched[fetched["date"] <= completed_date.isoformat()]
            new_rows = fetched[~fetched["date"].isin(set(history["date"].astype(str)))]
            if not new_rows.empty:
                history = (
                    pd.concat([history, new_rows], ignore_index=True)
                    .sort_values("date")
                    .reset_index(drop=True)
                )
                if save_to_cache:
                    save_dataset(_bond_cache_key(symbol), symbol, BOND_SOURCE, BOND_DATA_TYPE, history)
        except Exception as exc:  # 数据源失败时保留已有缓存
            error = f"新浪可转债日线：{exc}"
    history["date"] = history["date"].astype(str)
    listed = history["date"].min() if not history.empty else None
    # 上市首日之前（或尚未上市）按面值估值，只用于计算，不写入缓存。
    fill_end = min(end, date.fromisoformat(listed) - timedelta(days=1)) if listed else end
    face_days = [day.isoformat() for day in _a_share_sessions(start, fill_end)]
    face = pd.DataFrame({"date": face_days, "price": BOND_FACE_VALUE})
    combined = pd.concat([face, history], ignore_index=True).drop_duplicates("date", keep="last")
    return combined.sort_values("date").reset_index(drop=True), error


def fill_suspended_sessions(history: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """停牌日没有收盘价：在已有行情范围内按前一交易日收盘补齐，超出最新行情的日期不补。"""
    date_column = "date" if "date" in history.columns else "日期"
    price_column = "price" if "price" in history.columns else ("close" if "close" in history.columns else "收盘价")
    prices = pd.Series(
        pd.to_numeric(history[price_column], errors="coerce").to_numpy(),
        index=pd.to_datetime(history[date_column], errors="coerce").dt.normalize(),
    ).dropna()
    prices = prices[~prices.index.duplicated(keep="last")].sort_index()
    if prices.empty:
        return history
    last = min(end, prices.index.max().date())
    sessions = pd.DatetimeIndex(_a_share_sessions(start, last))
    if sessions.empty:
        return history
    window = prices.reindex(prices.index.union(sessions)).sort_index().ffill()
    filled = window[window.index.isin(sessions) | (window.index > pd.Timestamp(last))]
    filled = pd.concat([prices[prices.index < pd.Timestamp(start)], filled])
    filled = filled[~filled.index.duplicated(keep="last")].dropna()
    return pd.DataFrame({"date": filled.index.strftime("%Y-%m-%d"), "price": filled.to_numpy()})


def load_live_price_histories(
    trades: pd.DataFrame,
    *,
    market_now: datetime | None = None,
    allow_fetch: bool = False,
    save_to_cache: bool = False,
    api_key: str = "",
) -> tuple[dict[str, pd.DataFrame], list[str], list[str], bool]:
    """按账本需要读取各标的正式收盘价。

    返回 (histories, failures, warnings, complete)。已覆盖所需区间的标的不联网。
    """
    from services.position_analysis import latest_final_etf_trade_date, load_or_fetch_etf

    market_now = market_now or datetime.now(MARKET_TZ)
    target = latest_final_etf_trade_date(market_now)
    windows = required_price_windows(trades, target)
    histories: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    warnings: list[str] = []
    complete = True
    for symbol, (start, end) in sorted(windows.items()):
        if is_convertible_bond(symbol):
            history, error = load_bond_history(
                symbol,
                window=(start, end),
                allow_fetch=allow_fetch,
                save_to_cache=save_to_cache,
                completed_date=target,
            )
            latest = str(history["date"].max()) if not history.empty else ""
        else:
            item = load_or_fetch_etf(
                symbol,
                api_key=api_key,
                count=5000,
                adjust=FUND_ADJUST_NONE,
                allow_fetch=False,
                save_to_cache=False,
                market_now=market_now,
            )
            history = item.dataframe
            latest_ts = pd.to_datetime(item.latest_date, errors="coerce")
            error = None
            if allow_fetch and (pd.isna(latest_ts) or latest_ts.date() < end):
                item = load_or_fetch_etf(
                    symbol,
                    api_key=api_key,
                    count=5000,
                    adjust=FUND_ADJUST_NONE,
                    allow_fetch=True,
                    force_refresh=False,
                    save_to_cache=save_to_cache,
                    market_now=market_now,
                )
                history = item.dataframe
                latest_ts = pd.to_datetime(item.latest_date, errors="coerce")
                error = item.error
            latest = "" if pd.isna(latest_ts) else latest_ts.date().isoformat()
        if history is not None and not history.empty:
            histories[symbol] = fill_suspended_sessions(history, start, end)
        if error:
            (failures if allow_fetch else warnings).append(f"{symbol}：{error}")
        if not latest or latest < end.isoformat():
            complete = False
            warnings.append(f"{symbol}：正式收盘最新到{latest or '-'}，需要覆盖到{end.isoformat()}")
    return histories, failures, list(dict.fromkeys(warnings)), complete


__all__ = [
    "BOND_FACE_VALUE",
    "fill_suspended_sessions",
    "is_convertible_bond",
    "load_bond_history",
    "load_live_price_histories",
    "required_price_windows",
]
