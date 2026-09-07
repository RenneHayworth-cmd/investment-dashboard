"""回归测试：time_split / walk_forward 的连续路径口径修正。

修复背景（审计 A4）：旧实现测试段 `replace(settings, start_date=test_start)`
从空仓重新起步，且训练年化来自快速打分器（qfq、无整手/分红）而测试年化来自
严格引擎，`performance_decay_pct` 被口径差污染。修正后：

1. 测试段指标来自"从训练段起点连续起跑"的严格引擎，按测试窗切出（状态继承）；
2. 训练段年化与测试段年化均按窗前锚点口径由 `_test_window_metrics` 统一计算；
3. 策略与一直持有基准共用同一锚点，excess 无再入场效应。
"""

import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pandas as pd

from services.portfolio_audit_analysis_core import (
    _test_window_metrics,
    time_split_analysis,
    walk_forward_analysis,
)
from services.portfolio_audit_models import AuditAllocation, AuditRunResult, AuditSettings


def _make_market(rows: int = 260, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=rows)
    close = 10 * np.cumprod(1 + rng.normal(0.0004, 0.012, rows))
    open_ = close * (1 + rng.normal(0, 0.002, rows))
    return pd.DataFrame(
        {
            "trade_date": dates,
            "signal_close": close,
            "raw_close": close,
            "signal_open": open_,
            "raw_open": open_,
            "signal_high": np.maximum(open_, close) * 1.005,
            "signal_low": np.minimum(open_, close) * 0.995,
            "raw_high": np.maximum(open_, close) * 1.005,
            "raw_low": np.minimum(open_, close) * 0.995,
            "dividend_per_share": 0.0,
            "share_split_ratio": 1.0,
        }
    )


class TestWindowMetricsTests(unittest.TestCase):
    def test_seed_anchor_makes_first_window_day_counted(self):
        daily = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
                "portfolio_value": [100.0, 110.0, 99.0],
            }
        )
        metrics = _test_window_metrics(
            AuditRunResult(summary={}, daily=daily, trades=pd.DataFrame(), contribution=pd.DataFrame()),
            pd.Timestamp("2024-01-02"),
            pd.Timestamp("2024-01-03"),
        )
        # 锚点 = 窗前最后一天 100；窗内收益 = 99/100 - 1 = -1%
        self.assertAlmostEqual(metrics["total_return_pct"], -1.0)
        # 首日即破锚点 → 回撤 -10%
        self.assertAlmostEqual(metrics["max_drawdown_pct"], -10.0)

    def test_empty_window_is_nan(self):
        daily = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2024-01-01"]),
                "portfolio_value": [100.0],
            }
        )
        metrics = _test_window_metrics(
            AuditRunResult(summary={}, daily=daily, trades=pd.DataFrame(), contribution=pd.DataFrame()),
            pd.Timestamp("2025-01-01"),
            pd.Timestamp("2025-02-01"),
        )
        self.assertEqual(metrics["trading_days"], 0)
        self.assertTrue(np.isnan(metrics["annual_return_pct"]))


