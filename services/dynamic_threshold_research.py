from __future__ import annotations

from dataclasses import asdict, replace
from typing import Iterable

import numpy as np
import pandas as pd

from services.portfolio_audit import (
    AuditAllocation,
    AuditSettings,
    EXECUTION_AFTER_CLOSE,
    run_portfolio_audit,
)


SIGMA_PERIODS = (20, 40, 60, 90, 120)
PARAMETER_MULTIPLIERS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
ASYMMETRIC_MULTIPLIERS = (0.75, 1.0, 1.25)
HYBRID_ALPHA_MULTIPLIERS = (0.25, 0.5, 0.75)
HYBRID_K_MULTIPLIERS = (0.25, 0.5, 0.75, 1.0)
SCORE_WEIGHTS = {
    "longest_underwater_days": 0.45,
    "max_drawdown_pct": 0.25,
    "annual_volatility_pct": 0.15,
    "annual_return_pct": 0.10,
    "sharpe_ratio": 0.05,
}


def candidate_id(candidate: dict[str, object]) -> str:
    fields = (
        "stage",
        "model_family",
        "ma_period",
        "threshold_pct",
        "sigma_period",
        "buy_k",
        "sell_k",
        "buy_alpha_pct",
        "sell_alpha_pct",
    )
    return "|".join(str(candidate.get(field, "")) for field in fields)


def make_candidate(
    *,
    stage: str,
    model_family: str,
    ma_period: int,
    threshold_pct: float = 0.0,
    sigma_period: int = 60,
    buy_k: float = 0.0,
    sell_k: float = 0.0,
    buy_alpha_pct: float = 0.0,
    sell_alpha_pct: float = 0.0,
    anchor_k: float = np.nan,
    selection_allowed: bool = True,
) -> dict[str, object]:
    signal_rule = (
        "percent"
        if model_family in {"current_fixed", "retuned_fixed", "joint_fixed"}
        else "hybrid_sigma"
        if model_family == "hybrid_sigma"
        else "sigma"
    )
    candidate = {
        "stage": stage,
        "model_family": model_family,
        "signal_rule": signal_rule,
        "ma_period": int(ma_period),
        "threshold_pct": float(threshold_pct),
        "sigma_period": int(sigma_period),
        "buy_k": float(buy_k),
        "sell_k": float(sell_k),
        "buy_alpha_pct": float(buy_alpha_pct),
        "sell_alpha_pct": float(sell_alpha_pct),
        "anchor_k": float(anchor_k),
        "selection_allowed": bool(selection_allowed),
    }
    candidate["candidate_id"] = candidate_id(candidate)
    return candidate


def allocation_from_candidate(
    base: AuditAllocation,
    candidate: dict[str, object],
) -> AuditAllocation:
    return replace(
        base,
        weight_pct=100.0,
        strategy=base.strategy,
        ma_period=int(candidate["ma_period"]),
        threshold_pct=float(candidate["threshold_pct"]),
        signal_rule=str(candidate["signal_rule"]),
        sigma_period=int(candidate["sigma_period"]),
        buy_k=float(candidate["buy_k"]),
        sell_k=float(candidate["sell_k"]),
        buy_alpha_pct=float(candidate["buy_alpha_pct"]),
        sell_alpha_pct=float(candidate["sell_alpha_pct"]),
    )


def calculate_lagged_sigma(
    market: pd.DataFrame,
    ma_period: int,
    sigma_period: int,
) -> pd.DataFrame:
    data = market[["trade_date", "signal_close"]].copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.normalize()
    data["signal_close"] = pd.to_numeric(data["signal_close"], errors="coerce")
    data = data.dropna().sort_values("trade_date").drop_duplicates("trade_date")
    data["ma"] = data["signal_close"].rolling(ma_period, min_periods=ma_period).mean()
    data["deviation"] = data["signal_close"] / data["ma"] - 1
    data["sigma_prev"] = (
        data["deviation"]
        .rolling(sigma_period, min_periods=sigma_period)
        .std(ddof=1)
        .shift(1)
    )
    return data


def replace_signal_with_proxy(
    etf_market: pd.DataFrame,
    proxy_market: pd.DataFrame,
) -> pd.DataFrame:
    """Keep ETF execution prices fixed while replacing only its signal series."""
    proxy = proxy_market[["trade_date", "signal_close"]].copy()
    proxy["trade_date"] = pd.to_datetime(proxy["trade_date"], errors="coerce").dt.normalize()
    proxy = proxy.rename(columns={"signal_close": "proxy_signal_close"})
    merged = etf_market.copy()
    merged["trade_date"] = pd.to_datetime(merged["trade_date"], errors="coerce").dt.normalize()
    merged = merged.merge(proxy, on="trade_date", how="inner")
    for column in ("signal_open", "signal_close", "signal_high", "signal_low"):
        merged[column] = merged["proxy_signal_close"]
    return merged.drop(columns="proxy_signal_close")


