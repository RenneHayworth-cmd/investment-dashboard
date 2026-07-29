from __future__ import annotations

from dataclasses import replace
from typing import Iterable

import numpy as np
import pandas as pd

from services.portfolio_audit import (
    AuditAllocation,
    AuditRunResult,
    AuditSettings,
    calculate_performance_summary,
    run_portfolio_audit,
)


def build_benchmarks(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    strategy_result: AuditRunResult,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    symbols = [item.symbol for item in allocations]
    equal_hold = run_portfolio_audit(
        market_data,
        [replace(item, weight_pct=100 / len(symbols), strategy="hold") for item in allocations],
        settings,
    )
    permanent_weights = {"512890": 10.0, "159501": 5.0, "159655": 5.0}
    static_base = run_portfolio_audit(
        market_data,
        [replace(item, weight_pct=permanent_weights.get(item.symbol, 0), strategy="hold") for item in allocations if item.symbol in permanent_weights],
        settings,
    )
    unified = run_portfolio_audit(
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
    exposure_summary = calculate_performance_summary(exposure, settings.initial_capital)
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
        replacement = run_portfolio_audit(
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
        hold = run_portfolio_audit(
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
                result = run_portfolio_audit(
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
        result = run_portfolio_audit(market_data, candidates, settings)
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
            test = run_portfolio_audit(
                {item.symbol: market_data[item.symbol]},
                [selected],
                replace(settings, initial_capital=10000, start_date=test_start),
            )
            hold = run_portfolio_audit(
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
            test = run_portfolio_audit(
                {item.symbol: market_data[item.symbol]},
                [selected],
                replace(settings, initial_capital=10000, start_date=test_start, end_date=test_end),
            )
            hold = run_portfolio_audit(
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
        result = run_portfolio_audit(market_data, allocations, replace(settings, after_hours_fill_rate=float(fill_rate)))
        rows.append({"stress_type": "after_hours_fill_rate", "value": fill_rate, **_metric_subset(result.summary)})
    for slippage in sensitivity["slippage_bps"]:
        result = run_portfolio_audit(market_data, allocations, replace(settings, slippage_bp=float(slippage)))
        rows.append({"stress_type": "slippage_bp", "value": slippage, **_metric_subset(result.summary)})
    for cash_rate in sensitivity["cash_annual_rates"]:
        result = run_portfolio_audit(market_data, allocations, replace(settings, cash_annual_rate=float(cash_rate)))
        rows.append({"stress_type": "cash_annual_rate", "value": cash_rate, **_metric_subset(result.summary)})
    for mode in ("after_close", "next_open", "next_close", "t2_open"):
        result = run_portfolio_audit(market_data, allocations, replace(settings, execution_mode=mode))
        rows.append({"stress_type": "execution_mode", "value": mode, **_metric_subset(result.summary)})
    for missing in sensitivity["missed_signal_rates"]:
        result = run_portfolio_audit(market_data, allocations, replace(settings, missed_signal_rate=float(missing)))
        rows.append({"stress_type": "missed_signal_rate", "value": missing, **_metric_subset(result.summary)})

    ranked_symbols = baseline.contribution[baseline.contribution["symbol"] != "CASH"].sort_values("cumulative_contribution", ascending=False)
    for remove_count in (1, 2):
        removed = set(ranked_symbols.head(remove_count)["symbol"])
        remaining = [item for item in allocations if item.symbol not in removed]
        result = run_portfolio_audit(market_data, remaining, settings)
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
        result = run_portfolio_audit(market_data, allocations, settings, blocked_entries=blocked)
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


def missed_order_simulation_analysis(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    baseline: AuditRunResult,
    *,
    hold_annual_return_pct: float,
    unified_annual_return_pct: float,
    miss_rates: Iterable[float] = (0.0, 0.05, 0.10, 0.20),
    simulations: int = 1000,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    """Run reproducible missed-order paths without changing prices or target signals."""
    if simulations < 1000:
        raise ValueError("非零漏单率至少需要1000个随机种子。")
    if settings.execution_mode != "after_close" or settings.after_hours_fill_rate < 1:
        raise ValueError("多种子漏单审计仅复现盘后收盘价全部成交基线。")

    dates = pd.DatetimeIndex(pd.to_datetime(baseline.daily["trade_date"])).normalize()
    specs = _missed_order_sleeve_specs(market_data, allocations, baseline, dates, settings)
    seeds = np.arange(settings.random_seed, settings.random_seed + simulations, dtype=np.int64)
    random_paths = [
        np.vstack(
            [
                np.random.default_rng(
                    int(seed + int(spec["random_seed_offset"]))
                ).random(len(dates))
                for seed in seeds
            ]
        )
        for spec in specs
    ]
    frames = []
    normalized_rates = sorted({float(rate) for rate in miss_rates})
    for rate in normalized_rates:
        sides = ("none",) if np.isclose(rate, 0.0) else ("buy", "sell", "both")
        for side in sides:
            frames.append(
                _simulate_missed_order_paths(
                    specs,
                    dates,
                    settings,
                    seeds,
                    random_paths,
                    miss_rate=rate,
                    miss_side=side,
                    mechanism="target_correction",
                )
            )

    legacy = _simulate_missed_order_paths(
        specs,
        dates,
        settings,
        seeds[:1],
        [paths[:1] for paths in random_paths],
        miss_rate=0.20,
        miss_side="both",
        mechanism="legacy_permanent_suppression",
    )
    simulation = pd.concat([*frames, legacy], ignore_index=True)
    simulation["annual_excess_vs_hold_pct"] = (
        simulation["annual_return_pct"] - hold_annual_return_pct
    )
    simulation["annual_excess_vs_unified_pct"] = (
        simulation["annual_return_pct"] - unified_annual_return_pct
    )
    corrected = simulation[simulation["mechanism"] == "target_correction"].copy()
    zero_annual = float(
        corrected.loc[np.isclose(corrected["miss_rate"], 0.0), "annual_return_pct"].iloc[0]
    )
    corrected["annual_impact_vs_no_miss_pct"] = corrected["annual_return_pct"] - zero_annual
    simulation = simulation.merge(
        corrected[
            ["mechanism", "miss_side", "miss_rate", "seed", "annual_impact_vs_no_miss_pct"]
        ],
        on=["mechanism", "miss_side", "miss_rate", "seed"],
        how="left",
    )

    distribution_rows = []
    for (side, rate), group in corrected.groupby(["miss_side", "miss_rate"], sort=True):
        annual = group["annual_return_pct"]
        drawdown = group["max_drawdown_pct"]
        distribution_rows.append(
            {
                "mechanism": "target_correction",
                "miss_side": side,
                "miss_rate": rate,
                "simulation_count": len(group),
                "annual_return_mean_pct": float(annual.mean()),
                "annual_return_median_pct": float(annual.median()),
                "annual_return_std_pct": float(annual.std(ddof=1)),
                "annual_return_p05_pct": float(annual.quantile(0.05)),
                "annual_return_p25_pct": float(annual.quantile(0.25)),
                "annual_return_p75_pct": float(annual.quantile(0.75)),
                "annual_return_p95_pct": float(annual.quantile(0.95)),
                "max_drawdown_mean_pct": float(drawdown.mean()),
                "max_drawdown_median_pct": float(drawdown.median()),
                "max_drawdown_std_pct": float(drawdown.std(ddof=1)),
                "max_drawdown_p05_pct": float(drawdown.quantile(0.05)),
                "max_drawdown_p25_pct": float(drawdown.quantile(0.25)),
                "max_drawdown_p75_pct": float(drawdown.quantile(0.75)),
                "max_drawdown_p95_pct": float(drawdown.quantile(0.95)),
                "positive_excess_vs_hold_probability_pct": float(
                    (group["annual_excess_vs_hold_pct"] > 0).mean() * 100
                ),
                "positive_excess_vs_unified_probability_pct": float(
                    (group["annual_excess_vs_unified_pct"] > 0).mean() * 100
                ),
                "underperform_hold_probability_pct": float(
                    (group["annual_excess_vs_hold_pct"] < 0).mean() * 100
                ),
                "average_execution_delay_days": float(
                    group["average_execution_delay_days"].mean()
                ),
                "average_missed_buy_count": float(group["missed_buy_count"].mean()),
                "average_missed_sell_count": float(group["missed_sell_count"].mean()),
                "annual_impact_vs_no_miss_mean_pct": float(
                    group["annual_impact_vs_no_miss_pct"].mean()
                ),
            }
        )
    distribution = pd.DataFrame(distribution_rows)
    metadata = {
        "corrected_no_miss_annual_return_pct": zero_annual,
        "corrected_no_miss_final_value": float(
            corrected.loc[np.isclose(corrected["miss_rate"], 0.0), "final_value"].iloc[0]
        ),
        "baseline_final_value": float(baseline.summary["final_value"]),
        "no_miss_reconciliation_difference": float(
            corrected.loc[np.isclose(corrected["miss_rate"], 0.0), "final_value"].iloc[0]
            - baseline.summary["final_value"]
        ),
        "legacy_20pct_annual_return_pct": float(legacy["annual_return_pct"].iloc[0]),
        "legacy_20pct_final_value": float(legacy["final_value"].iloc[0]),
    }
    return simulation, distribution, metadata


def _missed_order_sleeve_specs(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    baseline: AuditRunResult,
    dates: pd.DatetimeIndex,
    settings: AuditSettings,
) -> list[dict[str, object]]:
    targets = baseline.component_daily.copy()
    targets["trade_date"] = pd.to_datetime(targets["trade_date"]).dt.normalize()
    specs: list[dict[str, object]] = []
    for allocation_index, item in enumerate(allocations):
        frame = market_data[item.symbol].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
        frame = frame.set_index("trade_date").reindex(dates)
        if frame[["raw_close", "dividend_per_share"]].isna().any().any():
            raise ValueError(f"{item.symbol} 在共同区间内存在行情缺口。")
        component = (
            targets[targets["symbol"] == item.symbol]
            .set_index("trade_date")
            .reindex(dates)
        )
        combined_target = pd.to_numeric(
            component["target_position"], errors="coerce"
        ).to_numpy(dtype=float)
        capital = settings.initial_capital * item.weight_pct / 100
        if item.strategy == "half_timing":
            sleeve_definitions = (
                ("长期", 0.5, np.ones(len(dates), dtype=float), allocation_index * 17),
                (
                    "择时",
                    0.5,
                    np.clip(combined_target * 2 - 1, 0, 1),
                    allocation_index * 17 + 1,
                ),
            )
        else:
            seed_offset = allocation_index * 17 + (
                0 if item.strategy == "hold" else 1
            )
            sleeve_definitions = (
                (
                    "长期" if item.strategy == "hold" else "择时",
                    1.0,
                    combined_target,
                    seed_offset,
                ),
            )
        for sleeve, fraction, target, seed_offset in sleeve_definitions:
            specs.append(
                {
                    "symbol": item.symbol,
                    "sleeve": sleeve,
                    "initial_capital": capital * fraction,
                    "target": target.astype(np.int8),
                    "random_seed_offset": seed_offset,
                    "raw_close": pd.to_numeric(frame["raw_close"], errors="coerce").to_numpy(dtype=float),
                    "dividend": pd.to_numeric(
                        frame["dividend_per_share"], errors="coerce"
                    ).fillna(0).to_numpy(dtype=float),
                    "share_split_ratio": pd.to_numeric(
                        frame.get(
                            "share_split_ratio",
                            pd.Series(1.0, index=frame.index),
                        ),
                        errors="coerce",
                    ).fillna(1.0).to_numpy(dtype=float),
                    "share_split_rounding": frame.get(
                        "share_split_rounding",
                        pd.Series("", index=frame.index),
                    ).fillna("").astype(str).to_numpy(),
                }
            )
    return specs


def _simulate_missed_order_paths(
    specs: list[dict[str, object]],
    dates: pd.DatetimeIndex,
    settings: AuditSettings,
    seeds: np.ndarray,
    random_paths: list[np.ndarray],
    *,
    miss_rate: float,
    miss_side: str,
    mechanism: str,
) -> pd.DataFrame:
    path_count = len(seeds)
    residual_weight = max(
        0.0,
        100.0
        - sum(float(spec["initial_capital"]) for spec in specs)
        / settings.initial_capital
        * 100,
    )
    residual_capital = settings.initial_capital * residual_weight / 100
    elapsed = (dates - dates[0]).days.to_numpy(dtype=float)
    residual_values = residual_capital * np.power(
        1 + settings.cash_annual_rate, elapsed / 365
    )
    portfolio_values = np.repeat(residual_values[:, None], path_count, axis=1)
    submitted_count = np.zeros(path_count, dtype=int)
    missed_buy_count = np.zeros(path_count, dtype=int)
    missed_sell_count = np.zeros(path_count, dtype=int)
    retried_count = np.zeros(path_count, dtype=int)
    filled_count = np.zeros(path_count, dtype=int)
    delay_total = np.zeros(path_count, dtype=float)
    slip = settings.slippage_bp / 10000

    for spec_index, spec in enumerate(specs):
        cash = np.full(path_count, float(spec["initial_capital"]), dtype=float)
        shares = np.zeros(path_count, dtype=float)
        origin_index = np.full(path_count, -1, dtype=int)
        origin_action = np.zeros(path_count, dtype=np.int8)
        suppressed_target = np.full(path_count, -1, dtype=np.int8)
        target = np.asarray(spec["target"], dtype=np.int8)
        close = np.asarray(spec["raw_close"], dtype=float)
        dividends = np.asarray(spec["dividend"], dtype=float)
        split_ratios = np.asarray(spec["share_split_ratio"], dtype=float)
        split_roundings = np.asarray(spec["share_split_rounding"], dtype=str)
        random_values = random_paths[spec_index]
        draw_index = np.zeros(path_count, dtype=int)
        sleeve_values = np.zeros((len(dates), path_count), dtype=float)

        for date_index in range(len(dates)):
            if date_index:
                days = int((dates[date_index] - dates[date_index - 1]).days)
                if settings.cash_annual_rate and days > 0:
                    cash *= (1 + settings.cash_annual_rate) ** (days / 365)
            split_ratio = float(split_ratios[date_index])
            if not np.isclose(split_ratio, 1.0):
                split_shares = shares * split_ratio
                split_rounding = split_roundings[date_index]
                if split_rounding == "ceil":
                    shares = np.ceil(split_shares - 1e-12)
                elif split_rounding == "round":
                    shares = np.rint(split_shares)
                else:
                    shares = np.floor(split_shares + 1e-12)
            if dividends[date_index]:
                cash += shares * dividends[date_index]

            target_today = int(target[date_index])
            if mechanism == "legacy_permanent_suppression":
                suppressed_target[suppressed_target != target_today] = -1
            actual = (shares > 0).astype(np.int8)
            action = np.where(
                (target_today == 1) & (actual == 0),
                1,
                np.where((target_today == 0) & (actual == 1), -1, 0),
            ).astype(np.int8)

            changed_direction = (action != 0) & (action != origin_action)
            origin_index[changed_direction] = date_index
            origin_action[changed_direction] = action[changed_direction]
            inactive = action == 0
            origin_index[inactive] = -1
            origin_action[inactive] = 0
            submitted_count += (action != 0).astype(int)
            retried_count += ((action != 0) & (origin_index < date_index)).astype(int)

            eligible = (
                action != 0
                if miss_side == "both"
                else action == 1
                if miss_side == "buy"
                else action == -1
                if miss_side == "sell"
                else np.zeros(path_count, dtype=bool)
            )
            draw = np.ones(path_count, dtype=float)
            draw_rows = np.flatnonzero(eligible)
            if len(draw_rows):
                draw[draw_rows] = random_values[
                    draw_rows, draw_index[draw_rows]
                ]
                draw_index[draw_rows] += 1
            missed = eligible & (draw < miss_rate)
            missed_buy_count += (missed & (action == 1)).astype(int)
            missed_sell_count += (missed & (action == -1)).astype(int)

            executable_action = action.copy()
            if mechanism == "legacy_permanent_suppression":
                was_suppressed = suppressed_target == target_today
                suppressed_target[missed] = target_today
                executable_action[was_suppressed | missed] = 0
            else:
                executable_action[missed] = 0

            buy = executable_action == 1
            if buy.any():
                execution_price = close[date_index] * (1 + slip)
                buy_shares = (
                    np.floor(
                        cash[buy]
                        / (execution_price * (1 + settings.commission_rate))
                        / settings.lot_size
                    )
                    * settings.lot_size
                )
                valid = buy_shares > 0
                buy_indices = np.flatnonzero(buy)[valid]
                buy_shares = buy_shares[valid]
                gross = buy_shares * execution_price
                cash[buy_indices] -= gross * (1 + settings.commission_rate)
                shares[buy_indices] = buy_shares
                delays = date_index - origin_index[buy_indices]
                delay_total[buy_indices] += delays
                filled_count[buy_indices] += 1
                origin_index[buy_indices] = -1
                origin_action[buy_indices] = 0

            sell = executable_action == -1
            if sell.any():
                sell_indices = np.flatnonzero(sell)
                execution_price = close[date_index] * (1 - slip)
                gross = shares[sell_indices] * execution_price
                cash[sell_indices] += gross * (1 - settings.commission_rate)
                shares[sell_indices] = 0.0
                delays = date_index - origin_index[sell_indices]
                delay_total[sell_indices] += delays
                filled_count[sell_indices] += 1
                origin_index[sell_indices] = -1
                origin_action[sell_indices] = 0

            sleeve_values[date_index] = cash + shares * close[date_index]
        portfolio_values += sleeve_values

    seeded = np.vstack(
        [np.full((1, path_count), settings.initial_capital), portfolio_values]
    )
    drawdowns = seeded / np.maximum.accumulate(seeded, axis=0) - 1
    total_return = portfolio_values[-1] / settings.initial_capital - 1
    calendar_days = max(1, int((dates[-1] - dates[0]).days))
    annual_return = np.power(1 + total_return, 365 / calendar_days) - 1
    return pd.DataFrame(
        {
            "mechanism": mechanism,
            "miss_side": miss_side,
            "miss_rate": miss_rate,
            "seed": seeds,
            "final_value": portfolio_values[-1],
            "total_return_pct": total_return * 100,
            "annual_return_pct": annual_return * 100,
            "max_drawdown_pct": drawdowns.min(axis=0) * 100,
            "average_execution_delay_days": np.divide(
                delay_total,
                filled_count,
                out=np.zeros(path_count, dtype=float),
                where=filled_count > 0,
            ),
            "order_submitted_count": submitted_count,
            "missed_buy_count": missed_buy_count,
            "missed_sell_count": missed_sell_count,
            "order_retried_count": retried_count,
            "order_filled_count": filled_count,
        }
    )


def full_history_validation(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
    *,
    research_end_date: str | pd.Timestamp,
    common_history_ratings: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Validate fixed parameters on each ETF's own available history."""
    common_history_ratings = common_history_ratings or {}
    comparison_rows: list[dict[str, object]] = []
    neighborhood_frames: list[pd.DataFrame] = []
    period_rows: list[dict[str, object]] = []
    end_date = pd.Timestamp(research_end_date).normalize()
    stages = (
        ("2018-2019", pd.Timestamp("2018-01-01"), pd.Timestamp("2019-12-31")),
        ("2020", pd.Timestamp("2020-01-01"), pd.Timestamp("2020-12-31")),
        ("2021", pd.Timestamp("2021-01-01"), pd.Timestamp("2021-12-31")),
        ("2022", pd.Timestamp("2022-01-01"), pd.Timestamp("2022-12-31")),
        ("2023", pd.Timestamp("2023-01-01"), pd.Timestamp("2023-12-31")),
        ("2024", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")),
        ("2025-2026-07-27", pd.Timestamp("2025-01-01"), end_date),
    )
    audit_settings = replace(
        settings,
        initial_capital=10000.0,
        start_date=None,
        end_date=end_date,
        missed_signal_rate=0.0,
        missed_order_side="none",
    )

    for item in allocations:
        if item.strategy == "hold":
            continue
        frame = market_data[item.symbol].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
        frame = frame[frame["trade_date"] <= end_date].sort_values("trade_date")
        current_item = replace(item, weight_pct=100)
        unified_item = replace(
            current_item, ma_period=20, threshold_pct=1.0, signal_rule="percent"
        )
        hold_item = replace(current_item, strategy="hold")
        current = run_portfolio_audit(
            {item.symbol: frame}, [current_item], audit_settings
        )
        unified = run_portfolio_audit(
            {item.symbol: frame}, [unified_item], audit_settings
        )
        hold = run_portfolio_audit(
            {item.symbol: frame}, [hold_item], audit_settings
        )
        comparison_rows.append(
            {
                "symbol": item.symbol,
                "name": item.name,
                "strategy": item.strategy,
                "current_ma_period": item.ma_period,
                "current_threshold_pct": item.threshold_pct,
                "available_start_date": frame["trade_date"].min(),
                "available_end_date": frame["trade_date"].max(),
                "trading_days": current.summary["trading_days"],
                "current_total_return_pct": current.summary["total_return_pct"],
                "current_annual_return_pct": current.summary["annual_return_pct"],
                "unified_total_return_pct": unified.summary["total_return_pct"],
                "unified_annual_return_pct": unified.summary["annual_return_pct"],
                "hold_total_return_pct": hold.summary["total_return_pct"],
                "hold_annual_return_pct": hold.summary["annual_return_pct"],
                "current_max_drawdown_pct": current.summary["max_drawdown_pct"],
                "unified_max_drawdown_pct": unified.summary["max_drawdown_pct"],
                "hold_max_drawdown_pct": hold.summary["max_drawdown_pct"],
                "current_sharpe_ratio": current.summary["sharpe_ratio"],
                "unified_sharpe_ratio": unified.summary["sharpe_ratio"],
                "hold_sharpe_ratio": hold.summary["sharpe_ratio"],
                "current_calmar_ratio": current.summary["calmar_ratio"],
                "unified_calmar_ratio": unified.summary["calmar_ratio"],
                "hold_calmar_ratio": hold.summary["calmar_ratio"],
                "current_average_position_pct": current.summary[
                    "average_etf_weight_pct"
                ],
                "unified_average_position_pct": unified.summary[
                    "average_etf_weight_pct"
                ],
                "current_trade_count": current.summary["trade_count"],
                "current_excess_vs_hold_total_pct": current.summary[
                    "total_return_pct"
                ]
                - hold.summary["total_return_pct"],
                "current_excess_vs_hold_annual_pct": current.summary[
                    "annual_return_pct"
                ]
                - hold.summary["annual_return_pct"],
                "current_excess_vs_unified_total_pct": current.summary[
                    "total_return_pct"
                ]
                - unified.summary["total_return_pct"],
                "current_excess_vs_unified_annual_pct": current.summary[
                    "annual_return_pct"
                ]
                - unified.summary["annual_return_pct"],
            }
        )

        ma_values = sorted(
            {
                max(5, item.ma_period + offset)
                for offset in (-10, -5, 0, 5, 10)
            }
        )
        threshold_values = sorted(
            {
                max(0.0, item.threshold_pct + offset)
                for offset in (-1.0, -0.5, 0.0, 0.5, 1.0)
            }
        )
        candidate_rows = []
        for ma_period in ma_values:
            for threshold_pct in threshold_values:
                candidate = replace(
                    current_item,
                    ma_period=int(ma_period),
                    threshold_pct=float(threshold_pct),
                    signal_rule="percent",
                )
                result = run_portfolio_audit(
                    {item.symbol: frame}, [candidate], audit_settings
                )
                candidate_rows.append(
                    {
                        "symbol": item.symbol,
                        "name": item.name,
                        "ma_period": int(ma_period),
                        "threshold_pct": float(threshold_pct),
                        "is_current_parameter": bool(
                            int(ma_period) == item.ma_period
                            and np.isclose(float(threshold_pct), item.threshold_pct)
                        ),
                        "annual_return_pct": result.summary["annual_return_pct"],
                        "max_drawdown_pct": result.summary["max_drawdown_pct"],
                        "sharpe_ratio": result.summary["sharpe_ratio"],
                        "calmar_ratio": result.summary["calmar_ratio"],
                        "average_position_pct": result.summary[
                            "average_etf_weight_pct"
                        ],
                        "trade_count": result.summary["trade_count"],
                    }
                )
        candidate_frame = pd.DataFrame(candidate_rows)
        candidate_frame["annual_return_rank"] = candidate_frame[
            "annual_return_pct"
        ].rank(method="min", ascending=False)
        candidate_frame["sharpe_rank"] = candidate_frame["sharpe_ratio"].rank(
            method="min", ascending=False
        )
        current_row = candidate_frame[candidate_frame["is_current_parameter"]].iloc[0]
        annual_mean = float(candidate_frame["annual_return_pct"].mean())
        annual_std = float(candidate_frame["annual_return_pct"].std(ddof=1))
        current_gap = float(current_row["annual_return_pct"] - annual_mean)
        annual_spread = float(
            candidate_frame["annual_return_pct"].max()
            - candidate_frame["annual_return_pct"].min()
        )
        spike_threshold = max(3.0, 1.5 * annual_std)
        spike = bool(
            int(current_row["annual_return_rank"]) == 1
            and current_gap > spike_threshold
        )
        rank_limit = max(1, int(np.ceil(len(candidate_frame) / 2)))
        if (
            not spike
            and abs(current_gap) <= 2
            and annual_std <= 3
            and annual_spread <= 10
            and current_row["annual_return_rank"] <= rank_limit
            and current_row["sharpe_rank"] <= rank_limit
        ):
            rating = "高"
        elif (
            not spike
            and abs(current_gap) <= 5
            and annual_std <= 6
            and annual_spread <= 20
        ):
            rating = "中"
        else:
            rating = "低"
        candidate_frame["current_annual_return_rank"] = int(
            current_row["annual_return_rank"]
        )
        candidate_frame["current_sharpe_rank"] = int(current_row["sharpe_rank"])
        candidate_frame["neighborhood_size"] = len(candidate_frame)
        candidate_frame["neighborhood_annual_mean_pct"] = annual_mean
        candidate_frame["neighborhood_annual_std_pct"] = annual_std
        candidate_frame["current_minus_neighborhood_mean_pct"] = current_gap
        candidate_frame["best_worst_annual_spread_pct"] = annual_spread
        candidate_frame["suspected_single_point_spike"] = spike
        candidate_frame["long_history_robustness_rating"] = rating
        candidate_frame["common_history_robustness_rating"] = (
            common_history_ratings.get(item.symbol, "未评级")
        )
        neighborhood_frames.append(candidate_frame)

        for stage, stage_start, stage_end in stages:
            available = frame[
                (frame["trade_date"] >= stage_start)
                & (frame["trade_date"] <= min(stage_end, end_date))
            ]
            base_row: dict[str, object] = {
                "symbol": item.symbol,
                "name": item.name,
                "period": stage,
                "period_requested_start": stage_start,
                "period_requested_end": min(stage_end, end_date),
            }
            if len(available) < 2:
                period_rows.append(
                    {
                        **base_row,
                        "status": "未上市或数据不足",
                        "actual_start_date": pd.NaT,
                        "actual_end_date": pd.NaT,
                        "trading_days": 0,
                        "current_return_pct": np.nan,
                        "unified_return_pct": np.nan,
                        "hold_return_pct": np.nan,
                        "current_max_drawdown_pct": np.nan,
                        "current_average_position_pct": np.nan,
                        "current_excess_vs_hold_pct": np.nan,
                    }
                )
                continue
            period_settings = replace(
                audit_settings,
                start_date=available["trade_date"].min(),
                end_date=available["trade_date"].max(),
            )
            period_current = run_portfolio_audit(
                {item.symbol: frame}, [current_item], period_settings
            )
            period_unified = run_portfolio_audit(
                {item.symbol: frame}, [unified_item], period_settings
            )
            period_hold = run_portfolio_audit(
                {item.symbol: frame}, [hold_item], period_settings
            )
            period_rows.append(
                {
                    **base_row,
                    "status": "已计算",
                    "actual_start_date": available["trade_date"].min(),
                    "actual_end_date": available["trade_date"].max(),
                    "trading_days": len(available),
                    "current_return_pct": period_current.summary[
                        "total_return_pct"
                    ],
                    "unified_return_pct": period_unified.summary[
                        "total_return_pct"
                    ],
                    "hold_return_pct": period_hold.summary["total_return_pct"],
                    "current_max_drawdown_pct": period_current.summary[
                        "max_drawdown_pct"
                    ],
                    "current_average_position_pct": period_current.summary[
                        "average_etf_weight_pct"
                    ],
                    "current_excess_vs_hold_pct": period_current.summary[
                        "total_return_pct"
                    ]
                    - period_hold.summary["total_return_pct"],
                }
            )

    comparison = pd.DataFrame(comparison_rows)
    neighborhood = (
        pd.concat(neighborhood_frames, ignore_index=True)
        if neighborhood_frames
        else pd.DataFrame()
    )
    periods = pd.DataFrame(period_rows)
    if not comparison.empty and not neighborhood.empty:
        ratings = (
            neighborhood[
                [
                    "symbol",
                    "long_history_robustness_rating",
                    "common_history_robustness_rating",
                ]
            ]
            .drop_duplicates("symbol")
        )
        comparison = comparison.merge(ratings, on="symbol", how="left")
    return comparison, neighborhood, periods
