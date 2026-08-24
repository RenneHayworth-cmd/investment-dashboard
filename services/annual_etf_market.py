from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from services.annual_etf_models import (
    ALL_SLOTS,
    HistoricalEtfRecord,
    _normalize_symbol,
    _optional_text,
)


def load_registry(path: str | Path) -> list[HistoricalEtfRecord]:
    frame = pd.read_csv(path, dtype={"symbol": str, "proxy_symbol": str})
    required = {
        "registry_version",
        "snapshot_date",
        "symbol",
        "name",
        "exchange",
        "listing_date",
        "tracked_index",
        "index_family",
        "direction",
        "source_url",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"年度ETF注册表缺少字段：{'、'.join(missing)}")
    records: list[HistoricalEtfRecord] = []
    for row in frame.to_dict("records"):
        listing_date = pd.to_datetime(row.get("listing_date"), errors="coerce")
        if pd.isna(listing_date):
            raise ValueError(f"{row.get('symbol')} 的上市日期无效。")
        active_value = str(row.get("active", "true")).strip().lower()
        records.append(
            HistoricalEtfRecord(
                symbol=_normalize_symbol(row.get("symbol")),
                name=str(row.get("name", "")).strip(),
                exchange=str(row.get("exchange", "")).strip().upper(),
                listing_date=pd.Timestamp(listing_date).normalize(),
                tracked_index=str(row.get("tracked_index", "")).strip(),
                index_family=str(row.get("index_family", "")).strip(),
                direction=str(row.get("direction", "")).strip(),
                source_url=str(row.get("source_url", "")).strip(),
                source_as_of=_optional_text(row.get("source_as_of", "")),
                proxy_symbol=_normalize_symbol(row.get("proxy_symbol", "")),
                proxy_path=_optional_text(row.get("proxy_path", "")),
                proxy_type=_optional_text(row.get("proxy_type", "")),
                proxy_available_date=(
                    pd.Timestamp(row.get("proxy_available_date")).normalize()
                    if pd.notna(row.get("proxy_available_date"))
                    and str(row.get("proxy_available_date", "")).strip()
                    else None
                ),
                proxy_source_url=_optional_text(row.get("proxy_source_url", "")),
                active=active_value not in {"0", "false", "no", "否"},
                product_type=str(row.get("product_type", "ETF") or "ETF").strip(),
                registry_version=_optional_text(row.get("registry_version", "")),
                snapshot_date=_optional_text(row.get("snapshot_date", "")),
            )
        )
    symbols = [item.symbol for item in records]
    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
    if duplicates:
        raise ValueError(f"年度ETF注册表存在重复代码：{'、'.join(duplicates)}")
    return records


def load_index_family_config(path: str | Path) -> dict[str, object]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config.get("directions"), dict):
        raise ValueError("指数族白名单缺少 directions 配置。")
    return config


def registry_frame(records: Iterable[HistoricalEtfRecord]) -> pd.DataFrame:
    rows = []
    for item in records:
        row = asdict(item)
        row["listing_date"] = item.listing_date.strftime("%Y-%m-%d")
        row["proxy_available_date"] = (
            item.proxy_available_date.strftime("%Y-%m-%d")
            if item.proxy_available_date is not None
            else ""
        )
        rows.append(row)
    return pd.DataFrame(rows)


