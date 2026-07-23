import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import latest_trade_date_text
from services.index_ma20 import (
    INDEX_CONFIG,
    INDEX_FINAL_HISTORY_SOURCE,
    INDEX_LONG_HISTORY_SOURCE,
    append_akshare_latest_index_row,
    append_eastmoney_latest_index_row,
    append_futures_spot_row,
    append_hk_index_spot_row,
    build_export_df,
    build_summary,
    fetch_index_history,
    fetch_eastmoney_clist_latest_index_row,
    filter_completed_market_dates,
    get_index_data_from_akshare_futures_main,
    overlay_finalized_index_rows,
    sanitize_index_report_market_dates,
    supplement_stale_yahoo_history,
)
from services.market_calendar import get_market_window, is_market_trading_day, latest_completed_trade_date
from services.position_analysis import _cache_has_expected_trade_date
from services.update_tasks import (
    append_cached_index_rows,
    build_index_update_message,
    enrich_index_report_indicators,
    fetch_index_report,
    has_current_index_quote,
    merge_index_report,
    persist_confirmed_index_report_row,
    refresh_cached_eastmoney_index_report,
    sync_index_long_history,
)


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "data": {
                "diff": [
                    {
                        "f12": "BK1158",
                        "f2": 1234.5,
                        "f124": "-",
                    }
                ]
            }
        }


class _FakeSession:
    trust_env = False

    def get(self, *args, **kwargs):
        return _FakeResponse()


