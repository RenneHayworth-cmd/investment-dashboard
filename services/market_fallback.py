"""Ordered, bounded HTTP failover for compatible market-data sources.

Adapters own instrument/adjustment compatibility. This module never writes caches
or substitutes a quote for a daily close.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from core.network import install_default_request_timeout

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketSource:
    name: str
    fetch: Callable[[], pd.DataFrame]
    cache_source: str = ""


def fetch_market_fallback(
    sources: Iterable[MarketSource],
    normalize: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    date_column: str,
    price_column: str,
    target_date=None,
    allow_partial: bool = True,
    allow_zero_price: bool = False,
    preserve_unfinished_rows: bool = False,
) -> pd.DataFrame:
    """Try each source once until valid data covers the requested session.

    Older, valid history is retained only after *all* candidates were tried. It
    carries a warning and is never labelled current; callers still check gaps.
    """
    install_default_request_timeout()
    failures: list[str] = []
    best = None
    best_date = None
    target = pd.Timestamp(target_date).normalize() if target_date is not None else None
    for source in sources:
        try:
            raw = source.fetch()
            if not isinstance(raw, pd.DataFrame) or raw.empty:
                raise ValueError("返回空数据或无效数据类型")
            frame = normalize(raw)
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                raise ValueError("标准化后没有有效记录")
            dates = pd.to_datetime(frame[date_column], errors="coerce")
            prices = pd.to_numeric(frame[price_column], errors="coerce")
            valid_price = prices.ge(0) if allow_zero_price else prices.gt(0)
            valid = dates.notna() & np.isfinite(prices) & valid_price
            # Future rows cannot satisfy a completed-session freshness check.
            eligible = valid & dates.le(target) if target is not None else valid
            if not eligible.any():
                raise ValueError("没有有效日期和正数价格")
            frame = frame.loc[valid if preserve_unfinished_rows else eligible].copy()
            latest = dates.loc[eligible].max().normalize()
            frame.attrs["market_data_source"] = source.name
            if source.cache_source:
                frame.attrs["source"] = source.cache_source
            if target is not None and latest < target:
                if best_date is None or latest > best_date or (latest == best_date and len(frame) > len(best)):
                    best, best_date = frame, latest
                raise ValueError(f"日期滞后：最新 {latest:%Y-%m-%d}，需要 {target:%Y-%m-%d}")
            frame.attrs["market_source_failures"] = tuple(failures)
            return frame
        except Exception as exc:
            reason = str(exc).strip() or type(exc).__name__
            failures.append(f"{source.name}：{reason}")
            logger.warning("行情接口失败，继续尝试备用接口：%s", failures[-1])
    message = "；".join(failures) or "没有兼容的行情接口"
    if best is not None and allow_partial:
        best.attrs["market_source_failures"] = tuple(failures)
        best.attrs["market_source_warning"] = f"所有接口均未覆盖目标日期；保留已有日线。{message}"
        return best
    raise RuntimeError(f"所有兼容行情接口获取失败：{message}")


def normalize_daily_prices(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep OHLC/volume columns while accepting vendor field names."""
    aliases = {
        "date": ("trade_date", "日期", "时间", "date", "datetime"),
        "close": ("close", "收盘", "收盘价", "price"),
        "open": ("open", "开盘", "开盘价"),
        "high": ("high", "最高", "最高价"),
        "low": ("low", "最低", "最低价"),
        "volume": ("volume", "成交量"),
        "amount": ("amount", "成交额"),
    }
    rename = {}
    for name, columns in aliases.items():
        found = next((column for column in columns if column in frame.columns), None)
        if found is not None:
            rename[found] = name
    result = frame.rename(columns=rename).copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    for column in ("close", "open", "high", "low", "volume", "amount"):
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date")