def validate_registry_against_whitelist(
    records: Iterable[HistoricalEtfRecord],
    whitelist: dict[str, object],
) -> pd.DataFrame:
    items = list(records)
    configured = whitelist.get("directions", {})
    excluded_keywords = [str(value).lower() for value in whitelist.get("excluded_keywords", [])]
    record_map = {item.symbol: item for item in items}
    rows = []
    for item in items:
        families = set(configured.get(item.direction, {}).get("families", []))
        haystack = f"{item.name} {item.tracked_index} {item.index_family}".lower()
        excluded = next((word for word in excluded_keywords if word and word in haystack), "")
        reason = ""
        eligible = True
        if not item.active:
            eligible = False
            reason = "注册表快照日非存续ETF"
        elif item.product_type.upper() != "ETF":
            eligible = False
            reason = "非ETF产品"
        elif item.direction not in ALL_SLOTS:
            eligible = False
            reason = "未登记方向"
        elif item.index_family not in families:
            eligible = False
            reason = "指数族不在白名单"
        elif excluded:
            eligible = False
            reason = f"命中排除关键词：{excluded}"
        elif item.proxy_symbol and item.proxy_symbol not in record_map:
            eligible = False
            reason = "代理ETF未登记在同一注册表"
        elif (
            item.proxy_symbol
            and record_map[item.proxy_symbol].tracked_index != item.tracked_index
        ):
            eligible = False
            reason = "代理ETF跟踪指数不一致"
        elif (
            item.proxy_symbol
            and record_map[item.proxy_symbol].listing_date >= item.listing_date
        ):
            eligible = False
            reason = "代理ETF不是更早上市产品"
        elif item.proxy_path and (
            item.proxy_type != "official_index"
            or item.proxy_available_date is None
            or not item.proxy_source_url
        ):
            eligible = False
            reason = "官方指数代理缺少类型、发布日期或来源"
        rows.append(
            {
                "symbol": item.symbol,
                "name": item.name,
                "direction": item.direction,
                "index_family": item.index_family,
                "tracked_index": item.tracked_index,
                "registry_eligible": eligible,
                "registry_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def annual_raw_cache_key(symbol: str) -> str:
    return f"annual_etf_v1_{_normalize_symbol(symbol)}"


def exchange_prefixed_symbol(symbol: str) -> str:
    normalized = _normalize_symbol(symbol)
    return ("sh" if normalized.startswith(("5", "6")) else "sz") + normalized


def fetch_annual_etf_raw_history(
    record: HistoricalEtfRecord,
    *,
    start_date: str = "20000101",
    end_date: str = "20991231",
) -> pd.DataFrame:
    """联网抓取未复权正式日线；调用方负责二次确认和原子缓存。"""
    import akshare as ak

    frame = ak.stock_zh_a_hist_tx(
        symbol=exchange_prefixed_symbol(record.symbol),
        start_date=start_date,
        end_date=end_date,
        adjust="",
    )
    if frame is None or frame.empty:
        raise RuntimeError(f"{record.symbol} 未获取到未复权正式日线。")
    normalized = normalize_annual_market_data(frame)
    if normalized.empty:
        raise RuntimeError(f"{record.symbol} 未复权正式日线无法标准化。")
    return frame


def fetch_annual_dividends(year: int) -> pd.DataFrame:
    """抓取指定年度基金分红公告表。"""
    import akshare as ak

    frame = ak.fund_fh_em(year=str(int(year)))
    if frame is None:
        return pd.DataFrame()
    return frame


def dividends_for_symbol(dividends: pd.DataFrame | None, symbol: str) -> pd.DataFrame:
    if dividends is None or dividends.empty:
        return pd.DataFrame()
    code_column = _column(dividends, ("基金代码", "symbol", "code"))
    if code_column is None:
        return pd.DataFrame()
    codes = dividends[code_column].astype(str).str.extract(r"(\d{6})", expand=False)
    return dividends.loc[codes == _normalize_symbol(symbol)].copy()


def share_splits_for_symbol(
    whitelist: dict[str, object],
    symbol: str,
) -> pd.DataFrame:
    configured = whitelist.get("known_share_splits", {})
    return pd.DataFrame(configured.get(_normalize_symbol(symbol), []))


def _column(frame: pd.DataFrame, names: Iterable[str]) -> str | None:
    normalized = {str(column).strip().lower(): column for column in frame.columns}
    for name in names:
        match = normalized.get(str(name).strip().lower())
        if match is not None:
            return match
    return None


def _normalize_actions(
    dividends: pd.DataFrame | None,
    share_splits: pd.DataFrame | None,
) -> tuple[pd.Series, pd.DataFrame]:
    dividend_series = pd.Series(dtype=float)
    if dividends is not None and not dividends.empty:
        data = dividends.copy()
        date_col = _column(data, ("trade_date", "除息日期", "除息日", "date", "effective_date"))
        value_col = _column(data, ("dividend_per_share", "分红", "每份分红", "dividend"))
        if date_col is not None and value_col is not None:
            data["_date"] = pd.to_datetime(data[date_col], errors="coerce").dt.normalize()
            data["_value"] = pd.to_numeric(data[value_col], errors="coerce")
            data = data.dropna(subset=["_date", "_value"])
            dividend_series = data.groupby("_date")["_value"].sum()
    split_frame = pd.DataFrame(columns=["effective_date", "ratio", "rounding", "source"])
    if share_splits is not None and not share_splits.empty:
        split_frame = share_splits.copy().rename(
            columns={"date": "effective_date", "split_ratio": "ratio"}
        )
        if not {"effective_date", "ratio"}.issubset(split_frame.columns):
            raise ValueError("份额折算配置缺少 effective_date 或 ratio。")
        split_frame["effective_date"] = pd.to_datetime(
            split_frame["effective_date"], errors="coerce"
        ).dt.normalize()
        split_frame["ratio"] = pd.to_numeric(split_frame["ratio"], errors="coerce")
        split_frame = split_frame.dropna(subset=["effective_date", "ratio"])
        if (split_frame["ratio"] <= 0).any():
            raise ValueError("份额折算比例必须大于0。")
        if "rounding" not in split_frame:
            split_frame["rounding"] = "floor"
        if "source" not in split_frame:
            split_frame["source"] = "官方基金公告"
    return dividend_series, split_frame


def normalize_annual_market_data(
    raw: pd.DataFrame,
    dividends: pd.DataFrame | None = None,
    share_splits: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build a point-in-time total-return signal from raw prices and effective actions."""
    if raw is None or raw.empty:
        raise ValueError("未复权行情为空。")
    frame = raw.copy()
    date_col = _column(frame, ("trade_date", "日期", "date", "datetime"))
    close_col = _column(frame, ("raw_close", "close", "收盘价", "收盘"))
    open_col = _column(frame, ("raw_open", "open", "开盘价", "开盘"))
    high_col = _column(frame, ("raw_high", "high", "最高价", "最高"))
    low_col = _column(frame, ("raw_low", "low", "最低价", "最低"))
    amount_col = _column(frame, ("amount", "成交额", "turnover"))
    if date_col is None or close_col is None:
        raise ValueError(f"未复权行情缺少日期或收盘价列：{list(frame.columns)}")
    result = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(frame[date_col], errors="coerce").dt.normalize(),
            "raw_close": pd.to_numeric(frame[close_col], errors="coerce"),
        }
    )
    result["raw_open"] = (
        pd.to_numeric(frame[open_col], errors="coerce") if open_col is not None else result["raw_close"]
    )
    result["raw_high"] = (
        pd.to_numeric(frame[high_col], errors="coerce")
        if high_col is not None
        else result[["raw_open", "raw_close"]].max(axis=1)
    )
    result["raw_low"] = (
        pd.to_numeric(frame[low_col], errors="coerce")
        if low_col is not None
        else result[["raw_open", "raw_close"]].min(axis=1)
    )
    result["amount"] = (
        pd.to_numeric(frame[amount_col], errors="coerce") if amount_col is not None else np.nan
    )
    result = (
        result.dropna(subset=["trade_date", "raw_close"])
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )
    dividend_series, split_frame = _normalize_actions(dividends, share_splits)
    result["dividend_per_share"] = result["trade_date"].map(dividend_series).fillna(0.0)
    result["share_split_ratio"] = 1.0
    result["share_split_rounding"] = ""
    result["share_split_source"] = ""
    for action in split_frame.itertuples(index=False):
        mask = result["trade_date"] == pd.Timestamp(action.effective_date)
        result.loc[mask, "share_split_ratio"] = float(action.ratio)
        result.loc[mask, "share_split_rounding"] = str(action.rounding or "floor")
        result.loc[mask, "share_split_source"] = str(action.source or "官方基金公告")

    signals = np.empty(len(result), dtype=float)
    signals[0] = float(result.iloc[0]["raw_close"])
    for index in range(1, len(result)):
        previous = float(result.iloc[index - 1]["raw_close"])
        current = float(result.iloc[index]["raw_close"])
        ratio = float(result.iloc[index]["share_split_ratio"])
        dividend = float(result.iloc[index]["dividend_per_share"])
        gross_return = (current * ratio + dividend) / previous if previous > 0 else 1.0
        signals[index] = signals[index - 1] * gross_return
    result["signal_close"] = signals
    factor = result["signal_close"] / result["raw_close"]
    result["signal_open"] = result["raw_open"] * factor
    result["signal_high"] = result["raw_high"] * factor
    result["signal_low"] = result["raw_low"] * factor
    result["is_proxy"] = False
    result["corporate_action_status"] = np.select(
        [
            (result["dividend_per_share"] > 0) & (result["share_split_ratio"] != 1),
            result["dividend_per_share"] > 0,
            result["share_split_ratio"] != 1,
        ],
        ["官方现金分红+份额折算", "官方现金分红", "官方份额折算"],
        default="无调整事件",
    )
    return result


def stitch_proxy_history(
    etf: pd.DataFrame,
    proxy: pd.DataFrame | None,
    listing_date: str | pd.Timestamp,
) -> pd.DataFrame:
    actual = etf.copy()
    actual["trade_date"] = pd.to_datetime(actual["trade_date"], errors="coerce").dt.normalize()
    actual = actual[actual["trade_date"] >= pd.Timestamp(listing_date).normalize()].copy()
    actual["is_proxy"] = False
    if proxy is None or proxy.empty or actual.empty:
        return actual.reset_index(drop=True)
    source = proxy.copy()
    source["trade_date"] = pd.to_datetime(source["trade_date"], errors="coerce").dt.normalize()
    source = source[source["trade_date"] < actual["trade_date"].min()].copy()
    source = source.sort_values("trade_date").drop_duplicates("trade_date")
    if source.empty:
        return actual.reset_index(drop=True)
    source_signal = pd.to_numeric(source["signal_close"], errors="coerce")
    source = source.loc[source_signal.notna()].copy()
    if source.empty:
        return actual.reset_index(drop=True)
    anchor = float(actual.iloc[0]["raw_close"])
    scale = anchor / float(source["signal_close"].iloc[-1])
    synthetic = pd.DataFrame({"trade_date": source["trade_date"]})
    synthetic["raw_close"] = pd.to_numeric(source["signal_close"], errors="coerce") * scale
    for column in ("raw_open", "raw_high", "raw_low"):
        signal_column = column.replace("raw_", "signal_")
        values = source[signal_column] if signal_column in source else source["signal_close"]
        synthetic[column] = pd.to_numeric(values, errors="coerce") * scale
    synthetic["signal_close"] = synthetic["raw_close"]
    synthetic["signal_open"] = synthetic["raw_open"]
    synthetic["signal_high"] = synthetic["raw_high"]
    synthetic["signal_low"] = synthetic["raw_low"]
    synthetic["amount"] = np.nan
    synthetic["dividend_per_share"] = 0.0
    synthetic["share_split_ratio"] = 1.0
    synthetic["share_split_rounding"] = ""
    synthetic["share_split_source"] = "代理收益链"
    synthetic["corporate_action_status"] = "代理研究区间"
    synthetic["is_proxy"] = True
    columns = list(dict.fromkeys([*actual.columns, *synthetic.columns]))
    return (
        pd.concat([synthetic.reindex(columns=columns), actual.reindex(columns=columns)], ignore_index=True)
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )
