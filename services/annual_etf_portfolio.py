from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from math import ceil, floor
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterable

import numpy as np
import pandas as pd


REGISTRY_VERSION = "annual-etf-registry-20260817-v1"
WHITELIST_VERSION = "annual-etf-index-families-20260817-v1"
ANNUAL_CACHE_SOURCE = "annual_etf"
ANNUAL_RAW_DATA_TYPE = "raw_history"
ANNUAL_DIVIDEND_DATA_TYPE = "corporate_actions"
ANNUAL_CACHE_PERIOD = "full_1d"
ANNUAL_DIVIDEND_CACHE_KEY = "annual_etf_dividends_v1"
DEFAULT_MA_PERIODS = (10, 15, 20, 25, 30)
DEFAULT_THRESHOLDS = (0.0, 0.5, 1.0, 1.5, 2.0)
SCORE_WEIGHTS = {
    "longest_underwater_days": 0.45,
    "max_drawdown_pct": 0.25,
    "annual_volatility_pct": 0.15,
    "annual_return_pct": 0.10,
    "sharpe_ratio": 0.05,
}
EXECUTION_SAME_CLOSE = "same_close"
EXECUTION_NEXT_CLOSE = "next_close"
PARKING_SYMBOL = "512890"
PARKING_LISTING_DATE = pd.Timestamp("2019-01-18")
US_SLOTS = ("us_sp500", "us_nasdaq")
NON_US_SLOTS = (
    "a_large",
    "a_mid_small",
    "a_growth",
    "smart_beta",
    "other_overseas",
    "gold",
)
ALL_SLOTS = (*US_SLOTS, *NON_US_SLOTS)
PARKING_SLOTS = {"a_mid_small", "a_growth"}


@dataclass(frozen=True)
class HistoricalEtfRecord:
    symbol: str
    name: str
    exchange: str
    listing_date: pd.Timestamp
    tracked_index: str
    index_family: str
    direction: str
    source_url: str
    source_as_of: str = ""
    proxy_symbol: str = ""
    proxy_path: str = ""
    proxy_type: str = ""
    proxy_available_date: pd.Timestamp | None = None
    proxy_source_url: str = ""
    active: bool = True
    product_type: str = "ETF"
    registry_version: str = ""
    snapshot_date: str = ""

    @property
    def tickflow_symbol(self) -> str:
        suffix = str(self.exchange).strip().upper()
        return f"{self.symbol}.{suffix}"


AnnualEtfRegistryEntry = HistoricalEtfRecord


@dataclass(frozen=True)
class AnnualBacktestSettings:
    start_year: int = 2019
    end_date: str | pd.Timestamp | None = None
    initial_capital: float = 500000.0
    commission_rate: float = 0.00006
    lot_size: int = 100
    cash_annual_rate: float = 0.015
    min_listing_days: int = 120
    turnover_window: int = 60
    min_turnover_days: int = 40
    max_history_years: int = 5
    train_ratio: float = 0.70
    min_train_days: int = 504
    min_validation_days: int = 252
    annual_return_gate_pct: float = 10.0
    ma_periods: tuple[int, ...] = DEFAULT_MA_PERIODS
    threshold_pcts: tuple[float, ...] = DEFAULT_THRESHOLDS
    registry_version: str = REGISTRY_VERSION
    whitelist_version: str = WHITELIST_VERSION


@dataclass(frozen=True)
class AnnualSelection:
    year: int
    slot: str
    symbol: str
    name: str
    ma_period: int
    threshold_pct: float
    strategy: str
    validation_score: float
    validation_annual_return_pct: float
    validation_sharpe: float
    return_gate_relaxed: bool
    proxy_ratio_pct: float
    decision_date: pd.Timestamp


@dataclass
class AnnualQualificationResult:
    qualification: pd.DataFrame
    research_data: dict[tuple[int, str], pd.DataFrame] = field(default_factory=dict)
    errors: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class AnnualPortfolioResult:
    summary: pd.DataFrame
    daily: pd.DataFrame
    yearly: pd.DataFrame
    selections: pd.DataFrame
    qualification: pd.DataFrame
    parameters: pd.DataFrame
    trades: pd.DataFrame
    migrations: pd.DataFrame
    contribution: pd.DataFrame
    errors: pd.DataFrame
    report_markdown: str
    fingerprint: str


@dataclass
class DirectionSleeveState:
    slot: str
    initial_capital: float
    cash: float
    current_symbol: str = ""
    current_name: str = ""
    current_strategy: str = "timing"
    ma_period: int = 20
    threshold_pct: float = 1.0
    parameter_effective_date: pd.Timestamp | None = None
    pending: AnnualSelection | None = None
    long_shares: float = 0.0
    timing_shares: float = 0.0
    parking_shares: float = 0.0
    long_cost: float = 0.0
    timing_cost: float = 0.0
    parking_cost: float = 0.0
    last_price: float = np.nan
    last_parking_price: float = np.nan

    @property
    def shares(self) -> float:
        return self.long_shares + self.timing_shares


def _empty_frame(columns: Iterable[str] = ()) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


def _normalize_symbol(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value or "").strip().upper()
    return text.split(".", 1)[0]


def _optional_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


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


def _decision_date(year: int, market_data: dict[str, pd.DataFrame]) -> pd.Timestamp | None:
    latest: list[pd.Timestamp] = []
    cutoff = pd.Timestamp(year - 1, 12, 31)
    for frame in market_data.values():
        if frame is None or frame.empty:
            continue
        dates = pd.to_datetime(frame["trade_date"], errors="coerce")
        dates = dates[dates <= cutoff]
        if not dates.empty:
            latest.append(pd.Timestamp(dates.max()).normalize())
    return max(latest) if latest else None


def _research_window(
    frame: pd.DataFrame,
    decision_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
) -> pd.DataFrame:
    start = decision_date - pd.DateOffset(years=settings.max_history_years) + pd.Timedelta(days=1)
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame.loc[dates.between(start, decision_date)].copy().reset_index(drop=True)


def _proxy_for_record(
    record: HistoricalEtfRecord,
    market_data: dict[str, pd.DataFrame],
    proxy_data: dict[str, pd.DataFrame] | None,
    decision_date: pd.Timestamp,
) -> pd.DataFrame | None:
    if record.proxy_symbol:
        candidate = market_data.get(record.proxy_symbol)
        if candidate is not None and not candidate.empty:
            return candidate
    if (
        record.proxy_available_date is not None
        and record.proxy_available_date > decision_date
    ):
        return None
    return (proxy_data or {}).get(record.symbol)


