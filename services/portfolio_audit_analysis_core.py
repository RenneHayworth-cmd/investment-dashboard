from __future__ import annotations

from dataclasses import replace
from typing import Iterable

import numpy as np
import pandas as pd

from services.portfolio_audit import (
    AuditAllocation,
    AuditRunResult,
    AuditSettings,
)


def _run_portfolio_audit(*args: object, **kwargs: object) -> AuditRunResult:
    # Resolve through the compatibility module at call time so existing mocks keep working.
    from services import portfolio_audit_analysis as facade

    return facade.run_portfolio_audit(*args, **kwargs)


def _calculate_performance_summary(
    daily: pd.DataFrame, initial_capital: float
) -> dict[str, object]:
    from services import portfolio_audit_analysis as facade

    return facade.calculate_performance_summary(daily, initial_capital)


def build_benchmarks(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    strategy_result: AuditRunResult,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    symbols = [item.symbol for item in allocations]
    equal_hold = _run_portfolio_audit(
        market_data,
        [replace(item, weight_pct=100 / len(symbols), strategy="hold") for item in allocations],
        settings,
    )
    permanent_weights = {"512890": 10.0, "159501": 5.0, "159655": 5.0}
    static_base = _run_portfolio_audit(
        market_data,
        [replace(item, weight_pct=permanent_weights.get(item.symbol, 0), strategy="hold") for item in allocations if item.symbol in permanent_weights],
        settings,
    )
    unified = _run_portfolio_audit(
        market_data,
        [
            replace(item, ma_period=20, threshold_pct=1.0)
            if item.strategy in ("timing", "half_timing")
            else item
            for item in allocations
        ],
        settings,
    )
    exposure = same_exposure_equal_weight_benchmark(
        market_data,
        symbols,
        strategy_result.daily,
        settings,
    )
    nav = strategy_result.daily[["trade_date", "portfolio_value"]].rename(columns={"portfolio_value": "当前策略"})
    for label, result in (
        ("十只ETF等权一直持有", equal_hold),
        ("静态永久底仓", static_base),
        ("统一MA20/1%", unified),
    ):
        nav = nav.merge(
            result.daily[["trade_date", "portfolio_value"]].rename(columns={"portfolio_value": label}),
            on="trade_date",
            how="inner",
        )
    nav = nav.merge(exposure[["trade_date", "portfolio_value"]].rename(columns={"portfolio_value": "相同每日总仓位等权"}), on="trade_date", how="inner")
    summaries = [{"benchmark": "当前策略", **strategy_result.summary}]
    summaries.extend(
        {"benchmark": label, **result.summary}
        for label, result in (
            ("十只ETF等权一直持有", equal_hold),
            ("静态永久底仓", static_base),
            ("统一MA20/1%", unified),
        )
    )
    exposure_summary = _calculate_performance_summary(exposure, settings.initial_capital)
    summaries.append({"benchmark": "相同每日总仓位等权", **exposure_summary})
    return pd.DataFrame(summaries), nav


def custom_parameter_attribution(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    baseline: AuditRunResult,
) -> pd.DataFrame:
    """Measure each custom parameter's common-window effect without removing the ETF."""
    rows: list[dict[str, object]] = []
    baseline_annual = float(baseline.summary["annual_return_pct"])
    baseline_total = float(baseline.summary["total_return_pct"])
    for allocation in allocations:
        if allocation.strategy not in ("timing", "half_timing"):
            continue
        replaced_allocations = [
            replace(item, ma_period=20, threshold_pct=1.0)
            if item.symbol == allocation.symbol
            else item
            for item in allocations
        ]
        replacement = _run_portfolio_audit(
            market_data, replaced_allocations, settings
        )
        rows.append(
            {
                "symbol": allocation.symbol,
                "name": allocation.name,
                "current_ma_period": allocation.ma_period,
                "current_threshold_pct": allocation.threshold_pct,
                "common_current_annual_return_pct": baseline_annual,
                "common_replace_with_unified_annual_return_pct": float(
                    replacement.summary["annual_return_pct"]
                ),
                "common_custom_parameter_annual_contribution_pct": (
                    baseline_annual - float(replacement.summary["annual_return_pct"])
                ),
                "common_custom_parameter_total_contribution_pct": (
                    baseline_total - float(replacement.summary["total_return_pct"])
                ),
            }
        )
    return pd.DataFrame(rows)


def same_exposure_equal_weight_benchmark(
    market_data: dict[str, pd.DataFrame],
    symbols: list[str],
    strategy_daily: pd.DataFrame,
    settings: AuditSettings,
) -> pd.DataFrame:
    dates = pd.DatetimeIndex(pd.to_datetime(strategy_daily["trade_date"]))
    total_returns = []
    for symbol in symbols:
        frame = market_data[symbol].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.set_index("trade_date").reindex(dates)
        previous = frame["raw_close"].shift(1)
        total_return = (frame["raw_close"] + frame["dividend_per_share"]) / previous - 1
        total_returns.append(total_return.rename(symbol))
    equal_return = pd.concat(total_returns, axis=1).mean(axis=1).fillna(0)
    target_exposure = strategy_daily.set_index("trade_date")["etf_weight_pct"].reindex(dates).fillna(0) / 100
    value = settings.initial_capital
    previous_exposure = 0.0
    previous_date = dates[0]
    rows = []
    for date_index, trade_date in enumerate(dates):
        elapsed = max(0, (trade_date - previous_date).days)
        cash_return = (1 + settings.cash_annual_rate) ** (elapsed / 365) - 1
        value *= 1 + previous_exposure * float(equal_return.iloc[date_index]) + (1 - previous_exposure) * cash_return
        exposure = float(target_exposure.iloc[date_index])
        turnover = abs(exposure - previous_exposure)
        value -= value * turnover * settings.commission_rate
        rows.append(
            {
                "trade_date": trade_date,
                "portfolio_value": value,
                "etf_weight_pct": exposure * 100,
                "cash_weight_pct": (1 - exposure) * 100,
            }
        )
        previous_exposure = exposure
        previous_date = trade_date
    return pd.DataFrame(rows)


def parameter_grid_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    ma_periods: Iterable[int],
    threshold_pcts: Iterable[float],
) -> pd.DataFrame:
    rows = []
    for item in allocations:
        if item.strategy == "hold":
            continue
        hold = _run_portfolio_audit(
            {item.symbol: market_data[item.symbol]},
            [replace(item, weight_pct=100, strategy="hold")],
            replace(settings, initial_capital=10000),
        )
        for ma_period in ma_periods:
            for threshold_pct in threshold_pcts:
                candidate = replace(
                    item,
                    weight_pct=100,
                    ma_period=int(ma_period),
                    threshold_pct=float(threshold_pct),
                    signal_rule="percent",
                )
                result = _run_portfolio_audit(
                    {item.symbol: market_data[item.symbol]},
                    [candidate],
                    replace(settings, initial_capital=10000),
                )
                rows.append(
                    {
                        "symbol": item.symbol,
                        "ma_period": int(ma_period),
                        "threshold_pct": float(threshold_pct),
                        **_metric_subset(result.summary),
                        "excess_vs_hold_pct": result.summary["total_return_pct"] - hold.summary["total_return_pct"],
                        "average_position_pct": result.summary["average_etf_weight_pct"],
                        "trade_count": result.summary["trade_count"],
                        "is_current_parameter": int(
                            int(ma_period) == item.ma_period and np.isclose(float(threshold_pct), item.threshold_pct)
                        ),
                    }
                )
    grid = pd.DataFrame(rows)
    if grid.empty:
        return grid
    grid["annual_return_rank"] = grid.groupby("symbol")["annual_return_pct"].rank(method="min", ascending=False)
    robustness = parameter_robustness(grid)
    return grid.merge(robustness, on="symbol", how="left")


