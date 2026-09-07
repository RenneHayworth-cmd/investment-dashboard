"""统一均线择时框架 services.ma_timing_core 的单元测试。

重点验证：
1. 状态机与既有各实现的语义一致（滞回、MA 不足保持、初始空仓）；
2. 参考模拟器 after_close 口径与既有 run_ma20_timing_backtest 完全等价；
3. next_open / next_close / 滑点 / 停牌 的执行行为符合口径定义。
"""

import unittest

import numpy as np
import pandas as pd

from services.ma_timing_core import (
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_NEXT_OPEN,
    TimingAccountConfig,
    ma_threshold_series,
    ma_threshold_states,
    ma_threshold_transitions,
    simulate_timing_account,
)


def _make_prices(values: list[float], start: str = "2024-01-01") -> pd.Series:
    index = pd.date_range(start, periods=len(values), freq="B")
    return pd.Series(values, index=index, dtype=float)


class StateMachineTests(unittest.TestCase):
    def test_hysteresis_band_keeps_position(self):
        # MA20=100（首日横盘 30 日后窗口全为 100）：>101 买入、<99 卖出、带内维持
        close = _make_prices([100.0] * 30 + [101.2, 100.5, 100.5, 100.5, 100.5, 98.5])
        states = ma_threshold_states(close, 20, 1.0)
        self.assertEqual(int(states.iloc[30]), 1)  # 101.2 > 101 买入
        # 100.5 落在带内 → 滞回保持持仓（不因回落到带内而卖出）
        self.assertTrue((states.iloc[31:35] == 1).all())
        self.assertEqual(int(states.iloc[35]), 0)  # 98.5 < 99 卖出

    def test_nan_ma_keeps_previous_state(self):
        close = _make_prices([100.0] * 25)
        close.iloc[:5] = np.nan
        states = ma_threshold_states(close, 10, 0.0)
        self.assertTrue((states.iloc[:12] == 0).all())

    def test_states_match_legacy_signal_frame(self):
        # 与 services.fund_rotation_timing._build_timing_signal_frame 的逐行实现一致
        from services.fund_rotation_timing import _build_timing_signal_frame

        rng = np.random.default_rng(11)
        prices = np.cumprod(1 + rng.normal(0, 0.015, 260)) * 100
        dates = pd.date_range("2023-01-02", periods=260, freq="B")
        data = pd.DataFrame({"trade_date": dates, "close": prices})
        frame = _build_timing_signal_frame(
            data, pd.DatetimeIndex(dates), ma_period=20, threshold_pct=1.0
        )
        unified = ma_threshold_states(
            pd.Series(prices, index=dates), 20, 1.0
        )
        pd.testing.assert_series_equal(
            unified, frame["信号仓位"].astype(int).rename("state"), check_freq=False
        )

    def test_transitions_table(self):
        close = _make_prices([100.0] * 10 + [120.0, 80.0])
        states = ma_threshold_states(close, 10, 0.0)
        transitions = ma_threshold_transitions(states, close)
        self.assertEqual(len(transitions), 2)
        self.assertEqual(int(transitions.iloc[0]["目标状态"]), 1)
        self.assertAlmostEqual(float(transitions.iloc[0]["转换价"]), 120.0)
        self.assertEqual(int(transitions.iloc[1]["目标状态"]), 0)
        self.assertAlmostEqual(float(transitions.iloc[1]["转换价"]), 80.0)


class AfterCloseEquivalenceTests(unittest.TestCase):
    """参考模拟器 after_close + 零滑点 == 既有 run_ma20_timing_backtest。"""

    def _run_case(self, seed: int, size: int) -> None:
        from services.fund_rotation import run_ma20_timing_backtest
        from services.fund_rotation_models import RotationInput

        rng = np.random.default_rng(seed)
        prices = np.cumprod(1 + rng.normal(0.0002, 0.018, size)) * 5.0
        dates = pd.date_range("2022-01-03", periods=size, freq="B")
        fund = RotationInput(
            symbol="TEST",
            name="测试",
            dataframe=pd.DataFrame({"trade_date": dates, "close": prices}),
        )
        legacy = run_ma20_timing_backtest(
            fund=fund,
            ma_period=20,
            threshold_pct=1.0,
            initial_capital=100_000.0,
            transaction_cost=0.00006,
            lot_size=100,
        )
        states = ma_threshold_states(
            pd.Series(prices, index=dates), 20, 1.0
        )
        unified = simulate_timing_account(
            pd.Series(prices, index=dates),
            states,
            TimingAccountConfig(
                initial_capital=100_000.0,
                transaction_cost=0.00006,
                lot_size=100,
                execution_mode=EXECUTION_AFTER_CLOSE,
            ),
        )
        pd.testing.assert_series_equal(
            unified.nav.round(2),
            legacy.data.set_index("日期")["账户净值"],
            check_names=False,
            check_freq=False,
        )
        self.assertEqual(len(unified.trades), len(legacy.trades))
        np.testing.assert_allclose(
            np.round(unified.realized_trade_pnls, 2),
            legacy.trades["本次交易盈亏金额"].dropna().to_numpy(),
            rtol=1e-6,
        )

    def test_equivalence_multiple_paths(self):
        for seed in (1, 7, 42):
            self._run_case(seed, 500)

    def test_metrics_summary_consistent(self):
        rng = np.random.default_rng(5)
        prices = np.cumprod(1 + rng.normal(0.0003, 0.02, 400)) * 3.0
        dates = pd.date_range("2023-06-01", periods=400, freq="B")
        states = ma_threshold_states(pd.Series(prices, index=dates), 20, 1.0)
        result = simulate_timing_account(
            pd.Series(prices, index=dates),
            states,
            TimingAccountConfig(initial_capital=50_000.0),
        )
        metrics = result.metrics()
        self.assertAlmostEqual(
            metrics["total_return_pct"],
            (result.nav.iloc[-1] / 50_000.0 - 1) * 100,
            places=2,
        )
        self.assertLessEqual(metrics["max_drawdown_pct"], 0.0)
        self.assertGreaterEqual(metrics["longest_underwater_days"], 0)


