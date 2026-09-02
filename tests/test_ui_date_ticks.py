import unittest
import pandas as pd
from datetime import datetime
from core.ui import build_sparse_trading_date_ticks, filter_by_time_range


class SparseTradingDateTicksTests(unittest.TestCase):
    def test_empty_dates(self):
        vals, labels = build_sparse_trading_date_ticks([])
        self.assertEqual(vals, [])
        self.assertEqual(labels, [])

    def test_short_dates_below_max_ticks(self):
        dates = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]
        vals, labels = build_sparse_trading_date_ticks(dates, max_ticks=7)
        self.assertEqual(vals, dates)
        self.assertEqual(labels, ["08-01", "08-02", "08-03", "08-04"])

    def test_single_year_sparse_sampling(self):
        dates = pd.date_range("2026-07-01", periods=30, freq="B").strftime("%Y-%m-%d").tolist()
        vals, labels = build_sparse_trading_date_ticks(dates, max_ticks=7)
        self.assertLessEqual(len(vals), 8)
        self.assertGreaterEqual(len(vals), 5)
        self.assertEqual(vals[0], dates[0])
        self.assertEqual(vals[-1], dates[-1])
        # In single year, labels should be MM-DD
        for label in labels:
            self.assertEqual(len(label), 5)
            self.assertIn("-", label)

    def test_multi_year_displays_year_on_transition(self):
        dates = pd.date_range("2025-10-01", periods=100, freq="B").strftime("%Y-%m-%d").tolist()
        vals, labels = build_sparse_trading_date_ticks(dates, max_ticks=7)
        self.assertEqual(vals[0], dates[0])
        self.assertEqual(vals[-1], dates[-1])
        # Multi year should have first label formatted as YYYY-MM-DD
        self.assertEqual(labels[0], vals[0])
        # And any tick transitioning to 2026 should be formatted with 2026-
        has_2026 = any("2026-" in label for label in labels)
        self.assertTrue(has_2026)


class FilterByTimeRangeTests(unittest.TestCase):
    def setUp(self):
        dates = pd.date_range("2025-01-01", "2026-09-01", freq="B")
        self.df = pd.DataFrame({"date": dates, "value": range(len(dates))})

    def test_filter_all(self):
        res = filter_by_time_range(self.df, "date", "全部")
        self.assertEqual(len(res), len(self.df))

    def test_filter_one_month(self):
        res = filter_by_time_range(self.df, "date", "近1月")
        min_date = pd.to_datetime(res["date"]).min()
        max_date = pd.to_datetime(res["date"]).max()
        self.assertLessEqual((max_date - min_date).days, 32)

    def test_filter_three_months(self):
        res = filter_by_time_range(self.df, "date", "近3月")
        min_date = pd.to_datetime(res["date"]).min()
        max_date = pd.to_datetime(res["date"]).max()
        self.assertLessEqual((max_date - min_date).days, 93)

    def test_filter_one_year(self):
        res = filter_by_time_range(self.df, "date", "近1年")
        min_date = pd.to_datetime(res["date"]).min()
        max_date = pd.to_datetime(res["date"]).max()
        self.assertLessEqual((max_date - min_date).days, 367)

    def test_filter_ytd(self):
        res = filter_by_time_range(self.df, "date", "今年以来")
        min_date = pd.to_datetime(res["date"]).min()
        self.assertEqual(min_date.year, 2026)