class TimeSplitContinuousPathTests(unittest.TestCase):
    def setUp(self):
        self.market = _make_market()
        self.item = AuditAllocation(symbol="X", name="X", weight_pct=100, strategy="timing", ma_period=10, threshold_pct=0.5)
        self.settings = AuditSettings(initial_capital=10000.0, start_date=self.market["trade_date"].iloc[0])
        self.grid = pd.DataFrame(
            [
                {"symbol": "X", "ma_period": ma, "threshold_pct": thr}
                for ma in (5, 10, 20)
                for thr in (0.5, 1.0)
            ]
        )

    def test_rows_have_expected_shape(self):
        frame = time_split_analysis(
            {"X": self.market}, [self.item], self.settings, self.grid, [0.7]
        )
        self.assertEqual(len(frame), 1)
        row = frame.iloc[0]
        self.assertEqual(row["test_type"], "time_split")
        self.assertEqual(int(row["selected_ma_period"]), self.item.ma_period or row["selected_ma_period"])
        self.assertIn("train_annual_return_pct", frame.columns)
        self.assertIn("performance_decay_pct", frame.columns)
        self.assertGreater(row["test_trading_days"], 0)

    def test_hold_skipped(self):
        hold_item = replace(self.item, strategy="hold")
        frame = time_split_analysis({"X": self.market}, [hold_item], self.settings, self.grid, [0.7])
        self.assertTrue(frame.empty)

    def test_engine_runs_from_path_start_not_test_start(self):
        """测试段起跑点必须是训练段起点（连续路径），而非 test_start。"""
        captured = {}

        def fake_runner(market_data, allocations, settings, **kwargs):
            captured["start_date"] = settings.start_date
            return AuditRunResult(
                summary={},
                daily=pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(self.market["trade_date"]),
                        "portfolio_value": np.linspace(10000, 12000, len(self.market)),
                    }
                ),
                trades=pd.DataFrame(),
                contribution=pd.DataFrame(),
            )

        with patch(
            "services.portfolio_audit_analysis_core._run_portfolio_audit",
            side_effect=fake_runner,
        ):
            time_split_analysis({"X": self.market}, [self.item], self.settings, self.grid, [0.7])
        # 策略与基准共 2 次引擎调用，全部从数据首日起跑
        self.assertEqual(pd.Timestamp(captured["start_date"]), pd.Timestamp(self.market["trade_date"].iloc[0]))


class WalkForwardContinuousPathTests(unittest.TestCase):
    def test_walk_forward_rows_and_continuous_path(self):
        market = _make_market(rows=320)
        item = AuditAllocation(symbol="X", name="X", weight_pct=100, strategy="timing", ma_period=10, threshold_pct=0.5)
        settings = AuditSettings(initial_capital=10000.0)
        captured_starts = []

        def fake_runner(market_data, allocations, settings, **kwargs):
            captured_starts.append(settings.start_date)
            return AuditRunResult(
                summary={},
                daily=pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(market["trade_date"]),
                        "portfolio_value": np.linspace(10000, 11000, len(market)),
                    }
                ),
                trades=pd.DataFrame(),
                contribution=pd.DataFrame(),
            )

        with patch(
            "services.portfolio_audit_analysis_core._run_portfolio_audit",
            side_effect=fake_runner,
        ):
            frame = walk_forward_analysis(
                {"X": market}, [item], settings, ma_periods=(5, 10), threshold_pcts=(0.5, 1.0),
                train_days=180, test_days=60,
            )
        self.assertFalse(frame.empty)
        self.assertTrue((frame["test_type"] == "walk_forward").all())
        # 每折 2 次调用（策略+基准），起跑点=训练段起点而非测试段起点
        self.assertEqual(len(captured_starts), 2 * len(frame))
        first_fold_train_start = pd.Timestamp(market["trade_date"].iloc[0])
        self.assertEqual(pd.Timestamp(captured_starts[0]), first_fold_train_start)

    def test_no_reentry_effect_in_excess(self):
        """同折内策略与基准必须共用同一锚点序列（不各自空仓重启）。"""
        market = _make_market(rows=300, seed=8)
        item = AuditAllocation(symbol="X", name="X", weight_pct=100, strategy="timing", ma_period=10, threshold_pct=0.5)
        settings = AuditSettings(initial_capital=10000.0)

        def fake_runner(market_data, allocations, settings, **kwargs):
            nav = np.linspace(10000, 12000, len(market))  # 单调上升
            return AuditRunResult(
                summary={},
                daily=pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(market["trade_date"]),
                        "portfolio_value": nav,
                    }
                ),
                trades=pd.DataFrame(),
                contribution=pd.DataFrame(),
            )

        with patch(
            "services.portfolio_audit_analysis_core._run_portfolio_audit",
            side_effect=fake_runner,
        ):
            frame = walk_forward_analysis(
                {"X": market}, [item], settings, ma_periods=(5,), threshold_pcts=(0.5,),
                train_days=180, test_days=60,
            )
        row = frame.iloc[0]
        # 两者路径完全一致 → 超额应为 0（旧口径下因各自空仓重启再入场，不会稳定为 0）
        self.assertAlmostEqual(float(row["test_excess_vs_hold_pct"]), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
