import unittest

import pandas as pd

from services.microcap import filter_active_microcap_stocks, build_microcap_snapshot_metrics


class SnapshotSuspensionTests(unittest.TestCase):
    def test_date_boundaries_and_original_rows_preserved(self):
        frame = pd.DataFrame({
            "代码": ["688121"] * 4 + ["000001"],
            "快照日期": ["2026-05-05", "2026-05-06", "2026-07-06", "2026-07-07", "2026-06-22"],
            "是否停牌": [False] * 5,
            "最新价": [6.48] * 5,
            "总市值(亿元)": [15.14] * 5,
        })
        original = frame.copy(deep=True)
        result = filter_active_microcap_stocks(frame)
        self.assertEqual(result.index.tolist(), [0, 3, 4])
        pd.testing.assert_frame_equal(frame, original)

    def test_next_stock_replaces_suspended_stock_in_top20(self):
        frame = pd.DataFrame({
            "代码": ["688121"] + [f"{i:06d}" for i in range(1, 22)],
            "名称": ["卓然股份"] + ["样本"] * 21,
            "快照日期": ["2026-06-22"] * 22,
            "最新价": [1.0] * 22,
            "总市值(亿元)": list(range(1, 23)),
            "是否停牌": [False] * 22,
        })
        result = build_microcap_snapshot_metrics(frame)
        self.assertEqual(result.iloc[0]["有效股票数"], 21)
        self.assertEqual(result.iloc[0]["微盘20均值(亿元)"], 11.5)