def parameter_robustness(grid: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol, group in grid.groupby("symbol"):
        current = group[group["is_current_parameter"] == 1]
        if current.empty:
            continue
        current = current.iloc[0]
        ma_values = sorted(group["ma_period"].unique())
        threshold_values = sorted(group["threshold_pct"].unique())
        ma_index = ma_values.index(current["ma_period"])
        threshold_index = threshold_values.index(current["threshold_pct"])
        adjacent_mas = set(ma_values[max(0, ma_index - 1) : ma_index + 2])
        adjacent_thresholds = set(threshold_values[max(0, threshold_index - 1) : threshold_index + 2])
        neighbors = group[
            group["ma_period"].isin(adjacent_mas)
            & group["threshold_pct"].isin(adjacent_thresholds)
            & ~(
                (group["ma_period"] == current["ma_period"])
                & (group["threshold_pct"] == current["threshold_pct"])
            )
        ]
        neighbor_mean = float(neighbors["annual_return_pct"].mean()) if not neighbors.empty else np.nan
        max_gap = float((neighbors["annual_return_pct"] - current["annual_return_pct"]).abs().max()) if not neighbors.empty else np.nan
        degradation = float(current["annual_return_pct"] - neighbor_mean) if np.isfinite(neighbor_mean) else np.nan
        if np.isfinite(degradation) and degradation <= 2 and max_gap <= 5:
            rating = "高"
        elif np.isfinite(degradation) and degradation <= 5 and max_gap <= 10:
            rating = "中"
        else:
            rating = "低"
        rows.append(
            {
                "symbol": symbol,
                "current_annual_return_rank": int(current["annual_return_rank"]),
                "neighbor_count": len(neighbors),
                "neighbor_average_annual_return_pct": neighbor_mean,
                "max_neighbor_performance_gap_pct": max_gap,
                "suspected_parameter_spike": bool(np.isfinite(degradation) and degradation > 5),
                "robustness_rating": rating,
            }
        )
    return pd.DataFrame(rows)


def atr_parameter_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    k_values: Iterable[float],
) -> pd.DataFrame:
    rows = []
    for k in k_values:
        candidates = [
            replace(item, ma_period=20, signal_rule="atr", atr_k=float(k))
            if item.strategy in ("timing", "half_timing")
            else item
            for item in allocations
        ]
        result = _run_portfolio_audit(market_data, candidates, settings)
        rows.append({"rule": "ATR20", "atr_k": float(k), **_metric_subset(result.summary)})
    return pd.DataFrame(rows)