class MarketAndCacheTests(unittest.TestCase):
    @patch("services.index_ma20.fetch_yahoo_latest_index_row")
    def test_stale_yahoo_history_retries_short_window(self, latest_row_mock):
        history = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-16", "2026-07-17"]),
                "close": [100.0, 101.0],
            }
        )
        latest_row_mock.return_value = pd.DataFrame(
            {"trade_date": pd.to_datetime(["2026-07-20"]), "close": [102.0]}
        )

        result = supplement_stale_yahoo_history(
            history,
            "^TEST",
            now=datetime(2026, 7, 21, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        )

        self.assertEqual(result["trade_date"].max(), pd.Timestamp("2026-07-20"))
        latest_row_mock.assert_called_once_with("^TEST")

    @patch("services.index_ma20.fetch_yahoo_latest_index_row")
    def test_current_yahoo_history_does_not_retry_short_window(self, latest_row_mock):
        history = pd.DataFrame(
            {"trade_date": pd.to_datetime(["2026-07-20"]), "close": [102.0]}
        )

        result = supplement_stale_yahoo_history(
            history,
            "^TEST",
            now=datetime(2026, 7, 21, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        )

        self.assertEqual(result["trade_date"].max(), pd.Timestamp("2026-07-20"))
        latest_row_mock.assert_not_called()

    def test_index_update_message_lists_only_actual_successes_as_successful(self):
        timings = [
            {"指数": "沪深300", "状态": "成功"},
            {"指数": "标普500", "状态": "使用缓存"},
            {"指数": "恒生科技", "状态": "最新可得"},
        ]

        message = build_index_update_message(
            selected_indexes={"沪深300", "标普500", "恒生科技"},
            selected_markets=set(),
            timings=timings,
            errors=["标普500: 最新数据未达到预期日期"],
        )

        self.assertIn("正式日线更新成功：沪深300", message)
        self.assertIn("仅保存最新可得数据：恒生科技", message)
        self.assertIn("正式日线未更新，沿用缓存：标普500", message)
        self.assertNotIn("正式日线更新成功：沪深300、标普500", message)
        self.assertIn("未完成原因：标普500: 最新数据未达到预期日期", message)

    def test_previous_close_is_current_during_next_trading_session(self):
        class CurrentSessionDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 17, 11, 30)
                return value.replace(tzinfo=tz) if tz is not None else value

        config = {"market_group": "A股", "require_current_quote": True}
        current = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-07-16"]),
                "沪深300_收盘价": [4500.0],
            }
        )
        stale = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-07-15"]),
                "沪深300_收盘价": [4400.0],
            }
        )

        with patch("services.update_tasks.datetime", CurrentSessionDateTime):
            self.assertTrue(has_current_index_quote(current, "沪深300", config))
            self.assertFalse(has_current_index_quote(stale, "沪深300", config))

    def test_crude_oil_config_uses_eastmoney_but_keeps_futures_session_symbol(self):
        config = INDEX_CONFIG["原油主连"]

        self.assertEqual(config["source"], "eastmoney_kline")
        self.assertEqual(config["code"], "142.scm")
        self.assertEqual(config["futures_symbol"], "SC0")
        self.assertEqual(config["source_correction_start"], "2026-07-10")

    def test_exchange_holidays_are_not_trading_days(self):
        cases = [
            ("A股", "2026-10-01T12:00:00"),
            ("日本", "2026-01-01T12:00:00"),
            ("韩国", "2026-01-01T12:00:00"),
            ("韩国", "2026-07-17T12:00:00"),
            ("美股", "2021-12-31T12:00:00"),
        ]
        for market_name, value in cases:
            market = get_market_window(market_name)
            self.assertIsNotNone(market)
            market_now = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(market.timezone))
            self.assertFalse(is_market_trading_day(market, market_now), market_name)

    def test_weekend_cache_accepts_latest_trading_day(self):
        cache = pd.DataFrame({"date": ["2026-07-10"]})
        sunday = datetime(2026, 7, 12, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertTrue(_cache_has_expected_trade_date(cache, market_now=sunday))

    def test_latest_completed_trade_date_excludes_open_session(self):
        market = get_market_window("A股")
        market_now = datetime(2026, 7, 13, 10, 0, tzinfo=ZoneInfo(market.timezone))

        self.assertEqual(str(latest_completed_trade_date(market, market_now)), "2026-07-10")

    def test_long_history_filter_excludes_current_open_session(self):
        class OpenSessionDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 13, 10, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-10", "2026-07-13"]),
                "close": [100.0, 101.0],
            }
        )
        with patch("services.index_ma20.datetime", OpenSessionDateTime):
            result = filter_completed_market_dates(raw, "A股")

        self.assertEqual(result["trade_date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-07-10"])

    def test_date_column_populates_cache_trade_date(self):
        data = pd.DataFrame({"date": ["2026-07-08", "invalid", "2026-07-10"]})

        self.assertEqual(latest_trade_date_text(data), "2026-07-10")

    @patch("requests.Session", return_value=_FakeSession())
    def test_eastmoney_quote_without_timestamp_is_rejected(self, _session):
        result = fetch_eastmoney_clist_latest_index_row(board_symbol="BK1158")

        self.assertIsNone(result)

    @patch("services.index_ma20.append_eastmoney_clist_latest_index_row", side_effect=lambda df, **kwargs: df)
    @patch("services.index_ma20.append_eastmoney_quote_row")
    def test_eastmoney_history_drops_non_trading_dates(self, quote_row, _clist_row):
        quote_row.return_value = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-03", "2026-07-04"]),
                "close": [100.0, 101.0],
            }
        )

        result = append_eastmoney_latest_index_row(None, pd.DataFrame(), "90.BK1158", board_symbol="BK1158")

        self.assertEqual(result["trade_date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-07-03"])

    def test_index_summary_keeps_card_when_ma20_is_unavailable(self):
        report = pd.DataFrame(
            {
                "日期": ["2026-07-09", "2026-07-10"],
                "恒生港股通高息低波_收盘价": [4012.13, 4023.84],
                "恒生港股通高息低波_MA20": [pd.NA, pd.NA],
                "恒生港股通高息低波_偏离率(%)": [pd.NA, pd.NA],
            }
        )

        summary = build_summary(report)
        row = summary.loc[summary["指数"] == "恒生港股通高息低波"].iloc[0]

        self.assertEqual(row["日期"], "2026-07-10")
        self.assertEqual(row["收盘价"], 4023.84)
        self.assertTrue(pd.isna(row["MA20"]))
        self.assertAlmostEqual(row["当日涨跌幅(%)"], (4023.84 / 4012.13 - 1) * 100)

    @patch("services.index_ma20.get_index_data_from_akshare_cni")
    def test_cni_index_uses_official_history_source(self, cni_mock):
        from services.index_ma20 import fetch_index_from_source

        expected = pd.DataFrame({"日期": ["2013-01-04"], "国证自由现金流_收盘价": [996.8178]})
        cni_mock.return_value = expected
        config = {"source": "akshare_cni", "code": "980092"}

        result = fetch_index_from_source("国证自由现金流", config, days=10000)

        self.assertIs(result, expected)
        cni_mock.assert_called_once_with("980092", "国证自由现金流", days=10000)

    def test_weekend_rows_are_removed_from_index_summary(self):
        report = pd.DataFrame(
            {
                "日期": ["2026-07-09", "2026-07-10", "2026-07-12"],
                "恒生科技_收盘价": [4700.0, 4721.66, 4721.66],
                "恒生科技_MA20": [4600.0, 4610.0, 4615.0],
                "恒生科技_偏离率(%)": [2.17, 2.42, 2.31],
            }
        )

        row = build_summary(report).loc[lambda data: data["指数"] == "恒生科技"].iloc[0]

        self.assertEqual(row["日期"], "2026-07-10")
        self.assertAlmostEqual(row["当日涨跌幅(%)"], (4721.66 / 4700.0 - 1) * 100)

    def test_market_sanitizer_only_clears_the_closed_market(self):
        report = pd.DataFrame(
            {
                "日期": ["2026-07-03"],
                "沪深300_收盘价": [4500.0],
                "标普500_收盘价": [6200.0],
            }
        )

        cleaned = sanitize_index_report_market_dates(report)

        self.assertEqual(cleaned.iloc[0]["沪深300_收盘价"], 4500.0)
        self.assertTrue(pd.isna(cleaned.iloc[0]["标普500_收盘价"]))

    def test_weekend_spot_helpers_do_not_append_fake_rows(self):
        class SundayDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 12, 12, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        raw = pd.DataFrame({"trade_date": pd.to_datetime(["2026-07-10"]), "close": [100.0]})
        with patch("services.index_ma20.datetime", SundayDateTime):
            china = append_akshare_latest_index_row(object(), raw, "000300")
            hong_kong = append_hk_index_spot_row(object(), raw, "HSTECH")
            futures = append_futures_spot_row(object(), raw, "I0")

        for result in (china, hong_kong, futures):
            self.assertEqual(result["trade_date"].max(), pd.Timestamp("2026-07-10"))

    @patch("akshare.futures_zh_spot")
    @patch("akshare.futures_zh_daily_sina")
    def test_index_futures_history_does_not_append_intraday_spot(self, daily_mock, spot_mock):
        daily_mock.return_value = pd.DataFrame(
            {
                "date": ["2026-07-21", "2026-07-22"],
                "close": [7685.8, 7611.0],
            }
        )

        result = get_index_data_from_akshare_futures_main("IC0", "中证500期货主连", days=30)

        spot_mock.assert_not_called()
        self.assertEqual(result["日期"].max(), "2026-07-22")

    @patch("services.update_tasks.append_eastmoney_latest_index_row", side_effect=lambda _ak, raw, *_args, **_kwargs: raw)
    def test_short_fallback_does_not_erase_cached_ma20(self, _append_mock):
        cached = pd.DataFrame(
            {
                "日期": ["2026-07-09", "2026-07-10"],
                "微盘股_收盘价": [3129.87, 3205.65],
                "微盘股_MA20": [3280.0, 3275.76],
                "微盘股_偏离率(%)": [-4.58, -2.14],
            }
        )

        refreshed = refresh_cached_eastmoney_index_report(
            cached,
            "微盘股",
            {"source": "eastmoney_kline", "code": "90.BK1158", "akshare_board_symbol": "BK1158"},
            days=30,
        )

        self.assertEqual(refreshed.iloc[-1]["微盘股_MA20"], 3275.76)

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.load_dataset", return_value=(None, None))
    def test_post_close_list_quote_is_saved_as_final_confirmation(self, _load_mock, save_mock):
        class PostCloseDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 15, 16, 13)
                return value.replace(tzinfo=tz) if tz is not None else value

        report = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-07-14", "2026-07-15"]),
                "恒生港股通高息低波_收盘价": [4108.46, 4143.65],
            }
        )
        config = {"market_group": "港股", "code": "124.HSHYLV"}

        with patch("services.update_tasks.datetime", PostCloseDateTime):
            persist_confirmed_index_report_row("恒生港股通高息低波", config, report)

        saved = save_mock.call_args.kwargs
        self.assertEqual(saved["source"], INDEX_FINAL_HISTORY_SOURCE)
        self.assertEqual(saved["df"]["trade_date"].max(), pd.Timestamp("2026-07-15"))
        self.assertEqual(saved["df"].iloc[-1]["close"], 4143.65)

    def test_index_raw_cache_never_replaces_existing_date(self):
        cached = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-09", "2026-07-10"]),
                "close": [100.0, 101.0],
            }
        )
        latest = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-10", "2026-07-13"]),
                "close": [999.0, 102.0],
            }
        )

        merged = append_cached_index_rows(cached, latest)

        self.assertEqual(len(merged), 3)
        self.assertEqual(merged.loc[merged["trade_date"] == pd.Timestamp("2026-07-10"), "close"].iloc[0], 101.0)
        self.assertEqual(merged.iloc[-1]["close"], 102.0)

    def test_finalized_rows_override_only_the_calculation_view(self):
        cached = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-13", "2026-07-14"]),
                "close": [90.0, 91.0],
            }
        )
        finalized = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-14"]),
                "close": [101.0],
            }
        )

        effective = overlay_finalized_index_rows(cached, finalized)

        self.assertEqual(cached.iloc[-1]["close"], 91.0)
        self.assertEqual(effective.iloc[-1]["close"], 101.0)

    def test_index_report_keeps_existing_values_and_appends_new_date(self):
        cached = pd.DataFrame(
            {
                "日期": ["2026-07-10"],
                "恒生科技_收盘价": [4721.66],
                "恒生科技_MA20": [4559.94],
            }
        )
        latest = pd.DataFrame(
            {
                "日期": ["2026-07-10", "2026-07-13"],
                "恒生科技_收盘价": [9999.0, 4800.0],
                "恒生科技_MA20": [9999.0, 4600.0],
            }
        )

        merged = merge_index_report(cached, latest)

        old_row = merged.loc[merged["日期"] == "2026-07-10"].iloc[0]
        self.assertEqual(old_row["恒生科技_收盘价"], 4721.66)
        self.assertEqual(old_row["恒生科技_MA20"], 4559.94)
        self.assertEqual(merged.loc[merged["日期"] == "2026-07-13", "恒生科技_收盘价"].iloc[0], 4800.0)

    def test_current_report_row_uses_long_history_for_indicators(self):
        history = pd.DataFrame(
            {
                "trade_date": pd.bdate_range(end="2026-07-10", periods=40),
                "close": [100.0] * 30 + [80.0] * 10,
            }
        )
        report = build_export_df(history, "微盘股", days=120)
        current = pd.DataFrame(
            {
                "日期": ["2026-07-13"],
                "微盘股_收盘价": [140.0],
                "微盘股_MA20": [pd.NA],
                "微盘股_偏离率(%)": [pd.NA],
                "微盘股_状态转变时间": [pd.NA],
                "微盘股_区间涨幅(%)": [pd.NA],
            }
        )
        report = merge_index_report(report, current)

        def load_side_effect(_symbol, source, _data_type):
            if source == INDEX_LONG_HISTORY_SOURCE:
                return history, {}
            return None, None

        with patch("services.update_tasks.load_dataset", side_effect=load_side_effect):
            enriched = enrich_index_report_indicators(report)

        latest = enriched.loc[enriched["日期"] == "2026-07-13"].iloc[0]
        self.assertFalse(pd.isna(latest["微盘股_MA20"]))
        self.assertFalse(pd.isna(latest["微盘股_偏离率(%)"]))
        self.assertFalse(pd.isna(latest["微盘股_状态转变时间"]))
        self.assertFalse(pd.isna(latest["微盘股_区间涨幅(%)"]))

    def test_index_report_calculates_each_historical_transition_interval(self):
        history = pd.DataFrame(
            {
                "trade_date": pd.bdate_range(end="2026-07-15", periods=24),
                "close": [100.0] * 20 + [90.0, 80.0, 120.0, 132.0],
            }
        )

        report = build_export_df(history, "测试指数", days=10000)

        first_below = report.loc[report["日期"] == "2026-07-10"].iloc[0]
        still_below = report.loc[report["日期"] == "2026-07-13"].iloc[0]
        first_above = report.loc[report["日期"] == "2026-07-14"].iloc[0]
        still_above = report.loc[report["日期"] == "2026-07-15"].iloc[0]

        self.assertEqual(first_below["测试指数_状态转变时间"], "2026-07-10")
        self.assertEqual(first_below["测试指数_区间涨幅(%)"], 0.0)
        self.assertEqual(still_below["测试指数_状态转变时间"], "2026-07-10")
        self.assertEqual(still_below["测试指数_区间涨幅(%)"], -11.11)
        self.assertEqual(first_above["测试指数_状态转变时间"], "2026-07-14")
        self.assertEqual(first_above["测试指数_区间涨幅(%)"], 0.0)
        self.assertEqual(still_above["测试指数_状态转变时间"], "2026-07-14")
        self.assertEqual(still_above["测试指数_区间涨幅(%)"], 10.0)

    def test_index_detail_reads_long_history_cache_without_network(self):
        long_cached = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-09", "2026-07-10"]),
                "close": [100.0, 101.0],
            }
        )
        config = {"source": "yahoo", "code": "^TEST", "market_group": "美股"}

        class SundayDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 12, 12, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        with (
            patch("core.cache.load_dataset", return_value=(long_cached, {})),
            patch("core.cache.save_dataset") as save_mock,
            patch("services.index_ma20.fetch_index_from_source") as fetch_mock,
            patch("services.index_ma20.datetime", SundayDateTime),
        ):
            result = fetch_index_history("测试指数", config, days=10000)

        self.assertEqual(len(result), 2)
        fetch_mock.assert_not_called()
        save_mock.assert_not_called()

    def test_index_detail_builds_source_correction_without_rewriting_long_history(self):
        long_cached = pd.DataFrame(
            {
                "trade_date": pd.bdate_range(end="2026-07-14", periods=300),
                "close": [450.0] * 297 + [471.2, 482.6, 519.4],
            }
        )
        finalized = long_cached.tail(3).copy()
        eastmoney_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-10", "2026-07-13", "2026-07-14"]),
                "close": [466.6, 478.0, 516.0],
            }
        )
        fetched = build_export_df(eastmoney_raw, "原油主连", days=30)
        config = INDEX_CONFIG["原油主连"]

        class ClosedSessionDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 14, 16, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        def load_side_effect(_symbol, source, _data_type):
            if source == INDEX_LONG_HISTORY_SOURCE:
                return long_cached, {}
            if source == "index_history":
                return long_cached, {}
            if source == INDEX_FINAL_HISTORY_SOURCE:
                return finalized, {}
            return None, None

        with (
            patch("core.cache.load_dataset", side_effect=load_side_effect),
            patch("core.cache.save_dataset") as save_mock,
            patch("services.index_ma20.fetch_index_from_source", return_value=fetched) as fetch_mock,
            patch("services.index_ma20.datetime", ClosedSessionDateTime),
        ):
            result = fetch_index_history("原油主连", config, days=10000)

        fetch_mock.assert_called_once_with("原油主连", config, days=30)
        corrected = result.set_index("日期")["原油主连_收盘价"]
        self.assertEqual(corrected.loc["2026-07-10"], 466.6)
        self.assertEqual(corrected.loc["2026-07-14"], 516.0)
        saved_sources = [call.kwargs["source"] for call in save_mock.call_args_list]
        self.assertEqual(saved_sources, ["index_source_correction_history"])

    def test_stale_index_detail_fetches_missing_window_and_keeps_existing_dates(self):
        long_cached = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-09", "2026-07-10"]),
                "close": [100.0, 101.0],
            }
        )
        latest_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-10", "2026-07-13", "2026-07-14"]),
                "close": [999.0, 102.0, 103.0],
            }
        )
        latest = build_export_df(latest_raw, "测试指数", days=30)
        config = {"source": "yahoo", "code": "^TEST", "market_group": "A股"}

        class ClosedSessionDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 14, 16, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        with (
            patch("core.cache.load_dataset", return_value=(long_cached, {})),
            patch("core.cache.save_dataset") as save_mock,
            patch("services.index_ma20.fetch_index_from_source", return_value=latest) as fetch_mock,
            patch("services.index_ma20.datetime", ClosedSessionDateTime),
        ):
            result = fetch_index_history("测试指数", config, days=10000)

        fetch_mock.assert_called_once_with("测试指数", config, days=30)
        saved_by_source = {call.kwargs["source"]: call.kwargs["df"] for call in save_mock.call_args_list}
        saved = saved_by_source[INDEX_LONG_HISTORY_SOURCE]
        old_close = saved.loc[saved["trade_date"] == pd.Timestamp("2026-07-10"), "close"].iloc[0]
        self.assertEqual(old_close, 101.0)
        self.assertEqual(saved["trade_date"].max(), pd.Timestamp("2026-07-14"))
        self.assertEqual(len(result), 4)

    def test_index_detail_append_preserves_rows_outside_calendar_coverage(self):
        long_cached = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["1999-02-26", "2026-07-10"]),
                "close": [100.0, 101.0],
            }
        )
        latest_raw = pd.DataFrame(
            {"trade_date": pd.to_datetime(["2026-07-14"]), "close": [102.0]}
        )
        latest = build_export_df(latest_raw, "测试指数", days=30)
        config = {"source": "akshare_global", "code": "测试指数", "market_group": "日本"}

        class ClosedSessionDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 14, 16, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        def load_side_effect(_symbol, source, _data_type):
            if source in (INDEX_LONG_HISTORY_SOURCE, "index_history"):
                return long_cached, {}
            return None, None

        with (
            patch("core.cache.load_dataset", side_effect=load_side_effect),
            patch("core.cache.save_dataset") as save_mock,
            patch("services.index_ma20.fetch_index_from_source", return_value=latest),
            patch("services.index_ma20.datetime", ClosedSessionDateTime),
        ):
            fetch_index_history("测试指数", config, days=10000)

        saved_by_source = {call.kwargs["source"]: call.kwargs["df"] for call in save_mock.call_args_list}
        saved_long = saved_by_source[INDEX_LONG_HISTORY_SOURCE]
        self.assertIn(pd.Timestamp("1999-02-26"), pd.to_datetime(saved_long["trade_date"]).tolist())
        self.assertIn(pd.Timestamp("2026-07-14"), pd.to_datetime(saved_long["trade_date"]).tolist())

    def test_first_index_detail_fetch_persists_long_history_without_replacing_old_dates(self):
        accumulated = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-08", "2026-07-09"]),
                "close": [100.0, 101.0],
            }
        )
        fetched_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-07", "2026-07-09", "2026-07-10"]),
                "close": [99.0, 999.0, 102.0],
            }
        )
        fetched = build_export_df(fetched_raw, "测试指数", days=10000)
        config = {"source": "yahoo", "code": "^TEST", "market_group": "美股"}

        def load_side_effect(_symbol, source, _data_type):
            if source == INDEX_LONG_HISTORY_SOURCE:
                return None, None
            return accumulated, {}

        with (
            patch("core.cache.load_dataset", side_effect=load_side_effect),
            patch("core.cache.save_dataset") as save_mock,
            patch("services.index_ma20.fetch_index_from_source", return_value=fetched) as fetch_mock,
        ):
            result = fetch_index_history("测试指数", config, days=10000)

        fetch_mock.assert_called_once_with("测试指数", config, days=10000)
        saved_by_source = {call.kwargs["source"]: call.kwargs["df"] for call in save_mock.call_args_list}
        self.assertIn("index_history", saved_by_source)
        self.assertIn(INDEX_LONG_HISTORY_SOURCE, saved_by_source)
        long_saved = saved_by_source[INDEX_LONG_HISTORY_SOURCE]
        old_close = long_saved.loc[long_saved["trade_date"] == pd.Timestamp("2026-07-09"), "close"].iloc[0]
        self.assertEqual(old_close, 101.0)
        self.assertEqual(len(long_saved), 4)
        self.assertEqual(len(result), 4)

    def test_daily_update_appends_new_dates_to_existing_long_history(self):
        long_cached = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-09", "2026-07-10"]),
                "close": [100.0, 101.0],
            }
        )
        latest = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-10", "2026-07-13"]),
                "close": [999.0, 102.0],
            }
        )

        with (
            patch("services.update_tasks.load_dataset", return_value=(long_cached, {})),
            patch("services.update_tasks.save_dataset") as save_mock,
        ):
            sync_index_long_history("index_raw_test", "测试指数", latest)

        saved = save_mock.call_args.kwargs["df"]
        self.assertEqual(len(saved), 3)
        old_close = saved.loc[saved["trade_date"] == pd.Timestamp("2026-07-10"), "close"].iloc[0]
        self.assertEqual(old_close, 101.0)
        self.assertEqual(saved.iloc[-1]["close"], 102.0)

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.fetch_one_index")
    @patch("services.update_tasks.load_dataset")
    def test_index_update_appends_to_raw_history_cache(self, load_dataset_mock, fetch_mock, save_mock):
        cached_raw = pd.DataFrame(
            {
                "trade_date": pd.bdate_range("2025-01-01", periods=300),
                "close": range(100, 400),
            }
        )
        latest_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-11", "2026-07-12"]),
                "close": [400, 401],
            }
        )
        load_dataset_mock.return_value = (cached_raw, {})
        fetch_mock.return_value = build_export_df(latest_raw, "微盘股", days=30)

        result = fetch_index_report("微盘股", {"source": "eastmoney_kline", "code": "90.BK1158"}, "", 30)

        fetch_mock.assert_called_once_with(
            "微盘股",
            {"source": "eastmoney_kline", "code": "90.BK1158"},
            api_key="",
            days=30,
        )
        saved_raw = next(
            call.kwargs["df"]
            for call in save_mock.call_args_list
            if call.kwargs.get("source") == "index_history"
        )
        self.assertEqual(len(saved_raw), 302)
        self.assertEqual(saved_raw.iloc[-1]["close"], 401)
        self.assertFalse(pd.isna(result.iloc[-1]["微盘股_MA20"]))

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.fetch_one_index")
    @patch("services.update_tasks.load_dataset")
    def test_existing_index_history_uses_short_incremental_request(self, load_dataset_mock, fetch_mock, _save_mock):
        cached_raw = pd.DataFrame(
            {
                "trade_date": pd.bdate_range("2025-01-01", periods=300),
                "close": range(100, 400),
            }
        )
        latest_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-09", "2026-07-10"]),
                "close": [400, 401],
            }
        )

        def load_side_effect(_symbol, source, _data_type):
            if source == INDEX_LONG_HISTORY_SOURCE:
                return None, None
            return cached_raw, {}

        load_dataset_mock.side_effect = load_side_effect
        fetch_mock.return_value = build_export_df(latest_raw, "微盘股", days=30)

        fetch_index_report("微盘股", {"source": "eastmoney_kline", "code": "90.BK1158"}, "", 365)

        self.assertEqual(fetch_mock.call_args.kwargs["days"], 30)

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.fetch_one_index")
    @patch("services.update_tasks.load_dataset")
    def test_daily_update_drops_open_session_row(self, load_dataset_mock, fetch_mock, save_mock):
        cached_raw = pd.DataFrame(
            {
                "trade_date": pd.bdate_range(end="2026-07-10", periods=300),
                "close": range(100, 400),
            }
        )
        fetched_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-10", "2026-07-13"]),
                "close": [401.0, 999.0],
            }
        )
        config = {"source": "yahoo", "code": "^TEST", "market_group": "A股"}

        class OpenSessionDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 13, 10, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        def load_side_effect(_symbol, source, _data_type):
            if source == "index_history":
                return cached_raw, {}
            return None, None

        load_dataset_mock.side_effect = load_side_effect
        fetch_mock.return_value = build_export_df(fetched_raw, "测试指数", days=30)
        with patch("services.index_ma20.datetime", OpenSessionDateTime):
            result = fetch_index_report("测试指数", config, "", 30)

        self.assertEqual(pd.to_datetime(result["日期"]).max(), pd.Timestamp("2026-07-10"))
        for call in save_mock.call_args_list:
            saved = call.kwargs.get("df")
            if saved is not None and "trade_date" in saved.columns:
                self.assertNotIn(pd.Timestamp("2026-07-13"), pd.to_datetime(saved["trade_date"]).tolist())

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.fetch_one_index")
    @patch("services.update_tasks.load_dataset")
    def test_post_close_rows_correct_report_without_overwriting_raw_cache(
        self,
        load_dataset_mock,
        fetch_mock,
        save_mock,
    ):
        cached_raw = pd.DataFrame(
            {
                "trade_date": pd.bdate_range(end="2026-07-13", periods=300),
                "close": [100.0] * 299 + [90.0],
            }
        )
        fetched_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-13", "2026-07-14"]),
                "close": [101.0, 102.0],
            }
        )
        config = {"source": "yahoo", "code": "^TEST", "market_group": "A股"}

        class ClosedSessionDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 14, 16, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        def load_side_effect(_symbol, source, _data_type):
            if source == "index_history":
                return cached_raw, {}
            return None, None

        load_dataset_mock.side_effect = load_side_effect
        fetch_mock.return_value = build_export_df(fetched_raw, "测试指数", days=30)
        with patch("services.index_ma20.datetime", ClosedSessionDateTime):
            result = fetch_index_report("测试指数", config, "", 30)

        latest = result.loc[result["日期"] == "2026-07-14", "测试指数_收盘价"].iloc[0]
        self.assertEqual(latest, 102.0)
        saved_by_source = {
            call.kwargs.get("source"): call.kwargs.get("df")
            for call in save_mock.call_args_list
            if call.kwargs.get("source")
        }
        raw_saved = saved_by_source["index_history"]
        finalized_saved = saved_by_source[INDEX_FINAL_HISTORY_SOURCE]
        self.assertEqual(
            raw_saved.loc[raw_saved["trade_date"] == pd.Timestamp("2026-07-13"), "close"].iloc[0],
            90.0,
        )
        self.assertEqual(
            finalized_saved.loc[finalized_saved["trade_date"] == pd.Timestamp("2026-07-13"), "close"].iloc[0],
            101.0,
        )

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.fetch_one_index")
    @patch("services.update_tasks.load_dataset")
    def test_crude_oil_update_uses_source_correction_without_rewriting_raw_cache(
        self,
        load_dataset_mock,
        fetch_mock,
        save_mock,
    ):
        cached_raw = pd.DataFrame(
            {
                "trade_date": pd.bdate_range(end="2026-07-14", periods=300),
                "close": [450.0] * 297 + [471.2, 482.6, 519.4],
            }
        )
        finalized = cached_raw.tail(3).copy()
        eastmoney_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-10", "2026-07-13", "2026-07-14"]),
                "close": [466.6, 478.0, 516.0],
            }
        )

        def load_side_effect(_symbol, source, _data_type):
            if source == "index_history":
                return cached_raw, {}
            if source == INDEX_FINAL_HISTORY_SOURCE:
                return finalized, {}
            return None, None

        load_dataset_mock.side_effect = load_side_effect
        fetch_mock.return_value = build_export_df(eastmoney_raw, "原油主连", days=30)

        result = fetch_index_report("原油主连", INDEX_CONFIG["原油主连"], "", 30)

        corrected = result.set_index("日期")["原油主连_收盘价"]
        self.assertEqual(corrected.loc["2026-07-10"], 466.6)
        self.assertEqual(corrected.loc["2026-07-14"], 516.0)
        saved_by_source = {
            call.kwargs["source"]: call.kwargs["df"]
            for call in save_mock.call_args_list
        }
        self.assertIn("index_source_correction_history", saved_by_source)
        self.assertNotIn("index_history", saved_by_source)

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.fetch_one_index")
    @patch("services.update_tasks.load_dataset")
    def test_same_day_finalization_does_not_rewrite_raw_history(
        self,
        load_dataset_mock,
        fetch_mock,
        save_mock,
    ):
        cached_raw = pd.DataFrame(
            {
                "trade_date": pd.bdate_range(end="2026-07-14", periods=300),
                "close": [100.0] * 299 + [90.0],
            }
        )
        fetched_raw = pd.DataFrame(
            {"trade_date": pd.to_datetime(["2026-07-14"]), "close": [102.0]}
        )
        config = {"source": "yahoo", "code": "^TEST", "market_group": "A股"}

        class ClosedSessionDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 14, 16, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        def load_side_effect(_symbol, source, _data_type):
            if source == "index_history":
                return cached_raw, {}
            return None, None

        load_dataset_mock.side_effect = load_side_effect
        fetch_mock.return_value = build_export_df(fetched_raw, "测试指数", days=30)
        with patch("services.index_ma20.datetime", ClosedSessionDateTime):
            result = fetch_index_report("测试指数", config, "", 30)

        self.assertEqual(result.iloc[-1]["测试指数_收盘价"], 102.0)
        saved_sources = [call.kwargs.get("source") for call in save_mock.call_args_list]
        self.assertIn(INDEX_FINAL_HISTORY_SOURCE, saved_sources)
        self.assertNotIn("index_history", saved_sources)

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.fetch_one_index")
    @patch("services.update_tasks.load_dataset", return_value=(None, None))
    def test_first_index_update_requests_history_for_ma20(self, _load_dataset, fetch_mock, _save_mock):
        raw = pd.DataFrame(
            {
                "trade_date": pd.date_range("2026-04-01", periods=80, freq="D"),
                "close": range(100, 180),
            }
        )
        fetch_mock.return_value = build_export_df(raw, "微盘股", days=1000)

        fetch_index_report("微盘股", {"source": "eastmoney_kline", "code": "90.BK1158"}, "", 30)

        self.assertEqual(fetch_mock.call_args.kwargs["days"], 1000)

    @patch("services.update_tasks.save_dataset")
    @patch("services.update_tasks.fetch_one_index")
    @patch("services.update_tasks.load_dataset", return_value=(None, None))
    def test_new_raw_cache_is_seeded_from_existing_report(self, _load_dataset, fetch_mock, save_mock):
        cached_raw = pd.DataFrame(
            {
                "trade_date": pd.date_range("2026-06-01", periods=40, freq="D"),
                "close": range(100, 140),
            }
        )
        cached_report = build_export_df(cached_raw, "微盘股", days=1000)
        latest_raw = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-07-11", "2026-07-12"]),
                "close": [140, 141],
            }
        )
        fetch_mock.return_value = build_export_df(latest_raw, "微盘股", days=1000)

        result = fetch_index_report(
            "微盘股",
            {"source": "eastmoney_kline", "code": "90.BK1158"},
            "",
            30,
            cached_report=cached_report,
        )

        saved_raw = save_mock.call_args.kwargs["df"]
        self.assertEqual(len(saved_raw), 42)
        self.assertFalse(pd.isna(result.iloc[-1]["微盘股_MA20"]))


if __name__ == "__main__":
    unittest.main()
