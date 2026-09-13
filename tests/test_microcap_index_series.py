from datetime import date
import unittest
from unittest.mock import patch

import pandas as pd

from services.microcap import load_microcap_index_series


class MicrocapIndexSeriesTests(unittest.TestCase):
    def load(self, history, finalized):
        with patch("services.microcap.load_dataset", side_effect=[(history, None), (finalized, None)]), patch(
            "services.index_ma20.latest_completed_trade_date", return_value=date(2026, 9, 7)
        ):
            return load_microcap_index_series()

    def test_final_close_adds_missing_september_seventh_and_overrides_raw(self):
        history = pd.DataFrame({"trade_date": ["2026-09-04"], "close": [3700.0]})
        finalized = pd.DataFrame({
            "trade_date": ["2026-09-04", "2026-09-07"], "close": [3712.26, 3739.71]
        })
        original = history.copy(deep=True)
        result = self.load(history, finalized)
        self.assertEqual(result["微盘股指数"].tolist(), [3712.26, 3739.71])
        self.assertEqual(result["日期_dt"].dt.strftime("%Y-%m-%d").tolist(), ["2026-09-04", "2026-09-07"])
        pd.testing.assert_frame_equal(history, original)

    def test_missing_finalized_cache_keeps_completed_raw_history_only(self):
        history = pd.DataFrame({
            "trade_date": ["2026-09-04", "2026-09-06", "2026-09-08"],
            "close": [3712.26, 3800.0, 3900.0],
        })
        result = self.load(history, None)
        self.assertEqual(result["微盘股指数"].tolist(), [3712.26])

    def test_finalized_only_cache_is_available(self):
        finalized = pd.DataFrame({"trade_date": ["2026-09-07"], "close": [3739.71]})
        self.assertEqual(self.load(None, finalized)["微盘股指数"].tolist(), [3739.71])

    def test_empty_cache_retains_chart_columns(self):
        result = self.load(None, None)
        self.assertTrue(result.empty)
        self.assertEqual(result.columns.tolist(), ["日期_dt", "微盘股指数"])