def preflight_annual_candidates(
    records: list[HistoricalEtfRecord],
    whitelist: dict[str, object],
    market_data: dict[str, pd.DataFrame],
    settings: AnnualBacktestSettings,
    proxy_data: dict[str, pd.DataFrame] | None = None,
) -> AnnualQualificationResult:
    registry_status = validate_registry_against_whitelist(records, whitelist).set_index("symbol")
    end_date = pd.Timestamp(settings.end_date or pd.Timestamp.today()).normalize()
    rows: list[dict[str, object]] = []
    research: dict[tuple[int, str], pd.DataFrame] = {}
    error_rows: list[dict[str, object]] = []
    end_year = end_date.year
    for year in range(int(settings.start_year), end_year + 1):
        decision = _decision_date(year, market_data)
        if decision is None:
            error_rows.append({"year": year, "stage": "资格预检", "error": "缺少上一年度正式行情"})
            continue
        for record in records:
            base = registry_status.loc[record.symbol]
            reason = str(base["registry_reason"] or "")
            qualified = bool(base["registry_eligible"])
            actual = market_data.get(record.symbol)
            actual_days = 0
            research_days = 0
            proxy_ratio = 0.0
            turnover_days = 0
            turnover_median = np.nan
            if actual is None or actual.empty:
                qualified = False
                reason = reason or "缺少未复权正式行情"
                stitched = pd.DataFrame()
            else:
                actual_dates = pd.to_datetime(actual["trade_date"], errors="coerce")
                actual_days = int(
                    ((actual_dates >= record.listing_date) & (actual_dates <= decision)).sum()
                )
                if decision < record.listing_date:
                    qualified = False
                    reason = reason or "决策日尚未上市"
                elif actual_days < settings.min_listing_days:
                    qualified = False
                    reason = reason or f"上市实际交易日不足{settings.min_listing_days}日"
                proxy = _proxy_for_record(record, market_data, proxy_data, decision)
                stitched = stitch_proxy_history(actual, proxy, record.listing_date)
                stitched = _research_window(stitched, decision, settings)
                research_days = len(stitched)
                proxy_ratio = float(stitched["is_proxy"].mean() * 100) if len(stitched) else 0.0
                split_index = int(ceil(research_days * settings.train_ratio))
                train_days = split_index
                validation_days = research_days - split_index
                if (
                    train_days < settings.min_train_days
                    or validation_days < settings.min_validation_days
                ):
                    qualified = False
                    reason = reason or (
                        f"70/30拆分后筛选{train_days}日、验证{validation_days}日，"
                        f"不足{settings.min_train_days}/{settings.min_validation_days}日"
                    )
                if not stitched.empty:
                    research[(year, record.symbol)] = stitched
                recent = actual.loc[actual_dates <= decision].tail(settings.turnover_window)
                amount = pd.to_numeric(recent.get("amount"), errors="coerce").dropna()
                turnover_days = len(amount)
                if turnover_days >= settings.min_turnover_days:
                    turnover_median = float(amount.median())
            rows.append(
                {
                    "year": year,
                    "decision_date": decision,
                    "symbol": record.symbol,
                    "name": record.name,
                    "direction": record.direction,
                    "tracked_index": record.tracked_index,
                    "index_family": record.index_family,
                    "listing_date": record.listing_date,
                    "actual_trading_days": actual_days,
                    "research_days": research_days,
                    "proxy_ratio_pct": proxy_ratio,
                    "turnover_valid_days": turnover_days,
                    "turnover_median": turnover_median,
                    "qualified_before_index_dedup": qualified,
                    "qualified": qualified,
                    "representative": False,
                    "reason": reason,
                }
            )
    qualification = pd.DataFrame(rows)
    if qualification.empty:
        return AnnualQualificationResult(qualification, research, pd.DataFrame(error_rows))

    for (_year, _index), group in qualification.groupby(["year", "tracked_index"], sort=False):
        eligible = group[group["qualified_before_index_dedup"]].copy()
        if eligible.empty:
            continue
        eligible["has_turnover"] = eligible["turnover_valid_days"] >= settings.min_turnover_days
        eligible["listing_date"] = pd.to_datetime(eligible["listing_date"])
        eligible = eligible.sort_values(
            ["has_turnover", "turnover_median", "listing_date", "research_days", "symbol"],
            ascending=[False, False, True, False, True],
            na_position="last",
        )
        winner = eligible.index[0]
        qualification.loc[winner, "representative"] = True
        for index in eligible.index[1:]:
            qualification.loc[index, "qualified"] = False
            qualification.loc[index, "reason"] = "同指数代表ETF未胜出"
    qualification.loc[
        qualification["qualified_before_index_dedup"] & ~qualification["representative"],
        "qualified",
    ] = False
    return AnnualQualificationResult(qualification, research, pd.DataFrame(error_rows))


def _desired_states(signal: pd.Series, ma_period: int, threshold_pct: float) -> tuple[pd.Series, pd.Series]:
    prices = pd.to_numeric(signal, errors="coerce")
    ma = prices.rolling(int(ma_period), min_periods=int(ma_period)).mean()
    threshold = float(threshold_pct) / 100
    state = 0
    states = []
    for price, average in zip(prices, ma):
        if np.isfinite(price) and np.isfinite(average):
            if price > average * (1 + threshold):
                state = 1
            elif price < average * (1 - threshold):
                state = 0
        states.append(state)
    return pd.Series(states, index=signal.index, dtype=int), ma


def _apply_split(shares: float, ratio: float, rounding: str) -> float:
    if shares <= 0 or not np.isfinite(ratio) or np.isclose(ratio, 1.0):
        return shares
    adjusted = shares * ratio
    if rounding == "ceil":
        return float(np.ceil(adjusted - 1e-12))
    if rounding == "round":
        return float(np.rint(adjusted))
    return float(np.floor(adjusted + 1e-12))


def _research_leg(
    frame: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    *,
    ma_period: int,
    threshold_pct: float,
    capital: float,
    commission_rate: float,
    lot_size: int,
    cash_annual_rate: float,
    always_hold: bool,
) -> pd.DataFrame:
    data = frame.copy().sort_values("trade_date").reset_index(drop=True)
    states, _ma = _desired_states(data["signal_close"], ma_period, threshold_pct)
    data["desired"] = 1 if always_hold else states
    selected = data[data["trade_date"].between(start_date, end_date)].copy()
    if selected.empty:
        return pd.DataFrame()
    cash = float(capital)
    shares = 0.0
    rows = []
    previous_date: pd.Timestamp | None = None
    for row in selected.itertuples(index=False):
        trade_date = pd.Timestamp(row.trade_date)
        if previous_date is not None and cash > 0 and cash_annual_rate:
            cash *= (1 + cash_annual_rate) ** (max(0, (trade_date - previous_date).days) / 365)
        shares = _apply_split(
            shares,
            float(getattr(row, "share_split_ratio", 1.0) or 1.0),
            str(getattr(row, "share_split_rounding", "") or ""),
        )
        dividend = float(getattr(row, "dividend_per_share", 0.0) or 0.0)
        if shares > 0 and dividend > 0:
            cash += shares * dividend
        price = float(row.raw_close)
        desired = int(row.desired)
        if desired and shares <= 0:
            affordable = cash / (price * (1 + commission_rate))
            buy_shares = floor(affordable / lot_size) * lot_size
            if buy_shares > 0:
                gross = buy_shares * price
                cash -= gross + gross * commission_rate
                shares = float(buy_shares)
        elif not desired and shares > 0:
            gross = shares * price
            cash += gross - gross * commission_rate
            shares = 0.0
        rows.append({"trade_date": trade_date, "value": cash + shares * price})
        previous_date = trade_date
    return pd.DataFrame(rows)


def _research_strategy(
    frame: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    *,
    strategy: str,
    ma_period: int,
    threshold_pct: float,
    settings: AnnualBacktestSettings,
    capital: float = 100000.0,
) -> pd.DataFrame:
    if strategy == "half_timing":
        hold = _research_leg(
            frame,
            start_date,
            end_date,
            ma_period=ma_period,
            threshold_pct=threshold_pct,
            capital=capital / 2,
            commission_rate=settings.commission_rate,
            lot_size=settings.lot_size,
            cash_annual_rate=settings.cash_annual_rate,
            always_hold=True,
        )
        timing = _research_leg(
            frame,
            start_date,
            end_date,
            ma_period=ma_period,
            threshold_pct=threshold_pct,
            capital=capital - capital / 2,
            commission_rate=settings.commission_rate,
            lot_size=settings.lot_size,
            cash_annual_rate=settings.cash_annual_rate,
            always_hold=False,
        )
        if hold.empty or timing.empty:
            return pd.DataFrame()
        merged = hold.merge(timing, on="trade_date", suffixes=("_hold", "_timing"))
        return pd.DataFrame(
            {"trade_date": merged["trade_date"], "portfolio_value": merged["value_hold"] + merged["value_timing"]}
        )
    timing = _research_leg(
        frame,
        start_date,
        end_date,
        ma_period=ma_period,
        threshold_pct=threshold_pct,
        capital=capital,
        commission_rate=settings.commission_rate,
        lot_size=settings.lot_size,
        cash_annual_rate=settings.cash_annual_rate,
        always_hold=False,
    )
    return timing.rename(columns={"value": "portfolio_value"})


def _longest_underwater_days(values: pd.Series, dates: pd.Series, initial_value: float) -> int:
    peak = float(initial_value)
    peak_date = pd.Timestamp(dates.iloc[0]) - pd.Timedelta(days=1)
    longest = 0
    for date, value in zip(pd.to_datetime(dates), pd.to_numeric(values, errors="coerce")):
        if not np.isfinite(value):
            continue
        if value >= peak - 1e-10:
            if value > peak:
                peak = float(value)
                peak_date = pd.Timestamp(date)
        else:
            longest = max(longest, int((pd.Timestamp(date) - peak_date).days))
    return int(longest)