def time_split_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    grid: pd.DataFrame,
    splits: Iterable[float],
) -> pd.DataFrame:
    rows = []
    for item in allocations:
        if item.strategy == "hold":
            continue
        dates = pd.DatetimeIndex(pd.to_datetime(market_data[item.symbol]["trade_date"])).sort_values()
        if settings.start_date is not None:
            dates = dates[dates >= pd.Timestamp(settings.start_date)]
        if settings.end_date is not None:
            dates = dates[dates <= pd.Timestamp(settings.end_date)]
        for split in splits:
            cut = max(2, min(len(dates) - 2, int(len(dates) * float(split))))
            train_end = dates[cut - 1]
            test_start = dates[cut]
            candidates = grid[grid["symbol"] == item.symbol].copy()
            train_rows = []
            for candidate in candidates.itertuples():
                candidate_item = replace(
                    item,
                    weight_pct=100,
                    ma_period=int(candidate.ma_period),
                    threshold_pct=float(candidate.threshold_pct),
                )
                score, train_annual = _fast_training_score(
                    market_data[item.symbol],
                    candidate_item,
                    pd.Timestamp(settings.start_date) if settings.start_date else dates[0],
                    train_end,
                    settings.commission_rate,
                )
                train_rows.append((score, train_annual, candidate_item))
            _, train_annual, selected = max(train_rows, key=lambda value: (value[0], value[1]))
            test = _run_portfolio_audit(
                {item.symbol: market_data[item.symbol]},
                [selected],
                replace(settings, initial_capital=10000, start_date=test_start),
            )
            hold = _run_portfolio_audit(
                {item.symbol: market_data[item.symbol]},
                [replace(item, weight_pct=100, strategy="hold")],
                replace(settings, initial_capital=10000, start_date=test_start),
            )
            rows.append(
                {
                    "test_type": "time_split",
                    "symbol": item.symbol,
                    "train_ratio": float(split),
                    "train_end": train_end,
                    "test_start": test_start,
                    "selected_ma_period": selected.ma_period,
                    "selected_threshold_pct": selected.threshold_pct,
                    "train_annual_return_pct": train_annual,
                    "test_annual_return_pct": test.summary["annual_return_pct"],
                    "test_max_drawdown_pct": test.summary["max_drawdown_pct"],
                    "test_excess_vs_hold_pct": test.summary["total_return_pct"] - hold.summary["total_return_pct"],
                    "performance_decay_pct": train_annual - test.summary["annual_return_pct"],
                    "test_trading_days": test.summary["trading_days"],
                }
            )
    return pd.DataFrame(rows)