def sigma_anchor(
    market: pd.DataFrame,
    *,
    ma_period: int,
    sigma_period: int,
    threshold_pct: float,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
) -> tuple[float, float, int]:
    sigma = calculate_lagged_sigma(market, ma_period, sigma_period)
    mask = sigma["trade_date"].between(pd.Timestamp(train_start), pd.Timestamp(train_end))
    valid = sigma.loc[mask, "sigma_prev"]
    valid = valid[np.isfinite(valid) & (valid > 0)]
    if valid.empty:
        return np.nan, np.nan, 0
    median_sigma = float(valid.median())
    return float((threshold_pct / 100) / median_sigma), median_sigma, int(len(valid))


def _longest_underwater_days(
    values: pd.Series,
    dates: pd.Series,
    initial_value: float,
    initial_date: pd.Timestamp,
) -> int:
    peak = float(initial_value)
    peak_date = pd.Timestamp(initial_date)
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
    return longest


def period_metrics(
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    fallback_initial: float,
) -> dict[str, object]:
    ordered = daily.copy()
    ordered["trade_date"] = pd.to_datetime(ordered["trade_date"]).dt.normalize()
    ordered = ordered.sort_values("trade_date").reset_index(drop=True)
    mask = ordered["trade_date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    selected = ordered.loc[mask].copy()
    if selected.empty:
        raise ValueError(f"区间无净值：{start_date} 至 {end_date}")
    prior = ordered.loc[ordered["trade_date"] < selected["trade_date"].iloc[0]]
    if prior.empty:
        initial_value = float(fallback_initial)
        initial_date = selected["trade_date"].iloc[0] - pd.Timedelta(days=1)
    else:
        initial_value = float(prior["portfolio_value"].iloc[-1])
        initial_date = pd.Timestamp(prior["trade_date"].iloc[-1])
    values = pd.to_numeric(selected["portfolio_value"], errors="coerce")
    seeded = pd.concat([pd.Series([initial_value]), values.reset_index(drop=True)], ignore_index=True)
    returns = seeded.pct_change().dropna()
    total_return = float(values.iloc[-1] / initial_value - 1)
    calendar_days = max(1, int((selected["trade_date"].iloc[-1] - selected["trade_date"].iloc[0]).days))
    annual_return = (1 + total_return) ** (365 / calendar_days) - 1 if total_return > -1 else -1.0
    volatility = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0.0
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * np.sqrt(252))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else 0.0
    )
    drawdown = seeded / seeded.cummax() - 1
    if trades is None or trades.empty:
        trade_count = 0
    else:
        execution_dates = pd.to_datetime(trades["execution_date"], errors="coerce").dt.normalize()
        trade_count = int(execution_dates.between(selected["trade_date"].iloc[0], selected["trade_date"].iloc[-1]).sum())
    return {
        "start_date": selected["trade_date"].iloc[0],
        "end_date": selected["trade_date"].iloc[-1],
        "trading_days": int(len(selected)),
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "max_drawdown_pct": float(drawdown.min() * 100),
        "annual_volatility_pct": volatility * 100,
        "sharpe_ratio": sharpe,
        "longest_underwater_days": _longest_underwater_days(
            values, selected["trade_date"], initial_value, initial_date
        ),
        "trade_count": trade_count,
        "final_value": float(values.iloc[-1]),
        "initial_value": initial_value,
    }


def split_windows(dates: Iterable[pd.Timestamp], parts: int = 3) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    normalized = pd.DatetimeIndex(pd.to_datetime(list(dates))).sort_values().unique()
    chunks = [chunk for chunk in np.array_split(normalized, parts) if len(chunk)]
    return [(pd.Timestamp(chunk[0]), pd.Timestamp(chunk[-1])) for chunk in chunks]


def _candidate_desired_position(
    market: pd.DataFrame,
    allocation: AuditAllocation,
) -> pd.Series:
    signal = pd.to_numeric(market["signal_close"], errors="coerce")
    ma = signal.rolling(allocation.ma_period, min_periods=allocation.ma_period).mean()
    if allocation.signal_rule == "percent":
        buy_threshold = pd.Series(allocation.threshold_pct / 100, index=market.index)
        sell_threshold = buy_threshold
    else:
        deviation = signal / ma - 1
        sigma_prev = (
            deviation.rolling(
                allocation.sigma_period, min_periods=allocation.sigma_period
            )
            .std(ddof=1)
            .shift(1)
        )
        valid_sigma = sigma_prev.where(np.isfinite(sigma_prev) & (sigma_prev > 0))
        buy_alpha = (
            allocation.buy_alpha_pct / 100
            if allocation.signal_rule == "hybrid_sigma"
            else 0.0
        )
        sell_alpha = (
            allocation.sell_alpha_pct / 100
            if allocation.signal_rule == "hybrid_sigma"
            else 0.0
        )
        buy_threshold = buy_alpha + allocation.buy_k * valid_sigma
        sell_threshold = sell_alpha + allocation.sell_k * valid_sigma
    desired = pd.Series(np.nan, index=market.index, dtype=float)
    desired.loc[signal > ma * (1 + buy_threshold)] = 1.0
    desired.loc[signal < ma * (1 - sell_threshold)] = 0.0
    return desired.ffill().fillna(0.0)


