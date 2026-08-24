from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import save_dataset
from services.futures_options_analysis import (
    DATA_TYPE_OPTIONS,
    FUTURES_OPTION_DATA_VERSION,
    add_indicators,
    append_option_spot_row,
    build_summary as build_futures_option_summary,
    fetch_futures_option_data,
    normalize_option_symbol,
)
from services.futures_spread import (
    append_futures_spot_row,
    build_spread_summary,
    calculate_spreads,
    contract_name,
    fetch_contracts,
    fetch_futures_daily,
)
from services.position_market import (
    _load_dataset_if_ready,
    _merge_by_date,
    _merge_current_day_refresh,
)
from services.position_models import (
    PositionItem,
    _current_cache_time_text,
    _futures_contract_cache_key,
    _futures_option_cache_candidates,
    _futures_option_cache_key,
    _market_cache_is_usable,
    _missing_item,
    _round_metric,
    _spread_cache_matches_contracts,
    format_cache_time,
    format_futures_position_name,
    format_spread_position_name,
    option_display_name,
)
from services.position_sessions import _cache_has_expected_trade_date

def load_or_fetch_futures_contract(
    contract: str,
    *,
    api_key: str = "",
    count: int = 500,
    ma_periods: list[int] | tuple[int, ...] = (5, 20, 60),
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
    realtime_preview: bool = False,
    market_now: datetime | None = None,
) -> PositionItem:
    contract = contract.strip().upper()
    display_name = format_futures_position_name(contract)
    cache_symbol = _futures_contract_cache_key(contract)
    cached_df, cache_meta = _load_dataset_if_ready(
        cache_symbol,
        "market",
        "futures_contract",
    )
    cache_ready = _market_cache_is_usable(cached_df)
    refresh_stale_cache = (
        allow_fetch
        and cache_ready
        and not _cache_has_expected_trade_date(cached_df, "date", market_now)
    )
    source = "本地缓存"
    status = "缓存"
    error = ""

    if cache_ready and not force_refresh and not refresh_stale_cache:
        result_df = cached_df.copy()
    else:
        if not allow_fetch:
            return _missing_item("期货", contract, display_name)
        try:
            if realtime_preview and cache_ready:
                latest_df = append_futures_spot_row(
                    cached_df,
                    contract,
                    replace_current_day=True,
                    market_now=market_now,
                )
            else:
                latest_df = fetch_futures_daily(
                    contract,
                    api_key=api_key,
                    prefer_realtime_snapshot=realtime_preview,
                    market_now=market_now,
                )
            latest_df = latest_df.tail(max(int(count), max(ma_periods))).copy()
            if cache_ready:
                result_df = (
                    _merge_current_day_refresh(cached_df, latest_df, "date")
                    if realtime_preview
                    else _merge_by_date(cached_df, latest_df, "date")
                )
            else:
                result_df = latest_df
            source = "TickFlow/AkShare"
            status = "已增量更新" if cache_ready else "已更新"
        except Exception as exc:
            if not cache_ready:
                return PositionItem(
                    "期货",
                    contract,
                    display_name,
                    "失败",
                    source="TickFlow/AkShare",
                    error=str(exc),
                )
            result_df = cached_df.copy()
            source = "本地缓存（刷新失败）"
            status = "缓存"
            error = str(exc)

    try:
        result_df["date"] = pd.to_datetime(result_df["date"], errors="coerce")
        result_df["close"] = pd.to_numeric(result_df["close"], errors="coerce")
        result_df = (
            result_df.dropna(subset=["date", "close"])
            .sort_values("date")
            .drop_duplicates("date", keep="first")
            .reset_index(drop=True)
        )
        result_df = add_indicators(result_df, ma_periods)
        summary = build_futures_option_summary(result_df)
    except Exception as exc:
        return PositionItem(
            "期货",
            contract,
            display_name,
            "失败",
            source=source,
            error=str(exc),
        )

    metrics = {
        "最新收盘": _round_metric(summary.get("最新收盘"), 4),
        "日涨跌(%)": _round_metric(result_df.iloc[-1].get("daily_return_pct")),
        "20日涨跌(%)": _round_metric(summary.get("20日涨跌幅(%)")),
        "20日波动(%)": _round_metric(summary.get("20日波动率(%)")),
        "价格百分位": _round_metric(summary.get("价格百分位")),
    }
    if status != "缓存" and save_to_cache and not realtime_preview:
        save_dataset(
            symbol=cache_symbol,
            name=f"{display_name}日线",
            source="market",
            data_type="futures_contract",
            df=result_df,
        )

    return PositionItem(
        category="期货",
        code=contract,
        name=display_name,
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


def load_or_fetch_spread(
    contracts: list[str],
    *,
    base_contract: str | None = None,
    api_key: str = "",
    max_workers: int = 2,
    allow_fetch: bool = True,
    force_refresh: bool = False,
    save_to_cache: bool = True,
    realtime_preview: bool = False,
    market_now: datetime | None = None,
) -> PositionItem:
    contracts = [contract.strip().upper() for contract in contracts if contract.strip()]
    if len(contracts) < 2:
        return PositionItem("期货价差", " ".join(contracts), "期货价差", "失败", error="至少需要两个合约。")

    base_contract = (base_contract or contracts[0]).strip().upper()
    configured_other_contract = next((contract for contract in contracts if contract != base_contract), "")
    configured_code = (
        f"{base_contract} - {configured_other_contract}"
        if configured_other_contract
        else base_contract
    )
    configured_name = format_spread_position_name(
        base_contract,
        configured_other_contract,
    )
    cache_symbol = f"futures_spread_{base_contract}"
    cached_df, cache_meta = _load_dataset_if_ready(cache_symbol, "akshare", "futures_spread")
    cache_ready = _spread_cache_matches_contracts(cached_df, contracts, base_contract)
    refresh_stale_cache = allow_fetch and cache_ready and not _cache_has_expected_trade_date(cached_df, "date")
    source = "本地缓存"
    status = "缓存"
    error = ""

    if cached_df is not None and cache_ready and not force_refresh and not refresh_stale_cache:
        spread_df = cached_df.copy()
        spread_df["date"] = pd.to_datetime(spread_df["date"], errors="coerce")
    else:
        if not allow_fetch:
            return _missing_item("期货价差", configured_code, configured_name)
        try:
            if realtime_preview and cached_df is not None and cache_ready:
                data = {}
                errors = []
                today = pd.Timestamp.now(tz="Asia/Shanghai").date()
                for contract in contracts:
                    close_col = f"{contract}_close"
                    contract_df = cached_df[["date", close_col]].rename(
                        columns={close_col: "close"}
                    )
                    contract_df = append_futures_spot_row(
                        contract_df,
                        contract,
                        replace_current_day=True,
                        market_now=market_now,
                    )
                    latest_contract_date = pd.to_datetime(
                        contract_df["date"], errors="coerce"
                    ).max()
                    if (
                        pd.isna(latest_contract_date)
                        or latest_contract_date.date() != today
                    ):
                        raise RuntimeError(f"{contract} 实时源未返回今日行情")
                    data[contract] = contract_df
            else:
                data, errors = fetch_contracts(
                    contracts,
                    max_workers=max_workers,
                    api_key=api_key,
                    prefer_realtime_snapshot=realtime_preview,
                    market_now=market_now,
                )
            latest_spread_df = calculate_spreads(data, base_contract)
            spread_df = (
                _merge_current_day_refresh(cached_df, latest_spread_df, "date")
                if cached_df is not None and cache_ready
                else latest_spread_df
            )
            source = "TickFlow/AkShare"
            status = "已增量更新" if cached_df is not None and cache_ready else "已更新"
            if errors:
                error = " | ".join(errors)
            if save_to_cache and not realtime_preview:
                save_dataset(
                    symbol=cache_symbol,
                    name=f"{contract_name(base_contract)} 期货价差",
                    source="akshare",
                    data_type="futures_spread",
                    df=spread_df,
                )
        except Exception as exc:
            if cached_df is None or not cache_ready:
                return PositionItem(
                    "期货价差",
                    configured_code,
                    configured_name,
                    "失败",
                    source="TickFlow/AkShare",
                    error=str(exc),
                )
            spread_df = cached_df.copy()
            spread_df["date"] = pd.to_datetime(spread_df["date"], errors="coerce")
            source = "本地缓存（刷新失败）"
            status = "缓存"
            error = str(exc)

    available_contracts = [contract for contract in contracts if f"{contract}_close" in spread_df.columns]
    summary_df = build_spread_summary(spread_df, available_contracts, base_contract)
    if summary_df.empty:
        return PositionItem(
            "期货价差",
            configured_code,
            configured_name,
            "失败",
            source=source,
            error="没有可展示的价差统计。",
        )

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
        name=format_spread_position_name(base_contract, other_contract),
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
    realtime_preview: bool = False,
    market_now: datetime | None = None,
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

    refresh_stale_cache = allow_fetch and cached_df is not None and not _cache_has_expected_trade_date(cached_df, "date")

    if cached_df is not None and not force_refresh and not refresh_stale_cache:
        result_df = cached_df.copy()
        source = "本地缓存"
        status = "缓存"
        error = ""
    else:
        if not allow_fetch:
            return _missing_item("期权", raw_code, normalize_option_symbol(raw_code))
        try:
            if realtime_preview and cached_df is not None:
                latest_result_df = append_option_spot_row(
                    cached_df,
                    raw_code,
                    replace_current_day=True,
                    market_now=market_now,
                )
                result_source = "AkShare期权实时快照"
                result_is_chain = False
            else:
                result = fetch_futures_option_data(
                    raw_symbol=raw_code,
                    data_type=DATA_TYPE_OPTIONS,
                    period=period,
                    count=int(count),
                    api_key="",
                    use_free=True,
                    ma_periods=ma_periods,
                    prefer_realtime_snapshot=realtime_preview,
                    market_now=market_now,
                )
                latest_result_df = result.dataframe.copy()
                result_source = result.source
                result_is_chain = result.is_chain
            result_df = (
                _merge_current_day_refresh(cached_df, latest_result_df, "date")
                if cached_df is not None
                else latest_result_df
            )
            if not result_is_chain and "_data_version" not in result_df.columns:
                result_df["_data_version"] = FUTURES_OPTION_DATA_VERSION
            source = result_source
            status = "已增量更新" if cached_df is not None else "已更新"
            error = ""
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
    if status != "缓存" and save_to_cache and not realtime_preview:
        save_dataset(
            symbol=_futures_option_cache_key(raw_code, DATA_TYPE_OPTIONS, period, int(count)),
            name=f"{normalize_option_symbol(raw_code)} 期货期权数据",
            source="market",
            data_type="futures_option",
            period=period,
            df=result_df,
        )
    display_code = normalize_option_symbol(raw_code)
    return PositionItem(
        category="期权",
        code=display_code,
        name=option_display_name(display_code),
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


def refresh_position_derivative_items(
    items: list[PositionItem],
    *,
    api_key: str = "",
    max_workers: int = 2,
    option_count: int = 500,
    market_now: datetime | None = None,
) -> tuple[list[PositionItem], list[str]]:
    refreshed: list[PositionItem] = []
    errors: list[str] = []
    for item in items:
        if item.category == "期货":
            latest = load_or_fetch_futures_contract(
                item.code,
                api_key=api_key,
                count=option_count,
                allow_fetch=True,
                force_refresh=True,
                save_to_cache=False,
                realtime_preview=True,
                market_now=market_now,
            )
        elif item.category == "期货价差":
            contracts = [part.strip() for part in item.code.split(" - ") if part.strip()]
            if len(contracts) < 2:
                errors.append(f"{item.name}: 无法识别价差合约")
                continue
            latest = load_or_fetch_spread(
                contracts,
                base_contract=contracts[0],
                api_key=api_key,
                max_workers=max_workers,
                allow_fetch=True,
                force_refresh=True,
                save_to_cache=False,
                realtime_preview=True,
                market_now=market_now,
            )
        elif item.category == "期权":
            latest = load_or_fetch_option(
                item.code,
                count=option_count,
                allow_fetch=True,
                force_refresh=True,
                save_to_cache=False,
                realtime_preview=True,
                market_now=market_now,
            )
        else:
            continue

        if latest.status == "失败" or "刷新失败" in latest.source:
            errors.append(f"{item.name}: {latest.error or latest.source}")
            continue
        latest_date = pd.to_datetime(latest.latest_date, errors="coerce")
        today = (market_now or datetime.now(ZoneInfo("Asia/Shanghai"))).date()
        if pd.isna(latest_date) or latest_date.date() != today:
            errors.append(f"{item.name}: 实时源未返回今日行情")
            continue
        refreshed.append(latest)
    return refreshed, errors
