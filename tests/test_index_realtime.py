import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_ma20 import INDEX_CONFIG
from services.index_realtime import (
    _daily_update_target,
    _futures_market_is_open,
    _fetch_futures_quote,
    _infer_main_contract_symbol,
    _market_is_open,
    apply_realtime_quotes_to_summary,
    daily_update_eligible_index_names,
    fetch_futures_main_contract_names,
    format_index_display_name,
    find_final_close_quote_names,
    find_pending_post_close_index_names,
    load_runtime_realtime_quotes,
    manual_quote_request_names,
    quote_is_visible_for_manual_display,
    quote_is_active_for_display,
    remember_runtime_realtime_quotes,
)


class IndexRealtimeTests(unittest.TestCase):
    def test_new_indexes_are_in_requested_card_order(self):
        names = list(INDEX_CONFIG)

        self.assertEqual(names[0], "上证指数")
        self.assertEqual(names[names.index("微盘股") + 1], "科创50")
        self.assertEqual(
            names[names.index("铁矿石主连") - 2 : names.index("铁矿石主连")],
            ["中证500期货主连", "中证1000期货主连"],
        )

    def test_index_page_has_no_periodic_refresh(self):
        page_source = (Path(__file__).parents[1] / "pages" / "1_指数监控.py").read_text(encoding="utf-8")

        self.assertNotIn("run_every=", page_source)
        self.assertNotIn("盘中卡片每10分钟自动刷新", page_source)
        self.assertIn('[data-stale="true"]', page_source)
        self.assertIn("opacity: 1 !important", page_source)
        self.assertIn('display_day = market_now.date() if status == "休市" else latest_day', page_source)

    def test_manual_lunch_quote_is_requested_only_once_for_mainland_instruments(self):
        lunch_time = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        first_names, first_keys = manual_quote_request_names(set(), now=lunch_time)
        second_names, _ = manual_quote_request_names(set(first_keys.values()), now=lunch_time)

        self.assertIn("沪深300", first_names)
        self.assertIn("原油主连", first_names)
        self.assertNotIn("沪深300", second_names)
        self.assertNotIn("原油主连", second_names)

    def test_runtime_quote_cache_returns_an_isolated_copy(self):
        quote_time = datetime(2026, 7, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        remember_runtime_realtime_quotes(
            {
                "测试指数": {
                    "price": 123.45,
                    "quote_time": quote_time,
                    "source": "测试",
                }
            }
        )

        first_read = load_runtime_realtime_quotes()
        first_read["测试指数"]["price"] = 999.0
        second_read = load_runtime_realtime_quotes()

        self.assertEqual(second_read["测试指数"]["price"], 123.45)
        self.assertEqual(second_read["测试指数"]["quote_time"], quote_time)

    def test_lunch_quote_remains_visible_until_mainland_afternoon_session(self):
        lunch_time = datetime(2026, 7, 15, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        quote = {
            "price": 4000.0,
            "quote_time": datetime(2026, 7, 15, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        }

        self.assertTrue(quote_is_visible_for_manual_display("沪深300", quote, now=lunch_time))

    def test_nikkei_lunch_close_is_requested_once_during_japan_break(self):
        japan_lunch = datetime(2026, 7, 23, 10, 45, tzinfo=ZoneInfo("Asia/Shanghai"))

        first_names, first_keys = manual_quote_request_names(set(), now=japan_lunch)
        second_names, _ = manual_quote_request_names(set(first_keys.values()), now=japan_lunch)

        self.assertIn("日经225", first_names)
        self.assertEqual(first_keys["日经225"], "日经225:2026-07-23:lunch")
        self.assertNotIn("日经225", second_names)

    def test_nikkei_lunch_close_remains_visible_during_japan_break(self):
        japan_lunch = datetime(2026, 7, 23, 10, 45, tzinfo=ZoneInfo("Asia/Shanghai"))
        quote = {
            "price": 66424.44,
            "quote_time": datetime(2026, 7, 23, 11, 30, tzinfo=ZoneInfo("Asia/Tokyo")),
        }

        self.assertTrue(quote_is_visible_for_manual_display("日经225", quote, now=japan_lunch))

    def test_hong_kong_lunch_closes_are_requested_once(self):
        hong_kong_lunch = datetime(2026, 7, 23, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        first_names, first_keys = manual_quote_request_names(set(), now=hong_kong_lunch)
        second_names, _ = manual_quote_request_names(set(first_keys.values()), now=hong_kong_lunch)

        self.assertIn("恒生科技", first_names)
        self.assertIn("恒生港股通高息低波", first_names)
        self.assertNotIn("恒生科技", second_names)
        self.assertNotIn("恒生港股通高息低波", second_names)

    def test_futures_lunch_quote_remains_visible_until_afternoon_session(self):
        before_futures_reopen = datetime(2026, 7, 23, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        quote = {
            "price": 800.0,
            "quote_time": datetime(2026, 7, 23, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        }

        self.assertTrue(
            quote_is_visible_for_manual_display("铁矿石主连", quote, now=before_futures_reopen)
        )

    def test_index_futures_reopen_at_one_pm(self):
        lunch_time = datetime(2026, 7, 23, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        reopened = datetime(2026, 7, 23, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))

        lunch_names, _ = manual_quote_request_names(set(), now=lunch_time)

        self.assertIn("中证500期货主连", lunch_names)
        self.assertIn("中证1000期货主连", lunch_names)
        self.assertFalse(_futures_market_is_open("IC0", now=lunch_time))
        self.assertTrue(_futures_market_is_open("IC0", now=reopened))

    def test_previous_day_runtime_quote_is_hidden_during_current_session(self):
        current_session = datetime(2026, 7, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        stale_quote = {
            "price": 4000.0,
            "quote_time": datetime(2026, 7, 22, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        }

        self.assertFalse(
            quote_is_visible_for_manual_display("沪深300", stale_quote, now=current_session)
        )

    def test_main_contract_inference_matches_source_quote_instead_of_highest_position(self):
        contracts = pd.DataFrame(
            {
                "symbol": ["SC0", "SC2608", "SC2609"],
                "trade": [520.9, 516.1, 520.9],
                "position": [42872, 22820, 42872],
                "volume": [78755, 77928, 78755],
            }
        )

        eastmoney_contract = _infer_main_contract_symbol(contracts, "SC0", reference_price=516.1)
        sina_contract = _infer_main_contract_symbol(contracts, "SC0", reference_price=520.9)

        self.assertEqual(eastmoney_contract, "SC2608")
        self.assertEqual(sina_contract, "SC2609")

        same_price = contracts.copy()
        same_price.loc[same_price["symbol"] == "SC2609", "trade"] = 516.1
        same_price_contract = _infer_main_contract_symbol(
            same_price,
            "SC0",
            reference_price=516.1,
            reference_position=22820,
            reference_volume=77928,
        )
        self.assertEqual(same_price_contract, "SC2608")

    def test_futures_display_name_includes_cached_contract(self):
        contracts = {"铁矿石主连": "I2609"}

        self.assertEqual(format_index_display_name("铁矿石主连", contracts), "铁矿石主连（I2609）")
        self.assertEqual(format_index_display_name("沪深300", contracts), "沪深300")

    @patch("akshare.futures_zh_realtime")
    def test_contract_names_are_refreshed_from_successful_manual_quotes(self, realtime_mock):
        rows = {
            "中证500指数期货": [("IC0", 7545.4, 174182), ("IC2609", 7545.4, 174182)],
            "中证1000股指期货": [("IM0", 7008.0, 235495), ("IM2609", 7008.0, 235495)],
            "铁矿石": [("I0", 762.0, 536320), ("I2609", 762.0, 536320)],
            "黄金": [("AU0", 879.64, 113826), ("AU2608", 879.64, 113826)],
            "白银": [("AG0", 14247.0, 190224), ("AG2608", 14247.0, 190224)],
            "原油": [
                ("SC0", 520.9, 42872),
                ("SC2608", 516.1, 22820),
                ("SC2609", 520.9, 42872),
            ],
        }

        def side_effect(symbol):
            return pd.DataFrame(
                [
                    {"symbol": code, "trade": price, "position": position, "volume": 1}
                    for code, price, position in rows[symbol]
                ]
            )

        realtime_mock.side_effect = side_effect
        quotes = {
            "中证500期货主连": {"price": 7545.4},
            "中证1000期货主连": {"price": 7008.0},
            "铁矿石主连": {"price": 762.0},
            "沪金主连": {"price": 879.64},
            "沪银主连": {"price": 14247.0},
            "原油主连": {"price": 516.1},
        }

        contracts = fetch_futures_main_contract_names(quotes)

        self.assertEqual(
            contracts,
            {
                "中证500期货主连": "IC2609",
                "中证1000期货主连": "IM2609",
                "铁矿石主连": "I2609",
                "沪金主连": "AU2608",
                "沪银主连": "AG2608",
                "原油主连": "SC2608",
            },
        )

    def test_us_market_open_conversion_handles_standard_and_daylight_time(self):
        winter = datetime(2026, 1, 5, 22, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        summer = datetime(2026, 7, 14, 21, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertTrue(_market_is_open("美股", now=winter))
        self.assertTrue(_market_is_open("美股", now=summer))

    def test_apply_realtime_quote_updates_card_fields_without_mutating_source(self):
        summary = pd.DataFrame(
            [
                {
                    "指数": "沪深300",
                    "日期": "2026-07-13",
                    "收盘价": 4700.0,
                    "前收盘价": 4680.0,
                    "当日涨跌幅(%)": 0.43,
                }
            ]
        )
        summary["日期"] = summary["日期"].astype("string[pyarrow]")
        quote_time = datetime(2026, 7, 14, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        result = apply_realtime_quotes_to_summary(
            summary,
            {
                "沪深300": {
                    "price": 4770.0,
                    "previous_close": 4750.0,
                    "change_pct": None,
                    "quote_time": quote_time,
                    "source": "东方财富",
                }
            },
        )

        self.assertEqual(summary.loc[0, "收盘价"], 4700.0)
        self.assertEqual(result.loc[0, "收盘价"], 4770.0)
        self.assertEqual(result.loc[0, "前收盘价"], 4750.0)
        self.assertAlmostEqual(result.loc[0, "当日涨跌幅(%)"], (4770 / 4750 - 1) * 100)
        self.assertEqual(result.loc[0, "日期"], "2026-07-14")
        self.assertEqual(result.loc[0, "实时来源"], "东方财富")

    def test_market_open_respects_sessions(self):
        open_time = datetime(2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        lunch_time = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertTrue(_market_is_open("A股", now=open_time))
        self.assertFalse(_market_is_open("A股", now=lunch_time))

    def test_after_a_share_close_only_hong_kong_is_still_live(self):
        china_time = datetime(2026, 7, 14, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertFalse(_market_is_open("A股", now=china_time))
        self.assertTrue(_market_is_open("港股", now=china_time))
        self.assertFalse(_market_is_open("日本", now=china_time))
        self.assertFalse(_market_is_open("韩国", now=china_time))
        self.assertFalse(quote_is_active_for_display("原油主连", now=china_time))
        self.assertTrue(quote_is_active_for_display("恒生科技", now=china_time))

    def test_manual_quotes_include_nikkei_but_exclude_kospi_on_korean_holiday(self):
        china_time = datetime(2026, 7, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        names, _ = manual_quote_request_names(set(), now=china_time)

        self.assertIn("日经225", names)
        self.assertNotIn("韩国KOSPI", names)

    @patch("akshare.futures_zh_spot")
    def test_futures_quote_uses_cached_daily_close_for_change(self, spot_mock):
        spot_mock.return_value = pd.DataFrame(
            {
                "current_price": [519.4],
                "last_close": [519.4],
                "last_settle_price": [477.3],
            }
        )

        quote = _fetch_futures_quote("沪金主连", "AU0")

        self.assertEqual(quote["price"], 519.4)
        self.assertIsNone(quote["previous_close"])
        self.assertIsNone(quote["change_pct"])

    @patch("akshare.futures_zh_realtime")
    def test_index_futures_quote_uses_cffex_realtime_table(self, realtime_mock):
        realtime_mock.return_value = pd.DataFrame(
            [
                {
                    "symbol": "IC0",
                    "trade": 7545.4,
                    "preclose": 7611.0,
                    "volume": 64188,
                    "position": 174182,
                    "tradedate": "2026-07-23",
                    "ticktime": "11:07:59",
                }
            ]
        )

        quote = _fetch_futures_quote("中证500期货主连", "IC0")

        realtime_mock.assert_called_once_with(symbol="中证500指数期货")
        self.assertEqual(quote["price"], 7545.4)
        self.assertEqual(quote["previous_close"], 7611.0)
        self.assertAlmostEqual(quote["change_pct"], (7545.4 / 7611.0 - 1) * 100)
        self.assertEqual(quote["quote_time"].isoformat(), "2026-07-23T11:07:59+08:00")

    @patch("services.index_realtime._fetch_eastmoney_quote")
    def test_crude_oil_quote_uses_eastmoney_main_contract(self, eastmoney_mock):
        eastmoney_mock.return_value = {
            "price": 516.9,
            "previous_close": 516.0,
            "change_pct": 0.17,
            "quote_time": datetime(2026, 7, 15, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            "source": "东方财富",
        }

        quote = _fetch_futures_quote("原油主连", "SC0")

        eastmoney_mock.assert_called_once_with("原油主连", "142.scm")
        self.assertEqual(quote["price"], 516.9)
        self.assertEqual(quote["source"], "东方财富")

    def test_final_close_fetch_runs_once_after_each_market_closes(self):
        summary = pd.DataFrame({"指数": ["日经225", "韩国KOSPI", "沪深300", "标普500"]})
        china_time = datetime(2026, 7, 14, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))

        names, keys = find_final_close_quote_names(summary, set(), now=china_time)

        self.assertEqual(names, {"日经225", "韩国KOSPI"})
        self.assertEqual(keys, {"日经225:2026-07-14", "韩国KOSPI:2026-07-14"})
        names_after_attempt, _ = find_final_close_quote_names(summary, keys, now=china_time)
        self.assertEqual(names_after_attempt, set())

    def test_daily_update_waits_until_market_close_delay(self):
        before_delay = datetime(2026, 7, 14, 14, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
        after_delay = datetime(2026, 7, 14, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(_daily_update_target("日经225", now=before_delay).isoformat(), "2026-07-13")
        self.assertEqual(_daily_update_target("韩国KOSPI", now=before_delay).isoformat(), "2026-07-13")
        self.assertEqual(_daily_update_target("日经225", now=after_delay).isoformat(), "2026-07-14")
        self.assertEqual(_daily_update_target("韩国KOSPI", now=after_delay).isoformat(), "2026-07-14")

        self.assertEqual(_daily_update_target("沪深300", now=after_delay).isoformat(), "2026-07-13")

    def test_hong_kong_daily_update_starts_ten_minutes_after_close(self):
        before_delay = datetime(2026, 7, 15, 16, 7, tzinfo=ZoneInfo("Asia/Shanghai"))
        after_delay = datetime(2026, 7, 15, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(_daily_update_target("恒生科技", now=before_delay).isoformat(), "2026-07-14")
        self.assertEqual(
            _daily_update_target("恒生港股通高息低波", now=before_delay).isoformat(),
            "2026-07-14",
        )
        self.assertEqual(_daily_update_target("恒生科技", now=after_delay).isoformat(), "2026-07-15")
        self.assertEqual(
            _daily_update_target("恒生港股通高息低波", now=after_delay).isoformat(),
            "2026-07-15",
        )

    @patch("services.index_realtime.load_dataset")
    def test_current_session_still_updates_missing_previous_close(self, load_dataset_mock):
        def load_side_effect(_symbol, _source, _data_type):
            return pd.DataFrame({"trade_date": ["2026-07-15"], "close": [100.0]}), {}

        load_dataset_mock.side_effect = load_side_effect
        current_session = datetime(2026, 7, 17, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        pending = find_pending_post_close_index_names(
            now=current_session,
            index_names={"沪深300", "恒生科技", "日经225", "韩国KOSPI"},
        )

        self.assertEqual(pending, {"沪深300", "恒生科技", "日经225", "韩国KOSPI"})

    @patch("services.index_realtime.load_dataset")
    def test_pending_daily_update_uses_finalized_cache_date(self, load_dataset_mock):
        def load_side_effect(symbol, _source, _data_type):
            latest = "2026-07-13" if "日经225" in symbol else "2026-07-14"
            return pd.DataFrame({"trade_date": [latest], "close": [100.0]}), {}

        load_dataset_mock.side_effect = load_side_effect
        china_time = datetime(2026, 7, 14, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))

        pending = find_pending_post_close_index_names(
            now=china_time,
            index_names={"日经225", "韩国KOSPI"},
        )

        self.assertEqual(pending, {"日经225"})

    @patch("services.index_realtime.load_dataset")
    def test_pending_daily_update_detects_recent_interior_gap(self, load_dataset_mock):
        cached = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(
                    ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-07"]
                ),
                "close": [100.0, 101.0, 102.0, 104.0],
            }
        )
        load_dataset_mock.return_value = (cached, {})
        after_close = datetime(2026, 8, 7, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        pending = find_pending_post_close_index_names(
            now=after_close,
            index_names={"日经225", "韩国KOSPI"},
        )

        self.assertEqual(pending, {"日经225", "韩国KOSPI"})

    @patch("services.index_realtime.load_dataset")
    def test_crude_oil_update_remains_pending_when_source_correction_is_missing(self, load_dataset_mock):
        def load_side_effect(_symbol, source, _data_type):
            if source == "index_final_history":
                return pd.DataFrame({"trade_date": ["2026-07-14"], "close": [519.4]}), {}
            return None, None

        load_dataset_mock.side_effect = load_side_effect
        after_close = datetime(2026, 7, 14, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        pending = find_pending_post_close_index_names(
            now=after_close,
            index_names={"原油主连"},
        )

        self.assertEqual(pending, {"原油主连"})

    def test_futures_night_session_is_open(self):
        night_time = datetime(2026, 7, 14, 21, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        after_iron_ore_close = datetime(2026, 7, 14, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        section_break = datetime(2026, 7, 14, 10, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
        closed_time = datetime(2026, 7, 14, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertTrue(_futures_market_is_open("I0", now=night_time))
        self.assertTrue(quote_is_active_for_display("原油主连", now=night_time))
        self.assertFalse(_futures_market_is_open("I0", now=after_iron_ore_close))
        self.assertTrue(_futures_market_is_open("AU0", now=after_iron_ore_close))
        self.assertFalse(_futures_market_is_open("I0", now=section_break))
        self.assertFalse(_futures_market_is_open("I0", now=closed_time))

    @patch("services.index_realtime._fetch_futures_quote")
    @patch("services.index_realtime._fetch_yahoo_quote")
    @patch("services.index_realtime._fetch_eastmoney_quote")
    def test_china_day_session_does_not_request_us_quotes(self, eastmoney, yahoo, futures):
        from services.index_realtime import fetch_realtime_index_quotes

        eastmoney.return_value = None
        yahoo.return_value = None
        futures.return_value = None
        china_day = datetime(2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        fetch_realtime_index_quotes(now=china_day, max_workers=1)

        requested_eastmoney = {call.args[0] for call in eastmoney.call_args_list}
        self.assertNotIn("标普500", requested_eastmoney)
        self.assertNotIn("纳斯达克100", requested_eastmoney)
        yahoo.assert_not_called()
        self.assertEqual(futures.call_count, 6)

    @patch("services.index_realtime._fetch_futures_quote")
    @patch("services.index_realtime._fetch_yahoo_quote")
    @patch("services.index_realtime._fetch_eastmoney_quote")
    def test_us_session_does_not_request_closed_asian_markets(self, eastmoney, yahoo, futures):
        from services.index_realtime import fetch_realtime_index_quotes

        eastmoney.return_value = None
        yahoo.return_value = None
        futures.return_value = None
        china_night = datetime(2026, 7, 14, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        fetch_realtime_index_quotes(now=china_night, max_workers=1)

        requested_eastmoney = {call.args[0] for call in eastmoney.call_args_list}
        self.assertEqual(requested_eastmoney, {"标普500", "纳斯达克100"})
        self.assertEqual(yahoo.call_count, 2)
        self.assertEqual(futures.call_count, 4)

    @patch("services.index_realtime._fetch_futures_quote")
    @patch("services.index_realtime._fetch_yahoo_quote")
    @patch("services.index_realtime._fetch_eastmoney_quote")
    def test_forced_final_fetch_requests_only_named_indexes(self, eastmoney, yahoo, futures):
        from services.index_realtime import fetch_realtime_index_quotes

        eastmoney.return_value = None
        yahoo.return_value = None
        futures.return_value = None

        fetch_realtime_index_quotes(force_index_names={"日经225"}, max_workers=1)

        self.assertEqual([call.args[0] for call in eastmoney.call_args_list], ["日经225"])
        yahoo.assert_not_called()
        futures.assert_not_called()

    @patch("services.index_realtime._fetch_futures_quote", side_effect=RuntimeError("futures down"))
    @patch("services.index_realtime._fetch_yahoo_quote", side_effect=RuntimeError("yahoo down"))
    @patch("services.index_realtime._fetch_eastmoney_quote", side_effect=RuntimeError("eastmoney down"))
    def test_realtime_batch_returns_empty_when_all_network_sources_fail(self, _eastmoney, _yahoo, _futures):
        from services.index_realtime import fetch_realtime_index_quotes

        china_day = datetime(2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        result = fetch_realtime_index_quotes(now=china_day, max_workers=2)

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