def walk_forward_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    ma_periods: Iterable[int],
    threshold_pcts: Iterable[float],
    train_days: int = 180,
    test_days: int = 60,
) -> pd.DataFrame:
    rows = []
    for item in allocations:
        if item.strategy == "hold":
            continue
        dates = pd.DatetimeIndex(pd.to_datetime(market_data[item.symbol]["trade_date"])).sort_values()
        if settings.start_date is not None:
            dates = dates[dates >= pd.Timestamp(settings.start_date)]
        offset = train_days
        while offset + test_days <= len(dates):
            train_start, train_end = dates[offset - train_days], dates[offset - 1]
            test_start, test_end = dates[offset], dates[min(offset + test_days - 1, len(dates) - 1)]
            candidates = []
            for ma in ma_periods:
                for threshold in threshold_pcts:
                    candidate = replace(item, weight_pct=100, ma_period=int(ma), threshold_pct=float(threshold))
                    score, annual_return = _fast_training_score(
                        market_data[item.symbol],
                        candidate,
                        train_start,
                        train_end,
                        settings.commission_rate,
                    )
                    candidates.append((score, annual_return, candidate))
            _, _, selected = max(candidates, key=lambda value: (value[0], value[1]))
            test = _run_portfolio_audit(
                {item.symbol: market_data[item.symbol]},
                [selected],
                replace(settings, initial_capital=10000, start_date=test_start, end_date=test_end),
            )
            hold = _run_portfolio_audit(
                {item.symbol: market_data[item.symbol]},
                [replace(item, weight_pct=100, strategy="hold")],
                replace(settings, initial_capital=10000, start_date=test_start, end_date=test_end),
            )
            rows.append(
                {
                    "test_type": "walk_forward",
                    "symbol": item.symbol,
                    "train_start": train_start,
                    "train_end": train_end,
                    "test_start": test_start,
                    "test_end": test_end,
                    "selected_ma_period": selected.ma_period,
                    "selected_threshold_pct": selected.threshold_pct,
                    "test_total_return_pct": test.summary["total_return_pct"],
                    "test_excess_vs_hold_pct": test.summary["total_return_pct"] - hold.summary["total_return_pct"],
                    "test_trading_days": test.summary["trading_days"],
                }
            )
            offset += test_days
    return pd.DataFrame(rows)


