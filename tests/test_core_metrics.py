"""统一指标模块 core.metrics 的单元测试。"""

import unittest

import numpy as np
import pandas as pd

from core.metrics import (
    annual_volatility,
    annualized_return,
    calmar_ratio,
    drawdown_series,
    interval_return_pct,
    longest_underwater_days,
    max_drawdown,
    seeded_returns,
    sharpe_ratio,
    total_return,
    trade_win_stats,
)


class AnnualizedReturnTests(unittest.TestCase):
    def test_one_year_double(self):
        self.assertAlmostEqual(annualized_return(1.0, 365), 1.0)

    def test_half_year_double_is_300pct(self):
        self.assertAlmostEqual(annualized_return(1.0, 182), (2.0) ** (365 / 182) - 1, places=6)

    def test_total_loss_is_minus_100pct(self):
        self.assertEqual(annualized_return(-1.0, 100), -1.0)
        self.assertEqual(annualized_return(-1.5, 100), -1.0)

    def test_zero_days_returns_zero(self):
        self.assertEqual(annualized_return(0.5, 0), 0.0)


class SharpeTests(unittest.TestCase):
    def test_constant_returns_zero_volatility(self):
        returns = pd.Series([0.001] * 100)
        self.assertEqual(sharpe_ratio(returns), 0.0)

    def test_zero_risk_free_matches_mean_over_std(self):
        rng = np.random.default_rng(7)
        returns = pd.Series(rng.normal(0.0005, 0.01, 500))
        expected = returns.mean() / returns.std(ddof=1) * np.sqrt(252)
        self.assertAlmostEqual(sharpe_ratio(returns), expected, places=10)

    def test_positive_risk_free_reduces_ratio(self):
        rng = np.random.default_rng(7)
        returns = pd.Series(rng.normal(0.0005, 0.01, 500))
        self.assertLess(
            sharpe_ratio(returns, risk_free_annual=0.015),
            sharpe_ratio(returns, risk_free_annual=0.0),
        )

    def test_short_series_returns_zero(self):
        self.assertEqual(sharpe_ratio(pd.Series([0.01])), 0.0)


class DrawdownTests(unittest.TestCase):
    def test_initial_capital_seeds_peak(self):
        # 首日即亏损必须计入回撤（播种初始资金语义）
        values = pd.Series([95_000.0, 100_000.0, 90_000.0])
        drawdown = drawdown_series(values, initial_capital=100_000.0)
        self.assertAlmostEqual(drawdown.iloc[0], -5.0)
        self.assertAlmostEqual(drawdown.iloc[1], 0.0)
        self.assertAlmostEqual(drawdown.min(), -10.0)
        self.assertAlmostEqual(max_drawdown(values, initial_capital=100_000.0), -10.0)

    def test_without_initial_capital_first_value_is_peak(self):
        values = pd.Series([100.0, 120.0, 60.0])
        self.assertAlmostEqual(max_drawdown(values), -50.0)


class UnderwaterTests(unittest.TestCase):
    def test_calendar_days_across_peak_recovery(self):
        dates = pd.to_datetime(
            ["2024-01-01", "2024-01-11", "2024-01-21", "2024-02-01"]
        )
        values = pd.Series([100.0, 90.0, 95.0, 100.0])
        # 种子峰值日=2023-12-31；01-11 水下11天、01-21 水下21天；02-01 回到峰值出水
        self.assertEqual(
            longest_underwater_days(values, dates=dates, initial_capital=100.0), 21
        )

    def test_equal_peak_stops_counting(self):
        dates = pd.to_datetime(["2024-01-01", "2024-01-10", "2024-01-20"])
        values = pd.Series([100.0, 100.0, 90.0])
        # 01-10 恰好等于峰值不重置峰值日；01-20 相对 01-01 水下 19 天
        self.assertEqual(longest_underwater_days(values, dates=dates), 19)

    def test_without_dates_counts_steps(self):
        values = pd.Series([100.0, 90.0, 95.0, 100.0])
        # 起点即峰值（步 0），90 在步 1、95 在步 2 仍水下，步 3 回到峰值出水
        self.assertEqual(longest_underwater_days(values, initial_capital=100.0), 3)

    def test_initial_capital_seed_counts_first_loss(self):
        values = pd.Series([95.0, 96.0])
        self.assertEqual(longest_underwater_days(values, initial_capital=100.0), 2)

    def test_nan_values_ignored(self):
        values = pd.Series([100.0, np.nan, 90.0, 100.0])
        dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-11", "2024-01-21"])
        self.assertEqual(longest_underwater_days(values, dates=dates), 10)


class MiscTests(unittest.TestCase):
    def test_seeded_returns_first_day_includes_cost(self):
        values = pd.Series([99_000.0, 100_000.0])
        returns = seeded_returns(values, 100_000.0)
        self.assertAlmostEqual(returns.iloc[0], -0.01)
        self.assertAlmostEqual(returns.iloc[1], 100_000.0 / 99_000.0 - 1)

    def test_annual_volatility_scales_with_sqrt(self):
        rng = np.random.default_rng(3)
        returns = pd.Series(rng.normal(0, 0.01, 1000))
        self.assertAlmostEqual(
            annual_volatility(returns), returns.std(ddof=1) * np.sqrt(252), places=10
        )

    def test_total_return(self):
        self.assertAlmostEqual(
            total_return(pd.Series([100.0, 110.0]), 100.0), 0.10, places=10
        )

    def test_calmar_ratio(self):
        # 年化收益与最大回撤都以百分比传入
        self.assertAlmostEqual(calmar_ratio(20.0, -10.0), 2.0)
        self.assertTrue(np.isnan(calmar_ratio(20.0, 0.0)))

    def test_trade_win_stats(self):
        closed, winning, win_rate = trade_win_stats([10.0, -5.0, 3.0, None])
        self.assertEqual(closed, 3)
        self.assertEqual(winning, 2)
        self.assertAlmostEqual(win_rate, 200 / 3)

    def test_interval_return_pct(self):
        self.assertAlmostEqual(interval_return_pct(100.0, 110.0), 10.0)
        self.assertTrue(np.isnan(interval_return_pct(0.0, 110.0)))


if __name__ == "__main__":
    unittest.main()
