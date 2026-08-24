from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

from services.portfolio_audit import (
    AuditAllocation,
    AuditRunResult,
    AuditSettings,
)


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