class ExecutionModeTests(unittest.TestCase):
    def _flat_data(self) -> tuple[pd.Series, pd.Series]:
        # 10 日横盘(均线形成)后：涨穿买入线 → 三日后跌破卖出线
        prices = [100.0] * 10 + [120.0] * 3 + [70.0] * 3
        index = pd.date_range("2024-01-01", periods=len(prices), freq="B")
        close = pd.Series(prices, index=index, dtype=float)
        states = ma_threshold_states(close, 10, 0.0)
        return close, states

    def test_next_open_executes_at_next_open(self):
        close, states = self._flat_data()
        open_ = close.copy()
        open_.iloc[11] = 115.0  # 信号在第 10 行收盘产生，次日（第 11 行）开盘 115 成交
        result = simulate_timing_account(
            close,
            states,
            TimingAccountConfig(execution_mode=EXECUTION_NEXT_OPEN, transaction_cost=0.0, lot_size=1),
            open_=open_,
        )
        buy_rows = result.trades[result.trades["操作"] == "买入"]
        self.assertEqual(len(buy_rows), 1)
        self.assertEqual(buy_rows.iloc[0]["日期"], close.index[11])
        self.assertAlmostEqual(buy_rows.iloc[0]["成交价"], 115.0)

    def test_next_close_executes_at_next_close(self):
        close, states = self._flat_data()
        result = simulate_timing_account(
            close,
            states,
            TimingAccountConfig(execution_mode=EXECUTION_NEXT_CLOSE, transaction_cost=0.0, lot_size=1),
        )
        buy_rows = result.trades[result.trades["操作"] == "买入"]
        self.assertEqual(len(buy_rows), 1)
        self.assertEqual(buy_rows.iloc[0]["日期"], close.index[11])
        self.assertAlmostEqual(buy_rows.iloc[0]["成交价"], 120.0)

    def test_slippage_applies_on_both_sides(self):
        close, states = self._flat_data()
        base = simulate_timing_account(
            close, states, TimingAccountConfig(transaction_cost=0.0, lot_size=1)
        )
        slipped = simulate_timing_account(
            close,
            states,
            TimingAccountConfig(transaction_cost=0.0, lot_size=1, slippage=0.001),
        )
        self.assertLess(slipped.nav.iloc[-1], base.nav.iloc[-1])

    def test_suspended_day_does_not_execute_or_revalue(self):
        close, states = self._flat_data()
        close_nan = close.copy()
        close_nan.iloc[11] = np.nan  # 买入应成交的次日停牌
        open_nan = close_nan.copy()
        result = simulate_timing_account(
            close_nan,
            states,
            TimingAccountConfig(execution_mode=EXECUTION_NEXT_OPEN, transaction_cost=0.0, lot_size=1),
            open_=open_nan,
        )
        buy_rows = result.trades[result.trades["操作"] == "买入"]
        self.assertEqual(len(buy_rows), 1)
        # 停牌次日才成交
        self.assertEqual(buy_rows.iloc[0]["日期"], close.index[12])
        # 停牌日净值沿用停牌前估值
        self.assertAlmostEqual(result.nav.iloc[11], result.nav.iloc[10], places=6)

    def test_lot_rounding(self):
        close = _make_prices([100.0] * 10 + [120.0] * 5)
        states = ma_threshold_states(close, 10, 0.0)
        result = simulate_timing_account(
            close,
            states,
            TimingAccountConfig(initial_capital=10_000.0, transaction_cost=0.0, lot_size=100),
        )
        # 10000 / 120 = 83.3 股 → 整手 100 取整后为 0 股，不应成交
        self.assertTrue(result.trades.empty)

    def test_zero_lot_uses_all_cash_when_affordable(self):
        close = _make_prices([100.0] * 10 + [110.0] * 5)
        states = ma_threshold_states(close, 10, 0.0)
        result = simulate_timing_account(
            close,
            states,
            TimingAccountConfig(initial_capital=10_000.0, transaction_cost=0.0, lot_size=1),
        )
        buy_rows = result.trades[result.trades["操作"] == "买入"]
        self.assertAlmostEqual(buy_rows.iloc[0]["份额"], 10000.0 / 110.0, places=2)


class ThresholdSeriesTests(unittest.TestCase):
    def test_lines_sanity(self):
        close = _make_prices([100.0] * 30)
        frame = ma_threshold_series(close, 20, 1.0)
        self.assertTrue(frame["ma"].iloc[:19].isna().all())
        self.assertAlmostEqual(frame["ma"].iloc[19], 100.0)
        self.assertAlmostEqual(frame["buy_line"].iloc[19], 101.0)
        self.assertAlmostEqual(frame["sell_line"].iloc[19], 99.0)


if __name__ == "__main__":
    unittest.main()
