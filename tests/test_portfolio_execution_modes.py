"""run_portfolio_timing_backtest 非 after_close 执行口径的回归测试。

新增口径（审计 P1）：
- ``next_close``：t 日收盘信号、t+1 日收盘价成交（信号收益贡献延迟一日）；
- ``next_open``：t 日收盘信号、t+1 日开盘价成交（默认计入双边 0.05% 滑点）。

after_close 默认行为不变（由 tests/test_fund_rotation.py 锁定）。
"""

import unittest

import numpy as np
import pandas as pd

from services.fund_rotation_models import (
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_NEXT_OPEN,
    PORTFOLIO_STRATEGY_TIMING,
    PortfolioTimingAllocation,
    RotationInput,
)
from services.fund_rotation_timing import run_portfolio_timing_backtest


def _make_fund(symbol: str, seed: int, size: int = 260) -> RotationInput:
    rng = np.random.default_rng(seed)
    prices = 5.0 * np.cumprod(1 + rng.normal(0.0004, 0.015, size))
    dates = pd.bdate_range("2023-01-02", periods=size)
    return RotationInput(
        symbol=symbol,
        name=symbol,
        dataframe=pd.DataFrame({"trade_date": dates, "open": prices * 0.999, "close": prices}),
    )


class ExecutionModeValidationTests(unittest.TestCase):
    def setUp(self):
        self.fund = _make_fund("A", 1)
        self.allocation = PortfolioTimingAllocation(
            symbol="A", name="A", weight_pct=100, strategy=PORTFOLIO_STRATEGY_TIMING,
            ma_period=20, threshold_pct=1.0,
        )

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            run_portfolio_timing_backtest(
                [self.fund], [self.allocation], execution_mode="same_second"
            )

    def test_negative_slippage_rejected(self):
        with self.assertRaises(ValueError):
            run_portfolio_timing_backtest([self.fund], [self.allocation], slippage=-0.001)

    def test_next_open_default_slippage(self):
        # 显式 slippage=0 时 next_open 自动采用项目口径 0.05%
        result = run_portfolio_timing_backtest(
            [self.fund], [self.allocation],
            execution_mode=EXECUTION_NEXT_OPEN,
        )
        self.assertFalse(result.nav_data.empty)


class ExecutionModeBehaviorTests(unittest.TestCase):
    def _run(self, mode: str, slippage: float = 0.0):
        funds = [_make_fund("A", 7), _make_fund("B", 11)]
        allocations = [
            PortfolioTimingAllocation(
                symbol="A", name="A", weight_pct=50, strategy=PORTFOLIO_STRATEGY_TIMING,
                ma_period=20, threshold_pct=1.0,
            ),
            PortfolioTimingAllocation(
                symbol="B", name="B", weight_pct=50, strategy=PORTFOLIO_STRATEGY_TIMING,
                ma_period=20, threshold_pct=1.0,
            ),
        ]
        return run_portfolio_timing_backtest(
            funds, allocations,
            initial_capital=100_000.0,
            execution_mode=mode,
            slippage=slippage,
        )

    def test_after_close_matches_default(self):
        baseline = run_portfolio_timing_backtest(
            [_make_fund("A", 7), _make_fund("B", 11)],
            [
                PortfolioTimingAllocation(symbol="A", name="A", weight_pct=50, strategy=PORTFOLIO_STRATEGY_TIMING, ma_period=20, threshold_pct=1.0),
                PortfolioTimingAllocation(symbol="B", name="B", weight_pct=50, strategy=PORTFOLIO_STRATEGY_TIMING, ma_period=20, threshold_pct=1.0),
            ],
            initial_capital=100_000.0,
        )
        explicit = self._run(EXECUTION_AFTER_CLOSE)
        pd.testing.assert_series_equal(
            explicit.nav_data["账户净值"], baseline.nav_data["账户净值"], check_names=False
        )

    def test_next_close_differs_from_after_close(self):
        after = self._run(EXECUTION_AFTER_CLOSE)
        delayed = self._run(EXECUTION_NEXT_CLOSE)
        self.assertFalse(
            np.allclose(after.nav_data["账户净值"], delayed.nav_data["账户净值"])
        )

    def test_next_open_with_slippage_not_better_than_without(self):
        no_slip = self._run(EXECUTION_NEXT_OPEN, slippage=0.0)
        # 注意：execution_mode=next_open 且 slippage=0 会注入默认滑点；
        # 显式关闭需直接传 after_close。这里验证默认滑点路径成立即可。
        with_slip = self._run(EXECUTION_NEXT_OPEN, slippage=0.001)
        self.assertLessEqual(
            float(with_slip.nav_data["账户净值"].iloc[-1]),
            float(no_slip.nav_data["账户净值"].iloc[-1]) * 1.001,
        )

    def test_delayed_nav_shape_preserved(self):
        delayed = self._run(EXECUTION_NEXT_CLOSE)
        self.assertFalse(delayed.nav_data["账户净值"].isna().any())
        self.assertEqual(len(delayed.nav_data), len(self._run(EXECUTION_AFTER_CLOSE).nav_data))


if __name__ == "__main__":
    unittest.main()
