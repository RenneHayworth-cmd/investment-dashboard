import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from services.portfolio_audit import (
    AuditAllocation,
    AuditSettings,
    normalize_audit_market_data,
    run_portfolio_audit,
)
from services.portfolio_audit_analysis import (
    custom_parameter_attribution,
    full_history_validation,
    missed_order_simulation_analysis,
)
from services.portfolio_audit_tracking import (
    NO_OOS_MESSAGE,
    create_frozen_strategy_snapshot,
    frozen_allocations,
    load_frozen_strategy_snapshot,
    run_out_of_sample_tracking,
)


def market_frame(
    raw_close: list[float],
    signal_close: list[float] | None = None,
    raw_open: list[float] | None = None,
) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=len(raw_close))
    signal_close = signal_close or raw_close
    raw_open = raw_open or raw_close
    return pd.DataFrame(
        {
            "trade_date": dates,
            "raw_open": raw_open,
            "raw_close": raw_close,
            "raw_high": np.maximum(raw_open, raw_close),
            "raw_low": np.minimum(raw_open, raw_close),
            "signal_open": signal_close,
            "signal_close": signal_close,
            "signal_high": signal_close,
            "signal_low": signal_close,
            "adjustment_factor": np.array(signal_close) / np.array(raw_close),
            "dividend_per_share": 0.0,
            "corporate_action_status": "无调整事件",
        }
    )