def stress_test_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    baseline: AuditRunResult,
    sensitivity: dict[str, list[float]],
) -> pd.DataFrame:
    rows = []
    for fill_rate in sensitivity["after_hours_fill_rates"]:
        result = _run_portfolio_audit(market_data, allocations, replace(settings, after_hours_fill_rate=float(fill_rate)))
        rows.append({"stress_type": "after_hours_fill_rate", "value": fill_rate, **_metric_subset(result.summary)})
    for slippage in sensitivity["slippage_bps"]:
        result = _run_portfolio_audit(market_data, allocations, replace(settings, slippage_bp=float(slippage)))
        rows.append({"stress_type": "slippage_bp", "value": slippage, **_metric_subset(result.summary)})
    for cash_rate in sensitivity["cash_annual_rates"]:
        result = _run_portfolio_audit(market_data, allocations, replace(settings, cash_annual_rate=float(cash_rate)))
        rows.append({"stress_type": "cash_annual_rate", "value": cash_rate, **_metric_subset(result.summary)})
    for mode in ("after_close", "next_open", "next_close", "t2_open"):
        result = _run_portfolio_audit(market_data, allocations, replace(settings, execution_mode=mode))
        rows.append({"stress_type": "execution_mode", "value": mode, **_metric_subset(result.summary)})
    for missing in sensitivity["missed_signal_rates"]:
        result = _run_portfolio_audit(market_data, allocations, replace(settings, missed_signal_rate=float(missing)))
        rows.append({"stress_type": "missed_signal_rate", "value": missing, **_metric_subset(result.summary)})

    ranked_symbols = baseline.contribution[baseline.contribution["symbol"] != "CASH"].sort_values("cumulative_contribution", ascending=False)
    for remove_count in (1, 2):
        removed = set(ranked_symbols.head(remove_count)["symbol"])
        remaining = [item for item in allocations if item.symbol not in removed]
        result = _run_portfolio_audit(market_data, remaining, settings)
        rows.append(
            {
                "stress_type": "remove_best_etf",
                "value": ",".join(sorted(removed)),
                **_metric_subset(result.summary),
            }
        )

    closed = baseline.trades[baseline.trades["action"] == "sell"].sort_values("realized_pnl", ascending=False)
    buys = baseline.trades[baseline.trades["action"] == "buy"]
    entry_keys = []
    for sell in closed.itertuples():
        prior = buys[
            (buys["symbol"] == sell.symbol)
            & (buys["sleeve"] == sell.sleeve)
            & (pd.to_datetime(buys["execution_date"]) <= pd.Timestamp(sell.execution_date))
        ]
        if not prior.empty:
            entry_keys.append((sell.symbol, pd.Timestamp(prior.iloc[-1]["signal_date"]).normalize()))
    for count in (1, 3, 5):
        blocked = set(entry_keys[:count])
        result = _run_portfolio_audit(market_data, allocations, settings, blocked_entries=blocked)
        rows.append({"stress_type": "remove_best_trades", "value": count, **_metric_subset(result.summary)})
    return pd.DataFrame(rows)


def risk_bucket_analysis(
    result: AuditRunResult,
    market_data: dict[str, pd.DataFrame],
    risk_buckets: dict[str, list[str]],
) -> pd.DataFrame:
    rows = []
    components = result.component_daily.copy()
    components["daily_pnl"] = components.groupby("symbol")["component_value"].diff().fillna(0)
    for bucket, symbols in risk_buckets.items():
        subset = components[components["symbol"].isin(symbols)]
        if subset.empty:
            continue
        daily_market = subset.groupby("trade_date")["market_value"].sum()
        daily_value = result.daily.set_index("trade_date")["portfolio_value"].reindex(daily_market.index)
        weight = daily_market / daily_value * 100
        signals = subset.pivot(index="trade_date", columns="symbol", values="signal")
        states = signals.replace({"持有": 1.0, "空仓": 0.0, "维持": np.nan, "等待均线": np.nan}).ffill().fillna(0.0)
        transitions = states.diff().fillna(states)
        returns = []
        signal_values = []
        for symbol in symbols:
            if symbol not in market_data:
                continue
            frame = market_data[symbol].set_index("trade_date").reindex(signals.index)
            returns.append(frame["signal_close"].pct_change().rename(symbol))
            if symbol in states:
                signal_values.append(states[symbol].rename(symbol))
        return_corr = _off_diagonal_mean(pd.concat(returns, axis=1).corr()) if returns else np.nan
        signal_corr = _off_diagonal_mean(pd.concat(signal_values, axis=1).corr()) if signal_values else np.nan
        rows.append(
            {
                "risk_bucket": bucket,
                "average_weight_pct": float(weight.mean()),
                "max_weight_pct": float(weight.max()),
                "return_contribution": float(subset["daily_pnl"].sum()),
                "max_drawdown_contribution": _component_drawdown_contribution(subset),
                "trade_count": int(result.trades["symbol"].isin(symbols).sum()) if not result.trades.empty else 0,
                "simultaneous_buy_ratio_pct": _simultaneous_transition_ratio(transitions, 1),
                "simultaneous_sell_ratio_pct": _simultaneous_transition_ratio(transitions, -1),
                "within_bucket_return_correlation": return_corr,
                "within_bucket_signal_correlation": signal_corr,
            }
        )
    return pd.DataFrame(rows)


