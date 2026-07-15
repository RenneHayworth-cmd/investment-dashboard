import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from services.futures_options_analysis import FUTURES_OPTION_DATA_VERSION
from services.futures_spread import SPREAD_CALCULATION_VERSION
from services.position_analysis import (
    DEFAULT_ETF_CODES,
    ETF_DISPLAY_NAMES,
    ETF_TIMING_STRATEGIES,
    PositionItem,
    _merge_by_date,
    build_etf_timing_table,
    calculate_etf_timing_snapshot,
    etf_final_close_ready,
    filter_final_etf_rows,
    latest_final_etf_trade_date,
    load_or_fetch_etf,
    load_or_fetch_option,
    load_or_fetch_spread,
)


class PositionAnalysisTests(unittest.TestCase):
    def test_default_etf_list_includes_new_holdings(self):
        self.assertEqual(
            DEFAULT_ETF_CODES,
            [
                "512890",
                "159201",
                "159545",
                "513260",
                "159655",
                "159501",
                "518850",
                "588000",
                "159915",
                "510500",
            ],
        )
        self.assertEqual(ETF_TIMING_STRATEGIES["510500"], (20, 1.0))
        self.assertNotIn("512890", ETF_TIMING_STRATEGIES)
        self.assertNotIn("518850", ETF_TIMING_STRATEGIES)
        self.assertEqual(ETF_DISPLAY_NAMES["159915"], "创业板ETF易方达")
        self.assertEqual(set(ETF_DISPLAY_NAMES), set(DEFAULT_ETF_CODES))

    def test_timing_snapshot_retains_state_inside_band_and_marks_transitions(self):
        dates = pd.date_range("2026-07-01", periods=7, freq="D")
        prices = [100.0, 100.0, 100.0, 104.0, 103.0, 100.0, 100.0]
        data = pd.DataFrame({"date": dates, "price": prices})

        held = calculate_etf_timing_snapshot(data.iloc[:5], ma_period=3, threshold_pct=1.0)
        sold = calculate_etf_timing_snapshot(data.iloc[:6], ma_period=3, threshold_pct=1.0)
        empty = calculate_etf_timing_snapshot(data, ma_period=3, threshold_pct=1.0)

        self.assertEqual(held["择时判断"], "持有")
        self.assertEqual(held["状态转换时间"], "2026-07-04")
        self.assertEqual(sold["择时判断"], "卖出")
        self.assertEqual(sold["状态转换时间"], "2026-07-06")
        self.assertEqual(empty["择时判断"], "空仓")
        self.assertEqual(empty["状态转换时间"], "2026-07-06")

    def test_etf_final_date_changes_at_1505(self):
        before = datetime(2026, 7, 15, 15, 4, tzinfo=ZoneInfo("Asia/Shanghai"))
        ready = datetime(2026, 7, 15, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertFalse(etf_final_close_ready(before))
        self.assertEqual(latest_final_etf_trade_date(before).isoformat(), "2026-07-14")
        self.assertTrue(etf_final_close_ready(ready))
        self.assertEqual(latest_final_etf_trade_date(ready).isoformat(), "2026-07-15")

    def test_unconfirmed_same_day_cache_is_excluded_after_1505(self):
        market_now = datetime(2026, 7, 15, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        cached = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-07-14", "2026-07-15"]),
                "收盘价": [1.0, 1.1],
            }
        )

        filtered = filter_final_etf_rows(
            cached,
            market_now=market_now,
            require_current_confirmation=True,
        )
        confirmed = cached.assign(_final_close_confirmed=[False, True])
        confirmed_filtered = filter_final_etf_rows(
            confirmed,
            market_now=market_now,
            require_current_confirmation=True,
        )

        self.assertEqual(filtered["日期"].max(), pd.Timestamp("2026-07-14"))
        self.assertEqual(confirmed_filtered["日期"].max(), pd.Timestamp("2026-07-15"))

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_intraday_etf_refresh_updates_item_without_saving(self, load_mock, fetch_mock, save_mock):
        cached_dates = pd.bdate_range(end="2026-07-14", periods=30)
        cached = pd.DataFrame(
            {
                "日期": cached_dates,
                "收盘价": [1.0 + index / 100 for index in range(len(cached_dates))],
                "symbol": "512890.SH",
                "name": "红利低波ETF华泰柏瑞",
            }
        )
        latest = pd.concat(
            [
                cached.tail(5),
                pd.DataFrame(
                    {
                        "日期": [pd.Timestamp("2026-07-15")],
                        "收盘价": [1.5],
                        "symbol": ["512890.SH"],
                        "name": ["红利低波ETF华泰柏瑞"],
                    }
                ),
            ],
            ignore_index=True,
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-07-14T16:00:00"})
        fetch_mock.return_value = latest

        item = load_or_fetch_etf(
            "512890",
            force_refresh=True,
            save_to_cache=True,
            allow_unfinished_session=True,
            market_now=datetime(2026, 7, 15, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(item.latest_date, "2026-07-15")
        save_mock.assert_not_called()

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_post_close_refresh_replaces_unconfirmed_same_day_row(self, load_mock, fetch_mock, save_mock):
        cached_dates = pd.bdate_range(end="2026-07-15", periods=30)
        cached = pd.DataFrame(
            {
                "日期": cached_dates,
                "收盘价": [1.0] * 29 + [1.1],
                "symbol": "512890.SH",
                "name": "红利低波ETF华泰柏瑞",
            }
        )
        latest = cached.tail(5).copy()
        latest.loc[latest["日期"] == pd.Timestamp("2026-07-15"), "收盘价"] = 1.2
        load_mock.return_value = (cached, {"last_update_time": "2026-07-15T14:30:00"})
        fetch_mock.return_value = latest

        item = load_or_fetch_etf(
            "512890",
            force_refresh=True,
            save_to_cache=True,
            market_now=datetime(2026, 7, 15, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        saved = save_mock.call_args.kwargs["df"]
        saved_today = saved.loc[saved["日期"] == pd.Timestamp("2026-07-15")].iloc[0]
        self.assertEqual(saved_today["收盘价"], 1.2)
        self.assertTrue(saved_today["_final_close_confirmed"])
        self.assertEqual(item.latest_date, "2026-07-15")
        self.assertEqual(item.metrics["最新价"], 1.2)

    @patch("services.position_analysis._load_dataset_if_ready")
    def test_strategy_ma_keeps_precision_for_three_decimal_display(self, load_mock):
        dates = pd.bdate_range(end="2026-07-14", periods=30)
        prices = [1.8] * 5 + [1.901] * 24 + [1.949]
        cached = pd.DataFrame(
            {
                "日期": dates,
                "收盘价": prices,
                "symbol": "159655.SZ",
                "name": "标普500ETF华夏",
            }
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-07-14T16:00:00"})

        item = load_or_fetch_etf("159655", allow_fetch=False)

        self.assertAlmostEqual(item.metrics["策略均线"], 1.90292, places=6)

    def test_etf_timing_table_excludes_derivatives_and_blanks_long_term_strategy(self):
        items = [
            PositionItem(
                "ETF",
                "512890.SH",
                "红利低波ETF",
                "缓存",
                metrics={"最新价": 1.2345, "日涨跌(%)": 0.5},
            ),
            PositionItem(
                "ETF",
                "513260.SH",
                "接口名称",
                "缓存",
                metrics={
                    "最新价": 1.1,
                    "日涨跌(%)": -0.2,
                    "策略参数": "MA20 / 1.0%",
                    "策略均线": 1.05,
                    "策略偏离(%)": 4.76,
                    "择时判断": "持有",
                    "状态转换时间": "2026-07-01",
                    "策略区间涨幅(%)": 3.0,
                },
            ),
            PositionItem("期货价差", "I2609-I2701", "铁矿石价差", "缓存"),
            PositionItem("期权", "I2609P730", "铁矿石看跌期权", "缓存"),
        ]

        table = build_etf_timing_table(items)

        self.assertEqual(table["代码"].tolist(), ["513260", "512890"])
        self.assertEqual(table.iloc[0]["ETF名称"], "恒生科技ETF汇添富")
        self.assertTrue(pd.isna(table.iloc[1]["策略参数"]))
        self.assertTrue(pd.isna(table.iloc[1]["对应均线"]))
        self.assertNotIn("期货价差", table.to_string())

    def test_missing_timed_etf_still_shows_configured_strategy(self):
        table = build_etf_timing_table(
            [PositionItem("ETF", "510500", "510500.SH", "无缓存")]
        )

        self.assertEqual(table.iloc[0]["ETF名称"], "中证500ETF南方")
        self.assertEqual(table.iloc[0]["策略参数"], "MA20 / 1.0%")
        self.assertTrue(pd.isna(table.iloc[0]["对应均线"]))

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
        self.assertEqual(item.name, "i2609P730 铁矿石看跌期权")
        self.assertEqual(len(item.dataframe), 3)
        same_day = item.dataframe.loc[item.dataframe["date"] == pd.Timestamp("2026-07-02")]
        self.assertEqual(same_day["close"].iloc[0], 11.0)
        saved = save_mock.call_args.kwargs["df"]
        self.assertEqual(len(saved), 3)
        self.assertTrue(saved["_data_version"].eq(FUTURES_OPTION_DATA_VERSION).all())

    @patch("services.position_analysis._load_dataset_if_ready")
    def test_supported_call_option_uses_its_actual_product_and_side_name(self, load_mock):
        cached = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
                "close": [100.0, 101.0],
                "volume": [10, 11],
                "open_interest": [20, 21],
                "_data_version": FUTURES_OPTION_DATA_VERSION,
            }
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-07-02T16:00:00"})

        item = load_or_fetch_option("MO2609C5800", allow_fetch=False)

        self.assertEqual(item.name, "mo2609C5800 中证1000股指看涨期权")

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
