from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.cache import load_dataset, save_dataset
from services.fund_analysis import (
    analyze_fund_nav,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
    normalize_nav_dataframe,
)
from services.futures_options_analysis import (
    DATA_TYPE_AUTO,
    DATA_TYPE_OPTIONS,
    FUTURES_OPTION_DATA_VERSION,
    add_indicators,
    build_summary as build_futures_option_summary,
    fetch_futures_option_data,
    normalize_option_symbol,
)
from services.futures_spread import (
    SPREAD_CALCULATION_VERSION,
    build_spread_summary,
    calculate_spreads,
    contract_name,
    fetch_contracts,
    parse_contracts,
    spread_respects_contract_cutoffs,
)


DEFAULT_ETF_CODES = ["512890", "159201", "159545", "513260", "159655", "159501", "518850"]
DEFAULT_SPREAD_CONTRACTS = ["I2609", "I2701"]
DEFAULT_OPTION_CODES = ["I2609P730", "I2609P740", "I2609P750", "I2609P760"]


@dataclass
class PositionItem:
    category: str
    code: str
    name: str
    status: str
    source: str = ""
    latest_date: str = ""
    cache_time: str = ""
    metrics: dict[str, object] = field(default_factory=dict)
    dataframe: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: str = ""


def parse_position_codes(text: str) -> list[str]:
    return parse_contracts(text)


def format_cache_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def _load_dataset_if_ready(symbol: str, source: str, data_type: str, period: str = "1d"):
    cached_df, meta = load_dataset(symbol, source, data_type, period=period)
    if cached_df is None or cached_df.empty:
        return None, meta
    return cached_df, meta


def _merge_by_date(old_df: pd.DataFrame | None, new_df: pd.DataFrame, date_column: str) -> pd.DataFrame:
    if old_df is None or old_df.empty:
        merged = new_df.copy()
    else:
        merged = pd.concat([old_df, new_df], ignore_index=True)
    if date_column not in merged.columns:
        return merged
    merged[date_column] = pd.to_datetime(merged[date_column], errors="coerce")
    merged = merged.dropna(subset=[date_column])
    return merged.sort_values(date_column).drop_duplicates(date_column, keep="last").reset_index(drop=True)


def _round_metric(value: object, digits: int = 2) -> object:
    if value is None or pd.isna(value):
        return float("nan")
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def _current_cache_time_text() -> str:
    return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")


def _cache_has_today(df: pd.DataFrame | None, date_column: str = "date") -> bool:
    if df is None or df.empty or date_column not in df.columns:
        return False
    dates = pd.to_datetime(df[date_column], errors="coerce").dropna()
    if dates.empty:
        return False
    today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
    return dates.max().normalize() >= today


def _futures_option_cache_key(symbol: str, data_type: str, period: str, count: int) -> str:
    safe_symbol = symbol.strip().replace(".", "_").replace("/", "_")
    return f"futures_option_{safe_symbol}_{data_type}_{period}_{int(count)}"


def _futures_option_cache_candidates(symbol: str, period: str, count: int) -> list[str]:
    symbols = []
    for candidate in (symbol.strip(), normalize_option_symbol(symbol), symbol.strip().upper()):
        if candidate and candidate not in symbols:
            symbols.append(candidate)

    keys = []
    for candidate in symbols:
        for data_type in (DATA_TYPE_OPTIONS, DATA_TYPE_AUTO):
            key = _futures_option_cache_key(candidate, data_type, period, count)
            if key not in keys:
                keys.append(key)
    return keys


def _market_cache_is_usable(df: pd.DataFrame | None) -> bool:
    return df is not None and not df.empty and "date" in df.columns and "close" in df.columns


def _spread_cache_matches_contracts(df: pd.DataFrame | None, contracts: list[str], base_contract: str) -> bool:
    if df is None or df.empty or "date" not in df.columns:
        return False
    required_columns = [f"{base_contract}_close"]
    required_columns.extend(
        f"spread_{base_contract}_vs_{contract}"
        for contract in contracts
        if contract != base_contract
    )
    if not all(column in df.columns for column in required_columns):
        return False
    if "_calculation_version" not in df.columns:
        return False
    if not df["_calculation_version"].eq(SPREAD_CALCULATION_VERSION).all():
        return False
    return spread_respects_contract_cutoffs(df, contracts, base_contract)