def _simulate_fast_sleeve(
    market: pd.DataFrame,
    desired: pd.Series,
    *,
    initial_capital: float,
    settings: AuditSettings,
) -> tuple[np.ndarray, list[pd.Timestamp]]:
    dates = pd.DatetimeIndex(pd.to_datetime(market["trade_date"])).normalize()
    raw_close = pd.to_numeric(market["raw_close"], errors="coerce").to_numpy(dtype=float)
    dividends = pd.to_numeric(
        market.get("dividend_per_share", pd.Series(0.0, index=market.index)), errors="coerce"
    ).fillna(0.0).to_numpy(dtype=float)
    split_ratios = pd.to_numeric(
        market.get("share_split_ratio", pd.Series(1.0, index=market.index)), errors="coerce"
    ).fillna(1.0).to_numpy(dtype=float)
    split_rounding = market.get(
        "share_split_rounding", pd.Series("", index=market.index)
    ).fillna("").astype(str).to_numpy()
    desired_values = desired.to_numpy(dtype=float)
    cash = float(initial_capital)
    shares = 0.0
    values = np.empty(len(market), dtype=float)
    execution_dates: list[pd.Timestamp] = []
    previous_date: pd.Timestamp | None = None
    slip = settings.slippage_bp / 10000
    for index, trade_date in enumerate(dates):
        if previous_date is not None and cash > 0 and settings.cash_annual_rate:
            calendar_days = max(0, int((trade_date - previous_date).days))
            cash *= (1 + settings.cash_annual_rate) ** (calendar_days / 365)
        if shares > 0 and not np.isclose(split_ratios[index], 1.0):
            adjusted = shares * split_ratios[index]
            if split_rounding[index] == "ceil":
                shares = float(np.ceil(adjusted - 1e-12))
            elif split_rounding[index] == "round":
                shares = float(np.rint(adjusted))
            else:
                shares = float(np.floor(adjusted + 1e-12))
        if shares > 0 and dividends[index] > 0:
            cash += shares * dividends[index]
        desired_state = int(desired_values[index] > 0)
        actual_state = int(shares > 0)
        if desired_state == 1 and actual_state == 0:
            execution_price = raw_close[index] * (1 + slip)
            affordable = cash / (execution_price * (1 + settings.commission_rate))
            buy_shares = np.floor(affordable / settings.lot_size) * settings.lot_size
            if buy_shares > 0:
                gross = buy_shares * execution_price
                cash -= gross + gross * settings.commission_rate
                shares = float(buy_shares)
                execution_dates.append(pd.Timestamp(trade_date))
        elif desired_state == 0 and actual_state == 1:
            execution_price = raw_close[index] * (1 - slip)
            gross = shares * execution_price
            cash += gross - gross * settings.commission_rate
            shares = 0.0
            execution_dates.append(pd.Timestamp(trade_date))
        values[index] = cash + shares * raw_close[index]
        previous_date = pd.Timestamp(trade_date)
    return values, execution_dates


def run_candidate_training_fast(
    market: pd.DataFrame,
    allocation: AuditAllocation,
    settings: AuditSettings,
    *,
    desired_override: pd.Series | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if (
        settings.execution_mode != EXECUTION_AFTER_CLOSE
        or settings.after_hours_fill_rate < 1
        or settings.missed_signal_rate > 0
    ):
        raise ValueError("快速训练路径只支持盘后全成交且无漏单的严格基线。")
    data = market.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.normalize()
    data = data.sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)
    desired = (
        _candidate_desired_position(data, allocation)
        if desired_override is None
        else pd.Series(desired_override.to_numpy(dtype=float), index=data.index)
    )
    if allocation.strategy == "half_timing":
        hold_values, hold_trades = _simulate_fast_sleeve(
            data,
            pd.Series(1.0, index=data.index),
            initial_capital=settings.initial_capital * 0.5,
            settings=settings,
        )
        timing_values, timing_trades = _simulate_fast_sleeve(
            data,
            desired,
            initial_capital=settings.initial_capital * 0.5,
            settings=settings,
        )
        values = hold_values + timing_values
        execution_dates = [*hold_trades, *timing_trades]
    elif allocation.strategy == "hold":
        values, execution_dates = _simulate_fast_sleeve(
            data,
            pd.Series(1.0, index=data.index),
            initial_capital=settings.initial_capital,
            settings=settings,
        )
    else:
        values, execution_dates = _simulate_fast_sleeve(
            data,
            desired,
            initial_capital=settings.initial_capital,
            settings=settings,
        )
    daily = pd.DataFrame(
        {"trade_date": data["trade_date"], "portfolio_value": values}
    )
    trades = pd.DataFrame({"execution_date": execution_dates})
    return daily, trades