def performance_metrics(
    daily: pd.DataFrame,
    initial_capital: float,
    cash_annual_rate: float,
    *,
    value_column: str = "portfolio_value",
    date_column: str = "trade_date",
) -> dict[str, object]:
    if daily is None or daily.empty:
        raise ValueError("净值序列为空。")
    observations = pd.DataFrame(
        {
            "date": pd.to_datetime(daily[date_column], errors="coerce"),
            "value": pd.to_numeric(daily[value_column], errors="coerce"),
        }
    ).dropna()
    if observations.empty:
        raise ValueError("净值序列没有有效日期和数值。")
    observations = observations.sort_values("date").reset_index(drop=True)
    values = observations["value"]
    dates = observations["date"]
    seeded = pd.concat([pd.Series([float(initial_capital)]), values], ignore_index=True)
    returns = seeded.pct_change().dropna()
    total_return = float(values.iloc[-1] / initial_capital - 1)
    elapsed = max(1, int((dates.iloc[-1] - dates.iloc[0]).days))
    annual_return = (1 + total_return) ** (365 / elapsed) - 1 if total_return > -1 else -1.0
    volatility = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0.0
    risk_free_daily = (1 + cash_annual_rate) ** (1 / 252) - 1
    sharpe = (
        float((returns.mean() - risk_free_daily) / returns.std(ddof=1) * np.sqrt(252))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else 0.0
    )
    drawdown = seeded / seeded.cummax() - 1
    return {
        "start_date": dates.iloc[0],
        "end_date": dates.iloc[-1],
        "trading_days": int(len(values)),
        "final_value": float(values.iloc[-1]),
        "net_profit": float(values.iloc[-1] - initial_capital),
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "max_drawdown_pct": float(drawdown.min() * 100),
        "annual_volatility_pct": volatility * 100,
        "sharpe_ratio": sharpe,
        "longest_underwater_days": _longest_underwater_days(values, dates, initial_capital),
    }


def score_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    scored = frame.copy()
    if scored.empty:
        return scored
    scored["underwater_score"] = scored["longest_underwater_days"].rank(
        ascending=False, pct=True, method="average"
    )
    scored["drawdown_score"] = scored["max_drawdown_pct"].rank(
        ascending=True, pct=True, method="average"
    )
    scored["volatility_score"] = scored["annual_volatility_pct"].rank(
        ascending=False, pct=True, method="average"
    )
    scored["return_score"] = scored["annual_return_pct"].rank(
        ascending=True, pct=True, method="average"
    )
    scored["sharpe_score"] = scored["sharpe_ratio"].rank(
        ascending=True, pct=True, method="average"
    )
    score_columns = {
        "longest_underwater_days": "underwater_score",
        "max_drawdown_pct": "drawdown_score",
        "annual_volatility_pct": "volatility_score",
        "annual_return_pct": "return_score",
        "sharpe_ratio": "sharpe_score",
    }
    scored["composite_score"] = sum(
        scored[score_columns[metric]] * weight for metric, weight in SCORE_WEIGHTS.items()
    )
    return scored


def _best_parameter(
    record: HistoricalEtfRecord,
    research: pd.DataFrame,
    settings: AnnualBacktestSettings,
    decision_date: pd.Timestamp,
) -> tuple[dict[str, object], pd.DataFrame, dict[str, object]]:
    split = int(ceil(len(research) * settings.train_ratio))
    train_days = split
    validation_days = len(research) - split
    if train_days < settings.min_train_days or validation_days < settings.min_validation_days:
        raise ValueError(
            f"70/30拆分后筛选{train_days}日、验证{validation_days}日，"
            f"不足{settings.min_train_days}/{settings.min_validation_days}日。"
        )
    train_start = pd.Timestamp(research.iloc[0]["trade_date"])
    train_end = pd.Timestamp(research.iloc[split - 1]["trade_date"])
    validation_start = pd.Timestamp(research.iloc[split]["trade_date"])
    validation_end = pd.Timestamp(research.iloc[-1]["trade_date"])
    strategy = "half_timing" if record.direction in US_SLOTS else "timing"
    rows = []
    for ma_period in settings.ma_periods:
        for threshold in settings.threshold_pcts:
            daily = _research_strategy(
                research,
                train_start,
                train_end,
                strategy=strategy,
                ma_period=int(ma_period),
                threshold_pct=float(threshold),
                settings=settings,
            )
            metrics = performance_metrics(daily, 100000.0, settings.cash_annual_rate)
            rows.append(
                {
                    "symbol": record.symbol,
                    "name": record.name,
                    "decision_date": decision_date,
                    "segment": "parameter_training",
                    "ma_period": int(ma_period),
                    "threshold_pct": float(threshold),
                    **metrics,
                }
            )
    scored = score_metrics(pd.DataFrame(rows))
    chosen = scored.sort_values(
        [
            "composite_score",
            "sharpe_ratio",
            "longest_underwater_days",
            "max_drawdown_pct",
            "annual_return_pct",
            "ma_period",
            "threshold_pct",
        ],
        ascending=[False, False, True, False, False, True, True],
    ).iloc[0].to_dict()
    validation_daily = _research_strategy(
        research,
        validation_start,
        validation_end,
        strategy=strategy,
        ma_period=int(chosen["ma_period"]),
        threshold_pct=float(chosen["threshold_pct"]),
        settings=settings,
    )
    validation = performance_metrics(validation_daily, 100000.0, settings.cash_annual_rate)
    validation.update(
        {
            "symbol": record.symbol,
            "name": record.name,
            "direction": record.direction,
            "tracked_index": record.tracked_index,
            "ma_period": int(chosen["ma_period"]),
            "threshold_pct": float(chosen["threshold_pct"]),
            "strategy": strategy,
            "validation_start": validation_start,
            "validation_end": validation_end,
        }
    )
    return chosen, scored, validation


def build_annual_selections(
    records: list[HistoricalEtfRecord],
    preflight: AnnualQualificationResult,
    settings: AnnualBacktestSettings,
    progress_callback: Callable[[str, float], None] | None = None,
) -> tuple[list[AnnualSelection], pd.DataFrame, pd.DataFrame]:
    records_by_symbol = {item.symbol: item for item in records}
    qualification = preflight.qualification
    selections: list[AnnualSelection] = []
    parameter_frames: list[pd.DataFrame] = []
    error_rows: list[dict[str, object]] = []
    years = sorted(qualification["year"].unique()) if not qualification.empty else []
    total = max(1, sum(len(group[group["qualified"]]) for _, group in qualification.groupby("year")))
    completed = 0
    for year in years:
        annual = qualification[(qualification["year"] == year) & qualification["qualified"]]
        validation_rows: list[dict[str, object]] = []
        for row in annual.itertuples(index=False):
            record = records_by_symbol[str(row.symbol)]
            try:
                _chosen, parameters, validation = _best_parameter(
                    record,
                    preflight.research_data[(int(year), record.symbol)],
                    settings,
                    pd.Timestamp(row.decision_date),
                )
                parameters.insert(0, "year", int(year))
                parameter_frames.append(parameters)
                validation["proxy_ratio_pct"] = float(row.proxy_ratio_pct)
                validation_rows.append(validation)
            except Exception as exc:
                error_rows.append(
                    {"year": int(year), "symbol": record.symbol, "stage": "参数筛选", "error": str(exc)}
                )
            completed += 1
            if progress_callback:
                progress_callback(f"年度筛选：{year} {record.symbol}", completed / total)
        validation_frame = pd.DataFrame(validation_rows)
        for slot in ALL_SLOTS:
            candidates = validation_frame[validation_frame["direction"] == slot].copy()
            if candidates.empty:
                error_rows.append({"year": int(year), "symbol": "", "stage": "年度选择", "error": f"{slot} 无合格候选"})
                continue
            candidates = score_metrics(candidates)
            gated = candidates[candidates["annual_return_pct"] >= settings.annual_return_gate_pct]
            relaxed = gated.empty
            ranked = candidates if relaxed else gated
            winner = ranked.sort_values(
                [
                    "composite_score",
                    "sharpe_ratio",
                    "longest_underwater_days",
                    "max_drawdown_pct",
                    "annual_return_pct",
                    "symbol",
                ],
                ascending=[False, False, True, False, False, True],
            ).iloc[0]
            record = records_by_symbol[str(winner["symbol"])]
            selections.append(
                AnnualSelection(
                    year=int(year),
                    slot=slot,
                    symbol=record.symbol,
                    name=record.name,
                    ma_period=int(winner["ma_period"]),
                    threshold_pct=float(winner["threshold_pct"]),
                    strategy="half_timing" if slot in US_SLOTS else "timing",
                    validation_score=float(winner["composite_score"]),
                    validation_annual_return_pct=float(winner["annual_return_pct"]),
                    validation_sharpe=float(winner["sharpe_ratio"]),
                    return_gate_relaxed=bool(relaxed),
                    proxy_ratio_pct=float(winner["proxy_ratio_pct"]),
                    decision_date=pd.Timestamp(qualification[qualification["year"] == year]["decision_date"].iloc[0]),
                )
            )
    parameters = pd.concat(parameter_frames, ignore_index=True) if parameter_frames else pd.DataFrame()
    return selections, parameters, pd.DataFrame(error_rows)