def drawdown_attribution(
    result: AuditRunResult,
    risk_buckets: dict[str, list[str]],
    top_n: int = 5,
) -> pd.DataFrame:
    daily = result.daily[["trade_date", "portfolio_value", "etf_weight_pct"]].copy()
    daily["peak"] = daily["portfolio_value"].cummax()
    daily["drawdown"] = daily["portfolio_value"] / daily["peak"] - 1
    episodes = []
    in_drawdown = False
    start_index = 0
    for index, value in enumerate(daily["drawdown"]):
        if value < 0 and not in_drawdown:
            in_drawdown = True
            start_index = max(0, index - 1)
        if in_drawdown and (value >= -1e-12 or index == len(daily) - 1):
            end_index = index
            segment = daily.iloc[start_index : end_index + 1]
            trough_index = int(segment["drawdown"].idxmin())
            episodes.append((start_index, trough_index, end_index if value >= -1e-12 else None, float(daily.loc[trough_index, "drawdown"])))
            in_drawdown = False
    episodes = sorted(episodes, key=lambda item: item[3])[:top_n]
    components = result.component_daily.pivot(index="trade_date", columns="symbol", values="component_value")
    component_market = result.component_daily.pivot(index="trade_date", columns="symbol", values="market_value")
    rows = []
    for rank, (start_idx, trough_idx, recovery_idx, depth) in enumerate(episodes, start=1):
        start_date = pd.Timestamp(daily.loc[start_idx, "trade_date"])
        trough_date = pd.Timestamp(daily.loc[trough_idx, "trade_date"])
        recovery_date = pd.Timestamp(daily.loc[recovery_idx, "trade_date"]) if recovery_idx is not None else pd.NaT
        peak_value = float(daily.loc[start_idx, "portfolio_value"])
        row = {
            "drawdown_rank": rank,
            "start_date": start_date,
            "trough_date": trough_date,
            "recovery_date": recovery_date,
            "drawdown_pct": depth * 100,
            "duration_trading_days": trough_idx - start_idx,
            "portfolio_weight_at_trough_pct": float(daily.loc[trough_idx, "etf_weight_pct"]),
        }
        for symbol in components.columns:
            row[f"{symbol}_contribution_pct"] = float((components.loc[trough_date, symbol] - components.loc[start_date, symbol]) / peak_value * 100)
        for bucket, symbols in risk_buckets.items():
            present = [symbol for symbol in symbols if symbol in component_market.columns]
            row[f"{bucket}_weight_pct"] = float(component_market.loc[trough_date, present].sum() / daily.loc[trough_idx, "portfolio_value"] * 100)
        rows.append(row)
    return pd.DataFrame(rows)


