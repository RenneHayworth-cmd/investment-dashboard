from __future__ import annotations

from dataclasses import replace

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
        current = _run_portfolio_audit(
            {item.symbol: frame}, [current_item], audit_settings
        )
        unified = _run_portfolio_audit(
            {item.symbol: frame}, [unified_item], audit_settings
        )
        hold = _run_portfolio_audit(
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
                result = _run_portfolio_audit(
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
            period_current = _run_portfolio_audit(
                {item.symbol: frame}, [current_item], period_settings
            )
            period_unified = _run_portfolio_audit(
                {item.symbol: frame}, [unified_item], period_settings
            )
            period_hold = _run_portfolio_audit(
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