def selections_frame(selections: Iterable[AnnualSelection]) -> pd.DataFrame:
    rows = []
    for item in selections:
        row = asdict(item)
        row["decision_date"] = item.decision_date
        rows.append(row)
    return pd.DataFrame(rows)


def _selection_map(selections: Iterable[AnnualSelection]) -> dict[tuple[int, str], AnnualSelection]:
    return {(item.year, item.slot): item for item in selections}


def _initial_weights(start_selections: list[AnnualSelection]) -> dict[str, float]:
    if {item.slot for item in start_selections} != set(ALL_SLOTS):
        missing = sorted(set(ALL_SLOTS) - {item.slot for item in start_selections})
        raise ValueError(f"起投年度缺少方向：{'、'.join(missing)}")
    us = sorted(
        [item for item in start_selections if item.slot in US_SLOTS],
        key=lambda item: (-item.validation_score, item.slot),
    )
    weights = {slot: 10.0 for slot in NON_US_SLOTS}
    weights[us[0].slot] = 25.0
    weights[us[1].slot] = 15.0
    if not np.isclose(sum(weights.values()), 100.0):
        raise AssertionError("起投方向权重合计必须为100%。")
    return weights


def _price_row(frame: pd.DataFrame | None, trade_date: pd.Timestamp) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    rows = frame[pd.to_datetime(frame["trade_date"]) == trade_date]
    return rows.iloc[-1] if not rows.empty else None


def _last_price(frame: pd.DataFrame | None, trade_date: pd.Timestamp) -> float:
    if frame is None or frame.empty:
        return np.nan
    dates = pd.to_datetime(frame["trade_date"])
    values = pd.to_numeric(frame.loc[dates <= trade_date, "raw_close"], errors="coerce").dropna()
    return float(values.iloc[-1]) if not values.empty else np.nan


def _signal_state(
    frame: pd.DataFrame | None,
    signal_date: pd.Timestamp,
    ma_period: int,
    threshold_pct: float,
) -> int | None:
    if frame is None or frame.empty:
        return None
    dates = pd.to_datetime(frame["trade_date"])
    history = frame.loc[dates <= signal_date].copy()
    if len(history) < ma_period:
        return None
    states, _ma = _desired_states(history["signal_close"], ma_period, threshold_pct)
    return int(states.iloc[-1])


def _trade(
    state: DirectionSleeveState,
    *,
    symbol: str,
    name: str,
    leg: str,
    action: str,
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    price: float,
    settings: AnnualBacktestSettings,
    reason: str,
) -> dict[str, object] | None:
    shares_field = {"long": "long_shares", "timing": "timing_shares", "parking": "parking_shares"}[leg]
    cost_field = {"long": "long_cost", "timing": "timing_cost", "parking": "parking_cost"}[leg]
    shares = float(getattr(state, shares_field))
    if not np.isfinite(price) or price <= 0:
        return None
    if action == "buy":
        affordable = state.cash / (price * (1 + settings.commission_rate))
        quantity = floor(affordable / settings.lot_size) * settings.lot_size
        if quantity <= 0:
            return None
        gross = quantity * price
        commission = gross * settings.commission_rate
        state.cash -= gross + commission
        setattr(state, shares_field, shares + float(quantity))
        setattr(state, cost_field, float(getattr(state, cost_field)) + gross + commission)
    else:
        quantity = shares
        if quantity <= 0:
            return None
        gross = quantity * price
        commission = gross * settings.commission_rate
        state.cash += gross - commission
        setattr(state, shares_field, 0.0)
        setattr(state, cost_field, 0.0)
    return {
        "slot": state.slot,
        "symbol": symbol,
        "name": name,
        "leg": leg,
        "signal_date": signal_date,
        "execution_date": execution_date,
        "action": action,
        "price": price,
        "shares": float(quantity),
        "gross_amount": gross,
        "commission": commission,
        "reason": reason,
        "cash_after": state.cash,
    }


def _sell_parking(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
) -> None:
    if state.parking_shares <= 0:
        return
    price = _last_price(market_data.get(PARKING_SYMBOL), execution_date)
    trade = _trade(
        state,
        symbol=PARKING_SYMBOL,
        name="红利低波ETF华泰柏瑞",
        leg="parking",
        action="sell",
        signal_date=signal_date,
        execution_date=execution_date,
        price=price,
        settings=settings,
        reason=reason,
    )
    if trade:
        trades.append(trade)


def _buy_parking(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
) -> None:
    if state.slot not in PARKING_SLOTS or execution_date < PARKING_LISTING_DATE or state.parking_shares > 0:
        return
    row = _price_row(market_data.get(PARKING_SYMBOL), execution_date)
    if row is None:
        return
    trade = _trade(
        state,
        symbol=PARKING_SYMBOL,
        name="红利低波ETF华泰柏瑞",
        leg="parking",
        action="buy",
        signal_date=signal_date,
        execution_date=execution_date,
        price=float(row["raw_close"]),
        settings=settings,
        reason=reason,
    )
    if trade:
        trades.append(trade)


def _sell_current(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
) -> None:
    price = _last_price(market_data.get(state.current_symbol), execution_date)
    for leg in ("timing", "long"):
        if float(getattr(state, f"{leg}_shares")) <= 0:
            continue
        trade = _trade(
            state,
            symbol=state.current_symbol,
            name=state.current_name,
            leg=leg,
            action="sell",
            signal_date=signal_date,
            execution_date=execution_date,
            price=price,
            settings=settings,
            reason=reason,
        )
        if trade:
            trades.append(trade)


def _activate_selection(state: DirectionSleeveState, selection: AnnualSelection, effective_date: pd.Timestamp) -> None:
    state.current_symbol = selection.symbol
    state.current_name = selection.name
    state.current_strategy = selection.strategy
    state.ma_period = selection.ma_period
    state.threshold_pct = selection.threshold_pct
    state.parameter_effective_date = effective_date
    state.pending = None


def _buy_active(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
    *,
    initial_half_hold: bool = False,
) -> None:
    row = _price_row(market_data.get(state.current_symbol), execution_date)
    if row is None:
        return
    price = float(row["raw_close"])
    _sell_parking(state, market_data, signal_date, execution_date, settings, trades, "目标ETF恢复持仓")
    if state.current_strategy == "half_timing" and state.long_shares <= 0:
        target_long_cash = state.cash / 2 if (initial_half_hold or state.timing_shares <= 0) else 0.0
        if target_long_cash > 0:
            saved_cash = state.cash
            state.cash = target_long_cash
            trade = _trade(
                state,
                symbol=state.current_symbol,
                name=state.current_name,
                leg="long",
                action="buy",
                signal_date=signal_date,
                execution_date=execution_date,
                price=price,
                settings=settings,
                reason=f"{reason}（长期半仓）",
            )
            unused = state.cash
            state.cash = saved_cash - target_long_cash + unused
            if trade:
                trade["cash_after"] = state.cash
                trades.append(trade)
    if state.timing_shares <= 0:
        trade = _trade(
            state,
            symbol=state.current_symbol,
            name=state.current_name,
            leg="timing",
            action="buy",
            signal_date=signal_date,
            execution_date=execution_date,
            price=price,
            settings=settings,
            reason=f"{reason}（择时仓）" if state.current_strategy == "half_timing" else reason,
        )
        if trade:
            trades.append(trade)


def _buy_half_long(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
) -> None:
    """买入美股方向不参与择时的长期半仓，不触碰择时半仓。"""
    if state.current_strategy != "half_timing" or state.long_shares > 0 or state.pending is not None:
        return
    row = _price_row(market_data.get(state.current_symbol), execution_date)
    if row is None:
        return
    target_cash = state.cash / 2
    saved_cash = state.cash
    state.cash = target_cash
    trade = _trade(
        state,
        symbol=state.current_symbol,
        name=state.current_name,
        leg="long",
        action="buy",
        signal_date=signal_date,
        execution_date=execution_date,
        price=float(row["raw_close"]),
        settings=settings,
        reason=reason,
    )
    unused = state.cash
    state.cash = saved_cash - target_cash + unused
    if trade:
        trade["cash_after"] = state.cash
        trades.append(trade)