def add_contribution_diagnostics(result: AuditRunResult) -> pd.DataFrame:
    contribution = result.contribution.copy()
    total_profit = float(result.daily["portfolio_value"].iloc[-1] - result.daily["portfolio_value"].iloc[0])
    if total_profit:
        contribution["contribution_share_pct"] = contribution["cumulative_contribution"] / total_profit * 100
    else:
        contribution["contribution_share_pct"] = np.nan
    rows = result.component_daily.copy()
    rows["component_drawdown"] = rows.groupby("symbol")["component_value"].transform(lambda values: values / values.cummax() - 1)
    max_dd = rows.groupby("symbol")["component_drawdown"].min() * 100
    contribution["max_drawdown_contribution"] = contribution["symbol"].map(max_dd)
    days = max(1, (pd.to_datetime(result.daily["trade_date"]).iloc[-1] - pd.to_datetime(result.daily["trade_date"]).iloc[0]).days)
    contribution["annualized_contribution"] = contribution["cumulative_contribution"] / result.summary["final_value"] * 365 / days
    return contribution


def _metric_subset(summary: dict[str, object]) -> dict[str, object]:
    return {
        key: summary.get(key)
        for key in (
            "total_return_pct",
            "annual_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
            "calmar_ratio",
            "final_value",
            "commission_cost",
            "slippage_cost",
            "average_etf_weight_pct",
            "trade_count",
        )
    }


def _off_diagonal_mean(matrix: pd.DataFrame) -> float:
    if matrix.shape[0] < 2:
        return np.nan
    values = matrix.to_numpy(dtype=float)
    off_diagonal = values[np.triu_indices_from(values, k=1)]
    return float(np.nanmean(off_diagonal)) if np.isfinite(off_diagonal).any() else np.nan


def _component_drawdown_contribution(subset: pd.DataFrame) -> float:
    values = subset.groupby("trade_date")["component_value"].sum()
    return float((values / values.cummax() - 1).min() * 100) if not values.empty else 0.0


def _simultaneous_transition_ratio(transitions: pd.DataFrame, direction: int) -> float:
    if transitions.shape[1] < 2:
        return np.nan
    event_counts = (transitions == direction).sum(axis=1)
    event_days = event_counts > 0
    return float((event_counts[event_days] >= 2).mean() * 100) if event_days.any() else 0.0


def _fast_training_score(
    frame: pd.DataFrame,
    allocation: AuditAllocation,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    commission_rate: float,
) -> tuple[float, float]:
    """Select parameters without future data; strict accounting remains in test runs."""
    data = frame[["trade_date", "signal_close"]].copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    data = data.sort_values("trade_date").set_index("trade_date")
    ma = data["signal_close"].rolling(allocation.ma_period, min_periods=allocation.ma_period).mean()
    threshold = allocation.threshold_pct / 100
    raw_signal = pd.Series(np.nan, index=data.index)
    raw_signal[data["signal_close"] > ma * (1 + threshold)] = 1.0
    raw_signal[data["signal_close"] < ma * (1 - threshold)] = 0.0
    state = raw_signal.ffill().fillna(0.0)
    if allocation.strategy == "half_timing":
        state = 0.5 + state * 0.5
    returns = data["signal_close"].pct_change().fillna(0)
    turnover = state.diff().abs().fillna(state)
    strategy_returns = state.shift(1).fillna(0) * returns - turnover * commission_rate
    strategy_returns = strategy_returns.loc[(strategy_returns.index >= start_date) & (strategy_returns.index <= end_date)]
    if len(strategy_returns) < 2:
        return -np.inf, -np.inf
    total = float((1 + strategy_returns).prod() - 1)
    days = max(1, int((strategy_returns.index[-1] - strategy_returns.index[0]).days))
    annual = (1 + total) ** (365 / days) - 1 if total > -1 else -1.0
    volatility = float(strategy_returns.std())
    sharpe = float(strategy_returns.mean() / volatility * np.sqrt(252)) if volatility > 0 else -np.inf
    return sharpe, annual * 100
