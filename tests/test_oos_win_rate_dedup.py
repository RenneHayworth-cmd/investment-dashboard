"""回归测试：动态阈值研究"独立 OOS 胜率"的窗口去重修正。

修复背景（审计 A5）：70/30 holdout 的测试窗与最后一折 Walk-Forward 的测试窗
在序列末尾重叠，旧实现把两者同时计入 `independent_wins`，同一段行情被统计两次。
修正后：holdout 恒计入，与 holdout 测试窗重叠的折被丢弃并记录
`oos_dedup_dropped_folds`。

通过构造最小 result dict 直接驱动 build_decision_summary 的相关代码路径。
"""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import run_dynamic_threshold_research as R  # noqa: E402


def _fold_frame(fold: int, test_start: str, test_end: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "fold": fold,
                "evaluation_window": "full",
                "model_family": model,
                "candidate_id": f"f{fold}_{model}",
                "test_start": pd.Timestamp(test_start),
                "test_end": pd.Timestamp(test_end),
                "composite_score": score,
                "min_window_score": score,
                "mean_window_score": score,
                "full_trade_count": 5,
            }
            for model, score in (
                ("current_fixed", 0.40),
                ("joint_fixed", 0.50),
                ("sigma_symmetric", 0.60),
            )
        ]
    )


def _holdout_row(model: str, score: float) -> dict[str, object]:
    return {
        "symbol": "000001",
        "name": "测试",
        "candidate_id": f"holdout_{model}",
        "stage": "holdout",
        "model_family": model,
        "signal_rule": "percent" if model == "current_fixed" else "sigma",
        "ma_period": 20,
        "threshold_pct": 1.0,
        "sigma_period": 60,
        "buy_k": 0.0,
        "sell_k": 0.0,
        "buy_alpha_pct": 0.0,
        "sell_alpha_pct": 0.0,
        "anchor_k": 0.0,
        "selection_allowed": True,
        "evaluation_window": "full",
        "trade_count": 5,
        "annual_return_pct": 10.0,
        "composite_score": score,
        "min_window_score": score,
        "mean_window_score": score,
        "score_std": 0.0,
        "subwindow_trade_count": 0,
        "full_annual_return_pct": 10.0,
        "full_composite_score": score,
        "full_trade_count": 5,
    }


def _stress_frame(label: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "evaluation_window": "full",
                "evaluation_label": label,
                "model_family": model,
                "candidate_id": f"stress_{label}_{model}",
                "composite_score": score,
                "full_trade_count": 5,
            }
            for model, score in (
                ("current_fixed", 0.40),
                ("joint_fixed", 0.50),
                ("sigma_symmetric", 0.60),
            )
        ]
    )


# holdout 子窗口（subwindow_0/1/2）打分行：aggregate_training_scores 以
# evaluation_window 前缀聚合出 min/mean_window_score；动态各子窗口全部优于当前，
# 保证 holdout 判胜可预期（min 0.6>0.4 且 mean 0.6>0.4）。
def _holdout_subwindow_rows(model: str, scores: tuple[float, float, float]) -> list[dict[str, object]]:
    rows = []
    for index, score in enumerate(scores):
        row = _holdout_row(model, score)
        row["evaluation_window"] = f"subwindow_{index}"
        row["candidate_id"] = f"holdout_{model}"
        rows.append(row)
    # full 行同样需要（aggregate 从 full 行映射 full_annual/composite/trade_count）
    full_row = _holdout_row(model, sum(scores) / 3)
    rows.append(full_row)
    return rows


def _make_result(holdout_end: str, folds: list[tuple[int, str, str]]) -> dict[str, object]:
    walk = pd.concat([_fold_frame(*fold) for fold in folds], ignore_index=True)
    holdout = pd.DataFrame(
        _holdout_subwindow_rows("current_fixed", (0.40, 0.40, 0.40))
        + _holdout_subwindow_rows("joint_fixed", (0.50, 0.50, 0.50))
        + _holdout_subwindow_rows("sigma_symmetric", (0.60, 0.60, 0.60))
    )
    stress = pd.concat(
        [_stress_frame(label) for label in ("after_close", "next_open_5bp")],
        ignore_index=True,
    )
    return {
        "fit": {
            "stage2": {"aggregate": pd.DataFrame()},
            "best_dynamic": {
                "model_family": "sigma_symmetric",
                "ma_period": 20,
                "sigma_period": 60,
                "buy_k": 0.0,
                "sell_k": 0.0,
                "buy_alpha_pct": 0.0,
                "sell_alpha_pct": 0.0,
            },
            "best_fixed": {"ma_period": 20, "threshold_pct": 1.0},
        },
        "baseline": pd.DataFrame(),
        "holdout": holdout,
        "stress": stress,
        "walk_forward": walk,
        "stability": pd.DataFrame(),
        "train_start": pd.Timestamp("2023-01-01"),
        "train_end": pd.Timestamp("2025-12-31"),
        "test_start": pd.Timestamp("2026-01-01"),
        "test_end": pd.Timestamp(holdout_end),
        "holdout_test_window": (pd.Timestamp("2026-01-01"), pd.Timestamp(holdout_end)),
        "candidates": [],
    }


def _build_one(result: dict[str, object]) -> pd.Series:
    base = R.AuditAllocation(
        symbol="000001", name="测试", weight_pct=100, strategy="timing", ma_period=20, threshold_pct=1.0
    )
    quality = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "name": "测试",
                "history_years": 5.0,
                "error": "",
            }
        ]
    )
    summary = R.build_decision_summary(
        {"000001": base},
        quality,
        {"000001": result},
    )
    return summary.iloc[0]


class IndependentOosWinRateTests(unittest.TestCase):
    def test_overlapping_last_fold_is_dropped(self):
        # holdout 测试窗 2026-01-01 ~ 2026-08-31；第2折与之重叠必须丢弃
        result = _make_result(
            "2026-08-31",
            folds=[
                (1, "2025-07-01", "2025-12-31"),
                (2, "2026-04-01", "2026-08-31"),
            ],
        )
        row = _build_one(result)
        self.assertEqual(str(row["oos_dedup_dropped_folds"]), "2")
        # 胜率 = holdout(胜) + 折1(动态0.6>当前0.4 胜) = 2/2
        self.assertAlmostEqual(float(row["independent_oos_win_rate"]), 1.0)

    def test_non_overlapping_folds_are_kept(self):
        result = _make_result(
            "2026-08-31",
            folds=[
                (1, "2024-07-01", "2024-12-31"),
                (2, "2025-07-01", "2025-12-31"),
            ],
        )
        row = _build_one(result)
        self.assertEqual(str(row["oos_dedup_dropped_folds"]), "无")

    def test_partially_overlapping_fold_is_dropped(self):
        # 折2 测试窗 2025-12-01 ~ 2026-06-30 与 holdout 起点 2026-01-01 部分重叠
        result = _make_result(
            "2026-08-31",
            folds=[
                (1, "2025-01-01", "2025-06-30"),
                (2, "2025-12-01", "2026-06-30"),
            ],
        )
        row = _build_one(result)
        self.assertEqual(str(row["oos_dedup_dropped_folds"]), "2")

    def test_walk_forward_scores_exclude_dropped_fold(self):
        # wf_better_current 只比较未丢弃折：折1 动态0.6>当前0.4 → True
        result = _make_result(
            "2026-08-31",
            folds=[
                (1, "2025-01-01", "2025-06-30"),
                (2, "2026-03-01", "2026-08-31"),
            ],
        )
        row = _build_one(result)
        self.assertTrue(bool(row["walk_forward_better_current"]))


if __name__ == "__main__":
    unittest.main()