def _annual_effective_dates(master_dates: pd.DatetimeIndex) -> dict[int, pd.Timestamp]:
    result = {}
    for year, dates in pd.Series(master_dates, index=master_dates).groupby(master_dates.year):
        result[int(year)] = pd.Timestamp(dates.iloc[0])
    return result


def _apply_actions(state: DirectionSleeveState, row: pd.Series | None, *, parking: bool = False) -> None:
    if row is None:
        return
    ratio = float(row.get("share_split_ratio", 1.0) or 1.0)
    rounding = str(row.get("share_split_rounding", "") or "")
    dividend = float(row.get("dividend_per_share", 0.0) or 0.0)
    if parking:
        state.parking_shares = _apply_split(state.parking_shares, ratio, rounding)
        if state.parking_shares > 0 and dividend > 0:
            state.cash += state.parking_shares * dividend
        return
    state.long_shares = _apply_split(state.long_shares, ratio, rounding)
    state.timing_shares = _apply_split(state.timing_shares, ratio, rounding)
    if state.shares > 0 and dividend > 0:
        state.cash += state.shares * dividend


def _value_state(state: DirectionSleeveState, market_data: dict[str, pd.DataFrame], date: pd.Timestamp) -> float:
    price = _last_price(market_data.get(state.current_symbol), date)
    parking_price = _last_price(market_data.get(PARKING_SYMBOL), date)
    if np.isfinite(price):
        state.last_price = price
    if np.isfinite(parking_price):
        state.last_parking_price = parking_price
    market = state.shares * (state.last_price if np.isfinite(state.last_price) else 0.0)
    parking = state.parking_shares * (
        state.last_parking_price if np.isfinite(state.last_parking_price) else 0.0
    )
    return float(state.cash + market + parking)