def _missing_item(category: str, code: str, name: str = "") -> PositionItem:
    return PositionItem(
        category=category,
        code=code,
        name=name or code,
        status="无缓存",
        error="本地暂无缓存；点击「加载持仓信息」可联网补齐。",
    )


def load_or_fetch_etf(
    code: str,
    *,
    api_key: str = "",
    count: int = 5000,
    adjust: str | None = "forward",
    ma_periods: list[int] | tuple[int, ...] = (20, 60, 120, 250),
    rsi_period: int = 14,
    base_date: str = "2024-09-24",
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
) -> PositionItem:
    raw_code = code.strip()
    try:
        symbol = infer_tickflow_symbol(raw_code)
    except Exception as exc:
        return PositionItem("ETF", raw_code, raw_code, "失败", error=str(exc))

    cache_symbol = f"fund_close_{symbol}_{adjust or 'none'}"
    period = f"{int(count)}_1d"
    cached_df, cache_meta = _load_dataset_if_ready(cache_symbol, "tickflow", "fund_close_raw", period=period)
    used_cache = cached_df is not None and not force_refresh
    source_df = cached_df.copy() if used_cache else None
    source = "本地缓存" if used_cache else "TickFlow"
    status = "缓存"
    error = ""

    if source_df is None:
        if not allow_fetch:
            return _missing_item("ETF", raw_code, symbol)
        try:
            if cached_df is not None:
                incremental_count = min(max(120, int(count) // 20), int(count))
                latest_df = fetch_tickflow_fund_close(
                    symbol=symbol,
                    api_key=api_key,
                    count=incremental_count,
                    adjust=adjust,
                )
                source_df = _merge_by_date(cached_df, latest_df, "日期")
                status = "已增量更新"
            else:
                source_df = fetch_tickflow_fund_close(
                    symbol=symbol,
                    api_key=api_key,
                    count=int(count),
                    adjust=adjust,
                )
                status = "已更新"
            if save_to_cache:
                save_dataset(
                    symbol=cache_symbol,
                    name=f"{symbol} 场内基金/股票原始收盘价",
                    source="tickflow",
                    data_type="fund_close_raw",
                    period=period,
                    df=source_df,
                )
        except Exception as exc:
            if cached_df is None:
                return PositionItem("ETF", raw_code, symbol, "失败", source="TickFlow", error=str(exc))
            source_df = cached_df.copy()
            source = "本地缓存（刷新失败）"
            status = "缓存"
            error = str(exc)

    try:
        fund_name, nav_df = normalize_nav_dataframe(source_df, fallback_name=f"{symbol} ETF")
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=ma_periods,
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
    return PositionItem(
        category="ETF",
        code=symbol,
        name=str(summary.get("基金名称") or fund_name),
        status=status,
        source=source,
        latest_date=str(summary.get("最新日期") or ""),
        cache_time=(
            _current_cache_time_text()
            if status != "缓存" and save_to_cache
            else format_cache_time(cache_meta.get("last_update_time") if cache_meta else "")
        ),
        metrics=metrics,
        dataframe=result.dataframe,
        error=error,
    )


def load_or_fetch_spread(
    contracts: list[str],
    *,
    base_contract: str | None = None,
    api_key: str = "",
    max_workers: int = 2,
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
) -> PositionItem:
    contracts = [contract.strip().upper() for contract in contracts if contract.strip()]
    if len(contracts) < 2:
        return PositionItem("期货价差", " ".join(contracts), "期货价差", "失败", error="至少需要两个合约。")

    base_contract = (base_contract or contracts[0]).strip().upper()
    cache_symbol = f"futures_spread_{base_contract}"
    cached_df, cache_meta = _load_dataset_if_ready(cache_symbol, "akshare", "futures_spread")
    cache_ready = _spread_cache_matches_contracts(cached_df, contracts, base_contract)
    refresh_stale_cache = allow_fetch and cache_ready and not _cache_has_today(cached_df, "date")
    source = "本地缓存"
    status = "缓存"
    error = ""

    if cached_df is not None and cache_ready and not force_refresh and not refresh_stale_cache:
        spread_df = cached_df.copy()
        spread_df["date"] = pd.to_datetime(spread_df["date"], errors="coerce")
    else:
        if not allow_fetch:
            code = f"{base_contract} - {'/'.join(contract for contract in contracts if contract != base_contract)}"
            return _missing_item("期货价差", code, "期货价差")
        try:
            data, errors = fetch_contracts(contracts, max_workers=max_workers, api_key=api_key)
            spread_df = calculate_spreads(data, base_contract)
            source = "TickFlow/AkShare"
            status = "已更新"
            if errors:
                error = " | ".join(errors)
            if save_to_cache:
                save_dataset(
                    symbol=cache_symbol,
                    name=f"{contract_name(base_contract)} 期货价差",
                    source="akshare",
                    data_type="futures_spread",
                    df=spread_df,
                )
        except Exception as exc:
            if cached_df is None or not cache_ready:
                return PositionItem("期货价差", base_contract, "期货价差", "失败", source="TickFlow/AkShare", error=str(exc))
            spread_df = cached_df.copy()
            spread_df["date"] = pd.to_datetime(spread_df["date"], errors="coerce")
            source = "本地缓存（刷新失败）"
            status = "缓存"
            error = str(exc)

    available_contracts = [contract for contract in contracts if f"{contract}_close" in spread_df.columns]
    summary_df = build_spread_summary(spread_df, available_contracts, base_contract)
    if summary_df.empty:
        return PositionItem("期货价差", base_contract, "期货价差", "失败", source=source, error="没有可展示的价差统计。")

    other_contract = next((contract for contract in available_contracts if contract != base_contract), "")
    spread_col = f"spread_{base_contract}_vs_{other_contract}"
    latest_rows = spread_df.dropna(subset=[spread_col]) if spread_col in spread_df.columns else pd.DataFrame()
    latest_row = latest_rows.iloc[-1] if not latest_rows.empty else spread_df.iloc[-1]
    summary_row = summary_df.iloc[0]
    code = f"{base_contract} - {other_contract}" if other_contract else base_contract
    spread_daily_change = float("nan")
    if spread_col in spread_df.columns:
        latest_spread = pd.to_numeric(spread_df[spread_col], errors="coerce").dropna()
        if len(latest_spread) >= 2:
            spread_daily_change = latest_spread.iloc[-1] - latest_spread.iloc[-2]
    metrics = {
        "最新价差": _round_metric(summary_row.get("最新价差"), 4),
        "价差日变化": _round_metric(spread_daily_change, 4),
        "最新占比(%)": _round_metric(summary_row.get("最新占比(%)")),
        "平均价差": _round_metric(summary_row.get("平均价差"), 4),
        "最大价差": _round_metric(summary_row.get("最大价差"), 4),
        "最小价差": _round_metric(summary_row.get("最小价差"), 4),
        f"{base_contract}收盘": _round_metric(latest_row.get(f"{base_contract}_close"), 4),
    }
    if other_contract:
        metrics[f"{other_contract}收盘"] = _round_metric(latest_row.get(f"{other_contract}_close"), 4)

    latest_date = ""
    if "date" in latest_row:
        latest_date = pd.Timestamp(latest_row["date"]).strftime("%Y-%m-%d")

    return PositionItem(
        category="期货价差",
        code=code,
        name=f"{contract_name(base_contract)} - {contract_name(other_contract)}" if other_contract else contract_name(base_contract),
        status=status,
        source=source,
        latest_date=latest_date,
        cache_time=(
            _current_cache_time_text()
            if status != "缓存" and save_to_cache
            else format_cache_time(cache_meta.get("last_update_time") if cache_meta else "")
        ),
        metrics=metrics,
        dataframe=spread_df,
        error=error,
    )


def load_or_fetch_option(
    code: str,
    *,
    period: str = "1d",
    count: int = 500,
    ma_periods: list[int] | tuple[int, ...] = (5, 20, 60),
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
) -> PositionItem:
    raw_code = code.strip()
    cache_candidates = _futures_option_cache_candidates(raw_code, period, int(count))
    cached_df = None
    cache_meta = None
    for cache_symbol in cache_candidates:
        candidate_df, candidate_meta = _load_dataset_if_ready(cache_symbol, "market", "futures_option", period=period)
        if _market_cache_is_usable(candidate_df):
            cached_df, cache_meta = candidate_df, candidate_meta
            break

    refresh_stale_cache = allow_fetch and cached_df is not None and not _cache_has_today(cached_df, "date")

    if cached_df is not None and not force_refresh and not refresh_stale_cache:
        result_df = cached_df.copy()
        source = "本地缓存"
        status = "缓存"
        error = ""
    else:
        if not allow_fetch:
            return _missing_item("期权", raw_code, normalize_option_symbol(raw_code))
        try:
            result = fetch_futures_option_data(
                raw_symbol=raw_code,
                data_type=DATA_TYPE_OPTIONS,
                period=period,
                count=int(count),
                api_key="",
                use_free=True,
                ma_periods=ma_periods,
            )
            result_df = result.dataframe.copy()
            if not result.is_chain and "_data_version" not in result_df.columns:
                result_df["_data_version"] = FUTURES_OPTION_DATA_VERSION
            source = result.source
            status = "已更新"
            error = ""
            if save_to_cache:
                save_dataset(
                    symbol=_futures_option_cache_key(raw_code, DATA_TYPE_OPTIONS, period, int(count)),
                    name=f"{normalize_option_symbol(raw_code)} 期货期权数据",
                    source="market",
                    data_type="futures_option",
                    period=period,
                    df=result_df,
                )
        except Exception as exc:
            if cached_df is None:
                return PositionItem("期权", raw_code, normalize_option_symbol(raw_code), "失败", source="AkShare", error=str(exc))
            result_df = cached_df.copy()
            source = "本地缓存（刷新失败）"
            status = "缓存"
            error = str(exc)

    try:
        result_df["date"] = pd.to_datetime(result_df["date"], errors="coerce")
        result_df["close"] = pd.to_numeric(result_df["close"], errors="coerce")
        result_df = result_df.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date").reset_index(drop=True)
        result_df = add_indicators(result_df, ma_periods)
        if "_data_version" not in result_df.columns:
            result_df["_data_version"] = FUTURES_OPTION_DATA_VERSION
        summary = build_futures_option_summary(result_df)
    except Exception as exc:
        return PositionItem("期权", raw_code, normalize_option_symbol(raw_code), "失败", source=source, error=str(exc))

    metrics = {
        "最新收盘": _round_metric(summary.get("最新收盘"), 4),
        "日涨跌(%)": _round_metric(result_df.iloc[-1].get("daily_return_pct")),
        "20日涨跌(%)": _round_metric(summary.get("20日涨跌幅(%)")),
        "20日波动(%)": _round_metric(summary.get("20日波动率(%)")),
        "价格百分位": _round_metric(summary.get("价格百分位")),
        "最新成交量": _round_metric(summary.get("最新成交量"), 0),
        "最新持仓量": _round_metric(summary.get("最新持仓量"), 0),
    }
    display_code = normalize_option_symbol(raw_code)
    return PositionItem(
        category="期权",
        code=display_code,
        name=f"{display_code} 铁矿石看跌期权",
        status=status,
        source=source,
        latest_date=str(summary.get("最新日期") or ""),
        cache_time=(
            _current_cache_time_text()
            if status != "缓存" and save_to_cache
            else format_cache_time(cache_meta.get("last_update_time") if cache_meta else "")
        ),
        metrics=metrics,
        dataframe=result_df,
        error=error,
    )
