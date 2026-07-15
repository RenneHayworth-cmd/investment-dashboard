import unittest

import numpy as np
import pandas as pd

from services.fund_rotation import (
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_OPEN,
    RotationInput,
    _calculate_momentum,
    _calculate_sharpe_ratio,
    _calculate_yearly_stats,
    _prepare_merged_data,
    build_standard_backtest_periods,
    normalize_rotation_dataframe,
    run_fund_rotation_backtest,
    run_ma20_timing_backtest,
)


class FundRotationTests(unittest.TestCase):
    def test_normalize_rotation_rejects_empty_input(self):
        with self.assertRaisesRegex(ValueError, "没有可回测的数据"):
            normalize_rotation_dataframe(pd.DataFrame(), fallback_name="测试")

    def test_rotation_rejects_position_count_above_fund_count(self):
        funds = [
            RotationInput("A", "A", pd.DataFrame()),
            RotationInput("B", "B", pd.DataFrame()),
        ]

        with self.assertRaisesRegex(ValueError, "持仓数量必须在 1 到 2 之间"):
            run_fund_rotation_backtest(funds, num_positions=3)

    def test_momentum_uses_full_previous_close_window(self):
        dates = pd.bdate_range("2026-01-01", periods=24)
        prices = pd.Series(range(100, 124), dtype=float)
        source = pd.DataFrame({"trade_date": dates, "open": prices, "close": prices})
        merged = _prepare_merged_data({"X": source})

        momentum = _calculate_momentum(merged, ["X"], lookback_period=22)

        self.assertTrue(pd.isna(momentum.iloc[22]["X"]))
        self.assertAlmostEqual(momentum.iloc[23]["X"], 122 / 100 - 1, places=12)

    def test_rebalance_waits_for_real_open_price(self):
        dates = pd.bdate_range("2025-12-29", "2026-01-30")
        stable = pd.DataFrame({"trade_date": dates, "open": 10.0, "close": 10.0})
        rising = pd.DataFrame({"trade_date": dates, "open": 10.0, "close": 10.0})
        rising.loc[rising["trade_date"] == pd.Timestamp("2026-01-16"), ["open", "close"]] = 20.0
        rising = rising[rising["trade_date"] != pd.Timestamp("2026-01-19")].copy()
        rising.loc[rising["trade_date"] >= pd.Timestamp("2026-01-20"), ["open", "close"]] = 30.0

        result = run_fund_rotation_backtest(
            [RotationInput("B", "B", stable), RotationInput("A", "A", rising)],
            frequency="week",
            lookback_period=2,
            num_positions=1,
            initial_capital=100000,
            execution_mode=EXECUTION_NEXT_OPEN,
        )

        a_trade = result.trades[
            (result.trades["标的代码"] == "A") & result.trades["卖出明细"].ne("")
        ].iloc[0]
        self.assertEqual(pd.Timestamp(a_trade["日期"]), pd.Timestamp("2026-01-20"))
        self.assertIn("买入价:30.0150", a_trade["买入明细"])
        self.assertLess(a_trade["本次交易盈亏金额"], 0)
        self.assertLess(a_trade["本次交易盈亏率(%)"], 0)
        self.assertEqual(result.summary["交易胜率(%)"], 0.0)

    def test_partial_rotation_keeps_common_holding(self):
        dates = pd.bdate_range("2026-01-01", periods=45)
        positions = np.arange(len(dates), dtype=float)
        prices = {
            "A": 100 + 1.2 * positions,
            "B": np.where(positions < 18, 100 + positions, 118 - 0.4 * (positions - 18)),
            "C": np.where(positions < 18, 100 + 0.1 * positions, 101.8 + 2.0 * (positions - 18)),
        }
        funds = [
            RotationInput(
                symbol,
                symbol,
                pd.DataFrame({"trade_date": dates, "open": values, "close": values}),
                trade_lot_size=1,
            )
            for symbol, values in prices.items()
        ]

        result = run_fund_rotation_backtest(
            funds,
            frequency="week",
            lookback_period=3,
            num_positions=2,
            initial_capital=100000,
            transaction_cost=0,
        )

        replacement = result.trades[
            result.trades["卖出明细"].ne("") & result.trades["买入明细"].ne("")
        ].iloc[0]
        self.assertEqual(set(replacement["标的代码"].split("; ")), {"A", "C"})
        self.assertNotIn("A 卖出", replacement["卖出明细"])
        self.assertNotIn("A 计划金额", replacement["买入明细"])
        self.assertIn("B 卖出", replacement["卖出明细"])
        self.assertIn("C 计划金额", replacement["买入明细"])

    def test_unselected_missing_open_does_not_delay_rebalance(self):
        dates = pd.bdate_range("2026-01-01", periods=25)
        positions = np.arange(len(dates), dtype=float)
        prices = {
            "A": 100 + positions * 2.0,
            "B": 100 + positions,
            "C": 100 - positions * 0.2,
        }
        funds = []
        for symbol, values in prices.items():
            data = pd.DataFrame({"trade_date": dates, "open": values, "close": values})
            if symbol == "C":
                data = data[data["trade_date"] != pd.Timestamp("2026-01-12")]
            funds.append(RotationInput(symbol, symbol, data, trade_lot_size=1))

        result = run_fund_rotation_backtest(
            funds,
            frequency="week",
            lookback_period=2,
            num_positions=1,
            execution_mode=EXECUTION_NEXT_OPEN,
        )

        self.assertEqual(result.start_date, pd.Timestamp("2026-01-12"))
        self.assertEqual(result.trades.iloc[0]["标的代码"], "A")

    def test_close_only_execution_does_not_apply_exchange_slippage(self):
        dates = pd.bdate_range("2026-01-01", periods=8)
        raw_a = pd.DataFrame({"日期": dates, "收盘价": 10.0, "symbol": "A", "name": "A"})
        raw_b = pd.DataFrame({"日期": dates, "收盘价": 9.0, "symbol": "B", "name": "B"})
        fund_a = normalize_rotation_dataframe(raw_a, fallback_name="A")
        fund_b = normalize_rotation_dataframe(raw_b, fallback_name="B")

        result = run_fund_rotation_backtest(
            [fund_a, fund_b],
            frequency="week",
            lookback_period=2,
            num_positions=1,
            transaction_cost=0,
            execution_mode=EXECUTION_NEXT_OPEN,
        )

        self.assertFalse(fund_a.apply_slippage)
        self.assertIn("买入价:10.0000", result.trades.iloc[0]["买入明细"])
        self.assertEqual(result.nav_data.iloc[0]["账户净值"], 100000.0)

    def test_after_close_mode_uses_current_close_signal_and_close_execution(self):
        dates = pd.bdate_range("2026-01-01", periods=8)
        a_close = pd.Series([100.0, 100.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0])
        b_close = pd.Series([100.0, 105.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0])
        funds = [
            RotationInput(
                "A",
                "A",
                pd.DataFrame({"trade_date": dates, "open": 90.0, "close": a_close}),
                trade_lot_size=1,
            ),
            RotationInput(
                "B",
                "B",
                pd.DataFrame({"trade_date": dates, "open": 90.0, "close": b_close}),
                trade_lot_size=1,
            ),
        ]

        result = run_fund_rotation_backtest(
            funds,
            frequency="week",
            lookback_period=2,
            num_positions=1,
            transaction_cost=0,
            execution_mode=EXECUTION_AFTER_CLOSE,
        )

        first_trade = result.trades.iloc[0]
        self.assertEqual(pd.Timestamp(first_trade["日期"]), pd.Timestamp("2026-01-05"))
        self.assertEqual(first_trade["标的代码"], "A")
        self.assertIn("买入价:120.0000", first_trade["买入明细"])
        self.assertEqual(result.summary["成交方式"], "盘后固定价（当日收盘信号/收盘成交）")

    def test_yearly_return_uses_previous_year_end(self):
        nav = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2025-12-31", "2026-01-02", "2026-12-31"]),
                "账户净值": [100.0, 110.0, 110.0],
            }
        )

        yearly = _calculate_yearly_stats(nav)

        self.assertEqual(yearly.loc[yearly["年份"] == 2026, "年收益率(%)"].iloc[0], 10.0)

    def test_sharpe_uses_average_daily_return(self):
        returns = pd.Series([0.01, 0.02, -0.01, 0.015])
        expected = returns.mean() / returns.std() * np.sqrt(252)

        self.assertAlmostEqual(_calculate_sharpe_ratio(returns), expected, places=12)

    def test_ma_timing_date_range_keeps_pre_window_history(self):
        dates = pd.bdate_range("2025-01-01", periods=80)
        prices = pd.Series(np.linspace(1.0, 1.8, len(dates)))
        fund = RotationInput(
            "X",
            "X",
            pd.DataFrame({"trade_date": dates, "open": prices, "close": prices}),
        )
        requested_start = dates[35]
        requested_end = dates[55]

        result = run_ma20_timing_backtest(
            fund,
            ma_period=20,
            start_date=requested_start,
            end_date=requested_end,
        )

        self.assertEqual(result.start_date, requested_start)
        self.assertEqual(result.end_date, requested_end)
        self.assertFalse(result.data["MA20"].isna().any())

    def test_ma_drawdown_and_risk_metrics_include_initial_capital(self):
        dates = pd.bdate_range("2026-01-01", periods=8)
        prices = pd.Series([8.0, 9.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        fund = RotationInput(
            "X",
            "X",
            pd.DataFrame({"trade_date": dates, "open": prices, "close": prices}),
        )

        result = run_ma20_timing_backtest(
            fund,
            ma_period=2,
            initial_capital=100,
            transaction_cost=0.10,
            lot_size=1,
            start_date=dates[2],
            end_date=dates[-1],
        )

        self.assertEqual(result.data.iloc[0]["账户净值"], 91.0)
        self.assertEqual(result.drawdown.iloc[0]["回撤(%)"], -9.0)
        self.assertEqual(result.summary["策略最大回撤(%)"], -9.0)
        self.assertLess(result.summary["夏普比率"], 0)

    def test_rotation_drawdown_includes_initial_capital(self):
        dates = pd.bdate_range("2026-01-01", periods=8)
        prices = pd.Series([10.0] * len(dates))
        funds = [
            RotationInput(
                symbol,
                symbol,
                pd.DataFrame({"trade_date": dates, "open": prices, "close": prices}),
                trade_lot_size=1,
            )
            for symbol in ("A", "B")
        ]

        result = run_fund_rotation_backtest(
            funds,
            frequency="week",
            lookback_period=2,
            num_positions=1,
            initial_capital=100,
            transaction_cost=0.10,
        )

        self.assertLess(result.nav_data.iloc[0]["账户净值"], 100)
        self.assertEqual(
            result.summary["策略最大回撤(%)"],
            result.summary["总收益率(%)"],
        )
        self.assertLess(result.summary["策略最大回撤(%)"], 0)

    def test_ma_buy_and_hold_return_uses_selected_interval_not_ma_period(self):
        dates = pd.bdate_range("2026-01-01", periods=30)
        prices = pd.Series(np.linspace(10.0, 20.0, len(dates)))
        fund = RotationInput(
            "X",
            "X",
            pd.DataFrame({"trade_date": dates, "open": prices, "close": prices}),
        )
        requested_start = dates[3]
        requested_end = dates[20]

        short_ma = run_ma20_timing_backtest(
            fund,
            ma_period=5,
            start_date=requested_start,
            end_date=requested_end,
        )
        long_ma = run_ma20_timing_backtest(
            fund,
            ma_period=10,
            start_date=requested_start,
            end_date=requested_end,
        )

        expected_return = (prices.iloc[20] / prices.iloc[3] - 1) * 100
        self.assertEqual(short_ma.start_date, requested_start)
        self.assertEqual(long_ma.start_date, requested_start)
        self.assertEqual(short_ma.summary["一直持有收益率(%)"], round(expected_return, 2))
        self.assertEqual(
            short_ma.summary["一直持有收益率(%)"],
            long_ma.summary["一直持有收益率(%)"],
        )
        self.assertEqual(long_ma.data.iloc[0]["信号"], "等待均线")

    def test_rotation_date_range_limits_all_outputs(self):
        dates = pd.bdate_range("2025-01-01", periods=100)
        a_prices = pd.Series(np.linspace(10.0, 15.0, len(dates)))
        b_prices = pd.Series(np.linspace(10.0, 12.0, len(dates)))
        funds = [
            RotationInput(
                "A",
                "A",
                pd.DataFrame({"trade_date": dates, "open": a_prices, "close": a_prices}),
            ),
            RotationInput(
                "B",
                "B",
                pd.DataFrame({"trade_date": dates, "open": b_prices, "close": b_prices}),
            ),
        ]
        requested_start = dates[40]
        requested_end = dates[65]

        result = run_fund_rotation_backtest(
            funds,
            frequency="week",
            lookback_period=22,
            start_date=requested_start,
            end_date=requested_end,
        )

        self.assertGreaterEqual(result.start_date, requested_start)
        self.assertLessEqual(result.end_date, requested_end)
        self.assertLessEqual(pd.to_datetime(result.nav_data["日期"]).max(), requested_end)
        self.assertLessEqual(pd.to_datetime(result.individual_nav_data["日期"]).max(), requested_end)

    def test_standard_periods_are_anchored_to_selected_end(self):
        periods = dict(build_standard_backtest_periods("2026-07-10"))

        self.assertEqual(periods["近一年"], pd.Timestamp("2025-07-10"))
        self.assertEqual(periods["今年来"], pd.Timestamp("2026-01-01"))
        self.assertEqual(periods["近三年"], pd.Timestamp("2023-07-10"))
        self.assertEqual(periods["近五年"], pd.Timestamp("2021-07-10"))
        self.assertIsNone(periods["成立来"])

    def test_ma_sell_records_profit_and_win_rate(self):
        dates = pd.bdate_range("2026-01-01", periods=4)
        prices = pd.Series([10.0, 11.0, 13.0, 12.0])
        fund = RotationInput(
            "X",
            "X",
            pd.DataFrame({"trade_date": dates, "open": prices, "close": prices}),
        )

        result = run_ma20_timing_backtest(
            fund,
            ma_period=2,
            threshold_pct=0,
            transaction_cost=0,
            lot_size=1,
        )

        sell = result.trades[result.trades["操作"] == "卖出"].iloc[0]
        self.assertGreater(sell["本次交易盈亏金额"], 0)
        self.assertGreater(sell["本次交易盈亏率(%)"], 0)
        self.assertEqual(result.summary["已平仓交易次数"], 1)
        self.assertEqual(result.summary["盈利交易次数"], 1)
        self.assertEqual(result.summary["交易胜率(%)"], 100.0)
        self.assertIn("策略最大回撤(%)", result.summary)
        self.assertEqual(result.summary["一直持有最大回撤(%)"], -7.69)


if __name__ == "__main__":
    unittest.main()