def simulate_annual_portfolio(
    market_data: dict[str, pd.DataFrame],
    selections: list[AnnualSelection],
    settings: AnnualBacktestSettings,
    *,
    execution_mode: str = EXECUTION_SAME_CLOSE,
    progress_callback: Callable[[str, float], None] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if execution_mode not in {EXECUTION_SAME_CLOSE, EXECUTION_NEXT_CLOSE}:
        raise ValueError(f"不支持的成交模式：{execution_mode}")
    selection_map = _selection_map(selections)
    start_selections = [item for item in selections if item.year == settings.start_year]
    weights = _initial_weights(start_selections)
    end_date = pd.Timestamp(settings.end_date or pd.Timestamp.today()).normalize()
    selected_symbols = {item.symbol for item in selections} | {PARKING_SYMBOL}
    dates = sorted(
        {
            pd.Timestamp(date).normalize()
            for symbol in selected_symbols
            for date in pd.to_datetime(market_data.get(symbol, pd.DataFrame()).get("trade_date", []), errors="coerce")
            if pd.notna(date) and pd.Timestamp(date).year >= settings.start_year and pd.Timestamp(date) <= end_date
        }
    )
    master_dates = pd.DatetimeIndex(dates)
    if master_dates.empty:
        raise ValueError("起投年份至结束日期没有正式交易日。")
    effective_dates = _annual_effective_dates(master_dates)
    start_date = effective_dates.get(settings.start_year)
    if start_date is None:
        raise ValueError("起投年份没有正式交易日。")
    master_dates = master_dates[master_dates >= start_date]
    states = {
        slot: DirectionSleeveState(
            slot=slot,
            initial_capital=settings.initial_capital * weight / 100,
            cash=settings.initial_capital * weight / 100,
        )
        for slot, weight in weights.items()
    }
    trades: list[dict[str, object]] = []
    migrations: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    previous_date: pd.Timestamp | None = None
    previous_master_date: pd.Timestamp | None = None
    for date_index, trade_date in enumerate(master_dates):
        if previous_date is not None:
            for state in states.values():
                if state.cash > 0 and settings.cash_annual_rate:
                    state.cash *= (1 + settings.cash_annual_rate) ** (
                        max(0, (trade_date - previous_date).days) / 365
                    )
        changed_slots: set[str] = set()
        if effective_dates.get(trade_date.year) == trade_date:
            for slot, state in states.items():
                selection = selection_map.get((trade_date.year, slot))
                if selection is None:
                    continue
                changed_slots.add(slot)
                if not state.current_symbol:
                    _activate_selection(state, selection, trade_date)
                elif state.current_symbol == selection.symbol:
                    _activate_selection(state, selection, trade_date)
                elif state.shares > 0:
                    state.pending = selection
                else:
                    old_symbol = state.current_symbol
                    _sell_parking(
                        state, market_data, trade_date, trade_date, settings, trades, "年度优选变化"
                    )
                    _activate_selection(state, selection, trade_date)
                    migrations.append(
                        {
                            "slot": slot,
                            "decision_year": trade_date.year,
                            "old_symbol": old_symbol,
                            "new_symbol": selection.symbol,
                            "exit_date": trade_date,
                            "entry_date": pd.NaT,
                            "status": "旧仓已空，更新等待目标",
                        }
                    )

        for state in states.values():
            _apply_actions(state, _price_row(market_data.get(state.current_symbol), trade_date))
            _apply_actions(state, _price_row(market_data.get(PARKING_SYMBOL), trade_date), parking=True)

        signal_date = trade_date if execution_mode == EXECUTION_SAME_CLOSE else previous_master_date
        for slot, state in states.items():
            if signal_date is None:
                continue
            if execution_mode == EXECUTION_NEXT_CLOSE and slot in changed_slots:
                continue
            _buy_half_long(
                state,
                market_data,
                signal_date,
                trade_date,
                settings,
                trades,
                "美股方向长期半仓建仓",
            )
            signal = _signal_state(
                market_data.get(state.current_symbol),
                signal_date,
                state.ma_period,
                state.threshold_pct,
            )
            if state.pending is not None and state.shares > 0:
                should_exit = signal == 0
                if state.current_strategy == "half_timing" and state.timing_shares <= 0 and slot in changed_slots:
                    should_exit = True
                if should_exit:
                    old_symbol = state.current_symbol
                    target = state.pending
                    _sell_current(
                        state,
                        market_data,
                        signal_date,
                        trade_date,
                        settings,
                        trades,
                        "旧标的退出后整批迁移",
                    )
                    _activate_selection(state, target, trade_date)
                    target_signal = _signal_state(
                        market_data.get(state.current_symbol),
                        signal_date,
                        state.ma_period,
                        state.threshold_pct,
                    )
                    entered = False
                    if target_signal == 1:
                        _buy_active(
                            state,
                            market_data,
                            signal_date,
                            trade_date,
                            settings,
                            trades,
                            "迁入当年优选",
                        )
                        entered = state.shares > 0
                    else:
                        _buy_parking(
                            state,
                            market_data,
                            signal_date,
                            trade_date,
                            settings,
                            trades,
                            "等待新标合买入信号",
                        )
                    migrations.append(
                        {
                            "slot": slot,
                            "decision_year": target.year,
                            "old_symbol": old_symbol,
                            "new_symbol": target.symbol,
                            "exit_date": trade_date,
                            "entry_date": trade_date if entered else pd.NaT,
                            "status": "同日迁入" if entered else ("转入512890等待" if state.parking_shares > 0 else "转入现金等待"),
                        }
                    )
                continue

            if signal == 1:
                _buy_active(
                    state,
                    market_data,
                    signal_date,
                    trade_date,
                    settings,
                    trades,
                    "均线买入信号",
                    initial_half_hold=(trade_date == start_date),
                )
            elif signal == 0 and state.timing_shares > 0:
                price = _last_price(market_data.get(state.current_symbol), trade_date)
                trade = _trade(
                    state,
                    symbol=state.current_symbol,
                    name=state.current_name,
                    leg="timing",
                    action="sell",
                    signal_date=signal_date,
                    execution_date=trade_date,
                    price=price,
                    settings=settings,
                    reason="均线卖出信号",
                )
                if trade:
                    trades.append(trade)
                _buy_parking(
                    state,
                    market_data,
                    signal_date,
                    trade_date,
                    settings,
                    trades,
                    "方向空仓承接",
                )
            elif signal == 0 and state.current_strategy == "timing" and state.shares <= 0:
                _buy_parking(
                    state,
                    market_data,
                    signal_date,
                    trade_date,
                    settings,
                    trades,
                    "方向空仓承接",
                )

        values = {slot: _value_state(state, market_data, trade_date) for slot, state in states.items()}
        total = float(sum(values.values()))
        row = {
            "trade_date": trade_date,
            "portfolio_value": total,
            "cash_value": float(sum(state.cash for state in states.values())),
            "etf_market_value": total - float(sum(state.cash for state in states.values())),
        }
        for slot, value in values.items():
            state = states[slot]
            row[f"{slot}_value"] = value
            row[f"{slot}_symbol"] = state.current_symbol
            row[f"{slot}_shares"] = state.shares
            row[f"{slot}_parking_shares"] = state.parking_shares
        rows.append(row)
        previous_date = trade_date
        previous_master_date = trade_date
        if progress_callback and date_index % max(1, len(master_dates) // 100) == 0:
            progress_callback(f"逐日模拟：{trade_date.date()}", date_index / max(1, len(master_dates)))
    daily = pd.DataFrame(rows)
    trade_frame = pd.DataFrame(trades)
    migration_frame = pd.DataFrame(migrations)
    contribution = calculate_direction_contribution(daily, weights, settings.initial_capital)
    return daily, trade_frame, migration_frame, contribution


def _simulate_annual_hold_benchmark(
    market_data: dict[str, pd.DataFrame],
    selections: list[AnnualSelection],
    settings: AnnualBacktestSettings,
) -> pd.DataFrame:
    selection_map = _selection_map(selections)
    start_selections = [item for item in selections if item.year == settings.start_year]
    weights = _initial_weights(start_selections)
    end_date = pd.Timestamp(settings.end_date or pd.Timestamp.today()).normalize()
    dates = pd.DatetimeIndex(
        sorted(
            {
                pd.Timestamp(date).normalize()
                for item in selections
                for date in pd.to_datetime(market_data.get(item.symbol, pd.DataFrame()).get("trade_date", []), errors="coerce")
                if pd.notna(date) and pd.Timestamp(date).year >= settings.start_year and pd.Timestamp(date) <= end_date
            }
        )
    )
    effective = _annual_effective_dates(dates)
    dates = dates[dates >= effective[settings.start_year]]
    states = {
        slot: {"cash": settings.initial_capital * weight / 100, "shares": 0.0, "symbol": "", "last_price": np.nan}
        for slot, weight in weights.items()
    }
    rows = []
    commission_total = 0.0
    trade_count = 0
    previous_date = None
    for trade_date in dates:
        for slot, state in states.items():
            if previous_date is not None and state["cash"] > 0:
                state["cash"] *= (1 + settings.cash_annual_rate) ** ((trade_date - previous_date).days / 365)
            current_frame = market_data.get(state["symbol"])
            current_row = _price_row(current_frame, trade_date)
            if current_row is not None and state["shares"] > 0:
                state["shares"] = _apply_split(
                    state["shares"],
                    float(current_row.get("share_split_ratio", 1.0) or 1.0),
                    str(current_row.get("share_split_rounding", "") or ""),
                )
                state["cash"] += state["shares"] * float(current_row.get("dividend_per_share", 0.0) or 0.0)
            if effective.get(trade_date.year) == trade_date:
                target = selection_map.get((trade_date.year, slot))
                if target is not None and target.symbol != state["symbol"]:
                    old_price = _last_price(current_frame, trade_date)
                    if state["shares"] > 0 and np.isfinite(old_price):
                        gross = state["shares"] * old_price
                        commission = gross * settings.commission_rate
                        state["cash"] += gross - commission
                        commission_total += commission
                        trade_count += 1
                        state["shares"] = 0.0
                    state["symbol"] = target.symbol
            row = _price_row(market_data.get(state["symbol"]), trade_date)
            if state["shares"] <= 0 and row is not None:
                price = float(row["raw_close"])
                quantity = floor(state["cash"] / (price * (1 + settings.commission_rate)) / settings.lot_size) * settings.lot_size
                if quantity > 0:
                    gross = quantity * price
                    commission = gross * settings.commission_rate
                    state["cash"] -= gross + commission
                    commission_total += commission
                    trade_count += 1
                    state["shares"] = float(quantity)
            price = _last_price(market_data.get(state["symbol"]), trade_date)
            if np.isfinite(price):
                state["last_price"] = price
        value = sum(state["cash"] + state["shares"] * (state["last_price"] if np.isfinite(state["last_price"]) else 0) for state in states.values())
        rows.append(
            {
                "trade_date": trade_date,
                "portfolio_value": value,
                "trade_count": trade_count,
                "commission_cost": commission_total,
            }
        )
        previous_date = trade_date
    return pd.DataFrame(rows)


def _simulate_parking_benchmark(
    market_data: dict[str, pd.DataFrame],
    master_dates: Iterable[pd.Timestamp],
    settings: AnnualBacktestSettings,
) -> pd.DataFrame:
    frame = market_data.get(PARKING_SYMBOL)
    if frame is None or frame.empty:
        raise ValueError("缺少512890正式行情，无法生成承接ETF基准。")
    dates = (
        pd.DatetimeIndex(pd.to_datetime(list(master_dates), errors="coerce"))
        .dropna()
        .unique()
        .sort_values()
    )
    if dates.empty:
        raise ValueError("缺少共同回测日期，无法生成512890基准。")
    cash = float(settings.initial_capital)
    shares = 0.0
    last_price = np.nan
    previous_date = None
    commission_total = 0.0
    trade_count = 0
    rows = []
    for trade_date in dates:
        row = _price_row(frame, trade_date)
        if previous_date is not None and cash > 0:
            cash *= (1 + settings.cash_annual_rate) ** ((trade_date - previous_date).days / 365)
        if row is not None:
            shares = _apply_split(
                shares,
                float(row.get("share_split_ratio", 1.0) or 1.0),
                str(row.get("share_split_rounding", "") or ""),
            )
            if shares > 0:
                cash += shares * float(row.get("dividend_per_share", 0.0) or 0.0)
            last_price = float(row["raw_close"])
        if shares <= 0 and trade_date >= PARKING_LISTING_DATE and row is not None:
            price = float(row["raw_close"])
            quantity = (
                floor(cash / (price * (1 + settings.commission_rate)) / settings.lot_size)
                * settings.lot_size
            )
            if quantity > 0:
                gross = quantity * price
                commission = gross * settings.commission_rate
                cash -= gross + commission
                shares = float(quantity)
                commission_total += commission
                trade_count += 1
        rows.append(
            {
                "trade_date": trade_date,
                "portfolio_value": cash + shares * (last_price if np.isfinite(last_price) else 0.0),
                "trade_count": trade_count,
                "commission_cost": commission_total,
            }
        )
        previous_date = trade_date
    return pd.DataFrame(rows)


def calculate_direction_contribution(
    daily: pd.DataFrame,
    weights: dict[str, float],
    initial_capital: float,
) -> pd.DataFrame:
    portfolio_return = pd.to_numeric(daily["portfolio_value"], errors="coerce").pct_change().dropna()
    variance = float(portfolio_return.var(ddof=1)) if len(portfolio_return) > 1 else 0.0
    rows = []
    for slot, weight in weights.items():
        values = pd.to_numeric(daily[f"{slot}_value"], errors="coerce")
        initial = initial_capital * weight / 100
        contribution_return = values.diff().div(pd.to_numeric(daily["portfolio_value"], errors="coerce").shift(1)).dropna()
        aligned = pd.concat([contribution_return.rename("component"), portfolio_return.rename("portfolio")], axis=1).dropna()
        covariance = float(aligned.cov().loc["component", "portfolio"]) if len(aligned) > 1 else 0.0
        risk_share = covariance / variance * 100 if variance > 0 else 0.0
        volatility_points = covariance / np.sqrt(variance) * np.sqrt(252) * 100 if variance > 0 else 0.0
        rows.append(
            {
                "slot": slot,
                "initial_capital": initial,
                "final_value": float(values.iloc[-1]),
                "profit": float(values.iloc[-1] - initial),
                "return_contribution_pct": float((values.iloc[-1] - initial) / initial_capital * 100),
                "risk_contribution_pct": risk_share,
                "annual_volatility_contribution_points": volatility_points,
            }
        )
    return pd.DataFrame(rows)


def _yearly_table(daily: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    series_columns = {
        "portfolio_value": "年度动态组合",
        "annual_hold_value": "年度选择一直持有",
        "parking_value": "全部持有512890",
        "next_close_value": "次日收盘压力",
    }
    rows = []
    for column, label in series_columns.items():
        if column not in daily:
            continue
        previous = initial_capital
        for year, group in daily.groupby(pd.to_datetime(daily["trade_date"]).dt.year):
            values = pd.to_numeric(group[column], errors="coerce").dropna()
            if values.empty:
                continue
            ending = float(values.iloc[-1])
            rows.append(
                {
                    "series": label,
                    "year": int(year),
                    "start_date": pd.Timestamp(group["trade_date"].iloc[0]),
                    "end_date": pd.Timestamp(group["trade_date"].iloc[-1]),
                    "start_value": previous,
                    "end_value": ending,
                    "return_pct": (ending / previous - 1) * 100 if previous else np.nan,
                }
            )
            previous = ending
    return pd.DataFrame(rows)


def _summary_row(
    label: str,
    daily: pd.DataFrame,
    settings: AnnualBacktestSettings,
    trades: pd.DataFrame | None = None,
) -> dict[str, object]:
    metrics = performance_metrics(daily, settings.initial_capital, settings.cash_annual_rate)
    metrics["series"] = label
    if trades is not None:
        metrics["trade_count"] = int(len(trades))
        metrics["commission_cost"] = (
            float(pd.to_numeric(trades.get("commission"), errors="coerce").fillna(0).sum())
            if not trades.empty
            else 0.0
        )
    else:
        metrics["trade_count"] = (
            int(pd.to_numeric(daily["trade_count"], errors="coerce").iloc[-1])
            if "trade_count" in daily and not daily.empty
            else pd.NA
        )
        metrics["commission_cost"] = (
            float(pd.to_numeric(daily["commission_cost"], errors="coerce").iloc[-1])
            if "commission_cost" in daily and not daily.empty
            else 0.0
        )
    return metrics


def build_markdown_report(
    summary: pd.DataFrame,
    selections: pd.DataFrame,
    settings: AnnualBacktestSettings,
    fingerprint: str,
    details: dict[str, pd.DataFrame] | None = None,
) -> str:
    actual_end_date: object = settings.end_date
    if not summary.empty and "end_date" in summary.columns:
        parsed_end_date = pd.to_datetime(summary["end_date"], errors="coerce").max()
        if pd.notna(parsed_end_date):
            actual_end_date = parsed_end_date.strftime("%Y-%m-%d")
    lines = [
        "# 历史年度 ETF 动态组合回测报告",
        "",
        f"- 起投年份：{settings.start_year}",
        f"- 实际结束日期：{actual_end_date}",
        f"- 初始资金：{settings.initial_capital:,.2f} 元，此后不追加资金",
        f"- 单边手续费：{settings.commission_rate * 10000:.2f} 万分点",
        f"- 现金年利率：{settings.cash_annual_rate * 100:.2f}%",
        f"- 整数手：{settings.lot_size} 份",
        f"- 注册表版本：{settings.registry_version}",
        f"- 指数族版本：{settings.whitelist_version}",
        f"- 数据指纹：`{fingerprint}`",
        "- 候选范围：注册表快照日仍上市的ETF，明确存在生存者偏差。",
        "- 主结果：当日收盘产生信号并按同一收盘全额成交的理想化模拟。",
        "- 压力结果：冻结相同年度选择和参数，仅改为下一交易日收盘成交。",
        "- 代理研究：仅用于上市前研究链，不会让未上市ETF提前成为可交易标的。",
        "",
        "## 结果摘要",
        "",
        summary.to_markdown(index=False) if not summary.empty else "无结果。",
        "",
        "## 年度选择",
        "",
        selections.to_markdown(index=False) if not selections.empty else "无年度选择。",
    ]
    detail_frames = details or {}
    for title, key in (
        ("年度收益", "yearly"),
        ("方向收益与风险贡献", "contribution"),
        ("实际迁移", "migrations"),
        ("失败明细", "errors"),
    ):
        frame = detail_frames.get(key)
        lines.extend(["", f"## {title}", ""])
        lines.append(
            frame.to_markdown(index=False)
            if frame is not None and not frame.empty
            else "无明细。"
        )
    lines.extend(
        [
            "",
            "## 附件说明",
            "",
            "页面可另行下载年度资格、25组参数遍历、实际交易、迁移、方向贡献、失败明细和每日净值CSV。",
        ]
    )
    return "\n".join(lines)


def _frame_fingerprint(frame: pd.DataFrame | None) -> dict[str, object]:
    if frame is None or frame.empty:
        return {"rows": 0}
    columns = sorted(str(column) for column in frame.columns)
    stable = frame.reindex(columns=columns).copy()
    for column in columns:
        stable[column] = stable[column].map(lambda value: "" if pd.isna(value) else str(value))
    encoded = stable.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return {
        "rows": len(stable),
        "columns": columns,
        "hash": sha256(encoded).hexdigest(),
    }


def data_fingerprint(
    market_data: dict[str, pd.DataFrame],
    settings: AnnualBacktestSettings,
    *,
    records: Iterable[HistoricalEtfRecord] | None = None,
    whitelist: dict[str, object] | None = None,
    proxy_data: dict[str, pd.DataFrame] | None = None,
) -> str:
    payload: dict[str, object] = {
        "settings": asdict(settings),
        "registry": [asdict(item) for item in sorted(records or [], key=lambda item: item.symbol)],
        "whitelist": whitelist or {},
        "market_data": {},
        "proxy_data": {},
    }
    for symbol in sorted(market_data):
        payload["market_data"][symbol] = _frame_fingerprint(market_data[symbol])
    for symbol in sorted(proxy_data or {}):
        payload["proxy_data"][symbol] = _frame_fingerprint((proxy_data or {})[symbol])
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return sha256(encoded).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


class AnnualCheckpointStore:
    def __init__(self, directory: str | Path, fingerprint: str):
        self.directory = Path(directory) / fingerprint
        self.fingerprint = fingerprint

    def _path(self, stage: str) -> Path:
        return self.directory / f"{stage}.csv"

    def load(self, stage: str) -> pd.DataFrame | None:
        path = self._path(stage)
        if not path.exists():
            return None
        try:
            frame = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        if list(frame.columns) == ["_empty"]:
            return pd.DataFrame()
        return frame

    def save(self, stage: str, frame: pd.DataFrame) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(stage)
        handle, temp_name = tempfile.mkstemp(prefix=f".{stage}.", suffix=".tmp", dir=self.directory)
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            to_save = frame if len(frame.columns) else pd.DataFrame({"_empty": []})
            to_save.to_csv(temp_path, index=False, encoding="utf-8-sig")
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        _atomic_write_json(
            self.directory / "manifest.json",
            {
                "fingerprint": self.fingerprint,
                "stages": sorted(item.stem for item in self.directory.glob("*.csv")),
            },
        )
        return path


def _restore_checkpoint_dates(frame: pd.DataFrame) -> pd.DataFrame:
    restored = frame.copy()
    for column in restored.columns:
        if column == "trade_date" or column.endswith("_date"):
            restored[column] = pd.to_datetime(restored[column], errors="coerce")
    return restored


def _restore_selections(frame: pd.DataFrame) -> list[AnnualSelection]:
    selections: list[AnnualSelection] = []
    for row in frame.to_dict("records"):
        relaxed = str(row.get("return_gate_relaxed", "")).strip().lower() in {"1", "true", "yes"}
        selections.append(
            AnnualSelection(
                year=int(row["year"]),
                slot=str(row["slot"]),
                symbol=_normalize_symbol(row["symbol"]),
                name=str(row["name"]),
                ma_period=int(row["ma_period"]),
                threshold_pct=float(row["threshold_pct"]),
                strategy=str(row["strategy"]),
                validation_score=float(row["validation_score"]),
                validation_annual_return_pct=float(row["validation_annual_return_pct"]),
                validation_sharpe=float(row["validation_sharpe"]),
                return_gate_relaxed=relaxed,
                proxy_ratio_pct=float(row["proxy_ratio_pct"]),
                decision_date=pd.Timestamp(row["decision_date"]),
            )
        )
    return selections


def run_annual_etf_backtest(
    records: list[HistoricalEtfRecord],
    whitelist: dict[str, object],
    market_data: dict[str, pd.DataFrame],
    settings: AnnualBacktestSettings,
    *,
    proxy_data: dict[str, pd.DataFrame] | None = None,
    checkpoint_dir: str | Path | None = None,
    progress_callback: Callable[[str, float], None] | None = None,
) -> AnnualPortfolioResult:
    registered_versions = {item.registry_version for item in records if item.registry_version}
    if registered_versions and registered_versions != {settings.registry_version}:
        raise ValueError(
            f"注册表版本{sorted(registered_versions)}与回测设置{settings.registry_version}不一致。"
        )
    configured_whitelist_version = str(whitelist.get("version", ""))
    if (
        configured_whitelist_version
        and configured_whitelist_version != settings.whitelist_version
    ):
        raise ValueError(
            f"白名单版本{configured_whitelist_version}与回测设置{settings.whitelist_version}不一致。"
        )
    fingerprint = data_fingerprint(
        market_data,
        settings,
        records=records,
        whitelist=whitelist,
        proxy_data=proxy_data,
    )
    store = AnnualCheckpointStore(checkpoint_dir, fingerprint) if checkpoint_dir else None
    final_stages = (
        "summary",
        "daily",
        "yearly",
        "selections",
        "qualification",
        "parameters",
        "trades",
        "migrations",
        "contribution",
        "errors",
    )
    if store:
        cached_final = {stage: store.load(stage) for stage in final_stages}
        if all(frame is not None for frame in cached_final.values()):
            if progress_callback:
                progress_callback("读取完整检查点", 1.0)
            restored = {
                stage: _restore_checkpoint_dates(frame)
                for stage, frame in cached_final.items()
            }
            report = build_markdown_report(
                restored["summary"],
                restored["selections"],
                settings,
                fingerprint,
                restored,
            )
            return AnnualPortfolioResult(
                summary=restored["summary"],
                daily=restored["daily"],
                yearly=restored["yearly"],
                selections=restored["selections"],
                qualification=restored["qualification"],
                parameters=restored["parameters"],
                trades=restored["trades"],
                migrations=restored["migrations"],
                contribution=restored["contribution"],
                errors=restored["errors"],
                report_markdown=report,
                fingerprint=fingerprint,
            )

    if progress_callback:
        progress_callback("准备数据", 0.02)
    cached_selection = store.load("selections") if store else None
    cached_qualification = store.load("qualification") if store else None
    cached_parameters = store.load("parameters") if store else None
    if (
        cached_selection is not None
        and not cached_selection.empty
        and cached_qualification is not None
        and cached_parameters is not None
    ):
        selection_df = _restore_checkpoint_dates(cached_selection)
        selections = _restore_selections(selection_df)
        parameters = _restore_checkpoint_dates(cached_parameters)
        preflight_errors = store.load("preflight_errors")
        selection_errors = store.load("selection_errors")
        preflight = AnnualQualificationResult(
            qualification=_restore_checkpoint_dates(cached_qualification),
            errors=preflight_errors if preflight_errors is not None else pd.DataFrame(),
        )
        if selection_errors is None:
            selection_errors = pd.DataFrame()
        if progress_callback:
            progress_callback("读取年度筛选检查点", 0.50)
    else:
        preflight = preflight_annual_candidates(
            records, whitelist, market_data, settings, proxy_data
        )
        if store:
            store.save("qualification", preflight.qualification)
            store.save("preflight_errors", preflight.errors)
        selection_progress = (
            (lambda label, fraction: progress_callback(label, 0.10 + 0.40 * fraction))
            if progress_callback
            else None
        )
        selections, parameters, selection_errors = build_annual_selections(
            records, preflight, settings, selection_progress
        )
        selection_df = selections_frame(selections)
        if store:
            store.save("selections", selection_df)
            store.save("parameters", parameters)
            store.save("selection_errors", selection_errors)

    missing_start = set(ALL_SLOTS) - {
        item.slot for item in selections if item.year == settings.start_year
    }
    if missing_start:
        errors = pd.concat([preflight.errors, selection_errors], ignore_index=True)
        detail = "、".join(sorted(missing_start))
        raise ValueError(
            f"起投年度缺少合格方向：{detail}。请先补齐注册表、代理或正式行情。\n"
            f"{errors.to_string(index=False)}"
        )

    main_stage_names = ("daily", "trades", "migrations", "contribution")
    cached_main = {stage: store.load(stage) for stage in main_stage_names} if store else {}
    if store and all(frame is not None for frame in cached_main.values()):
        daily = _restore_checkpoint_dates(cached_main["daily"])
        trades = _restore_checkpoint_dates(cached_main["trades"])
        migrations = _restore_checkpoint_dates(cached_main["migrations"])
        contribution = cached_main["contribution"]
        if progress_callback:
            progress_callback("读取主模拟检查点", 0.70)
    else:
        if progress_callback:
            progress_callback("逐日模拟：主结果", 0.55)
        main_progress = (
            (lambda label, fraction: progress_callback(label, 0.55 + 0.15 * fraction))
            if progress_callback
            else None
        )
        daily, trades, migrations, contribution = simulate_annual_portfolio(
            market_data,
            selections,
            settings,
            execution_mode=EXECUTION_SAME_CLOSE,
            progress_callback=main_progress,
        )
        if store:
            for stage, frame in zip(
                main_stage_names, (daily, trades, migrations, contribution)
            ):
                store.save(stage, frame)

    cached_stress = store.load("stress") if store else None
    cached_stress_trades = store.load("stress_trades") if store else None
    if cached_stress is not None and cached_stress_trades is not None:
        stress = _restore_checkpoint_dates(cached_stress)
        stress_trades = _restore_checkpoint_dates(cached_stress_trades)
    else:
        if progress_callback:
            progress_callback("逐日模拟：次日收盘压力", 0.75)
        stress, stress_trades, _stress_migrations, _stress_contribution = simulate_annual_portfolio(
            market_data, selections, settings, execution_mode=EXECUTION_NEXT_CLOSE
        )
        if store:
            store.save("stress", stress)
            store.save("stress_trades", stress_trades)

    hold = store.load("annual_hold") if store else None
    if hold is None:
        hold = _simulate_annual_hold_benchmark(market_data, selections, settings)
        if store:
            store.save("annual_hold", hold)
    else:
        hold = _restore_checkpoint_dates(hold)
    parking = store.load("parking_benchmark") if store else None
    if parking is None:
        parking = _simulate_parking_benchmark(market_data, daily["trade_date"], settings)
        if store:
            store.save("parking_benchmark", parking)
    else:
        parking = _restore_checkpoint_dates(parking)

    comparison = daily[["trade_date", "portfolio_value"]].rename(
        columns={"portfolio_value": "main_value"}
    )
    comparison = (
        comparison.merge(
            hold[["trade_date", "portfolio_value"]].rename(
                columns={"portfolio_value": "annual_hold_value"}
            ),
            on="trade_date",
            how="left",
        )
        .merge(
            parking[["trade_date", "portfolio_value"]].rename(
                columns={"portfolio_value": "parking_value"}
            ),
            on="trade_date",
            how="left",
        )
        .merge(
            stress[["trade_date", "portfolio_value"]].rename(
                columns={"portfolio_value": "next_close_value"}
            ),
            on="trade_date",
            how="left",
        )
    )
    for column in ("annual_hold_value", "parking_value", "next_close_value"):
        comparison[column] = pd.to_numeric(comparison[column], errors="coerce").ffill()
    comparison_indexed = comparison.set_index("trade_date")
    for column in comparison.columns:
        if column.endswith("_value"):
            daily[column] = comparison_indexed[column].reindex(daily["trade_date"]).to_numpy()

    summary = pd.DataFrame(
        [
            _summary_row("年度动态组合", daily, settings, trades),
            _summary_row("年度选择一直持有", hold, settings),
            _summary_row("全部持有512890", parking, settings),
            _summary_row("次日收盘压力", stress, settings, stress_trades),
        ]
    )
    yearly = _yearly_table(daily, settings.initial_capital)
    errors = pd.concat([preflight.errors, selection_errors], ignore_index=True)
    report = build_markdown_report(
        summary,
        selection_df,
        settings,
        fingerprint,
        {
            "yearly": yearly,
            "contribution": contribution,
            "migrations": migrations,
            "errors": errors,
        },
    )
    if store:
        final_frames = {
            "summary": summary,
            "daily": daily,
            "yearly": yearly,
            "selections": selection_df,
            "qualification": preflight.qualification,
            "parameters": parameters,
            "trades": trades,
            "migrations": migrations,
            "contribution": contribution,
            "errors": errors,
        }
        for stage, frame in final_frames.items():
            store.save(stage, frame)
    if progress_callback:
        progress_callback("生成报告", 1.0)
    return AnnualPortfolioResult(
        summary=summary,
        daily=daily,
        yearly=yearly,
        selections=selection_df,
        qualification=preflight.qualification,
        parameters=parameters,
        trades=trades,
        migrations=migrations,
        contribution=contribution,
        errors=errors,
        report_markdown=report,
        fingerprint=fingerprint,
    )
