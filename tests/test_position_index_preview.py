from datetime import datetime
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import patch

import pandas as pd

from services.position_timing import build_position_index_timing_table, build_recent_position_operation_guidance


class IndexPreviewTests(unittest.TestCase):
    def test_missing_previous_session_suppresses_preview_until_history_is_complete(self):
        history = pd.DataFrame({
            "trade_date": pd.bdate_range(end="2026-09-04", periods=30),
            "close": [100.0] * 30,
        })
        original = history.copy(deep=True)
        now = datetime(2026, 9, 8, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        quotes = {"微盘股": {"price": 102, "quote_time": now}}
        with patch("services.position_timing._load_position_index_timing_history", return_value=history):
            row = build_position_index_timing_table(realtime_quotes=quotes, market_now=now).iloc[0]
        self.assertEqual(row["最新收盘"], 102)
        self.assertEqual(row["数据截止日"], "2026-09-08")
        self.assertIn("2026-09-07", row["数据状态"])
        self.assertIn("暂停择时预判", row["数据状态"])
        for column in ("当日涨跌幅(%)", "对应均线", "偏离率(%)", "择时判断", "状态转换时间", "区间涨幅(%)"):
            self.assertTrue(pd.isna(row[column]), column)
        pd.testing.assert_frame_equal(history, original)
        complete = pd.concat([history, pd.DataFrame({
            "trade_date": [pd.Timestamp("2026-09-07")], "close": [90.0],
        })], ignore_index=True)
        with patch("services.position_timing._load_position_index_timing_history", return_value=complete):
            restored = build_position_index_timing_table(realtime_quotes=quotes, market_now=now).iloc[0]
        self.assertEqual(restored["择时判断"], "买入")
        self.assertAlmostEqual(restored["当日涨跌幅(%)"], (102 / 90 - 1) * 100)

    def test_internal_history_gap_blocks_preview_even_when_latest_session_exists(self):
        dates = pd.bdate_range(end="2026-09-07", periods=50)
        history = pd.DataFrame({"trade_date": dates, "close": 100.0}).drop(index=5)
        now = datetime(2026, 9, 8, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch("services.position_timing._load_position_index_timing_history", return_value=history):
            row = build_position_index_timing_table(
                realtime_quotes={"微盘股": {"price": 110, "quote_time": now}}, market_now=now,
            ).iloc[0]
        self.assertTrue(pd.isna(row["择时判断"]))
        self.assertIn(dates[5].strftime("%Y-%m-%d"), row["数据状态"])

    def test_holiday_gap_does_not_block_preview(self):
        history = pd.DataFrame({
            "trade_date": pd.bdate_range(end="2026-09-30", periods=30), "close": 100.0,
        })
        now = datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch("services.position_timing._load_position_index_timing_history", return_value=history):
            row = build_position_index_timing_table(
                realtime_quotes={"微盘股": {"price": 110, "quote_time": now}}, market_now=now,
            ).iloc[0]
        self.assertEqual(row["择时判断"], "买入")
        self.assertIn("实时预判", row["数据状态"])

    def test_guidance_includes_formal_index_changes_within_seven_days(self):
        history = pd.DataFrame({
            "trade_date": pd.bdate_range(end="2026-09-07", periods=30),
            "close": [100.0] * 24 + [110.0] * 5 + [90.0],
        })
        with patch("services.position_timing._load_position_index_timing_history", return_value=history):
            guidance = build_recent_position_operation_guidance([], days=7)
        self.assertEqual(set(guidance["代码"]), {"BK1158", "000905"})
        self.assertEqual(set(guidance["操作指引"]), {"卖出"})
        self.assertTrue((guidance["日期"] == "2026-09-07").all())
        self.assertIn("标的名称", guidance.columns)

    def test_preview_recalculates_signal_without_mutating_formal_history(self):
        history = pd.DataFrame({
            "trade_date": pd.bdate_range(end="2026-09-04", periods=30),
            "close": [100.0] * 30,
        })
        original = history.copy(deep=True)
        now = datetime(2026, 9, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch("services.position_timing._load_position_index_timing_history", return_value=history):
            bullish = build_position_index_timing_table(
                realtime_quotes={"微盘股": {"price": 110, "quote_time": now}}, market_now=now,
            ).iloc[0]
            neutral = build_position_index_timing_table(
                realtime_quotes={"微盘股": {"price": 101, "quote_time": now}}, market_now=now,
            ).iloc[0]
            stale = build_position_index_timing_table(
                realtime_quotes={"微盘股": {"price": 110, "quote_time": "2026-09-04 15:00"}}, market_now=now,
            ).iloc[0]
        self.assertEqual(bullish["择时判断"], "买入")
        self.assertAlmostEqual(bullish["对应均线"], (1400 + 110) / 15)
        self.assertIn("实时预判", bullish["数据状态"])
        self.assertEqual(neutral["择时判断"], "空仓")
        self.assertEqual(stale["数据状态"], "正式收盘缓存")
        pd.testing.assert_frame_equal(history, original)

    def test_confirmed_close_supersedes_retained_quote(self):
        history = pd.DataFrame({
            "trade_date": pd.bdate_range(end="2026-09-07", periods=30),
            "close": [100.0] * 30,
        })
        now = datetime(2026, 9, 7, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch("services.position_timing._load_position_index_timing_history", return_value=history):
            row = build_position_index_timing_table(
                realtime_quotes={"微盘股": {"price": 110, "quote_time": now}}, market_now=now,
            ).iloc[0]
        self.assertEqual(row["最新收盘"], 100)
        self.assertEqual(row["数据状态"], "正式收盘缓存")


def test_csi1000_uses_ma30_two_percent_without_mutating_formal_history():
    history = pd.DataFrame({'trade_date':pd.bdate_range(end='2026-09-10',periods=40),'close':100.})
    original = history.copy(deep=True)
    now = datetime(2026,9,11,10,0,tzinfo=ZoneInfo('Asia/Shanghai'))
    with patch('services.position_timing._load_position_index_timing_history',return_value=history):
        table=build_position_index_timing_table(realtime_quotes={'中证1000':{'price':130.,'quote_time':now}},market_now=now)
    row=table.loc[table['代码']=='000852'].iloc[0]
    assert row['策略参数']=='MA30 / 2.0%'
    assert row['对应均线']==101.
    assert row['择时判断']=='买入'
    pd.testing.assert_frame_equal(history,original)