class PortfolioAuditTests(unittest.TestCase):
    def test_normalization_keeps_raw_adjusted_and_official_dividend_separate(self):
        dates = pd.bdate_range("2026-01-01", periods=3)
        raw = pd.DataFrame({"date": dates, "open": [10, 9, 10], "close": [10, 9, 10]})
        qfq = pd.DataFrame({"date": dates, "open": [9, 9, 10], "close": [9, 9, 10]})

        dividends = pd.DataFrame({"除息日期": [dates[1]], "分红": [1.0]})
        data, actions = normalize_audit_market_data(raw, qfq, dividends)

        self.assertEqual(data.loc[0, "raw_close"], 10)
        self.assertEqual(data.loc[0, "signal_close"], 9)
        self.assertAlmostEqual(data.loc[1, "dividend_per_share"], 1.0)
        self.assertEqual(actions.iloc[0]["corporate_action_status"], "官方现金分红")

    def test_rounding_noise_is_not_inferred_as_dividend(self):
        dates = pd.bdate_range("2026-01-01", periods=3)
        raw = pd.DataFrame({"date": dates, "open": [1.0, 1.003, 1.006], "close": [1.0, 1.003, 1.006]})
        qfq = pd.DataFrame({"date": dates, "open": [0.5, 0.502, 0.503], "close": [0.5, 0.502, 0.503]})

        data, actions = normalize_audit_market_data(raw, qfq)

        self.assertEqual(data["dividend_per_share"].sum(), 0)
        self.assertTrue(actions.empty)

    def test_official_share_split_is_recorded_separately(self):
        dates = pd.bdate_range("2026-01-01", periods=3)
        raw = pd.DataFrame({"date": dates, "open": [10, 10, 5], "close": [10, 10, 5]})
        qfq = pd.DataFrame({"date": dates, "open": [5, 5, 5], "close": [5, 5, 5]})
        splits = pd.DataFrame(
            {
                "effective_date": [dates[2]],
                "ratio": [2.0],
                "rounding": ["floor"],
                "source": ["测试公告"],
            }
        )

        data, actions = normalize_audit_market_data(raw, qfq, share_split_df=splits)

        self.assertEqual(data.loc[2, "share_split_ratio"], 2.0)
        self.assertEqual(data.loc[2, "corporate_action_status"], "官方份额折算")
        self.assertEqual(actions.iloc[0]["share_split_source"], "测试公告")

    def test_share_split_adjusts_shares_without_creating_false_loss(self):
        frame = market_frame(
            raw_close=[10, 10, 5, 5],
            signal_close=[5, 5, 5, 5],
        )
        frame["share_split_ratio"] = [1.0, 1.0, 2.0, 1.0]
        frame["share_split_rounding"] = ["", "", "floor", ""]
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "hold")],
            AuditSettings(initial_capital=1000, lot_size=1, commission_rate=0),
        )

        self.assertEqual(result.daily.iloc[-1]["A_shares"], 200)
        self.assertEqual(result.daily.iloc[-1]["portfolio_value"], 1000)

    def test_signal_uses_adjusted_close_but_execution_uses_raw_close(self):
        frame = market_frame(
            raw_close=[10, 10, 20, 20],
            signal_close=[1, 1, 2, 2],
        )
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "timing", 2, 0)],
            AuditSettings(initial_capital=1000, lot_size=1, commission_rate=0),
        )

        buy = result.trades[result.trades["action"] == "buy"].iloc[0]
        self.assertEqual(buy["raw_reference_price"], 20)
        self.assertEqual(buy["execution_price"], 20)
        self.assertEqual(buy["shares"], 50)

    def test_next_open_uses_next_trading_day_open(self):
        frame = market_frame(
            raw_close=[10, 10, 12, 12],
            signal_close=[10, 10, 12, 12],
            raw_open=[10, 10, 12, 13],
        )
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "timing", 2, 0)],
            AuditSettings(
                initial_capital=1000,
                lot_size=1,
                commission_rate=0,
                execution_mode="next_open",
            ),
        )

        buy = result.trades.iloc[0]
        self.assertEqual(buy["signal_date"], frame.loc[2, "trade_date"])
        self.assertEqual(buy["execution_date"], frame.loc[3, "trade_date"])
        self.assertEqual(buy["execution_price"], 13)

    def test_failed_after_close_order_moves_to_next_open(self):
        frame = market_frame(
            raw_close=[10, 10, 12, 12],
            signal_close=[10, 10, 12, 12],
            raw_open=[10, 10, 12, 13],
        )
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "timing", 2, 0)],
            AuditSettings(
                initial_capital=1000,
                lot_size=1,
                commission_rate=0,
                after_hours_fill_rate=0,
            ),
        )

        self.assertEqual(result.trades.iloc[0]["execution_date"], frame.loc[3, "trade_date"])
        self.assertEqual(result.trades.iloc[0]["execution_price"], 13)
        self.assertIn("顺延", result.trades.iloc[0]["execution_status"])

    def test_dividend_is_credited_once_to_cash(self):
        frame = market_frame([10, 10, 10])
        frame.loc[1, "dividend_per_share"] = 1.0
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "hold")],
            AuditSettings(initial_capital=1000, lot_size=1, commission_rate=0),
        )

        self.assertEqual(result.summary["dividend_income"], 100)
        self.assertEqual(result.daily.iloc[-1]["portfolio_value"], 1100)

    def test_half_timing_preserves_permanent_half(self):
        frame = market_frame([10, 9, 8, 7])
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "half_timing", 2, 0)],
            AuditSettings(initial_capital=1000, lot_size=1, commission_rate=0),
        )

        self.assertEqual(result.daily.iloc[-1]["A_shares"], 50)
        self.assertEqual(result.daily.iloc[-1]["A_signal"], "空仓")

    def test_contribution_realized_pnl_ignores_opening_trade_nan(self):
        frame = market_frame([10, 11, 9, 12, 8])
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "timing", 2, 0)],
            AuditSettings(initial_capital=1000, lot_size=1, commission_rate=0),
        )

        self.assertTrue(np.isfinite(result.contribution.iloc[0]["realized_pnl"]))

    def test_residual_weight_remains_cash_and_earns_configured_rate(self):
        frame = market_frame([10, 10, 10])
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 50, "hold")],
            AuditSettings(initial_capital=1000, lot_size=1, commission_rate=0, cash_annual_rate=0.02),
        )

        self.assertGreater(result.daily.iloc[-1]["portfolio_value"], 1000)
        self.assertAlmostEqual(result.daily.iloc[-1]["etf_weight_pct"], 50, places=1)

    def test_custom_parameter_attribution_replaces_only_one_symbol(self):
        frame = market_frame([10, 10, 12, 12, 9, 9, 13, 13] * 4)
        allocations = [AuditAllocation("A", "A", 100, "timing", 20, 1)]
        settings = AuditSettings(initial_capital=1000, lot_size=1, commission_rate=0)
        baseline = run_portfolio_audit({"A": frame}, allocations, settings)

        attribution = custom_parameter_attribution(
            {"A": frame}, allocations, settings, baseline
        )

        self.assertEqual(attribution.iloc[0]["symbol"], "A")
        self.assertAlmostEqual(
            attribution.iloc[0]["common_custom_parameter_annual_contribution_pct"],
            0.0,
        )

    def test_fixed_seed_makes_fill_simulation_reproducible(self):
        frame = market_frame([10, 11, 9, 12, 8, 13, 7, 14])
        settings = AuditSettings(
            initial_capital=1000,
            lot_size=1,
            commission_rate=0,
            after_hours_fill_rate=0.5,
            random_seed=7,
        )
        allocation = [AuditAllocation("A", "A", 100, "timing", 2, 0)]

        first = run_portfolio_audit({"A": frame}, allocation, settings)
        second = run_portfolio_audit({"A": frame}, allocation, settings)

        pd.testing.assert_frame_equal(first.trades, second.trades)


    def test_missed_buy_is_retried_on_next_trading_day(self):
        frame = market_frame([10, 10, 12, 12, 12])
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "timing", 2, 0)],
            AuditSettings(
                initial_capital=1000,
                lot_size=1,
                commission_rate=0,
                random_seed=7,
                missed_signal_rate=0.5,
                missed_order_side="buy",
            ),
        )

        missed_row = result.daily[result.daily["A_order_missed"]].iloc[0]
        filled = result.trades.iloc[0]
        self.assertEqual(missed_row["trade_date"], frame.loc[2, "trade_date"])
        self.assertEqual(filled["execution_date"], frame.loc[3, "trade_date"])
        self.assertTrue(filled["order_retried"])
        self.assertEqual(filled["execution_delay_days"], 1)
        self.assertEqual(result.summary["missed_buy_count"], 1)
        self.assertGreaterEqual(result.summary["order_retried_count"], 1)

    def test_target_change_cancels_old_pending_direction(self):
        frame = market_frame([10, 10, 12, 8])
        result = run_portfolio_audit(
            {"A": frame},
            [AuditAllocation("A", "A", 100, "timing", 2, 0)],
            AuditSettings(
                initial_capital=1000,
                lot_size=1,
                commission_rate=0,
                execution_mode="next_open",
            ),
        )

        self.assertTrue(result.trades.empty)
        self.assertEqual(result.daily.iloc[-1]["A_target_position"], 0)
        self.assertIn(
            "取消旧方向",
            result.daily.iloc[-1]["A_execution_status"],
        )

    def test_buy_and_sell_misses_are_independent(self):
        frame = market_frame([10, 10, 12, 12, 8, 8])
        allocation = [AuditAllocation("A", "A", 100, "timing", 2, 0)]
        buy_only = run_portfolio_audit(
            {"A": frame},
            allocation,
            AuditSettings(
                initial_capital=1000,
                lot_size=1,
                commission_rate=0,
                random_seed=7,
                missed_signal_rate=0.5,
                missed_order_side="buy",
            ),
        )
        sell_only = run_portfolio_audit(
            {"A": frame},
            allocation,
            AuditSettings(
                initial_capital=1000,
                lot_size=1,
                commission_rate=0,
                missed_signal_rate=1.0,
                missed_order_side="sell",
            ),
        )

        self.assertGreater(buy_only.summary["missed_buy_count"], 0)
        self.assertEqual(buy_only.summary["missed_sell_count"], 0)
        self.assertIn("sell", set(buy_only.trades["action"]))
        self.assertEqual(sell_only.summary["missed_buy_count"], 0)
        self.assertGreater(sell_only.summary["missed_sell_count"], 0)
        self.assertEqual(list(sell_only.trades["action"]), ["buy"])

    def test_multi_seed_missed_order_results_are_reproducible(self):
        frame = market_frame(
            raw_close=[10, 10, 12, 12, 6, 6],
            signal_close=[5, 5, 6, 6, 6, 6],
        )
        frame["share_split_ratio"] = [1.0, 1.0, 1.0, 1.0, 2.0, 1.0]
        frame["share_split_rounding"] = ["", "", "", "", "floor", ""]
        allocation = [AuditAllocation("A", "A", 100, "timing", 2, 0)]
        settings = AuditSettings(
            initial_capital=1000,
            lot_size=1,
            commission_rate=0,
            random_seed=12,
        )
        baseline = run_portfolio_audit({"A": frame}, allocation, settings)

        first, first_distribution, first_meta = missed_order_simulation_analysis(
            {"A": frame},
            allocation,
            settings,
            baseline,
            hold_annual_return_pct=0,
            unified_annual_return_pct=0,
            miss_rates=(0.0, 0.10),
            simulations=1000,
        )
        second, second_distribution, second_meta = missed_order_simulation_analysis(
            {"A": frame},
            allocation,
            settings,
            baseline,
            hold_annual_return_pct=0,
            unified_annual_return_pct=0,
            miss_rates=(0.0, 0.10),
            simulations=1000,
        )

        pd.testing.assert_frame_equal(first, second)
        pd.testing.assert_frame_equal(first_distribution, second_distribution)
        self.assertEqual(first_meta, second_meta)
        self.assertAlmostEqual(
            first_meta["corrected_no_miss_final_value"],
            baseline.summary["final_value"],
            places=8,
        )

    def test_full_history_does_not_use_pre_inception_dates(self):
        frame = market_frame(list(np.linspace(10, 14, 40)))
        frame["trade_date"] = pd.bdate_range("2020-03-02", periods=len(frame))
        allocation = [AuditAllocation("A", "A", 100, "timing", 10, 1)]
        comparison, neighborhood, periods = full_history_validation(
            {"A": frame},
            allocation,
            AuditSettings(
                initial_capital=1000,
                lot_size=1,
                commission_rate=0,
                start_date="2018-01-01",
            ),
            research_end_date="2026-07-27",
        )

        self.assertEqual(
            pd.Timestamp(comparison.iloc[0]["available_start_date"]),
            frame["trade_date"].min(),
        )
        self.assertEqual(comparison.iloc[0]["trading_days"], len(frame))
        pre_inception = periods[
            (periods["symbol"] == "A") & (periods["period"] == "2018-2019")
        ].iloc[0]
        self.assertEqual(pre_inception["status"], "未上市或数据不足")
        self.assertEqual(pre_inception["trading_days"], 0)
        self.assertFalse(neighborhood.empty)

    def test_frozen_parameters_cannot_be_overwritten(self):
        allocation = [AuditAllocation("A", "A", 100, "timing", 20, 1)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frozen.json"
            create_frozen_strategy_snapshot(path, allocation)
            original = path.read_bytes()

            with self.assertRaises(FileExistsError):
                create_frozen_strategy_snapshot(
                    path,
                    [replace(allocation[0], ma_period=25)],
                )

            self.assertEqual(path.read_bytes(), original)
            snapshot = load_frozen_strategy_snapshot(path)
            self.assertEqual(frozen_allocations(snapshot)[0].ma_period, 20)

    def test_out_of_sample_uses_frozen_parameters_and_excludes_research_dates(self):
        frame = market_frame([10, 10, 11, 12])
        frame["trade_date"] = pd.to_datetime(
            ["2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29"]
        )
        allocation = [AuditAllocation("A", "A", 100, "hold", 20, 1)]
        settings = AuditSettings(
            initial_capital=1000,
            lot_size=1,
            commission_rate=0,
            start_date="2026-07-24",
            end_date="2026-07-27",
        )
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            frozen_path = directory_path / "frozen.json"
            create_frozen_strategy_snapshot(frozen_path, allocation)
            daily, summary, _ = run_out_of_sample_tracking(
                {"A": frame},
                frozen_path,
                settings,
                directory_path,
            )

            self.assertTrue((pd.to_datetime(daily["trade_date"]) >= pd.Timestamp("2026-07-28")).all())
            self.assertEqual(len(daily), 2)
            portfolio = summary[summary["record_type"] == "portfolio"].iloc[0]
            self.assertAlmostEqual(
                float(portfolio["final_value"]),
                float(daily.iloc[-1]["strategy_value"]),
                places=8,
            )
            persisted = pd.read_csv(directory_path / "out_of_sample_daily.csv")
            self.assertEqual(len(persisted), len(daily))

    def test_no_true_out_of_sample_days_produces_empty_framework(self):
        frame = market_frame([10, 10])
        frame["trade_date"] = pd.to_datetime(["2026-07-24", "2026-07-27"])
        allocation = [AuditAllocation("A", "A", 100, "hold")]
        settings = AuditSettings(
            initial_capital=1000,
            lot_size=1,
            commission_rate=0,
            start_date="2026-07-24",
            end_date="2026-07-27",
        )
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            frozen_path = directory_path / "frozen.json"
            create_frozen_strategy_snapshot(frozen_path, allocation)
            daily, summary, report = run_out_of_sample_tracking(
                {"A": frame},
                frozen_path,
                settings,
                directory_path,
            )

            self.assertTrue(daily.empty)
            self.assertEqual(summary.iloc[0]["status"], NO_OOS_MESSAGE)
            self.assertIn(NO_OOS_MESSAGE, report)



if __name__ == "__main__":
    unittest.main()
