import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from services.futures_options_analysis import FUTURES_OPTION_DATA_VERSION
from services.futures_spread import SPREAD_CALCULATION_VERSION
from services.position_analysis import _merge_by_date, load_or_fetch_option, load_or_fetch_spread


class PositionAnalysisTests(unittest.TestCase):
    def test_merge_by_date_keeps_existing_same_day_value(self):
        cached = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
                "close": [10.0, 11.0],
            }
        )
        latest = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-02", "2026-07-03"]),
                "close": [12.0, 13.0],
            }
        )

        merged = _merge_by_date(cached, latest, "date")

        self.assertEqual(len(merged), 3)
        self.assertEqual(merged.loc[merged["date"] == pd.Timestamp("2026-07-02"), "close"].iloc[0], 11.0)

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_futures_option_data")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_option_refresh_incrementally_merges_cache(self, load_mock, fetch_mock, save_mock):
        cached = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
                "close": [10.0, 11.0],
                "volume": [100, 110],
                "open_interest": [1000, 1010],
                "_data_version": FUTURES_OPTION_DATA_VERSION,
            }
        )
        latest = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-02", "2026-07-03"]),
                "close": [12.0, 13.0],
                "volume": [120, 130],
                "open_interest": [1020, 1030],
            }
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-07-02T16:00:00"})
        fetch_mock.return_value = SimpleNamespace(dataframe=latest, is_chain=False, source="AkShare期权")

        item = load_or_fetch_option("I2609P730", count=500, force_refresh=True)

        self.assertEqual(item.status, "已增量更新")
        self.assertEqual(len(item.dataframe), 3)
        same_day = item.dataframe.loc[item.dataframe["date"] == pd.Timestamp("2026-07-02")]
        self.assertEqual(same_day["close"].iloc[0], 11.0)
        saved = save_mock.call_args.kwargs["df"]
        self.assertEqual(len(saved), 3)
        self.assertTrue(saved["_data_version"].eq(FUTURES_OPTION_DATA_VERSION).all())

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.calculate_spreads")
    @patch("services.position_analysis.fetch_contracts", return_value=({}, []))
    @patch("services.position_analysis._spread_cache_matches_contracts", return_value=True)
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_spread_refresh_incrementally_merges_cache(
        self,
        load_mock,
        _cache_ready_mock,
        _fetch_mock,
        calculate_mock,
        save_mock,
    ):
        cached = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
                "I2609_close": [800.0, 805.0],
                "I2701_close": [780.0, 782.0],
                "spread_I2609_vs_I2701": [20.0, 23.0],
                "spread_I2609_vs_I2701_pct": [2.5, 2.8571],
                "_calculation_version": SPREAD_CALCULATION_VERSION,
            }
        )
        latest = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-02", "2026-07-03"]),
                "I2609_close": [806.0, 810.0],
                "I2701_close": [783.0, 785.0],
                "spread_I2609_vs_I2701": [23.0, 25.0],
                "spread_I2609_vs_I2701_pct": [2.8536, 3.0864],
                "_calculation_version": SPREAD_CALCULATION_VERSION,
            }
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-07-02T16:00:00"})
        calculate_mock.return_value = latest

        item = load_or_fetch_spread(["I2609", "I2701"], force_refresh=True)

        self.assertEqual(item.status, "已增量更新")
        self.assertEqual(len(item.dataframe), 3)
        same_day = item.dataframe.loc[item.dataframe["date"] == pd.Timestamp("2026-07-02")]
        self.assertEqual(same_day["I2609_close"].iloc[0], 805.0)
        self.assertEqual(len(save_mock.call_args.kwargs["df"]), 3)


if __name__ == "__main__":
    unittest.main()