def evaluate_candidates(
    market: pd.DataFrame,
    base: AuditAllocation,
    candidates: list[dict[str, object]],
    settings: AuditSettings,
    *,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    include_subwindows: bool = True,
) -> pd.DataFrame:
    dates = pd.DatetimeIndex(pd.to_datetime(market["trade_date"])).sort_values().unique()
    dates = dates[(dates >= pd.Timestamp(start_date)) & (dates <= pd.Timestamp(end_date))]
    if len(dates) < 2:
        raise ValueError("评估区间共同交易日不足。")
    windows = [("full", pd.Timestamp(dates[0]), pd.Timestamp(dates[-1]))]
    if include_subwindows:
        windows.extend(
            (f"subwindow_{index}", window_start, window_end)
            for index, (window_start, window_end) in enumerate(split_windows(dates, 3), start=1)
        )
    rows: list[dict[str, object]] = []
    run_settings = replace(
        settings,
        initial_capital=float(settings.initial_capital),
        start_date=pd.Timestamp(dates[0]),
        end_date=pd.Timestamp(dates[-1]),
        execution_mode=settings.execution_mode or EXECUTION_AFTER_CLOSE,
    )
    indicator_market = market.copy()
    indicator_market["trade_date"] = pd.to_datetime(
        indicator_market["trade_date"], errors="coerce"
    ).dt.normalize()
    indicator_market = (
        indicator_market[indicator_market["trade_date"] <= dates[-1]]
        .sort_values("trade_date")
        .drop_duplicates("trade_date")
        .reset_index(drop=True)
    )
    selected_mask = indicator_market["trade_date"].isin(dates)
    selected_market = indicator_market.loc[selected_mask].reset_index(drop=True)
    for candidate in _deduplicate_candidates(candidates):
        allocation = allocation_from_candidate(base, candidate)
        indicator_desired = _candidate_desired_position(indicator_market, allocation)
        selected_desired = indicator_desired.loc[selected_mask].reset_index(drop=True)
        daily, trades = run_candidate_training_fast(
            selected_market,
            allocation,
            run_settings,
            desired_override=selected_desired,
        )
        for window_name, window_start, window_end in windows:
            metrics = period_metrics(
                daily,
                trades,
                start_date=window_start,
                end_date=window_end,
                fallback_initial=settings.initial_capital,
            )
            rows.append(
                {
                    "symbol": base.symbol,
                    "name": base.name,
                    **candidate,
                    "evaluation_window": window_name,
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def score_evaluations(evaluations: pd.DataFrame) -> pd.DataFrame:
    scored = evaluations.copy()
    if scored.empty:
        return scored
    scored["underwater_score"] = scored.groupby("evaluation_window")[
        "longest_underwater_days"
    ].rank(ascending=False, pct=True, method="average")
    scored["drawdown_score"] = scored.groupby("evaluation_window")[
        "max_drawdown_pct"
    ].rank(ascending=True, pct=True, method="average")
    scored["volatility_score"] = scored.groupby("evaluation_window")[
        "annual_volatility_pct"
    ].rank(ascending=False, pct=True, method="average")
    scored["return_score"] = scored.groupby("evaluation_window")[
        "annual_return_pct"
    ].rank(ascending=True, pct=True, method="average")
    scored["sharpe_score"] = scored.groupby("evaluation_window")[
        "sharpe_ratio"
    ].rank(ascending=True, pct=True, method="average")
    scored["composite_score"] = (
        scored["underwater_score"] * SCORE_WEIGHTS["longest_underwater_days"]
        + scored["drawdown_score"] * SCORE_WEIGHTS["max_drawdown_pct"]
        + scored["volatility_score"] * SCORE_WEIGHTS["annual_volatility_pct"]
        + scored["return_score"] * SCORE_WEIGHTS["annual_return_pct"]
        + scored["sharpe_score"] * SCORE_WEIGHTS["sharpe_ratio"]
    )
    return scored


def aggregate_training_scores(scored: pd.DataFrame) -> pd.DataFrame:
    if scored.empty:
        return pd.DataFrame()
    config_columns = [
        "symbol",
        "name",
        "candidate_id",
        "stage",
        "model_family",
        "signal_rule",
        "ma_period",
        "threshold_pct",
        "sigma_period",
        "buy_k",
        "sell_k",
        "buy_alpha_pct",
        "sell_alpha_pct",
        "anchor_k",
        "selection_allowed",
    ]
    full = scored[scored["evaluation_window"] == "full"].set_index("candidate_id")
    sub = scored[scored["evaluation_window"].str.startswith("subwindow_")]
    aggregate = (
        sub.groupby("candidate_id", as_index=False)
        .agg(
            min_window_score=("composite_score", "min"),
            mean_window_score=("composite_score", "mean"),
            score_std=("composite_score", lambda values: float(values.std(ddof=0))),
            subwindow_trade_count=("trade_count", "sum"),
        )
    )
    configs = scored[config_columns].drop_duplicates("candidate_id")
    aggregate = configs.merge(aggregate, on="candidate_id", how="left")
    aggregate["full_annual_return_pct"] = aggregate["candidate_id"].map(
        full["annual_return_pct"]
    )
    aggregate["full_composite_score"] = aggregate["candidate_id"].map(
        full["composite_score"]
    )
    aggregate["full_trade_count"] = aggregate["candidate_id"].map(full["trade_count"])
    return aggregate


def choose_candidate(
    aggregate: pd.DataFrame,
    *,
    families: set[str] | None = None,
) -> tuple[dict[str, object] | None, bool]:
    pool = aggregate.copy()
    if families is not None:
        pool = pool[pool["model_family"].isin(families)]
    pool = pool[pool["selection_allowed"]].copy()
    if pool.empty:
        return None, False
    meets_return = pool["full_annual_return_pct"] >= 10.0
    gate_relaxed = not bool(meets_return.any())
    if not gate_relaxed:
        pool = pool[meets_return]
    pool = pool.sort_values(
        [
            "min_window_score",
            "mean_window_score",
            "score_std",
            "subwindow_trade_count",
            "candidate_id",
        ],
        ascending=[False, False, True, True, True],
    )
    return pool.iloc[0].to_dict(), gate_relaxed


def stage1_candidates(
    market: pd.DataFrame,
    base: AuditAllocation,
    *,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
) -> list[dict[str, object]]:
    candidates = [
        make_candidate(
            stage="stage1",
            model_family="current_fixed",
            ma_period=base.ma_period,
            threshold_pct=base.threshold_pct,
        )
    ]
    candidates.extend(
        make_candidate(
            stage="stage1",
            model_family="retuned_fixed",
            ma_period=base.ma_period,
            threshold_pct=base.threshold_pct * multiplier,
        )
        for multiplier in PARAMETER_MULTIPLIERS
    )
    for sigma_period in SIGMA_PERIODS:
        anchor, _median, _count = sigma_anchor(
            market,
            ma_period=base.ma_period,
            sigma_period=sigma_period,
            threshold_pct=base.threshold_pct,
            train_start=train_start,
            train_end=train_end,
        )
        if not np.isfinite(anchor):
            continue
        candidates.extend(
            make_candidate(
                stage="stage1",
                model_family="sigma_symmetric",
                ma_period=base.ma_period,
                sigma_period=sigma_period,
                buy_k=anchor * multiplier,
                sell_k=anchor * multiplier,
                anchor_k=anchor,
            )
            for multiplier in PARAMETER_MULTIPLIERS
        )
    return _deduplicate_candidates(candidates)


def _best_symmetric_by_period(aggregate: pd.DataFrame) -> pd.DataFrame:
    symmetric = aggregate[aggregate["model_family"] == "sigma_symmetric"].copy()
    if symmetric.empty:
        return symmetric
    return (
        symmetric.sort_values(
            ["min_window_score", "mean_window_score", "score_std", "candidate_id"],
            ascending=[False, False, True, True],
        )
        .drop_duplicates(["ma_period", "sigma_period"])
        .reset_index(drop=True)
    )


def derived_stage1_candidates(
    base: AuditAllocation,
    aggregate: pd.DataFrame,
) -> list[dict[str, object]]:
    best_by_period = _best_symmetric_by_period(aggregate)
    if best_by_period.empty:
        return []
    best_symmetric = best_by_period.sort_values(
        ["min_window_score", "mean_window_score", "score_std"],
        ascending=[False, False, True],
    ).iloc[0]
    asymmetric = [
        make_candidate(
            stage="stage1",
            model_family="sigma_asymmetric",
            ma_period=base.ma_period,
            sigma_period=int(best_symmetric["sigma_period"]),
            buy_k=float(best_symmetric["buy_k"]) * buy_multiplier,
            sell_k=float(best_symmetric["sell_k"]) * sell_multiplier,
            anchor_k=float(best_symmetric["anchor_k"]),
        )
        for buy_multiplier in ASYMMETRIC_MULTIPLIERS
        for sell_multiplier in ASYMMETRIC_MULTIPLIERS
    ]
    robust_periods = best_by_period.sort_values(
        ["min_window_score", "mean_window_score", "score_std"],
        ascending=[False, False, True],
    ).head(2)
    hybrid: list[dict[str, object]] = []
    for row in robust_periods.itertuples(index=False):
        for alpha_multiplier in HYBRID_ALPHA_MULTIPLIERS:
            for k_multiplier in HYBRID_K_MULTIPLIERS:
                hybrid.append(
                    make_candidate(
                        stage="stage1",
                        model_family="hybrid_sigma",
                        ma_period=base.ma_period,
                        sigma_period=int(row.sigma_period),
                        buy_k=float(row.anchor_k) * k_multiplier,
                        sell_k=float(row.anchor_k) * k_multiplier,
                        buy_alpha_pct=base.threshold_pct * alpha_multiplier,
                        sell_alpha_pct=base.threshold_pct * alpha_multiplier,
                        anchor_k=float(row.anchor_k),
                    )
                )
    return _deduplicate_candidates([*asymmetric, *hybrid])


def _family_stability(
    scored: pd.DataFrame,
    aggregate: pd.DataFrame,
) -> pd.DataFrame:
    current = aggregate[aggregate["model_family"] == "current_fixed"]
    if current.empty:
        return pd.DataFrame()
    current_row = current.iloc[0]
    current_scores = scored[
        (scored["candidate_id"] == current_row["candidate_id"])
        & scored["evaluation_window"].str.startswith("subwindow_")
    ][["evaluation_window", "composite_score"]].rename(
        columns={"composite_score": "current_score"}
    )
    rows: list[dict[str, object]] = []
    for family in ("sigma_symmetric", "sigma_asymmetric", "hybrid_sigma"):
        best, gate_relaxed = choose_candidate(aggregate, families={family})
        if best is None:
            continue
        family_scores = scored[
            (scored["candidate_id"] == best["candidate_id"])
            & scored["evaluation_window"].str.startswith("subwindow_")
        ][["evaluation_window", "composite_score"]]
        comparison = family_scores.merge(current_scores, on="evaluation_window", how="inner")
        wins = int((comparison["composite_score"] > comparison["current_score"]).sum())
        stable = bool(
            best["min_window_score"] > current_row["min_window_score"]
            and best["mean_window_score"] > current_row["mean_window_score"]
            and wins >= 2
        )
        rows.append(
            {
                "model_family": family,
                "candidate_id": best["candidate_id"],
                "stable_training_advantage": stable,
                "subwindow_wins_vs_current": wins,
                "return_gate_relaxed": gate_relaxed,
                "min_window_score": best["min_window_score"],
                "mean_window_score": best["mean_window_score"],
            }
        )
    return pd.DataFrame(rows)


def run_stage1(
    market: pd.DataFrame,
    base: AuditAllocation,
    settings: AuditSettings,
    *,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
) -> dict[str, object]:
    initial_candidates = stage1_candidates(
        market, base, train_start=train_start, train_end=train_end
    )
    initial_eval = evaluate_candidates(
        market,
        base,
        initial_candidates,
        settings,
        start_date=train_start,
        end_date=train_end,
    )
    initial_scored = score_evaluations(initial_eval)
    initial_aggregate = aggregate_training_scores(initial_scored)
    derived = derived_stage1_candidates(base, initial_aggregate)
    all_candidates = _deduplicate_candidates([*initial_candidates, *derived])
    derived_eval = (
        evaluate_candidates(
            market,
            base,
            derived,
            settings,
            start_date=train_start,
            end_date=train_end,
        )
        if derived
        else pd.DataFrame()
    )
    evaluations = pd.concat([initial_eval, derived_eval], ignore_index=True)
    scored = score_evaluations(evaluations)
    aggregate = aggregate_training_scores(scored)
    family_stability = _family_stability(scored, aggregate)
    return {
        "candidates": pd.DataFrame(all_candidates),
        "evaluations": scored,
        "aggregate": aggregate,
        "family_stability": family_stability,
    }


def local_ma_periods(current_ma: int) -> tuple[int, ...]:
    return tuple(sorted({max(5, current_ma - 10), max(5, current_ma - 5), current_ma, current_ma + 5, current_ma + 10}))


def _stage2_initial_candidates(
    market: pd.DataFrame,
    base: AuditAllocation,
    stable_families: set[str],
    *,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
) -> list[dict[str, object]]:
    candidates = [
        make_candidate(
            stage="stage2",
            model_family="current_fixed",
            ma_period=base.ma_period,
            threshold_pct=base.threshold_pct,
        )
    ]
    need_sigma_calibration = bool(stable_families)
    for ma_period in local_ma_periods(base.ma_period):
        candidates.extend(
            make_candidate(
                stage="stage2",
                model_family="joint_fixed",
                ma_period=ma_period,
                threshold_pct=base.threshold_pct * multiplier,
            )
            for multiplier in PARAMETER_MULTIPLIERS
        )
        if not need_sigma_calibration:
            continue
        for sigma_period in SIGMA_PERIODS:
            anchor, _median, _count = sigma_anchor(
                market,
                ma_period=ma_period,
                sigma_period=sigma_period,
                threshold_pct=base.threshold_pct,
                train_start=train_start,
                train_end=train_end,
            )
            if not np.isfinite(anchor):
                continue
            candidates.extend(
                make_candidate(
                    stage="stage2",
                    model_family="sigma_symmetric",
                    ma_period=ma_period,
                    sigma_period=sigma_period,
                    buy_k=anchor * multiplier,
                    sell_k=anchor * multiplier,
                    anchor_k=anchor,
                    selection_allowed="sigma_symmetric" in stable_families,
                )
                for multiplier in PARAMETER_MULTIPLIERS
            )
    return _deduplicate_candidates(candidates)


def _stage2_derived_candidates(
    base: AuditAllocation,
    aggregate: pd.DataFrame,
    stable_families: set[str],
) -> list[dict[str, object]]:
    best_by_period = _best_symmetric_by_period(aggregate)
    if best_by_period.empty:
        return []
    candidates: list[dict[str, object]] = []
    for ma_period, ma_group in best_by_period.groupby("ma_period"):
        ranked = ma_group.sort_values(
            ["min_window_score", "mean_window_score", "score_std"],
            ascending=[False, False, True],
        )
        best = ranked.iloc[0]
        if "sigma_asymmetric" in stable_families:
            candidates.extend(
                make_candidate(
                    stage="stage2",
                    model_family="sigma_asymmetric",
                    ma_period=int(ma_period),
                    sigma_period=int(best["sigma_period"]),
                    buy_k=float(best["buy_k"]) * buy_multiplier,
                    sell_k=float(best["sell_k"]) * sell_multiplier,
                    anchor_k=float(best["anchor_k"]),
                )
                for buy_multiplier in ASYMMETRIC_MULTIPLIERS
                for sell_multiplier in ASYMMETRIC_MULTIPLIERS
            )
        if "hybrid_sigma" in stable_families:
            for row in ranked.head(2).itertuples(index=False):
                for alpha_multiplier in HYBRID_ALPHA_MULTIPLIERS:
                    for k_multiplier in HYBRID_K_MULTIPLIERS:
                        candidates.append(
                            make_candidate(
                                stage="stage2",
                                model_family="hybrid_sigma",
                                ma_period=int(ma_period),
                                sigma_period=int(row.sigma_period),
                                buy_k=float(row.anchor_k) * k_multiplier,
                                sell_k=float(row.anchor_k) * k_multiplier,
                                buy_alpha_pct=base.threshold_pct * alpha_multiplier,
                                sell_alpha_pct=base.threshold_pct * alpha_multiplier,
                                anchor_k=float(row.anchor_k),
                            )
                        )
    return _deduplicate_candidates(candidates)


def run_stage2(
    market: pd.DataFrame,
    base: AuditAllocation,
    settings: AuditSettings,
    *,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    stable_families: set[str],
) -> dict[str, object]:
    initial_candidates = _stage2_initial_candidates(
        market,
        base,
        stable_families,
        train_start=train_start,
        train_end=train_end,
    )
    initial_eval = evaluate_candidates(
        market,
        base,
        initial_candidates,
        settings,
        start_date=train_start,
        end_date=train_end,
    )
    initial_scored = score_evaluations(initial_eval)
    initial_aggregate = aggregate_training_scores(initial_scored)
    derived = _stage2_derived_candidates(base, initial_aggregate, stable_families)
    all_candidates = _deduplicate_candidates([*initial_candidates, *derived])
    derived_eval = (
        evaluate_candidates(
            market,
            base,
            derived,
            settings,
            start_date=train_start,
            end_date=train_end,
        )
        if derived
        else pd.DataFrame()
    )
    evaluations = pd.concat([initial_eval, derived_eval], ignore_index=True)
    scored = score_evaluations(evaluations)
    aggregate = aggregate_training_scores(scored)
    best_fixed, fixed_gate_relaxed = choose_candidate(
        aggregate, families={"joint_fixed"}
    )
    best_dynamic, dynamic_gate_relaxed = choose_candidate(
        aggregate,
        families=stable_families,
    )
    return {
        "candidates": pd.DataFrame(all_candidates),
        "evaluations": scored,
        "aggregate": aggregate,
        "best_fixed": best_fixed,
        "best_dynamic": best_dynamic,
        "fixed_return_gate_relaxed": fixed_gate_relaxed,
        "dynamic_return_gate_relaxed": dynamic_gate_relaxed,
    }


def fit_two_stage_model(
    market: pd.DataFrame,
    base: AuditAllocation,
    settings: AuditSettings,
    *,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
) -> dict[str, object]:
    stage1 = run_stage1(
        market,
        base,
        settings,
        train_start=train_start,
        train_end=train_end,
    )
    stability = stage1["family_stability"]
    stable_families = (
        set(stability.loc[stability["stable_training_advantage"], "model_family"])
        if not stability.empty
        else set()
    )
    stage2 = run_stage2(
        market,
        base,
        settings,
        train_start=train_start,
        train_end=train_end,
        stable_families=stable_families,
    )
    current = stage2["aggregate"][
        stage2["aggregate"]["model_family"] == "current_fixed"
    ].iloc[0].to_dict()
    return {
        "stage1": stage1,
        "stage2": stage2,
        "stable_families": stable_families,
        "current": current,
        "best_fixed": stage2["best_fixed"],
        "best_dynamic": stage2["best_dynamic"],
    }


def evaluate_frozen_models(
    market: pd.DataFrame,
    base: AuditAllocation,
    settings: AuditSettings,
    candidates: list[dict[str, object]],
    *,
    path_start: pd.Timestamp,
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
    evaluation_label: str,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    market_dates = pd.DatetimeIndex(pd.to_datetime(market["trade_date"])).sort_values().unique()
    test_dates = market_dates[
        (market_dates >= pd.Timestamp(test_start))
        & (market_dates <= pd.Timestamp(test_end))
    ]
    windows = [("full", test_start, test_end)] + [
        (f"subwindow_{index}", start, end)
        for index, (start, end) in enumerate(
            split_windows(test_dates, 3),
            start=1,
        )
    ]
    run_settings = replace(settings, start_date=path_start, end_date=test_end)
    for candidate in _deduplicate_candidates(candidates):
        result = run_portfolio_audit(
            {base.symbol: market},
            [allocation_from_candidate(base, candidate)],
            run_settings,
        )
        for window_name, window_start, window_end in windows:
            rows.append(
                {
                    "symbol": base.symbol,
                    "name": base.name,
                    "evaluation_label": evaluation_label,
                    **candidate,
                    "evaluation_window": window_name,
                    **period_metrics(
                        result.daily,
                        result.trades,
                        start_date=window_start,
                        end_date=window_end,
                        fallback_initial=settings.initial_capital,
                    ),
                }
            )
    return score_evaluations(pd.DataFrame(rows))


def holdout_split_dates(market: pd.DataFrame, train_fraction: float = 0.70) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    dates = pd.DatetimeIndex(pd.to_datetime(market["trade_date"])).sort_values().unique()
    split_index = min(max(int(len(dates) * train_fraction), 1), len(dates) - 1)
    return (
        pd.Timestamp(dates[0]),
        pd.Timestamp(dates[split_index - 1]),
        pd.Timestamp(dates[split_index]),
        pd.Timestamp(dates[-1]),
    )


def walk_forward_schedule(market: pd.DataFrame) -> tuple[str, pd.DateOffset, pd.DateOffset]:
    dates = pd.DatetimeIndex(pd.to_datetime(market["trade_date"])).sort_values().unique()
    years = (dates[-1] - dates[0]).days / 365.25
    if years >= 5:
        return "3年训练/1年测试", pd.DateOffset(years=3), pd.DateOffset(years=1)
    if years >= 3:
        return "2年训练/半年测试", pd.DateOffset(years=2), pd.DateOffset(months=6)
    return "1年训练/3个月测试（探索性）", pd.DateOffset(years=1), pd.DateOffset(months=3)


def build_walk_forward_windows(market: pd.DataFrame) -> list[dict[str, object]]:
    dates = pd.DatetimeIndex(pd.to_datetime(market["trade_date"])).sort_values().unique()
    tier, train_offset, test_offset = walk_forward_schedule(market)
    windows: list[dict[str, object]] = []
    train_start = pd.Timestamp(dates[0])
    fold = 1
    while True:
        requested_train_end = train_start + train_offset
        requested_test_end = requested_train_end + test_offset
        train_dates = dates[(dates >= train_start) & (dates < requested_train_end)]
        if len(train_dates) < 2:
            break
        train_end = pd.Timestamp(train_dates[-1])
        test_dates = dates[(dates > train_end) & (dates < requested_test_end)]
        if len(test_dates) < 2:
            break
        if pd.Timestamp(test_dates[-1]) < requested_test_end - pd.Timedelta(days=31):
            break
        test_start = pd.Timestamp(test_dates[0])
        test_end = pd.Timestamp(test_dates[-1])
        windows.append(
            {
                "fold": fold,
                "tier": tier,
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
            }
        )
        train_start = test_start
        fold += 1
    return windows


def selected_candidates(fit: dict[str, object]) -> list[dict[str, object]]:
    rows = [fit["current"], fit["best_fixed"]]
    if fit.get("best_dynamic") is not None:
        rows.append(fit["best_dynamic"])
    return _deduplicate_candidates([_candidate_config(row) for row in rows if row is not None])


def parameter_stability_rows(
    stage2_aggregate: pd.DataFrame,
    selected: dict[str, object] | None,
) -> pd.DataFrame:
    if selected is None or stage2_aggregate.empty:
        return pd.DataFrame()
    dynamic = stage2_aggregate[
        stage2_aggregate["model_family"].isin(
            {"sigma_symmetric", "sigma_asymmetric", "hybrid_sigma"}
        )
    ].copy()
    if dynamic.empty:
        return dynamic
    selected_ma = int(selected["ma_period"])
    selected_sigma = int(selected["sigma_period"])
    sigma_index = SIGMA_PERIODS.index(selected_sigma) if selected_sigma in SIGMA_PERIODS else -1
    neighboring_sigma = {selected_sigma}
    if sigma_index > 0:
        neighboring_sigma.add(SIGMA_PERIODS[sigma_index - 1])
    if 0 <= sigma_index < len(SIGMA_PERIODS) - 1:
        neighboring_sigma.add(SIGMA_PERIODS[sigma_index + 1])
    buy_k = float(selected["buy_k"])
    sell_k = float(selected["sell_k"])
    ratio_buy = np.where(buy_k > 0, dynamic["buy_k"] / buy_k, np.nan)
    ratio_sell = np.where(sell_k > 0, dynamic["sell_k"] / sell_k, np.nan)
    mask = (
        dynamic["ma_period"].between(selected_ma - 5, selected_ma + 5)
        & dynamic["sigma_period"].isin(neighboring_sigma)
        & pd.Series(ratio_buy, index=dynamic.index).between(0.75, 1.25)
        & pd.Series(ratio_sell, index=dynamic.index).between(0.75, 1.25)
    )
    neighbors = dynamic.loc[mask].copy()
    selected_score = float(selected["mean_window_score"])
    neighbors["score_ratio_vs_selected"] = neighbors["mean_window_score"] / selected_score if selected_score else np.nan
    neighbors["stable_neighbor"] = neighbors["score_ratio_vs_selected"] >= 0.90
    return neighbors


def _candidate_config(row: dict[str, object]) -> dict[str, object]:
    keys = {
        "candidate_id",
        "stage",
        "model_family",
        "signal_rule",
        "ma_period",
        "threshold_pct",
        "sigma_period",
        "buy_k",
        "sell_k",
        "buy_alpha_pct",
        "sell_alpha_pct",
        "anchor_k",
        "selection_allowed",
    }
    return {key: row[key] for key in keys if key in row}


def _deduplicate_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    deduplicated: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        normalized = dict(candidate)
        normalized["candidate_id"] = candidate_id(normalized)
        deduplicated.setdefault(str(normalized["candidate_id"]), normalized)
    return list(deduplicated.values())


def serialize_allocation(allocation: AuditAllocation) -> dict[str, object]:
    return asdict(allocation)
