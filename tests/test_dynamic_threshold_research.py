import unittest

import numpy as np
import pandas as pd

from services.dynamic_threshold_research import (
    SCORE_WEIGHTS,
    calculate_lagged_sigma,
    choose_candidate,
    evaluate_candidates,
    fit_two_stage_model,
    replace_signal_with_proxy,
    run_candidate_training_fast,
    score_evaluations,
    stage1_candidates,
)
from services.portfolio_audit import AuditAllocation, AuditSettings, run_portfolio_audit


def research_market(prices: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", periods=len(prices))
    values = np.asarray(prices, dtype=float)
    return pd.DataFrame(
        {
            "trade_date": dates,
            "raw_open": values,
            "raw_close": values,
            "raw_high": values,
            "raw_low": values,
            "signal_open": values,
            "signal_close": values,
            "signal_high": values,
            "signal_low": values,
            "adjustment_factor": 1.0,
            "dividend_per_share": 0.0,
            "share_split_ratio": 1.0,
            "share_split_rounding": "",
            "share_split_source": "",
            "corporate_action_status": "无调整事件",
        }
    )


class DynamicThresholdResearchTests(unittest.TestCase):
    def test_future_prices_do_not_change_training_candidates(self):
        prices = (100 + np.sin(np.arange(180) / 7) * 8 + np.arange(180) * 0.05).tolist()
        first = research_market(prices)
        second = first.copy()
        second.loc[150:, "signal_close"] *= 5
        base = AuditAllocation("A", "A", 100, "timing", 20, 1.0)
        train_start = first.loc[0, "trade_date"]
        train_end = first.loc[149, "trade_date"]

        first_candidates = pd.DataFrame(
            stage1_candidates(first, base, train_start=train_start, train_end=train_end)
        ).sort_values("candidate_id").reset_index(drop=True)
        second_candidates = pd.DataFrame(
            stage1_candidates(second, base, train_start=train_start, train_end=train_end)
        ).sort_values("candidate_id").reset_index(drop=True)

        pd.testing.assert_frame_equal(first_candidates, second_candidates)

    def test_ma_change_recalculates_deviation_and_sigma(self):
        market = research_market([10, 11, 9, 12, 8, 13, 7, 14, 9, 15])
        ma2 = calculate_lagged_sigma(market, ma_period=2, sigma_period=3)
        ma3 = calculate_lagged_sigma(market, ma_period=3, sigma_period=3)

        self.assertFalse(ma2["deviation"].equals(ma3["deviation"]))
        self.assertFalse(ma2["sigma_prev"].fillna(-1).equals(ma3["sigma_prev"].fillna(-1)))

    def test_scoring_uses_requested_weights(self):
        rows = pd.DataFrame(
            [
                {
                    "candidate_id": "better",
                    "evaluation_window": "subwindow_1",
                    "longest_underwater_days": 10,
                    "max_drawdown_pct": -5,
                    "annual_volatility_pct": 10,
                    "annual_return_pct": 15,
                    "sharpe_ratio": 1.2,
                },
                {
                    "candidate_id": "worse",
                    "evaluation_window": "subwindow_1",
                    "longest_underwater_days": 20,
                    "max_drawdown_pct": -10,
                    "annual_volatility_pct": 20,
                    "annual_return_pct": 5,
                    "sharpe_ratio": 0.4,
                },
            ]
        )
        scored = score_evaluations(rows).set_index("candidate_id")

        self.assertAlmostEqual(sum(SCORE_WEIGHTS.values()), 1.0)
        self.assertAlmostEqual(scored.loc["better", "composite_score"], 1.0)
        self.assertAlmostEqual(scored.loc["worse", "composite_score"], 0.5)

    def test_ten_percent_gate_is_relaxed_only_when_every_candidate_misses(self):
        base_rows = pd.DataFrame(
            [
                {
                    "candidate_id": "high-score-low-return",
                    "model_family": "sigma_symmetric",
                    "selection_allowed": True,
                    "full_annual_return_pct": 9.0,
                    "min_window_score": 0.9,
                    "mean_window_score": 0.9,
                    "score_std": 0.0,
                    "subwindow_trade_count": 2,
                },
                {
                    "candidate_id": "eligible",
                    "model_family": "sigma_symmetric",
                    "selection_allowed": True,
                    "full_annual_return_pct": 10.0,
                    "min_window_score": 0.8,
                    "mean_window_score": 0.8,
                    "score_std": 0.0,
                    "subwindow_trade_count": 3,
                },
            ]
        )

        selected, relaxed = choose_candidate(base_rows)
        self.assertEqual(selected["candidate_id"], "eligible")
        self.assertFalse(relaxed)

        below = base_rows.copy()
        below["full_annual_return_pct"] = [8.0, 9.0]
        selected, relaxed = choose_candidate(below)
        self.assertEqual(selected["candidate_id"], "high-score-low-return")
        self.assertTrue(relaxed)

    def test_candidate_evaluation_is_deterministic(self):
        market = research_market(
            (100 + np.sin(np.arange(120) / 4) * 10 + np.arange(120) * 0.1).tolist()
        )
        base = AuditAllocation("A", "A", 100, "timing", 10, 1.0)
        candidate = stage1_candidates(
            market,
            base,
            train_start=market.loc[0, "trade_date"],
            train_end=market.loc[99, "trade_date"],
        )[:3]
        settings = AuditSettings(initial_capital=10000, commission_rate=0.00006, lot_size=100)

        first = evaluate_candidates(
            market,
            base,
            candidate,
            settings,
            start_date=market.loc[0, "trade_date"],
            end_date=market.loc[99, "trade_date"],
        )
        second = evaluate_candidates(
            market,
            base,
            candidate,
            settings,
            start_date=market.loc[0, "trade_date"],
            end_date=market.loc[99, "trade_date"],
        )
        pd.testing.assert_frame_equal(first, second)

    def test_oos_prices_cannot_change_fitted_parameter_group(self):
        prices = (100 + np.sin(np.arange(180) / 5) * 9 + np.arange(180) * 0.08).tolist()
        first = research_market(prices)
        second = first.copy()
        second.loc[150:, ["signal_open", "signal_close", "signal_high", "signal_low"]] *= 4
        second.loc[150:, ["raw_open", "raw_close", "raw_high", "raw_low"]] *= 4
        base = AuditAllocation("A", "A", 100, "timing", 10, 1.0)
        settings = AuditSettings(initial_capital=10000, commission_rate=0.00006, lot_size=100)
        train_start = first.loc[0, "trade_date"]
        train_end = first.loc[149, "trade_date"]

        first_fit = fit_two_stage_model(
            first, base, settings, train_start=train_start, train_end=train_end
        )
        second_fit = fit_two_stage_model(
            second, base, settings, train_start=train_start, train_end=train_end
        )

        self.assertEqual(first_fit["stable_families"], second_fit["stable_families"])
        for key in ("best_fixed", "best_dynamic"):
            first_id = first_fit[key]["candidate_id"] if first_fit[key] else None
            second_id = second_fit[key]["candidate_id"] if second_fit[key] else None
            self.assertEqual(first_id, second_id)

    def test_proxy_control_changes_signal_but_not_etf_execution_prices(self):
        etf = research_market([10, 11, 12, 13, 14])
        proxy = research_market([100, 90, 80, 70, 60])

        controlled = replace_signal_with_proxy(etf, proxy)

        np.testing.assert_array_equal(controlled["raw_close"], etf["raw_close"])
        np.testing.assert_array_equal(controlled["signal_close"], proxy["signal_close"])

    def test_fast_training_path_matches_strict_audit(self):
        market = research_market(
            (100 + np.sin(np.arange(180) / 5) * 12 + np.arange(180) * 0.04).tolist()
        )
        settings = AuditSettings(
            initial_capital=100000,
            commission_rate=0.00006,
            lot_size=100,
            slippage_bp=5,
        )
        allocations = [
            AuditAllocation("A", "A", 100, "timing", 20, 1.0),
            AuditAllocation(
                "A", "A", 100, "timing", 20, signal_rule="sigma", sigma_period=40, buy_k=1.0, sell_k=1.25
            ),
            AuditAllocation(
                "A", "A", 100, "half_timing", 20, signal_rule="hybrid_sigma", sigma_period=40,
                buy_k=0.5, sell_k=0.75, buy_alpha_pct=0.25, sell_alpha_pct=0.5
            ),
        ]
        for allocation in allocations:
            with self.subTest(rule=allocation.signal_rule, strategy=allocation.strategy):
                strict = run_portfolio_audit({"A": market}, [allocation], settings)
                fast_daily, fast_trades = run_candidate_training_fast(
                    market, allocation, settings
                )
                np.testing.assert_allclose(
                    fast_daily["portfolio_value"],
                    strict.daily["portfolio_value"],
                    rtol=0,
                    atol=1e-8,
                )
                strict_dates = pd.to_datetime(strict.trades["execution_date"]).reset_index(drop=True)
                fast_dates = pd.to_datetime(fast_trades["execution_date"]).reset_index(drop=True)
                pd.testing.assert_series_equal(fast_dates, strict_dates, check_names=False)


if __name__ == "__main__":
    unittest.main()
