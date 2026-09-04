import inspect
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from components.position.formatting import format_etf_table_value
from services import position_analysis as position_facade
from services import position_models, position_runtime

from services.fund_analysis import (
    FUND_ADJUST_BACKWARD_ADDITIVE,
    FUND_ADJUST_FORWARD_ADDITIVE,
    stamp_fund_history_metadata,
)

from services.futures_options_analysis import (
    FUTURES_OPTION_DATA_VERSION,
    append_option_spot_row,
    fetch_option_from_akshare,
)
from services.futures_spread import (
    SPREAD_CALCULATION_VERSION,
    append_futures_spot_row,
    fetch_futures_daily,
    fetch_futures_daily_from_akshare,
)
from services.position_analysis import (
    DEFAULT_ETF_CODES,
    DEFAULT_FUTURES_CONTRACTS,
    DEFAULT_OPTION_CODES,
    DEFAULT_SPREAD_CONTRACTS,
    DEFAULT_SPREAD_GROUPS,
    ETF_DISPLAY_NAMES,
    ETF_MIDSESSION_TIMING_REFRESH_SECONDS,
    ETF_MORNING_TIMING_REFRESH_SECONDS,
    ETF_PORTFOLIO_WEIGHTS_PCT,
    ETF_POSITION_STRATEGIES,
    ETF_REALTIME_TIMING_REFRESH_SECONDS,
    ETF_TIMING_STRATEGIES,
    POSITION_INDEX_TIMING_STRATEGIES,
    PositionItem,
    _adjusted_history_has_overlap_changes,
    _append_sina_final_close,
    _fetch_eastmoney_exchange_fund_close,
    _fetch_sina_exchange_fund_close,
    _fetch_sina_exchange_fund_final_close,
    _fetch_sina_exchange_fund_quote,
    _request_sina_realtime_snapshot,
    _merge_by_date,
    _merge_current_day_refresh,
    apply_etf_realtime_quote,
    apply_etf_realtime_quotes_to_items,
    build_position_timing_performance,
    apply_etf_realtime_quote_to_timing,
    build_recent_etf_operation_guidance,
    build_etf_timing_table,
    build_position_index_timing_table,
    calculate_512890_parking_snapshot,
    calculate_etf_timing_snapshot,
    etf_afternoon_timing_fetch_ready,
    etf_cache_has_latest_final_close,
    etf_final_close_ready,
    etf_intraday_quote_ready,
    etf_lunch_timing_fetch_ready,
    etf_lunch_timing_preview_ready,
    etf_morning_timing_fetch_ready,
    etf_morning_timing_preview_ready,
    etf_realtime_timing_ready,
    etf_position_decision,
    fetch_tickflow_etf_quotes,
    filter_current_etf_realtime_quotes,
    filter_final_etf_rows,
    latest_final_etf_trade_date,
    load_runtime_etf_quotes,
    load_runtime_etf_quote_state,
    load_or_fetch_etf,
    load_or_fetch_futures_contract,
    load_or_fetch_option,
    load_or_fetch_spread,
    parse_spread_groups,
    refresh_position_derivative_items,
    refresh_runtime_etf_quotes,
    remember_runtime_etf_quotes,
)
from services.position_performance import (
    _build_current_positions,
    _normalize_trade_display_names,
)


def _v2_adjusted_history(df: pd.DataFrame) -> pd.DataFrame:
    return stamp_fund_history_metadata(df, FUND_ADJUST_FORWARD_ADDITIVE)


class PositionAnalysisTests(unittest.TestCase):
    @staticmethod
    def _position_performance_items() -> list[PositionItem]:
        buy_codes = {"159501", "518850", "510500", "513310"}
        hold_codes = {"159201", "159655", "159552", "513880"}
        empty_codes = {"159545", "159967", "512890"}
        warmup_dates = pd.bdate_range(end="2026-08-04", periods=40)
        valuation_dates = pd.to_datetime(["2026-08-05", "2026-08-06"])
        items: list[PositionItem] = []
        for code in [*ETF_PORTFOLIO_WEIGHTS_PCT, "512890"]:
            warmup_prices = [100.0] * len(warmup_dates)
            if code in hold_codes:
                warmup_prices[-1] = 120.0
                valuation_prices = [120.0, 120.0]
            elif code in buy_codes:
                valuation_prices = [120.0, 120.0]
            elif code in empty_codes:
                valuation_prices = [100.0, 100.0]
            else:
                raise AssertionError(code)
            frame = pd.DataFrame(
                {
                    "date": warmup_dates.append(pd.DatetimeIndex(valuation_dates)),
                    "price": warmup_prices + valuation_prices,
                }
            )
            items.append(
                PositionItem(
                    "ETF",
                    code,
                    ETF_DISPLAY_NAMES[code],
                    "缓存",
                    latest_date="2026-08-06",
                    dataframe=frame,
                    formal_history_valid=True,
                )
            )
        return items

    def test_position_performance_builds_only_fresh_buy_initial_positions(self):
        result = build_position_timing_performance(
            self._position_performance_items(),
            market_now=datetime(2026, 8, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(result.summary["初始建仓代码"], ["159501", "510500", "513310", "518850"])
        first_day_trades = result.trades[
            pd.to_datetime(result.trades["日期"]).dt.normalize()
            == pd.Timestamp("2026-08-05")
        ]
        self.assertEqual(
            set(first_day_trades["交易标的"]),
            {"159501", "510500", "513310", "518850"},
        )
        self.assertNotIn("512890", set(first_day_trades["交易标的"]))
        self.assertFalse((result.trades["代码"] == "159967").any())
        first = result.daily.iloc[0]
        self.assertEqual(first["账户资产"], 500_000.0)
        self.assertEqual(first["每日盈亏"], 0.0)
        self.assertEqual(first["每日收益率(%)"], 0.0)
        self.assertEqual(first["净值"], 1.0)
        self.assertGreater(result.summary["初始手续费"], 0)
        self.assertEqual(
            set(result.positions["代码"]),
            {"159501", "510500", "513310", "518850"},
        )
        self.assertNotIn("512890", set(result.positions["代码"]))
        self.assertAlmostEqual(
            float(result.positions["持仓市值"].sum()),
            float(result.daily.iloc[-1]["持仓市值"]),
            places=2,
        )
        self.assertTrue(
            {"当日盈亏", "当日收益率(%)", "当日收益基数"}.issubset(
                result.positions.columns
            )
        )
        self.assertAlmostEqual(
            float(result.positions["当日盈亏"].sum()),
            float(result.daily.iloc[-1]["每日盈亏"]),
            places=2,
        )

    def test_position_performance_stops_before_first_incomplete_formal_session(self):
        result = build_position_timing_performance(
            self._position_performance_items(),
            market_now=datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(result.summary["正式数据截止日"], "2026-08-06")
        self.assertTrue(any("2026-08-07 正式日线不完整" in item for item in result.warnings))

    def test_position_performance_keeps_upstream_confirmed_same_day_close(self):
        result = build_position_timing_performance(
            self._position_performance_items(),
            market_now=datetime(2026, 8, 6, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.summary["正式数据截止日"], "2026-08-06")
        self.assertEqual(len(result.daily), 2)

    def test_position_performance_rejects_invalid_adjusted_cache(self):
        items = self._position_performance_items()
        items[0].formal_history_valid = False

        result = build_position_timing_performance(
            items,
            market_now=datetime(2026, 8, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertTrue(result.daily.empty)
        self.assertIn("前复权缓存校验未通过", result.errors[0])

    def test_position_performance_parking_position_uses_full_fund_name(self):
        trades = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-08-06"]),
                "代码": ["510500"],
                "交易标的": ["512890"],
                "交易标的名称": ["512890"],
                "操作": ["买入"],
                "份额": [100],
                "成交金额": [100.0],
                "手续费": [0.01],
            }
        )
        positions = _build_current_positions(
            trades,
            {
                "512890": pd.DataFrame(
                    {"trade_date": pd.to_datetime(["2026-08-06"]), "close": [1.0]}
                )
            },
            valuation_date=pd.Timestamp("2026-08-06"),
            account_assets=500_000.0,
        )

        self.assertEqual(positions.iloc[0]["代码"], "512890")
        self.assertEqual(positions.iloc[0]["基金名称"], "红利低波ETF华泰柏瑞")

    def test_position_performance_trade_detail_uses_full_parking_fund_name(self):
        trades = _normalize_trade_display_names(
            pd.DataFrame(
                {
                    "交易标的": ["512890"],
                    "交易标的名称": ["512890"],
                    "操作": ["买入"],
                }
            )
        )

        self.assertEqual(trades.iloc[0]["交易标的"], "512890")
        self.assertEqual(trades.iloc[0]["交易标的名称"], "红利低波ETF华泰柏瑞")

    def test_adjusted_overlap_change_detection_distinguishes_append_from_rebuild(self):
        old = _v2_adjusted_history(
            pd.DataFrame(
                {
                    "日期": pd.to_datetime(["2026-08-10", "2026-08-11"]),
                    "收盘价": [1.20, 1.21],
                }
            )
        )
        unchanged = _v2_adjusted_history(
            pd.DataFrame(
                {
                    "日期": pd.to_datetime(["2026-08-11", "2026-08-12"]),
                    "收盘价": [1.21, 1.22],
                }
            )
        )
        changed = unchanged.copy()
        changed.loc[changed["日期"] == pd.Timestamp("2026-08-11"), "收盘价"] = 1.196

        self.assertFalse(_adjusted_history_has_overlap_changes(old, unchanged))
        self.assertTrue(_adjusted_history_has_overlap_changes(old, changed))

    def test_161128_maps_additive_and_unadjusted_modes_to_akshare(self):
        calls = []

        def fake_history(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame(
                {
                    "日期": ["2026-08-11", "2026-08-12"],
                    "开盘": [1.0, 1.1],
                    "收盘": [1.1, 1.2],
                }
            )

        fake_akshare = SimpleNamespace(fund_etf_hist_em=fake_history)
        with patch.dict("sys.modules", {"akshare": fake_akshare}):
            for mode, expected in (
                ("forward_additive", "qfq"),
                ("backward_additive", "hfq"),
                ("none", ""),
            ):
                result = _fetch_eastmoney_exchange_fund_close(
                    symbol="161128.SZ",
                    count=2,
                    adjust=mode,
                )
                self.assertEqual(calls[-1]["adjust"], expected)
                self.assertTrue(result["_adjust_mode"].eq(mode).all())

            with self.assertRaisesRegex(ValueError, "比例复权"):
                _fetch_eastmoney_exchange_fund_close(
                    symbol="161128.SZ",
                    count=2,
                    adjust="forward",
                )

    def test_default_spread_uses_current_iron_ore_contracts(self):
        self.assertEqual(DEFAULT_SPREAD_CONTRACTS, ["I2701", "I2705"])

    def test_default_spread_groups_include_iron_ore_and_csi_1000(self):
        self.assertEqual(
            DEFAULT_SPREAD_GROUPS,
            [["I2701", "I2705"], ["IM2609", "IM2703"]],
        )

    def test_parse_spread_groups_keeps_each_line_independent(self):
        self.assertEqual(
            parse_spread_groups("I2609 I2705\nIM2609 IM2703"),
            [["I2609", "I2705"], ["IM2609", "IM2703"]],
        )

    def test_spread_refresh_replaces_only_current_day(self):
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
        prior_day = today - pd.Timedelta(days=1)
        cached = pd.DataFrame(
            {
                "date": [prior_day, today],
                "IM2609_close": [7400.0, 7438.8],
                "IM2703_close": [7070.0, 7080.0],
                "spread_IM2609_vs_IM2703": [330.0, 358.8],
            }
        )
        refreshed = pd.DataFrame(
            {
                "date": [prior_day, today],
                "IM2609_close": [7401.0, 7451.8],
                "IM2703_close": [7071.0, 7087.8],
                "spread_IM2609_vs_IM2703": [330.0, 364.0],
            }
        )

        merged = _merge_current_day_refresh(cached, refreshed, "date")

        self.assertEqual(merged.loc[merged["date"] == prior_day, "IM2609_close"].iloc[0], 7400.0)
        self.assertEqual(merged.loc[merged["date"] == today, "IM2609_close"].iloc[0], 7451.8)
        self.assertEqual(
            merged.loc[merged["date"] == today, "spread_IM2609_vs_IM2703"].iloc[0],
            364.0,
        )

    @patch("services.position_analysis._load_dataset_if_ready", return_value=(None, None))
    def test_missing_default_spread_keeps_full_contract_names(self, _load_mock):
        item = load_or_fetch_spread(DEFAULT_SPREAD_CONTRACTS, allow_fetch=False)

        self.assertEqual(item.code, "I2701 - I2705")
        self.assertEqual(item.name, "I2701 - I2705 (铁矿石)")

    @patch("services.position_analysis._load_dataset_if_ready", return_value=(None, None))
    def test_default_derivative_holdings_use_iron_ore_futures_without_options(self, _load_mock):
        item = load_or_fetch_futures_contract(DEFAULT_FUTURES_CONTRACTS[0], allow_fetch=False)

        self.assertEqual(DEFAULT_FUTURES_CONTRACTS, ["I2701"])
        self.assertEqual(DEFAULT_OPTION_CODES, [])
        self.assertEqual(item.code, "I2701")
        self.assertEqual(item.name, "I2701 (铁矿石期货)")

    @patch("services.position_analysis._load_dataset_if_ready", return_value=(None, None))
    def test_missing_csi_1000_spread_keeps_full_contract_names(self, _load_mock):
        item = load_or_fetch_spread(["IM2609", "IM2703"], allow_fetch=False)

        self.assertEqual(item.code, "IM2609 - IM2703")
        self.assertEqual(item.name, "IM2609 - IM2703 (中证1000股指)")

    def test_position_page_uses_one_week_operation_guidance(self):
        root = Path(__file__).parents[1]
        page_source = (root / "pages" / "5_持仓分析.py").read_text(
            encoding="utf-8"
        )
        realtime_source = (root / "components" / "position" / "realtime.py").read_text(
            encoding="utf-8"
        )
        coordinator_source = (
            root / "components" / "position" / "coordinator.py"
        ).read_text(encoding="utf-8")

        self.assertIn('st.subheader("近一周操作指引")', realtime_source)
        self.assertIn("build_etf_timing_table(timing_items)", realtime_source)
        self.assertIn("preview_refresh_due", realtime_source)
        self.assertIn("morning_refresh_due", realtime_source)
        self.assertIn('morning_refresh_band = (', realtime_source)
        self.assertIn('lunch_refresh_band = "afternoon"', realtime_source)
        self.assertIn("apply_etf_realtime_quotes_to_items", realtime_source)
        self.assertIn("filter_current_etf_realtime_quotes(", realtime_source)
        self.assertIn("retain_after_close=True", realtime_source)
        self.assertIn("and formal_close_missing", realtime_source)
        self.assertIn("allow_close_retention=(", realtime_source)
        self.assertIn("if timing_preview_window and active_preview_quotes:", realtime_source)
        self.assertIn("refresh_position_derivative_items", realtime_source)
        self.assertIn(
            'derivative_state_key = "position_derivative_realtime_preview"',
            realtime_source,
        )
        self.assertIn("derivative_preview_version = 3", realtime_source)
        self.assertIn('st.text_area(\n                "期货持仓"', coordinator_source)
        self.assertNotIn('st.text_area("期权持仓"', coordinator_source)
        self.assertIn("render_position_cards(card_items)", realtime_source)
        self.assertIn(
            "本次实时更新时间为：{realtime_update_text}", realtime_source
        )
        self.assertIn("show_cache_caption=not update_clicked", coordinator_source)
        self.assertIn(
            '@st.fragment(run_every=f"{ETF_REALTIME_TIMING_REFRESH_SECONDS}s")',
            page_source,
        )
        self.assertIn(
            "build_recent_etf_operation_guidance(formal_items, days=7)",
            realtime_source,
        )
        self.assertNotIn("近一月操作指引", realtime_source)

    def test_etf_table_formatter_accepts_dash_in_numeric_columns(self):
        self.assertEqual(format_etf_table_value("对应均线", "-"), "-")
        self.assertEqual(format_etf_table_value("最新价", 1.9), "1.900")
        self.assertEqual(format_etf_table_value("最新收盘", 3760.75), "3760.75")
        self.assertEqual(format_etf_table_value("当日涨跌幅(%)", 1.234), "1.23")

    def test_recent_operation_guidance_lists_pure_and_half_position_transitions(self):
        data = pd.DataFrame(
            {
                "date": pd.date_range("2026-07-01", periods=7, freq="D"),
                "price": [100.0, 100.0, 100.0, 104.0, 103.0, 100.0, 100.0],
            }
        )
        pure = PositionItem("ETF", "513260.SH", "恒生科技ETF汇添富", "缓存", dataframe=data)
        half = PositionItem("ETF", "159501.SZ", "纳指ETF嘉实", "缓存", dataframe=data)

        with patch.dict(
            ETF_TIMING_STRATEGIES,
            {"513260": (3, 1.0), "159501": (3, 1.0)},
            clear=True,
        ):
            result = build_recent_etf_operation_guidance([pure, half], days=4)

        self.assertEqual(
            set(result["操作指引"]),
            {"买入", "卖出", "加至满仓", "降至半仓"},
        )
        half_rows = result[result["代码"] == "159501"]
        self.assertEqual(set(half_rows["操作后仓位"]), {"持有", "半仓"})
        self.assertEqual(result["日期"].min(), "2026-07-04")

    def test_recent_operation_guidance_adds_512890_buy_for_active_transfer_source(self):
        source_data = pd.DataFrame(
            {
                "date": pd.date_range("2026-07-01", periods=7, freq="D"),
                "price": [100.0, 100.0, 100.0, 104.0, 103.0, 100.0, 100.0],
            }
        )
        parking_data = pd.DataFrame(
            {
                "date": pd.date_range("2026-07-01", periods=7, freq="D"),
                "price": [1.10, 1.11, 1.12, 1.13, 1.14, 1.15, 1.16],
            }
        )
        source = PositionItem("ETF", "510500.SH", "中证500ETF南方", "缓存", dataframe=source_data)
        parking = PositionItem("ETF", "512890.SH", "红利低波ETF华泰柏瑞", "缓存", dataframe=parking_data)

        with patch.dict(ETF_TIMING_STRATEGIES, {"510500": (3, 1.0)}, clear=True):
            result = build_recent_etf_operation_guidance([source, parking], days=4)

        parking_rows = result[result["代码"] == "512890"]
        self.assertEqual(len(parking_rows), 1)
        self.assertEqual(parking_rows.iloc[0]["操作指引"], "买入")
        self.assertEqual(parking_rows.iloc[0]["策略参数"], "承接510500空仓资金")
        self.assertEqual(parking_rows.iloc[0]["触发收盘价"], 1.15)

    def test_half_timing_position_decisions_keep_the_base_half(self):
        self.assertEqual(ETF_POSITION_STRATEGIES["159501"], "半仓持有半仓择时")
        self.assertEqual(etf_position_decision("159501", "买入"), "加至满仓")
        self.assertEqual(etf_position_decision("159501", "持有"), "持有")
        self.assertEqual(etf_position_decision("159501", "卖出"), "降至半仓")
        self.assertEqual(etf_position_decision("159501", "空仓"), "半仓")
        self.assertEqual(etf_position_decision("159655", "空仓"), "半仓")
        self.assertEqual(etf_position_decision("159201", "空仓"), "空仓")
        self.assertEqual(etf_position_decision("159545", "空仓"), "空仓")
        self.assertEqual(etf_position_decision("518850", "空仓"), "空仓")
        self.assertEqual(etf_position_decision("513260", "空仓"), "空仓")

    def test_runtime_etf_quote_cache_returns_an_isolated_copy(self):
        remember_runtime_etf_quotes(
            {
                "159201.SZ": {
                    "price": 1.234,
                    "quote_time": datetime(2026, 7, 21, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
                }
            }
        )

        loaded = load_runtime_etf_quotes()
        loaded["159201"]["price"] = 9.999

        self.assertEqual(load_runtime_etf_quotes()["159201"]["price"], 1.234)

    def test_current_etf_quotes_survive_same_day_reruns_only(self):
        timezone = ZoneInfo("Asia/Shanghai")
        quotes = {
            "159201": {
                "price": 1.234,
                "quote_time": datetime(2026, 7, 21, 10, 30, tzinfo=timezone),
            },
            "588000": {
                "price": 1.111,
                "quote_time": datetime(2026, 7, 20, 14, 30, tzinfo=timezone),
            },
        }

        intraday = filter_current_etf_realtime_quotes(
            quotes,
            market_now=datetime(2026, 7, 21, 11, 0, tzinfo=timezone),
        )
        after_close = filter_current_etf_realtime_quotes(
            quotes,
            market_now=datetime(2026, 7, 21, 15, 5, tzinfo=timezone),
        )
        retained_after_close = filter_current_etf_realtime_quotes(
            quotes,
            market_now=datetime(2026, 7, 21, 15, 5, tzinfo=timezone),
            retain_after_close=True,
        )

        self.assertEqual(set(intraday), {"159201"})
        self.assertEqual(after_close, {})
        self.assertEqual(set(retained_after_close), {"159201"})

    def test_position_page_stays_clear_while_data_is_loading(self):
        root = Path(__file__).parents[1]
        page_source = (root / "pages" / "5_持仓分析.py").read_text(
            encoding="utf-8"
        )
        cards_source = (
            root / "components" / "position" / "cards_tables.py"
        ).read_text(encoding="utf-8")

        self.assertIn('[data-stale="true"]', page_source)
        self.assertIn("opacity: 1 !important", page_source)
        self.assertIn("position-operation-guidance-table", cards_source)
        self.assertIn('action in {"买入", "加至满仓"}', cards_source)
        self.assertIn('action in {"卖出", "降至半仓"}', cards_source)

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
                "161128",
                "518850",
                "588000",
                "159915",
                "510500",
                "159967",
                "159552",
                "513310",
                "513880",
            ],
        )
        self.assertEqual(ETF_TIMING_STRATEGIES["510500"], (15, 1.0))
        self.assertEqual(ETF_TIMING_STRATEGIES["159201"], (20, 0.5))
        self.assertEqual(ETF_TIMING_STRATEGIES["588000"], (20, 1.0))
        self.assertEqual(ETF_TIMING_STRATEGIES["159967"], (25, 2.0))
        self.assertEqual(ETF_TIMING_STRATEGIES["518850"], (30, 1.5))
        self.assertEqual(ETF_TIMING_STRATEGIES["161128"], (25, 1.5))
        self.assertEqual(ETF_TIMING_STRATEGIES["159552"], (10, 2.5))
        self.assertEqual(ETF_TIMING_STRATEGIES["513310"], (15, 0.5))
        self.assertEqual(ETF_TIMING_STRATEGIES["513880"], (10, 2.0))
        self.assertEqual(ETF_POSITION_STRATEGIES["161128"], "纯择时")
        self.assertEqual(ETF_POSITION_STRATEGIES["159552"], "纯择时")
        self.assertEqual(ETF_POSITION_STRATEGIES["513310"], "纯择时")
        self.assertEqual(ETF_POSITION_STRATEGIES["513880"], "纯择时")
        self.assertNotIn("512890", ETF_TIMING_STRATEGIES)
        self.assertEqual(ETF_DISPLAY_NAMES["159915"], "创业板ETF易方达")
        self.assertEqual(ETF_DISPLAY_NAMES["161128"], "标普信息科技LOF易方达")
        self.assertEqual(ETF_DISPLAY_NAMES["159967"], "创业板成长ETF华夏")
        self.assertEqual(ETF_DISPLAY_NAMES["159552"], "中证2000增强ETF招商")
        self.assertEqual(ETF_DISPLAY_NAMES["513310"], "中韩半导体ETF华泰柏瑞")
        self.assertEqual(ETF_DISPLAY_NAMES["513880"], "日经225ETF华安")
        self.assertEqual(set(ETF_DISPLAY_NAMES), set(DEFAULT_ETF_CODES))

    @patch("services.position_analysis.load_dataset")
    def test_index_timing_reference_uses_formal_index_caches(self, load_mock):
        micro_dates = pd.bdate_range("2026-01-05", periods=20)
        csi_dates = pd.bdate_range("2026-01-05", periods=30)
        histories = {
            "index_raw_微盘股": pd.DataFrame(
                {"trade_date": micro_dates, "close": [100.0] * len(micro_dates)}
            ),
            "index_raw_000905.SH": pd.DataFrame(
                {
                    "trade_date": csi_dates,
                    "close": [100.0] * 14 + [103.0] * 10 + [95.0] * 6,
                }
            ),
        }
        finalized = {
            symbol: pd.DataFrame(
                {
                    "trade_date": [history["trade_date"].max() + pd.offsets.BDay(1)],
                    "close": [104.0 if symbol == "index_raw_微盘股" else 95.0],
                }
            )
            for symbol, history in histories.items()
        }

        def load_side_effect(symbol, source, data_type, period="1d"):
            del data_type, period
            if source == "index_history":
                return histories.get(symbol), {"last_trade_date": "2026-02-17"}
            if source == "index_final_history":
                return finalized.get(symbol), {"last_trade_date": "2026-02-18"}
            return None, None

        load_mock.side_effect = load_side_effect

        table = build_position_index_timing_table().set_index("指数名称")

        self.assertEqual(
            POSITION_INDEX_TIMING_STRATEGIES,
            {
                "微盘股": {"code": "BK1158", "ma_period": 15, "threshold_pct": 2.5},
                "中证500": {"code": "000905", "ma_period": 15, "threshold_pct": 1.0},
            },
        )
        self.assertEqual(table.index.tolist(), ["微盘股指数", "中证500"])
        self.assertEqual(table.loc["微盘股指数", "代码"], "BK1158")
        self.assertEqual(table.loc["微盘股指数", "策略参数"], "MA15 / 2.5%")
        self.assertEqual(table.loc["微盘股指数", "择时判断"], "买入")
        self.assertEqual(table.loc["中证500", "策略参数"], "MA15 / 1.0%")
        self.assertEqual(table.loc["中证500", "择时判断"], "空仓")
        self.assertTrue((table["数据状态"] == "正式收盘缓存").all())

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
        self.assertEqual(sold["上一状态转换时间"], "2026-07-04")
        self.assertAlmostEqual(sold["策略上一区间涨幅(%)"], (100.0 / 104.0 - 1) * 100)
        self.assertEqual(empty["择时判断"], "空仓")
        self.assertEqual(empty["状态转换时间"], "2026-07-06")
        self.assertEqual(empty["上一状态转换时间"], "2026-07-04")
        self.assertAlmostEqual(empty["策略上一区间涨幅(%)"], (100.0 / 104.0 - 1) * 100)

    def test_etf_final_date_changes_at_1505(self):
        before = datetime(2026, 7, 15, 15, 4, tzinfo=ZoneInfo("Asia/Shanghai"))
        ready = datetime(2026, 7, 15, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertFalse(etf_final_close_ready(before))
        self.assertEqual(latest_final_etf_trade_date(before).isoformat(), "2026-07-14")
        self.assertTrue(etf_final_close_ready(ready))
        self.assertEqual(latest_final_etf_trade_date(ready).isoformat(), "2026-07-15")

    def test_etf_cache_freshness_uses_latest_confirmed_close_for_each_market_phase(self):
        timezone = ZoneInfo("Asia/Shanghai")
        friday_cache = pd.DataFrame({"日期": [pd.Timestamp("2026-07-24")]})
        monday_cache = pd.DataFrame({"日期": [pd.Timestamp("2026-07-27")]})

        self.assertTrue(
            etf_cache_has_latest_final_close(
                friday_cache,
                market_now=datetime(2026, 7, 27, 10, 0, tzinfo=timezone),
            )
        )
        self.assertFalse(
            etf_cache_has_latest_final_close(
                friday_cache,
                market_now=datetime(2026, 7, 27, 15, 5, tzinfo=timezone),
            )
        )
        self.assertTrue(
            etf_cache_has_latest_final_close(
                monday_cache,
                market_now=datetime(2026, 7, 27, 15, 5, tzinfo=timezone),
            )
        )

    def test_etf_intraday_quote_window_excludes_preopen_postclose_and_holidays(self):
        timezone = ZoneInfo("Asia/Shanghai")

        self.assertFalse(etf_intraday_quote_ready(datetime(2026, 7, 15, 9, 29, tzinfo=timezone)))
        self.assertTrue(etf_intraday_quote_ready(datetime(2026, 7, 15, 10, 0, tzinfo=timezone)))
        self.assertTrue(etf_intraday_quote_ready(datetime(2026, 7, 15, 12, 0, tzinfo=timezone)))
        self.assertTrue(etf_intraday_quote_ready(datetime(2026, 7, 15, 15, 4, tzinfo=timezone)))
        self.assertFalse(etf_intraday_quote_ready(datetime(2026, 7, 15, 15, 5, tzinfo=timezone)))
        self.assertFalse(etf_intraday_quote_ready(datetime(2026, 7, 18, 10, 0, tzinfo=timezone)))

    @patch("tickflow.TickFlow")
    def test_fetch_tickflow_etf_quotes_uses_realtime_endpoint_and_rejects_stale_rows(self, tickflow_mock):
        current_timestamp = int(
            datetime(2026, 7, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
        )
        stale_timestamp = int(
            datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
        )
        tickflow_mock.return_value.quotes.get.return_value = pd.DataFrame(
            [
                {
                    "symbol": "512890.SH",
                    "last_price": 1.2,
                    "prev_close": 1.1,
                    "timestamp": current_timestamp,
                    "ext.change_pct": 0.09,
                },
                {
                    "symbol": "159201.SZ",
                    "last_price": 1.3,
                    "prev_close": 1.25,
                    "timestamp": stale_timestamp,
                },
            ]
        )

        quotes = fetch_tickflow_etf_quotes(
            ["512890", "159201"],
            api_key="test-key",
            market_now=datetime(2026, 7, 15, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        tickflow_mock.return_value.quotes.get.assert_called_once_with(
            symbols=["512890.SH", "159201.SZ"],
            as_dataframe=True,
        )
        self.assertEqual(list(quotes), ["512890"])
        self.assertAlmostEqual(quotes["512890"]["change_pct"], 9.090909, places=5)

    @patch("tickflow.TickFlow")
    def test_fetch_tickflow_etf_quotes_splits_requests_into_batches_of_five(self, tickflow_mock):
        current_timestamp = int(
            datetime(2026, 7, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
        )

        def build_batch(*, symbols, as_dataframe):
            self.assertTrue(as_dataframe)
            return pd.DataFrame(
                [
                    {
                        "symbol": symbol,
                        "last_price": 1.2,
                        "prev_close": 1.1,
                        "timestamp": current_timestamp,
                    }
                    for symbol in symbols
                ]
            )

        tickflow_mock.return_value.quotes.get.side_effect = build_batch
        quotes = fetch_tickflow_etf_quotes(
            DEFAULT_ETF_CODES,
            api_key="test-key",
            market_now=datetime(2026, 7, 15, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        request_sizes = [
            len(call.kwargs["symbols"])
            for call in tickflow_mock.return_value.quotes.get.call_args_list
        ]
        self.assertEqual(request_sizes, [5, 5, 5])
        self.assertEqual(set(quotes), set(DEFAULT_ETF_CODES))

    def test_runtime_quote_batch_is_shared_and_only_refetches_for_new_scope(self):
        market_now = datetime(2026, 7, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        def quotes_for(codes, **_kwargs):
            return {
                code: {
                    "symbol": code,
                    "price": 1.0,
                    "quote_time": market_now,
                }
                for code in codes
            }

        position_runtime._RUNTIME_ETF_QUOTE_CACHE.clear()
        position_runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear()
        with patch(
            "services.position_runtime.fetch_tickflow_etf_quotes",
            side_effect=quotes_for,
        ) as fetch_mock:
            first = refresh_runtime_etf_quotes(
                ["159501", "518850"], api_key="key", market_now=market_now
            )
            second = refresh_runtime_etf_quotes(
                ["159501"], api_key="key", market_now=market_now
            )
            expanded = refresh_runtime_etf_quotes(
                ["159501", "510500"], api_key="key", market_now=market_now
            )

        self.assertEqual(fetch_mock.call_count, 2)
        self.assertEqual(set(first), {"159501", "518850"})
        self.assertEqual(set(second), {"159501"})
        self.assertEqual(set(expanded), {"159501", "510500"})
        self.assertEqual(load_runtime_etf_quote_state()["band"], "上午")

    def test_failed_first_lunch_fetch_retries_after_ten_minutes(self):
        morning_now = datetime(2026, 7, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        first_lunch = datetime(2026, 7, 15, 11, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
        retry_lunch = datetime(2026, 7, 15, 11, 41, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

        def quotes_for(codes, *, market_now, **_kwargs):
            return {
                code: {"symbol": code, "price": 1.0, "quote_time": market_now}
                for code in codes
            }

        position_runtime._RUNTIME_ETF_QUOTE_CACHE.clear()
        position_runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear()
        with patch(
            "services.position_runtime.fetch_tickflow_etf_quotes",
            side_effect=quotes_for,
        ):
            refresh_runtime_etf_quotes(
                ["159501"], api_key="key", market_now=morning_now
            )
        with patch(
            "services.position_runtime.fetch_tickflow_etf_quotes",
            side_effect=RuntimeError("lunch failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "lunch failed"):
                refresh_runtime_etf_quotes(
                    ["159501"], api_key="key", market_now=first_lunch
                )
        with patch(
            "services.position_runtime.fetch_tickflow_etf_quotes",
            side_effect=quotes_for,
        ) as retry_mock:
            result = refresh_runtime_etf_quotes(
                ["159501"], api_key="key", market_now=retry_lunch
            )

        retry_mock.assert_called_once()
        self.assertEqual(result["159501"]["quote_time"], retry_lunch)
        state = load_runtime_etf_quote_state()
        self.assertEqual(state["last_success_band"], "午间")
        self.assertEqual(state["last_success_trade_date"], "2026-07-15")

    @patch("services.position_analysis._fetch_sina_exchange_fund_quote")
    @patch("tickflow.TickFlow")
    def test_fetch_tickflow_etf_quotes_uses_sina_when_lof_is_missing(
        self,
        tickflow_mock,
        sina_quote_mock,
    ):
        market_now = datetime(
            2026, 8, 4, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        timestamp = int(market_now.timestamp() * 1000)
        tickflow_mock.return_value.quotes.get.return_value = pd.DataFrame(
            [
                {
                    "symbol": "512890.SH",
                    "last_price": 1.2,
                    "prev_close": 1.1,
                    "timestamp": timestamp,
                }
            ]
        )
        sina_quote_mock.return_value = {
            "symbol": "161128.SZ",
            "price": 6.815,
            "previous_close": 6.770,
            "change_pct": 0.6647,
            "quote_time": market_now,
        }

        quotes = fetch_tickflow_etf_quotes(
            ["512890", "161128"],
            api_key="test-key",
            market_now=market_now,
        )

        self.assertEqual(set(quotes), {"512890", "161128"})
        sina_quote_mock.assert_called_once_with(
            symbol="161128.SZ",
            market_now=market_now,
        )

    def test_apply_etf_realtime_quote_updates_card_only(self):
        cached_data = pd.DataFrame({"date": pd.to_datetime(["2026-07-14"]), "price": [1.1]})
        cached_item = PositionItem(
            "ETF",
            "512890.SH",
            "红利低波ETF华泰柏瑞",
            "缓存",
            source="本地缓存",
            latest_date="2026-07-14",
            metrics={"最新价": 1.1, "日涨跌(%)": 0.0, "20日涨跌(%)": 2.5},
            dataframe=cached_data,
        )
        quote_time = datetime(2026, 7, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        updated = apply_etf_realtime_quote(
            cached_item,
            {
                "symbol": "512890.SH",
                "price": 1.2,
                "previous_close": 1.1,
                "change_pct": 0.09,
                "quote_time": quote_time,
            },
        )

        self.assertEqual(updated.status, "盘中")
        self.assertEqual(updated.latest_date, "2026-07-15")
        self.assertEqual(updated.metrics["最新价"], 1.2)
        self.assertAlmostEqual(updated.metrics["日涨跌(%)"], 9.09, places=2)
        self.assertEqual(updated.metrics["20日涨跌(%)"], 2.5)
        self.assertTrue(updated.dataframe.equals(cached_data))

    def test_realtime_quote_batch_updates_etf_card_without_timing_strategy(self):
        quote_time = datetime(2026, 7, 15, 9, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
        long_term_etf = PositionItem(
            "ETF",
            "512890.SH",
            "红利低波ETF华泰柏瑞",
            "缓存",
            metrics={"最新价": 1.1, "日涨跌(%)": 0.0},
            dataframe=pd.DataFrame({"date": [pd.Timestamp("2026-07-14")], "price": [1.1]}),
        )
        spread = PositionItem(
            "期货价差",
            "I2609 - I2705",
            "I2609 - I2705 (铁矿石)",
            "缓存",
            dataframe=pd.DataFrame({"date": [pd.Timestamp("2026-07-14")]}),
        )

        updated = apply_etf_realtime_quotes_to_items(
            [long_term_etf, spread],
            {
                "512890": {
                    "symbol": "512890.SH",
                    "price": 1.2,
                    "previous_close": 1.1,
                    "quote_time": quote_time,
                }
            },
        )

        self.assertNotIn("512890", ETF_TIMING_STRATEGIES)
        self.assertEqual(updated[0].status, "盘中")
        self.assertEqual(updated[0].metrics["最新价"], 1.2)
        self.assertIs(updated[1], spread)

    @patch("services.position_analysis.load_or_fetch_option")
    @patch("services.position_analysis.load_or_fetch_spread")
    @patch("services.position_analysis.load_or_fetch_futures_contract")
    def test_derivative_realtime_refresh_is_transient(
        self,
        futures_mock,
        spread_mock,
        option_mock,
    ):
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
        futures_item = PositionItem(
            "期货",
            "I2609",
            "I2609 (铁矿石期货)",
            "缓存",
            dataframe=pd.DataFrame({"date": [today]}),
        )
        spread_item = PositionItem(
            "期货价差",
            "I2609 - I2705",
            "I2609 - I2705 (铁矿石)",
            "缓存",
            dataframe=pd.DataFrame({"date": [today]}),
        )
        option_item = PositionItem(
            "期权",
            "i2609P730",
            "i2609P730 铁矿石看跌期权",
            "缓存",
            dataframe=pd.DataFrame({"date": [today]}),
        )
        futures_mock.return_value = PositionItem(
            "期货",
            futures_item.code,
            futures_item.name,
            "已增量更新",
            latest_date=today.strftime("%Y-%m-%d"),
            dataframe=futures_item.dataframe,
        )
        spread_mock.return_value = PositionItem(
            "期货价差",
            spread_item.code,
            spread_item.name,
            "已增量更新",
            latest_date=today.strftime("%Y-%m-%d"),
            dataframe=spread_item.dataframe,
        )
        option_mock.return_value = PositionItem(
            "期权",
            option_item.code,
            option_item.name,
            "已增量更新",
            latest_date=today.strftime("%Y-%m-%d"),
            dataframe=option_item.dataframe,
        )

        refreshed, errors = refresh_position_derivative_items(
            [futures_item, spread_item, option_item],
            api_key="test-key",
            max_workers=2,
            option_count=500,
        )

        self.assertEqual(len(refreshed), 3)
        self.assertEqual(errors, [])
        self.assertFalse(futures_mock.call_args.kwargs["save_to_cache"])
        self.assertFalse(spread_mock.call_args.kwargs["save_to_cache"])
        self.assertFalse(option_mock.call_args.kwargs["save_to_cache"])
        self.assertTrue(futures_mock.call_args.kwargs["realtime_preview"])
        self.assertTrue(spread_mock.call_args.kwargs["realtime_preview"])
        self.assertTrue(option_mock.call_args.kwargs["realtime_preview"])
        self.assertEqual(spread_mock.call_args.args[0], ["I2609", "I2705"])

    @patch("akshare.futures_zh_spot")
    def test_futures_spot_preview_replaces_same_day_daily_value(self, spot_mock):
        today = pd.Timestamp("2026-08-07")
        market_now = datetime(2026, 8, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        history = pd.DataFrame({"date": [today], "close": [710.0]})
        spot_mock.return_value = pd.DataFrame({"current_price": [719.0]})

        with patch("services.futures_spread._today_china", return_value=today):
            unchanged = append_futures_spot_row(history, "I2609", market_now=market_now)
            preview = append_futures_spot_row(
                history,
                "I2609",
                replace_current_day=True,
                market_now=market_now,
            )

        self.assertEqual(unchanged.iloc[-1]["close"], 710.0)
        self.assertEqual(preview.iloc[-1]["close"], 719.0)

    @patch("services.futures_spread._fetch_futures_spot_from_sina_direct")
    @patch("akshare.futures_zh_spot")
    def test_futures_spot_falls_back_to_direct_sina_after_proxy_failure(
        self,
        spot_mock,
        direct_mock,
    ):
        market_now = datetime(2026, 8, 25, 9, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
        history = pd.DataFrame(
            {"date": [pd.Timestamp("2026-08-24")], "close": [729.5]}
        )
        spot_mock.side_effect = requests.exceptions.ProxyError("dead proxy")
        direct_mock.return_value = pd.DataFrame(
            {
                "current_price": [727.0],
                "quote_date": ["2026-08-25"],
                "quote_time": ["09:40:00"],
            }
        )

        result = append_futures_spot_row(
            history,
            "I2609",
            replace_current_day=True,
            market_now=market_now,
        )

        self.assertEqual(result.iloc[-1]["date"], pd.Timestamp("2026-08-25"))
        self.assertEqual(result.iloc[-1]["close"], 727.0)

    @patch("services.futures_spread._fetch_futures_spot_from_sina_direct")
    @patch("akshare.futures_zh_spot")
    def test_futures_spot_rejects_stale_direct_snapshot(
        self,
        spot_mock,
        direct_mock,
    ):
        market_now = datetime(2026, 8, 25, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        history = pd.DataFrame(
            {"date": [pd.Timestamp("2026-08-24")], "close": [7425.8]}
        )
        spot_mock.side_effect = requests.exceptions.ProxyError("dead proxy")
        direct_mock.return_value = pd.DataFrame(
            {
                "current_price": [7425.8],
                "quote_date": ["2026-08-24"],
                "quote_time": ["15:00:00"],
            }
        )

        result = append_futures_spot_row(
            history,
            "IM2609",
            replace_current_day=True,
            market_now=market_now,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[-1]["date"], pd.Timestamp("2026-08-24"))

    @patch("services.futures_spread._fetch_futures_daily_from_sina_direct")
    @patch("akshare.futures_zh_daily_sina")
    def test_futures_daily_falls_back_to_direct_sina_after_proxy_failure(
        self,
        daily_mock,
        direct_mock,
    ):
        daily_mock.side_effect = requests.exceptions.ProxyError("dead proxy")
        direct_mock.return_value = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-21", "2026-08-24"]),
                "close": [719.5, 729.5],
            }
        )

        result = fetch_futures_daily_from_akshare("I2609")

        self.assertEqual(result.iloc[-1]["date"], pd.Timestamp("2026-08-24"))
        self.assertEqual(result.iloc[-1]["close"], 729.5)

    @patch("akshare.option_commodity_contract_table_sina")
    def test_option_spot_preview_replaces_same_day_daily_value(self, chain_mock):
        today = pd.Timestamp("2026-08-07")
        market_now = datetime(2026, 8, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        history = pd.DataFrame({"date": [today], "close": [16.0]})
        chain_mock.return_value = pd.DataFrame(
            {
                "看跌合约-看跌期权合约": ["i2609P730"],
                "看跌合约-最新价": [17.5],
                "看跌合约-持仓量": [7000],
            }
        )

        with patch("services.futures_options_analysis._today_china", return_value=today):
            unchanged = append_option_spot_row(history, "i2609P730", market_now=market_now)
            preview = append_option_spot_row(
                history,
                "i2609P730",
                replace_current_day=True,
                market_now=market_now,
            )

        self.assertEqual(unchanged.iloc[-1]["close"], 16.0)
        self.assertEqual(preview.iloc[-1]["close"], 17.5)
        self.assertEqual(preview.iloc[-1]["open_interest"], 7000)

    @patch("services.position_analysis.fetch_contracts")
    @patch("services.position_analysis.append_futures_spot_row")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_spread_realtime_preview_uses_cached_history_and_spot_only(
        self,
        load_mock,
        spot_mock,
        fetch_mock,
    ):
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
        prior_day = today - pd.Timedelta(days=1)
        cached = pd.DataFrame(
            {
                "date": [prior_day],
                "I2701_close": [710.0],
                "I2705_close": [695.0],
                "spread_I2701_vs_I2705": [15.0],
                "spread_I2701_vs_I2705_pct": [2.1127],
                "_calculation_version": [SPREAD_CALCULATION_VERSION],
            }
        )
        load_mock.return_value = (cached, {"last_update_time": prior_day.isoformat()})

        def add_spot(df, contract, *, replace_current_day=False, market_now=None):
            price = 719.0 if contract == "I2701" else 698.0
            return pd.concat(
                [df, pd.DataFrame({"date": [today], "close": [price]})],
                ignore_index=True,
            )

        spot_mock.side_effect = add_spot

        item = load_or_fetch_spread(
            ["I2701", "I2705"],
            force_refresh=True,
            save_to_cache=False,
            realtime_preview=True,
        )

        fetch_mock.assert_not_called()
        self.assertEqual(spot_mock.call_count, 2)
        self.assertEqual(item.latest_date, today.strftime("%Y-%m-%d"))
        self.assertEqual(item.metrics["最新价差"], 21.0)

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_futures_daily")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_futures_contract_formal_refresh_appends_without_overwriting_history(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        cached = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-06", "2026-08-07"]),
                "close": [700.0, 705.0],
            }
        )
        fetched = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-07", "2026-08-10"]),
                "close": [999.0, 710.0],
            }
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-08-07T16:00:00"})
        fetch_mock.return_value = fetched

        item = load_or_fetch_futures_contract(
            "I2609",
            force_refresh=True,
            save_to_cache=True,
            market_now=datetime(2026, 8, 10, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(item.category, "期货")
        self.assertEqual(item.latest_date, "2026-08-10")
        self.assertEqual(item.metrics["最新收盘"], 710.0)
        self.assertEqual(
            item.dataframe.loc[item.dataframe["date"] == pd.Timestamp("2026-08-07"), "close"].iloc[0],
            705.0,
        )
        save_mock.assert_called_once()
        self.assertEqual(save_mock.call_args.kwargs["symbol"], "futures_contract_I2609")
        self.assertEqual(save_mock.call_args.kwargs["data_type"], "futures_contract")

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_futures_daily")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_futures_contract_stale_formal_response_is_not_saved_as_success(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        cached = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-21"]), "close": [719.5]}
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-08-21T16:00:00"})
        fetch_mock.return_value = cached.copy()

        item = load_or_fetch_futures_contract(
            "I2609",
            force_refresh=True,
            save_to_cache=True,
            market_now=datetime(2026, 8, 24, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(item.status, "缓存")
        self.assertIn("正式日线最新到 2026-08-21", item.error)
        self.assertIn("预期 2026-08-24", item.error)
        save_mock.assert_not_called()

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_futures_daily")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_futures_contract_accepts_previous_completed_day_during_session(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        cached = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-21"]), "close": [719.5]}
        )
        fetched = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-21", "2026-08-24"]),
                "close": [719.5, 729.5],
            }
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-08-21T16:00:00"})
        fetch_mock.return_value = fetched

        item = load_or_fetch_futures_contract(
            "I2609",
            force_refresh=True,
            save_to_cache=True,
            market_now=datetime(2026, 8, 25, 9, 40, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(item.status, "已增量更新")
        self.assertEqual(item.latest_date, "2026-08-24")
        save_mock.assert_called_once()

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_futures_daily")
    @patch("services.position_analysis.append_futures_spot_row")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_futures_contract_realtime_preview_uses_spot_without_saving(
        self,
        load_mock,
        spot_mock,
        fetch_mock,
        save_mock,
    ):
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
        prior_day = today - pd.Timedelta(days=1)
        cached = pd.DataFrame({"date": [prior_day], "close": [705.0]})
        load_mock.return_value = (cached, {"last_update_time": prior_day.isoformat()})
        spot_mock.return_value = pd.concat(
            [cached, pd.DataFrame({"date": [today], "close": [719.0]})],
            ignore_index=True,
        )

        item = load_or_fetch_futures_contract(
            "I2609",
            force_refresh=True,
            save_to_cache=True,
            realtime_preview=True,
        )

        fetch_mock.assert_not_called()
        save_mock.assert_not_called()
        self.assertEqual(item.latest_date, today.strftime("%Y-%m-%d"))
        self.assertEqual(item.metrics["最新收盘"], 719.0)

    @patch("services.position_analysis.fetch_futures_option_data")
    @patch("services.position_analysis.append_option_spot_row")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_option_realtime_preview_uses_cached_history_and_spot_only(
        self,
        load_mock,
        spot_mock,
        fetch_mock,
    ):
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
        prior_day = today - pd.Timedelta(days=1)
        cached = pd.DataFrame(
            {
                "date": [prior_day],
                "close": [16.0],
                "open_interest": [6800.0],
                "_data_version": [FUTURES_OPTION_DATA_VERSION],
            }
        )
        load_mock.return_value = (cached, {"last_update_time": prior_day.isoformat()})
        spot_mock.return_value = pd.concat(
            [
                cached,
                pd.DataFrame(
                    {
                        "date": [today],
                        "close": [17.5],
                        "open_interest": [7000.0],
                    }
                ),
            ],
            ignore_index=True,
        )

        item = load_or_fetch_option(
            "I2609P730",
            force_refresh=True,
            save_to_cache=False,
            realtime_preview=True,
        )

        fetch_mock.assert_not_called()
        self.assertEqual(item.latest_date, today.strftime("%Y-%m-%d"))
        self.assertEqual(item.metrics["最新收盘"], 17.5)
        self.assertEqual(item.metrics["最新持仓量"], 7000.0)

    @patch("services.futures_spread.append_futures_spot_row")
    @patch("services.futures_spread.fetch_futures_daily_from_akshare")
    @patch("services.futures_spread.fetch_futures_daily_from_tickflow")
    def test_formal_futures_daily_does_not_append_intraday_snapshot(
        self, tickflow_mock, akshare_mock, spot_mock
    ):
        history = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-06", "2026-08-07", "2026-08-10"]), "close": [700, 705, 710]}
        )
        tickflow_mock.return_value = history
        akshare_mock.return_value = history
        market_now = datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        result = fetch_futures_daily("I2609", market_now=market_now)

        spot_mock.assert_not_called()
        self.assertEqual(result["date"].max(), pd.Timestamp("2026-08-07"))

    @patch("services.futures_options_analysis.append_option_spot_row")
    def test_formal_option_daily_does_not_append_intraday_snapshot(self, spot_mock):
        history = pd.DataFrame(
            {"date": ["2026-08-07", "2026-08-10"], "close": [16.0, 17.0]}
        )
        fake_akshare = SimpleNamespace(
            option_commodity_hist_sina=lambda symbol: history,
        )
        market_now = datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch.dict("sys.modules", {"akshare": fake_akshare}):
            result, source, is_chain = fetch_option_from_akshare(
                "i2609P730", "1d", 500, market_now=market_now
            )

        spot_mock.assert_not_called()
        self.assertEqual(result["date"].max(), pd.Timestamp("2026-08-07"))
        self.assertEqual(source, "AkShare期权日线")
        self.assertFalse(is_chain)

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_contracts")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_realtime_spread_preview_never_saves_even_if_requested(
        self, load_mock, fetch_mock, save_mock
    ):
        load_mock.return_value = (None, None)
        today = pd.Timestamp("2026-08-10")
        fetch_mock.return_value = (
            {
                "I2609": pd.DataFrame({"date": [today], "close": [719.0]}),
                "I2705": pd.DataFrame({"date": [today], "close": [698.0]}),
            },
            [],
        )

        load_or_fetch_spread(
            ["I2609", "I2705"],
            force_refresh=True,
            save_to_cache=True,
            realtime_preview=True,
            market_now=datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        save_mock.assert_not_called()

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_futures_option_data")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_realtime_option_preview_never_saves_even_if_requested(
        self, load_mock, fetch_mock, save_mock
    ):
        load_mock.return_value = (None, None)
        today = pd.Timestamp("2026-08-10")
        fetch_mock.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"date": [today], "close": [17.5]}),
            source="AkShare期权实时快照",
            is_chain=False,
        )

        load_or_fetch_option(
            "I2609P730",
            force_refresh=True,
            save_to_cache=True,
            realtime_preview=True,
            market_now=datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        save_mock.assert_not_called()

    def test_holdings_page_gates_fragment_network_until_load(self):
        root = Path(__file__).parents[1]
        coordinator_source = (
            root / "components" / "position" / "coordinator.py"
        ).read_text(encoding="utf-8")
        realtime_source = (root / "components" / "position" / "realtime.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'st.session_state.position_updates_enabled = False', coordinator_source
        )
        self.assertIn(
            'updates_enabled and position.etf_morning_timing_fetch_ready',
            realtime_source,
        )
        self.assertIn('save_to_cache=save_to_cache', realtime_source)
        self.assertIn(
            '"强制重新检查已是最新的ETF缓存",\n            value=False,',
            coordinator_source,
        )

    @patch("services.position_analysis.load_or_fetch_spread")
    def test_derivative_realtime_refresh_reports_failure(self, spread_mock):
        item = PositionItem(
            "期货价差",
            "IM2609 - IM2703",
            "IM2609 - IM2703 (中证1000股指)",
            "缓存",
        )
        spread_mock.return_value = PositionItem(
            item.category,
            item.code,
            item.name,
            "缓存",
            source="本地缓存（刷新失败）",
            error="network unavailable",
        )

        refreshed, errors = refresh_position_derivative_items([item])

        self.assertEqual(refreshed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("network unavailable", errors[0])

    def test_etf_realtime_timing_window_is_only_1450_to_1500(self):
        timezone = ZoneInfo("Asia/Shanghai")

        self.assertFalse(
            etf_realtime_timing_ready(
                datetime(2026, 7, 15, 14, 49, 59, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_realtime_timing_ready(
                datetime(2026, 7, 15, 14, 50, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_realtime_timing_ready(
                datetime(2026, 7, 15, 14, 59, 59, tzinfo=timezone)
            )
        )
        self.assertFalse(
            etf_realtime_timing_ready(
                datetime(2026, 7, 15, 15, 0, tzinfo=timezone)
            )
        )
        self.assertEqual(ETF_REALTIME_TIMING_REFRESH_SECONDS, 120)
        self.assertFalse(
            etf_realtime_timing_ready(
                datetime(2026, 7, 18, 14, 58, tzinfo=timezone)
            )
        )

    def test_etf_morning_timing_fetches_until_lunch_with_two_refresh_intervals(self):
        timezone = ZoneInfo("Asia/Shanghai")

        self.assertFalse(
            etf_morning_timing_fetch_ready(
                datetime(2026, 7, 15, 9, 29, 59, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_morning_timing_fetch_ready(
                datetime(2026, 7, 15, 9, 30, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_morning_timing_fetch_ready(
                datetime(2026, 7, 15, 9, 59, 59, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_morning_timing_fetch_ready(
                datetime(2026, 7, 15, 10, 0, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_morning_timing_fetch_ready(
                datetime(2026, 7, 15, 11, 29, 59, tzinfo=timezone)
            )
        )
        self.assertFalse(
            etf_morning_timing_fetch_ready(
                datetime(2026, 7, 15, 11, 30, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_morning_timing_preview_ready(
                datetime(2026, 7, 15, 11, 29, 59, tzinfo=timezone)
            )
        )
        self.assertFalse(
            etf_morning_timing_preview_ready(
                datetime(2026, 7, 15, 11, 30, tzinfo=timezone)
            )
        )
        self.assertEqual(ETF_MORNING_TIMING_REFRESH_SECONDS, 600)
        self.assertEqual(ETF_MIDSESSION_TIMING_REFRESH_SECONDS, 1800)
        self.assertFalse(
            etf_morning_timing_fetch_ready(
                datetime(2026, 7, 18, 9, 40, tzinfo=timezone)
            )
        )

    def test_etf_afternoon_timing_fetches_from_1300_to_1450(self):
        timezone = ZoneInfo("Asia/Shanghai")

        self.assertFalse(
            etf_afternoon_timing_fetch_ready(
                datetime(2026, 7, 15, 12, 59, 59, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_afternoon_timing_fetch_ready(
                datetime(2026, 7, 15, 13, 0, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_afternoon_timing_fetch_ready(
                datetime(2026, 7, 15, 14, 49, 59, tzinfo=timezone)
            )
        )
        self.assertFalse(
            etf_afternoon_timing_fetch_ready(
                datetime(2026, 7, 15, 14, 50, tzinfo=timezone)
            )
        )
        self.assertFalse(
            etf_afternoon_timing_fetch_ready(
                datetime(2026, 7, 18, 13, 30, tzinfo=timezone)
            )
        )

    def test_etf_lunch_timing_fetches_during_break_and_stays_visible_until_1450(self):
        timezone = ZoneInfo("Asia/Shanghai")

        self.assertFalse(
            etf_lunch_timing_fetch_ready(
                datetime(2026, 7, 15, 11, 29, 59, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_lunch_timing_fetch_ready(
                datetime(2026, 7, 15, 11, 30, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_lunch_timing_fetch_ready(
                datetime(2026, 7, 15, 12, 59, 59, tzinfo=timezone)
            )
        )
        self.assertFalse(
            etf_lunch_timing_fetch_ready(
                datetime(2026, 7, 15, 13, 0, tzinfo=timezone)
            )
        )
        self.assertTrue(
            etf_lunch_timing_preview_ready(
                datetime(2026, 7, 15, 14, 49, 59, tzinfo=timezone)
            )
        )
        self.assertFalse(
            etf_lunch_timing_preview_ready(
                datetime(2026, 7, 15, 14, 50, tzinfo=timezone)
            )
        )
        self.assertFalse(
            etf_lunch_timing_fetch_ready(
                datetime(2026, 7, 18, 12, 0, tzinfo=timezone)
            )
        )

    def test_lunch_timing_preview_recalculates_without_mutating_history(self):
        timezone = ZoneInfo("Asia/Shanghai")
        cached_data = pd.DataFrame(
            {
                "date": pd.date_range("2026-07-10", periods=5, freq="D"),
                "price": [100.0, 100.0, 100.0, 104.0, 103.0],
            }
        )
        cached_item = PositionItem(
            "ETF",
            "513260.SH",
            "恒生科技ETF汇添富",
            "缓存",
            source="本地缓存",
            latest_date="2026-07-14",
            metrics={"最新价": 103.0, "日涨跌(%)": 0.0},
            dataframe=cached_data,
        )
        lunch_quote_time = datetime(2026, 7, 15, 11, 30, tzinfo=timezone)

        with patch.dict(ETF_TIMING_STRATEGIES, {"513260": (3, 1.0)}, clear=True):
            updated = apply_etf_realtime_quote_to_timing(
                cached_item,
                {
                    "symbol": "513260.SH",
                    "price": 100.0,
                    "previous_close": 103.0,
                    "quote_time": lunch_quote_time,
                },
                market_now=datetime(2026, 7, 15, 13, 30, tzinfo=timezone),
            )

        self.assertEqual(updated.status, "午间预判")
        self.assertEqual(updated.metrics["择时判断"], "卖出")
        self.assertIn("午间收盘行情", updated.source)
        self.assertTrue(updated.dataframe.equals(cached_data))
        self.assertTrue(cached_item.dataframe.equals(cached_data))

    def test_morning_timing_preview_recalculates_without_mutating_history(self):
        timezone = ZoneInfo("Asia/Shanghai")
        cached_data = pd.DataFrame(
            {
                "date": pd.date_range("2026-07-10", periods=5, freq="D"),
                "price": [100.0, 100.0, 100.0, 104.0, 103.0],
            }
        )
        cached_item = PositionItem(
            "ETF",
            "513260.SH",
            "恒生科技ETF汇添富",
            "缓存",
            source="本地缓存",
            latest_date="2026-07-14",
            metrics={"最新价": 103.0, "日涨跌(%)": 0.0},
            dataframe=cached_data,
        )
        market_now = datetime(2026, 7, 15, 9, 40, tzinfo=timezone)

        with patch.dict(ETF_TIMING_STRATEGIES, {"513260": (3, 1.0)}, clear=True):
            updated = apply_etf_realtime_quote_to_timing(
                cached_item,
                {
                    "symbol": "513260.SH",
                    "price": 100.0,
                    "previous_close": 103.0,
                    "quote_time": market_now,
                },
                market_now=market_now,
            )

        self.assertEqual(updated.status, "早盘预判")
        self.assertEqual(updated.metrics["择时判断"], "卖出")
        self.assertIn("早盘实时行情", updated.source)
        self.assertEqual(updated.metrics["最新价"], 100.0)
        self.assertTrue(updated.dataframe.equals(cached_data))
        self.assertTrue(cached_item.dataframe.equals(cached_data))

    def test_parking_etf_timing_row_uses_same_transient_quote_as_card(self):
        timezone = ZoneInfo("Asia/Shanghai")
        cached_data = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-13", "2026-07-14"]),
                "price": [1.20, 1.21],
            }
        )
        cached_item = PositionItem(
            "ETF",
            "512890.SH",
            ETF_DISPLAY_NAMES["512890"],
            "缓存",
            source="本地缓存",
            latest_date="2026-07-14",
            metrics={"最新价": 1.21, "日涨跌(%)": 0.0},
            dataframe=cached_data,
        )
        preview_cases = (
            (datetime(2026, 7, 15, 10, 0, tzinfo=timezone), False),
            (datetime(2026, 7, 15, 13, 30, tzinfo=timezone), False),
            (datetime(2026, 7, 15, 14, 58, tzinfo=timezone), False),
            (datetime(2026, 7, 15, 15, 2, tzinfo=timezone), True),
        )
        for market_now, allow_close_retention in preview_cases:
            with self.subTest(market_now=market_now):
                updated = apply_etf_realtime_quote_to_timing(
                    cached_item,
                    {
                        "symbol": "512890.SH",
                        "price": 1.234,
                        "previous_close": 1.21,
                        "quote_time": market_now,
                    },
                    market_now=market_now,
                    allow_close_retention=allow_close_retention,
                )
                table = build_etf_timing_table([updated]).set_index("代码")

                self.assertEqual(updated.status, "盘中")
                self.assertEqual(updated.metrics["最新价"], 1.234)
                self.assertAlmostEqual(
                    updated.metrics["日涨跌(%)"],
                    (1.234 / 1.21 - 1) * 100,
                    places=2,
                )
                self.assertEqual(table.loc["512890", "最新价"], 1.234)
                self.assertTrue(updated.dataframe.equals(cached_data))
        self.assertTrue(cached_item.dataframe.equals(cached_data))

    def test_realtime_timing_preview_recalculates_without_mutating_history(self):
        timezone = ZoneInfo("Asia/Shanghai")
        cached_data = pd.DataFrame(
            {
                "date": pd.date_range("2026-07-10", periods=5, freq="D"),
                "price": [100.0, 100.0, 100.0, 104.0, 103.0],
            }
        )
        cached_item = PositionItem(
            "ETF",
            "513260.SH",
            "恒生科技ETF汇添富",
            "缓存",
            source="本地缓存",
            latest_date="2026-07-14",
            metrics={"最新价": 103.0, "日涨跌(%)": 0.0},
            dataframe=cached_data,
        )
        market_now = datetime(2026, 7, 15, 14, 58, tzinfo=timezone)

        with patch.dict(ETF_TIMING_STRATEGIES, {"513260": (3, 1.0)}, clear=True):
            updated = apply_etf_realtime_quote_to_timing(
                cached_item,
                {
                    "symbol": "513260.SH",
                    "price": 100.0,
                    "previous_close": 103.0,
                    "quote_time": market_now,
                },
                market_now=market_now,
            )

        self.assertEqual(updated.status, "实时预判")
        self.assertEqual(updated.metrics["最新价"], 100.0)
        self.assertAlmostEqual(updated.metrics["策略均线"], 102.333333, places=6)
        self.assertEqual(updated.metrics["择时判断"], "卖出")
        self.assertEqual(updated.metrics["状态转换时间"], "2026-07-15")
        self.assertTrue(updated.dataframe.equals(cached_data))
        self.assertTrue(cached_item.dataframe.equals(cached_data))

        after_close = apply_etf_realtime_quote_to_timing(
            cached_item,
            {
                "symbol": "513260.SH",
                "price": 100.0,
                "quote_time": datetime(2026, 7, 15, 15, 0, tzinfo=timezone),
            },
            market_now=datetime(2026, 7, 15, 15, 0, tzinfo=timezone),
        )
        self.assertIs(after_close, cached_item)

        with patch.dict(ETF_TIMING_STRATEGIES, {"513260": (3, 1.0)}, clear=True):
            retained_after_close = apply_etf_realtime_quote_to_timing(
                cached_item,
                {
                    "symbol": "513260.SH",
                    "price": 100.0,
                    "quote_time": datetime(2026, 7, 15, 15, 0, tzinfo=timezone),
                },
                market_now=datetime(2026, 7, 15, 15, 0, tzinfo=timezone),
                allow_close_retention=True,
            )
        self.assertEqual(retained_after_close.status, "收盘待确认")
        self.assertEqual(retained_after_close.metrics["最新价"], 100.0)
        self.assertEqual(retained_after_close.metrics["择时判断"], "卖出")
        self.assertTrue(retained_after_close.dataframe.equals(cached_data))

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
    @patch("services.position_analysis._fetch_eastmoney_exchange_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready", return_value=(None, None))
    def test_161128_uses_eastmoney_history_and_akshare_cache(
        self,
        load_mock,
        eastmoney_mock,
        tickflow_mock,
        save_mock,
    ):
        dates = pd.bdate_range(end="2026-07-31", periods=300)
        eastmoney_mock.return_value = pd.DataFrame(
            {
                "日期": dates,
                "开盘价": [1.0 + index / 1000 for index in range(len(dates))],
                "收盘价": [1.001 + index / 1000 for index in range(len(dates))],
                "symbol": "161128.SZ",
                "name": "标普信息科技LOF易方达",
            }
        )

        item = load_or_fetch_etf(
            "161128",
            allow_fetch=True,
            save_to_cache=True,
            market_now=datetime(2026, 8, 2, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        load_mock.assert_called_once_with(
            "fund_close_v2_161128.SZ_forward_additive",
            "akshare",
            "fund_close_raw",
            period="5000_1d",
        )
        eastmoney_mock.assert_called_once_with(
            symbol="161128.SZ",
            count=5000,
            adjust="forward_additive",
        )
        tickflow_mock.assert_not_called()
        self.assertEqual(save_mock.call_args.kwargs["source"], "akshare")
        self.assertEqual(item.status, "已更新")
        self.assertEqual(item.source, "东方财富/AkShare")
        self.assertEqual(item.name, "标普信息科技LOF易方达")
        self.assertEqual(item.latest_date, "2026-07-31")
        self.assertEqual(item.metrics["策略参数"], "MA25 / 1.5%")

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._fetch_sina_exchange_fund_close")
    @patch(
        "services.position_analysis._fetch_eastmoney_exchange_fund_close",
        side_effect=ConnectionError("eastmoney unavailable"),
    )
    @patch("services.position_analysis._load_dataset_if_ready", return_value=(None, None))
    def test_161128_uses_sina_fallback_when_eastmoney_is_unavailable(
        self,
        _load_mock,
        eastmoney_mock,
        sina_mock,
        tickflow_mock,
        save_mock,
    ):
        dates = pd.bdate_range(end="2026-07-31", periods=300)
        sina_mock.return_value = pd.DataFrame(
            {
                "日期": dates,
                "开盘价": [6.0 + index / 1000 for index in range(len(dates))],
                "收盘价": [6.001 + index / 1000 for index in range(len(dates))],
                "symbol": "161128.SZ",
                "name": "标普信息科技LOF易方达",
            }
        )

        item = load_or_fetch_etf(
            "161128",
            allow_fetch=True,
            save_to_cache=True,
            market_now=datetime(2026, 8, 2, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        eastmoney_mock.assert_called_once()
        sina_mock.assert_called_once_with(
            symbol="161128.SZ",
            count=5000,
            adjust="forward_additive",
        )
        tickflow_mock.assert_not_called()
        self.assertEqual(save_mock.call_args.kwargs["source"], "akshare")
        self.assertEqual(item.status, "已更新")
        self.assertEqual(item.source, "新浪财经备用源")
        self.assertEqual(item.latest_date, "2026-07-31")

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_adjusted_factor_change_rebuilds_only_that_symbol(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        cached_dates = pd.bdate_range(end="2026-08-11", periods=40)
        cached = _v2_adjusted_history(
            pd.DataFrame(
                {
                    "日期": cached_dates,
                    "收盘价": [1.20 + index / 1000 for index in range(40)],
                    "symbol": ["159545.SZ"] * 40,
                    "name": ["恒生红利低波ETF易方达"] * 40,
                    "_final_close_confirmed": [True] * 40,
                }
            )
        )
        recent = cached.tail(10).copy()
        recent["收盘价"] = recent["收盘价"] - 0.014
        recent = pd.concat(
            [
                recent,
                pd.DataFrame(
                    {
                        "日期": [pd.Timestamp("2026-08-12")],
                        "收盘价": [1.285],
                        "symbol": ["159545.SZ"],
                        "name": ["恒生红利低波ETF易方达"],
                    }
                ),
            ],
            ignore_index=True,
        )
        rebuilt = cached.copy()
        rebuilt["收盘价"] = rebuilt["收盘价"] - 0.014
        rebuilt = pd.concat([rebuilt, recent.tail(1)], ignore_index=True)
        load_mock.return_value = (cached, {"last_update_time": "2026-08-11T16:00:00"})
        fetch_mock.side_effect = [recent, rebuilt]

        item = load_or_fetch_etf(
            "159545",
            api_key="test-key",
            allow_fetch=True,
            force_refresh=True,
            save_to_cache=True,
            market_now=datetime(2026, 8, 12, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(fetch_mock.call_count, 2)
        self.assertEqual(item.status, "已重建")
        self.assertTrue(item.formal_history_valid)
        saved = save_mock.call_args.kwargs["df"]
        first_date = cached_dates[0]
        self.assertAlmostEqual(
            float(saved.loc[saved["日期"] == first_date, "收盘价"].iloc[0]),
            float(cached.loc[cached["日期"] == first_date, "收盘价"].iloc[0]) - 0.014,
        )

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_failed_adjusted_rebuild_keeps_card_but_blocks_signals(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        dates = pd.bdate_range(end="2026-08-11", periods=40)
        cached = _v2_adjusted_history(
            pd.DataFrame(
                {
                    "日期": dates,
                    "收盘价": [1.20 + index / 1000 for index in range(40)],
                    "symbol": ["159545.SZ"] * 40,
                    "name": ["恒生红利低波ETF易方达"] * 40,
                    "_final_close_confirmed": [True] * 40,
                }
            )
        )
        changed_recent = cached.tail(10).copy()
        changed_recent["收盘价"] = changed_recent["收盘价"] - 0.014
        load_mock.return_value = (cached, {"last_update_time": "2026-08-11T16:00:00"})
        fetch_mock.side_effect = [changed_recent, RuntimeError("full fetch failed")]

        item = load_or_fetch_etf(
            "159545",
            api_key="test-key",
            allow_fetch=True,
            force_refresh=True,
            save_to_cache=True,
            market_now=datetime(2026, 8, 12, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(item.status, "缓存待校验")
        self.assertFalse(item.formal_history_valid)
        self.assertIn("全量重建失败", item.error)
        self.assertEqual(item.cache_time, "2026-08-11 16:00:00")
        self.assertEqual(item.metrics["最新价"], round(float(cached["收盘价"].iloc[-1]), 4))
        self.assertNotIn("策略均线", item.metrics)
        self.assertTrue(build_recent_etf_operation_guidance([item], days=7).empty)
        save_mock.assert_not_called()

    @patch("services.position_analysis._fetch_sina_exchange_fund_final_close")
    def test_sina_raw_close_is_not_appended_to_backward_adjusted_history(
        self,
        final_close_mock,
    ):
        history = stamp_fund_history_metadata(
            pd.DataFrame(
                {
                    "日期": pd.to_datetime(["2026-07-31"]),
                    "收盘价": [6.77],
                }
            ),
            FUND_ADJUST_BACKWARD_ADDITIVE,
        )

        result = _append_sina_final_close(
            history,
            symbol="161128.SZ",
            adjust=FUND_ADJUST_BACKWARD_ADDITIVE,
            market_now=datetime(2026, 8, 3, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        final_close_mock.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertIn("不能追加到后复权正式历史", result.attrs["position_history_warning"])

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready", return_value=(None, None))
    def test_new_cache_is_not_saved_when_full_history_lags_latest_completed_session(
        self,
        _load_mock,
        fetch_mock,
        save_mock,
    ):
        dates = pd.bdate_range(end="2026-08-11", periods=40)
        fetch_mock.return_value = pd.DataFrame(
            {
                "日期": dates,
                "收盘价": [1.2 + index / 1000 for index in range(len(dates))],
                "symbol": ["159545.SZ"] * len(dates),
                "name": ["恒生红利低波ETF易方达"] * len(dates),
            }
        )

        item = load_or_fetch_etf(
            "159545",
            allow_fetch=True,
            save_to_cache=True,
            market_now=datetime(2026, 8, 12, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(item.status, "失败")
        self.assertFalse(item.formal_history_valid)
        self.assertIn("尚未覆盖最新完成交易日2026-08-12", item.error)
        save_mock.assert_not_called()

    @patch("services.position_analysis._load_dataset_if_ready")
    def test_159545_fixed_20260812_additive_sample(self, load_mock):
        dates = pd.bdate_range("2026-06-15", "2026-08-12")
        final_prices = {
            "2026-07-30": 1.356,
            "2026-07-31": 1.337,
            "2026-08-03": 1.327,
            "2026-08-04": 1.307,
            "2026-08-05": 1.304,
            "2026-08-06": 1.290,
            "2026-08-07": 1.279,
            "2026-08-10": 1.291,
            "2026-08-11": 1.293,
            "2026-08-12": 1.285,
        }
        prices = []
        for date in dates:
            if date < pd.Timestamp("2026-07-06"):
                price = 1.20
            elif date == pd.Timestamp("2026-07-06"):
                price = 1.23
            elif date < pd.Timestamp("2026-07-22"):
                price = 1.28 + (date.day % 5) * 0.005
            elif date <= pd.Timestamp("2026-07-29"):
                price = 1.349
            else:
                price = final_prices[date.strftime("%Y-%m-%d")]
            prices.append(price)
        cached = _v2_adjusted_history(
            pd.DataFrame(
                {
                    "日期": dates,
                    "收盘价": prices,
                    "symbol": ["159545.SZ"] * len(dates),
                    "name": ["恒生红利低波ETF易方达"] * len(dates),
                    "_final_close_confirmed": [True] * len(dates),
                }
            )
        )
        load_mock.return_value = (cached, {"last_update_time": "2026-08-12T16:00:00"})

        item = load_or_fetch_etf(
            "159545",
            allow_fetch=False,
            market_now=datetime(2026, 8, 13, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        snapshot = calculate_etf_timing_snapshot(
            item.dataframe,
            ma_period=10,
            threshold_pct=1.0,
        )

        self.assertAlmostEqual(float(snapshot["策略均线"]), 1.3069, places=4)
        self.assertAlmostEqual(float(snapshot["策略偏离(%)"]), -1.6757, places=4)
        self.assertAlmostEqual(
            float(item.dataframe.iloc[-1]["daily_return_pct"]),
            -0.6187,
            places=4,
        )
        self.assertEqual(snapshot["择时判断"], "空仓")
        self.assertEqual(snapshot["状态转换时间"], "2026-08-04")
        self.assertEqual(snapshot["上一状态转换时间"], "2026-07-06")
        guidance = build_recent_etf_operation_guidance([item], days=10)
        self.assertFalse(
            bool(
                ((guidance["日期"] == "2026-08-12") & (guidance["操作指引"] == "买入")).any()
            )
        )

    @patch("services.position_analysis._ensure_sina_adjustment_is_identity")
    @patch("services.position_analysis.requests.get")
    @patch("py_mini_racer.MiniRacer")
    def test_sina_fallback_normalizes_utc_dates(
        self,
        decoder_mock,
        request_mock,
        _adjustment_mock,
    ):
        response = SimpleNamespace(
            text='var sz161128="encoded";',
            raise_for_status=lambda: None,
        )
        request_mock.return_value = response
        decoder_mock.return_value.call.return_value = [
            {
                "date": pd.Timestamp("2026-07-31", tz="UTC"),
                "open": 6.748,
                "close": 6.770,
            }
        ]

        result = _fetch_sina_exchange_fund_close(
            symbol="161128.SZ",
            count=5000,
            adjust="forward",
        )

        self.assertEqual(result["日期"].iloc[0], pd.Timestamp("2026-07-31"))
        self.assertIsNone(result["日期"].dt.tz)

    @patch("services.position_analysis.requests.get")
    def test_sina_realtime_quote_accepts_same_day_lof_snapshot(self, request_mock):
        payload = (
            'var hq_str_sz161128="标普科技,6.698,6.770,6.815,6.819,6.698,'
            '6.815,6.816,15044731,101903072.217,677016,6.815,22700,6.813,'
            '1000,6.812,6000,6.806,2000,6.805,31700,6.816,6900,6.817,'
            '68400,6.818,58202,6.819,235368,6.820,2026-08-04,14:30:00,00";\n'
        )
        request_mock.return_value = SimpleNamespace(
            content=payload.encode("gb18030"),
            raise_for_status=lambda: None,
        )

        quote = _fetch_sina_exchange_fund_quote(
            symbol="161128.SZ",
            market_now=datetime(
                2026, 8, 4, 14, 31, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
        )

        self.assertEqual(quote["symbol"], "161128.SZ")
        self.assertEqual(quote["price"], 6.815)
        self.assertAlmostEqual(quote["change_pct"], 0.664697, places=5)
        self.assertEqual(quote["quote_time"].hour, 14)
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(request_kwargs["timeout"], 15)
        self.assertIn("Referer", request_kwargs["headers"])

    @patch("services.position_analysis.requests.Session")
    @patch(
        "services.position_analysis.requests.get",
        side_effect=requests.exceptions.ProxyError("proxy unavailable"),
    )
    def test_sina_realtime_snapshot_retries_direct_after_proxy_error(
        self,
        _request_mock,
        session_mock,
    ):
        direct_response = SimpleNamespace()
        session_mock.return_value.__enter__.return_value.get.return_value = direct_response

        result = _request_sina_realtime_snapshot("sz161128")

        self.assertIs(result, direct_response)
        direct_session = session_mock.return_value.__enter__.return_value
        self.assertFalse(direct_session.trust_env)
        direct_session.get.assert_called_once()

    @patch("services.position_analysis.requests.get")
    def test_sina_final_close_snapshot_accepts_same_day_close(self, request_mock):
        payload = (
            'var hq_str_sz161128="标普科技,6.698,6.770,6.815,6.819,6.698,'
            '6.815,6.816,15044731,101903072.217,677016,6.815,22700,6.813,'
            '1000,6.812,6000,6.806,2000,6.805,31700,6.816,6900,6.817,'
            '68400,6.818,58202,6.819,235368,6.820,2026-08-03,15:00:00,00";\n'
        )
        request_mock.return_value = SimpleNamespace(
            content=payload.encode("gb18030"),
            raise_for_status=lambda: None,
        )

        result = _fetch_sina_exchange_fund_final_close(
            symbol="161128.SZ",
            market_now=datetime(2026, 8, 3, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(result["日期"].iloc[0], pd.Timestamp("2026-08-03"))
        self.assertEqual(result["开盘价"].iloc[0], 6.698)
        self.assertEqual(result["收盘价"].iloc[0], 6.815)
        self.assertTrue(result["_final_close_confirmed"].iloc[0])
        request_kwargs = request_mock.call_args.kwargs
        self.assertEqual(request_kwargs["timeout"], 15)
        self.assertIn("Referer", request_kwargs["headers"])

    @patch("services.position_analysis.requests.get")
    def test_sina_final_close_snapshot_rejects_stale_trade_date(self, request_mock):
        payload = (
            'var hq_str_sz161128="标普科技,6.698,6.770,6.815,6.819,6.698,'
            '6.815,6.816,15044731,101903072.217,677016,6.815,22700,6.813,'
            '1000,6.812,6000,6.806,2000,6.805,31700,6.816,6900,6.817,'
            '68400,6.818,58202,6.819,235368,6.820,2026-07-31,15:00:00,00";\n'
        )
        request_mock.return_value = SimpleNamespace(
            content=payload.encode("gb18030"),
            raise_for_status=lambda: None,
        )

        with self.assertRaisesRegex(ValueError, "最新完成交易日应为 2026-08-03"):
            _fetch_sina_exchange_fund_final_close(
                symbol="161128.SZ",
                market_now=datetime(2026, 8, 3, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis._fetch_sina_exchange_fund_final_close")
    @patch("services.position_analysis._fetch_eastmoney_exchange_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_161128_appends_confirmed_close_when_history_source_lags(
        self,
        load_mock,
        eastmoney_mock,
        final_close_mock,
        save_mock,
    ):
        dates = pd.bdate_range(end="2026-07-31", periods=40)
        cached = pd.DataFrame(
            {
                "日期": dates,
                "开盘价": [6.5] * len(dates),
                "收盘价": [6.5] * (len(dates) - 1) + [6.770],
                "symbol": ["161128.SZ"] * len(dates),
                "name": ["标普信息科技LOF易方达"] * len(dates),
                "_final_close_confirmed": [True] * len(dates),
            }
        )
        cached = _v2_adjusted_history(cached)
        load_mock.return_value = (cached, {"last_update_time": "2026-07-31T16:00:00"})
        eastmoney_mock.return_value = cached.drop(columns="_final_close_confirmed")
        final_close_mock.return_value = pd.DataFrame(
            {
                "日期": [pd.Timestamp("2026-08-03")],
                "开盘价": [6.698],
                "收盘价": [6.815],
                "symbol": ["161128.SZ"],
                "name": ["标普信息科技LOF易方达"],
                "_final_close_confirmed": [True],
            }
        )
        market_now = datetime(2026, 8, 3, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        item = load_or_fetch_etf(
            "161128",
            allow_fetch=True,
            save_to_cache=True,
            market_now=market_now,
        )

        saved = save_mock.call_args.kwargs["df"]
        saved_close = saved.loc[saved["日期"] == pd.Timestamp("2026-08-03")].iloc[0]
        self.assertEqual(saved_close["收盘价"], 6.815)
        self.assertTrue(saved_close["_final_close_confirmed"])
        self.assertEqual(
            saved.loc[saved["日期"] == pd.Timestamp("2026-07-31"), "收盘价"].iloc[0],
            6.770,
        )
        self.assertEqual(item.latest_date, "2026-08-03")
        self.assertEqual(item.metrics["最新价"], 6.815)
        self.assertEqual(item.source, "东方财富/AkShare + 新浪收盘快照")
        final_close_mock.assert_called_once_with(symbol="161128.SZ", market_now=market_now)

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_weekend_load_incrementally_fetches_missing_friday_close(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        cached_dates = pd.bdate_range(end="2026-07-23", periods=30)
        cached = pd.DataFrame(
            {
                "日期": cached_dates,
                "收盘价": [1.0] * len(cached_dates),
                "symbol": "512890.SH",
                "name": "红利低波ETF华泰柏瑞",
            }
        )
        friday = pd.DataFrame(
            {
                "日期": [pd.Timestamp("2026-07-24")],
                "收盘价": [1.2],
                "symbol": ["512890.SH"],
                "name": ["红利低波ETF华泰柏瑞"],
            }
        )
        cached = _v2_adjusted_history(cached)
        load_mock.return_value = (cached, {"last_update_time": "2026-07-23T16:00:00"})
        fetch_mock.return_value = pd.concat([cached.tail(5), friday], ignore_index=True)

        item = load_or_fetch_etf(
            "512890",
            api_key="test-key",
            allow_fetch=True,
            force_refresh=False,
            save_to_cache=True,
            market_now=datetime(2026, 7, 26, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        fetch_mock.assert_called_once()
        saved = save_mock.call_args.kwargs["df"]
        self.assertEqual(saved["日期"].max(), pd.Timestamp("2026-07-24"))
        self.assertTrue(saved.loc[saved["日期"] == pd.Timestamp("2026-07-24"), "_final_close_confirmed"].iloc[0])
        self.assertEqual(item.latest_date, "2026-07-24")
        self.assertEqual(item.metrics["最新价"], 1.2)

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_weekend_load_does_not_refetch_current_friday_close(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        cached_dates = pd.bdate_range(end="2026-07-24", periods=30)
        cached = pd.DataFrame(
            {
                "日期": cached_dates,
                "收盘价": [1.0] * len(cached_dates),
                "symbol": "512890.SH",
                "name": "红利低波ETF华泰柏瑞",
                "_final_close_confirmed": True,
            }
        )
        cached = _v2_adjusted_history(cached)
        load_mock.return_value = (cached, {"last_update_time": "2026-07-24T16:00:00"})

        item = load_or_fetch_etf(
            "512890",
            api_key="test-key",
            allow_fetch=True,
            force_refresh=False,
            save_to_cache=True,
            market_now=datetime(2026, 7, 26, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        fetch_mock.assert_not_called()
        save_mock.assert_not_called()
        self.assertEqual(item.latest_date, "2026-07-24")

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_monday_intraday_load_backfills_friday_but_excludes_monday_quote(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        cached_dates = pd.bdate_range(end="2026-07-23", periods=30)
        cached = pd.DataFrame(
            {
                "日期": cached_dates,
                "收盘价": [1.0] * len(cached_dates),
                "symbol": "512890.SH",
                "name": "红利低波ETF华泰柏瑞",
            }
        )
        latest = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-07-24", "2026-07-27"]),
                "收盘价": [1.2, 1.3],
                "symbol": ["512890.SH", "512890.SH"],
                "name": ["红利低波ETF华泰柏瑞", "红利低波ETF华泰柏瑞"],
            }
        )
        cached = _v2_adjusted_history(cached)
        load_mock.return_value = (cached, {"last_update_time": "2026-07-23T16:00:00"})
        fetch_mock.return_value = pd.concat([cached.tail(5), latest], ignore_index=True)

        item = load_or_fetch_etf(
            "512890",
            api_key="test-key",
            allow_fetch=True,
            force_refresh=False,
            save_to_cache=True,
            market_now=datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        fetch_mock.assert_called_once()
        saved = save_mock.call_args.kwargs["df"]
        self.assertEqual(saved["日期"].max(), pd.Timestamp("2026-07-24"))
        self.assertNotIn(pd.Timestamp("2026-07-27"), set(saved["日期"]))
        self.assertEqual(item.latest_date, "2026-07-24")
        self.assertEqual(item.metrics["最新价"], 1.2)

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_tickflow_fund_close")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_load_backfills_every_missing_completed_session(
        self,
        load_mock,
        fetch_mock,
        save_mock,
    ):
        cached_dates = pd.bdate_range(end="2026-07-20", periods=30)
        cached = pd.DataFrame(
            {
                "日期": cached_dates,
                "收盘价": [1.0] * len(cached_dates),
                "symbol": "512890.SH",
                "name": "红利低波ETF华泰柏瑞",
            }
        )
        missing_dates = pd.to_datetime(["2026-07-21", "2026-07-22", "2026-07-23"])
        latest = pd.DataFrame(
            {
                "日期": missing_dates,
                "收盘价": [1.1, 1.2, 1.3],
                "symbol": ["512890.SH"] * 3,
                "name": ["红利低波ETF华泰柏瑞"] * 3,
            }
        )
        cached = _v2_adjusted_history(cached)
        load_mock.return_value = (cached, {"last_update_time": "2026-07-20T16:00:00"})
        fetch_mock.return_value = pd.concat([cached.tail(5), latest], ignore_index=True)

        item = load_or_fetch_etf(
            "512890",
            api_key="test-key",
            allow_fetch=True,
            force_refresh=False,
            save_to_cache=True,
            market_now=datetime(2026, 7, 23, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        saved = save_mock.call_args.kwargs["df"]
        saved_dates = set(pd.to_datetime(saved["日期"]))
        self.assertTrue(set(missing_dates).issubset(saved_dates))
        self.assertTrue(saved.loc[saved["日期"].isin(missing_dates), "_final_close_confirmed"].all())
        self.assertEqual(item.latest_date, "2026-07-23")

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
        cached = _v2_adjusted_history(cached)
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
        cached = _v2_adjusted_history(cached)
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
        cached = _v2_adjusted_history(cached)
        load_mock.return_value = (cached, {"last_update_time": "2026-07-14T16:00:00"})

        item = load_or_fetch_etf("159655", allow_fetch=False)

        self.assertAlmostEqual(item.metrics["策略均线"], 1.90292, places=6)

    def test_etf_timing_table_excludes_derivatives_and_includes_parking_etf(self):
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
                "518850.SH",
                "黄金ETF",
                "缓存",
                metrics={
                    "最新价": 6.2,
                    "日涨跌(%)": 0.1,
                    "策略参数": "MA30 / 1.5%",
                    "策略均线": 6.0,
                    "策略偏离(%)": 3.33,
                    "择时判断": "持有",
                },
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
                    "上一状态转换时间": "2026-06-18",
                    "策略上一区间涨幅(%)": -2.5,
                },
            ),
            PositionItem("期货价差", "I2609-I2705", "铁矿石价差", "缓存"),
            PositionItem("期权", "I2609P730", "铁矿石看跌期权", "缓存"),
        ]

        table = build_etf_timing_table(items)

        self.assertEqual(table["代码"].tolist(), ["513260", "518850", "512890"])
        self.assertEqual(table.iloc[0]["ETF名称"], "恒生科技ETF汇添富")
        self.assertEqual(
            table.columns.tolist()[-4:],
            ["状态转换时间", "区间涨幅(%)", "上一状态转换时间", "上一区间涨幅(%)"],
        )
        self.assertEqual(table.iloc[0]["上一状态转换时间"], "2026-06-18")
        self.assertEqual(table.iloc[0]["上一区间涨幅(%)"], -2.5)
        self.assertEqual(
            table.loc[table["代码"] == "512890", "ETF名称"].iloc[0],
            "红利低波ETF华泰柏瑞",
        )
        self.assertEqual(table.loc[table["代码"] == "512890", "组合权重比例"].iloc[0], "-")
        self.assertEqual(table.loc[table["代码"] == "512890", "择时判断"].iloc[0], "-")
        self.assertEqual(table.iloc[1]["策略参数"], "MA30 / 1.5%")
        self.assertNotIn("期货价差", table.to_string())

    def test_etf_timing_table_uses_current_portfolio_weights(self):
        items = [
            PositionItem("ETF", code, ETF_DISPLAY_NAMES[code], "缓存")
            for code in DEFAULT_ETF_CODES
        ]

        table = build_etf_timing_table(items).set_index("代码")

        self.assertEqual(sum(ETF_PORTFOLIO_WEIGHTS_PCT.values()), 100)
        self.assertEqual(table.loc["159201", "组合权重比例"], "5%")
        self.assertEqual(table.loc["159501", "组合权重比例"], "15%")
        self.assertEqual(table.loc["513260", "组合权重比例"], "0%")
        self.assertEqual(table.loc["159915", "组合权重比例"], "0%")

    def test_512890_parking_snapshot_uses_only_aggregate_position_transitions(self):
        dates = pd.date_range("2026-07-01", periods=6, freq="D")

        def source_item(code, prices):
            return PositionItem(
                "ETF",
                code,
                ETF_DISPLAY_NAMES[code],
                "缓存",
                dataframe=pd.DataFrame({"date": dates, "price": prices}),
            )

        items = [
            PositionItem(
                "ETF",
                "512890",
                ETF_DISPLAY_NAMES["512890"],
                "缓存",
                metrics={"最新价": 1.5, "日涨跌(%)": 0.2},
                dataframe=pd.DataFrame(
                    {"date": dates, "price": [1.0, 1.1, 1.2, 1.3, 1.4, 1.5]}
                ),
            ),
            source_item("510500", [10.0, 11.0, 9.0, 9.0, 9.0, 9.0]),
            source_item("159967", [10.0, 11.0, 12.0, 10.0, 12.0, 13.0]),
            source_item("159552", [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]),
        ]
        patched_strategies = {
            "510500": (2, 0.0),
            "159967": (2, 0.0),
            "159552": (2, 0.0),
        }

        with patch.dict(ETF_TIMING_STRATEGIES, patched_strategies, clear=True):
            snapshot = calculate_512890_parking_snapshot(items)
            table = build_etf_timing_table(items).set_index("代码")

        self.assertEqual(snapshot["组合权重比例"], "10%")
        self.assertEqual(snapshot["择时判断"], "持有")
        self.assertEqual(snapshot["状态转换时间"], "2026-07-03")
        self.assertEqual(snapshot["上一状态转换时间"], "-")
        self.assertAlmostEqual(snapshot["策略区间涨幅(%)"], 25.0)
        self.assertEqual(table.loc["512890", "ETF名称"], "红利低波ETF华泰柏瑞")
        self.assertEqual(table.loc["512890", "组合权重比例"], "10%")
        self.assertEqual(table.loc["512890", "状态转换时间"], "2026-07-03")

    def test_512890_parking_snapshot_calculates_current_and_previous_intervals(self):
        dates = pd.date_range("2026-07-01", periods=6, freq="D")

        def source_item(code, prices):
            return PositionItem(
                "ETF",
                code,
                ETF_DISPLAY_NAMES[code],
                "缓存",
                dataframe=pd.DataFrame({"date": dates, "price": prices}),
            )

        items = [
            PositionItem(
                "ETF",
                "512890",
                ETF_DISPLAY_NAMES["512890"],
                "缓存",
                metrics={"最新价": 1.5},
                dataframe=pd.DataFrame(
                    {"date": dates, "price": [1.0, 1.1, 1.2, 1.3, 1.4, 1.5]}
                ),
            ),
            source_item("510500", [10.0, 11.0, 9.0, 9.0, 11.0, 11.0]),
            source_item("159967", [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]),
            source_item("159552", [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]),
        ]

        with patch.dict(
            ETF_TIMING_STRATEGIES,
            {"510500": (2, 0.0), "159967": (2, 0.0), "159552": (2, 0.0)},
            clear=True,
        ):
            snapshot = calculate_512890_parking_snapshot(items)

        self.assertEqual(snapshot["组合权重比例"], "0%")
        self.assertEqual(snapshot["择时判断"], "空仓")
        self.assertEqual(snapshot["状态转换时间"], "2026-07-05")
        self.assertEqual(snapshot["上一状态转换时间"], "2026-07-03")
        self.assertAlmostEqual(snapshot["策略区间涨幅(%)"], (1.5 / 1.4 - 1) * 100)
        self.assertAlmostEqual(snapshot["策略上一区间涨幅(%)"], (1.4 / 1.2 - 1) * 100)

    def test_512890_parking_weight_counts_each_empty_transfer_source(self):
        for empty_count, expected_weight in ((0, "0%"), (1, "10%"), (2, "20%"), (3, "30%")):
            source_items = []
            for index, code in enumerate(("510500", "159967", "159552")):
                source_items.append(
                    PositionItem(
                        "ETF",
                        code,
                        ETF_DISPLAY_NAMES[code],
                        "盘中",
                        latest_date="2026-07-07",
                        metrics={"择时判断": "空仓" if index < empty_count else "持有"},
                    )
                )
            parking = PositionItem(
                "ETF",
                "512890",
                ETF_DISPLAY_NAMES["512890"],
                "盘中",
                latest_date="2026-07-07",
                metrics={"最新价": 1.6},
            )

            snapshot = calculate_512890_parking_snapshot([parking, *source_items])

            self.assertEqual(snapshot["组合权重比例"], expected_weight)
            self.assertEqual(snapshot["择时判断"], "持有" if empty_count else "空仓")

    def test_missing_timed_etf_still_shows_configured_strategy(self):
        table = build_etf_timing_table(
            [
                PositionItem("ETF", "510500", "510500.SH", "无缓存"),
                PositionItem("ETF", "513310", "513310.SH", "无缓存"),
                PositionItem("ETF", "513880", "513880.SH", "无缓存"),
            ]
        )

        self.assertEqual(table.iloc[0]["ETF名称"], "中证500ETF南方")
        self.assertEqual(table.iloc[0]["策略参数"], "MA15 / 1.0%")
        self.assertTrue(pd.isna(table.iloc[0]["对应均线"]))
        by_code = table.set_index("代码")
        self.assertEqual(by_code.loc["513310", "策略参数"], "MA15 / 0.5%")
        self.assertEqual(by_code.loc["513880", "策略参数"], "MA10 / 2.0%")

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

    @patch("services.position_analysis.save_dataset")
    @patch("services.position_analysis.fetch_futures_option_data")
    @patch("services.position_analysis._load_dataset_if_ready")
    def test_option_refresh_updates_current_price_without_erasing_cached_fields(
        self,
        load_mock,
        fetch_mock,
        _save_mock,
    ):
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
        prior_day = today - pd.Timedelta(days=1)
        cached = pd.DataFrame(
            {
                "date": [prior_day, today],
                "close": [30.8, 16.0],
                "open_interest": [6754.0, 6826.0],
                "_data_version": FUTURES_OPTION_DATA_VERSION,
            }
        )
        latest = pd.DataFrame(
            {
                "date": [prior_day, today],
                "close": [31.0, 15.6],
                "open_interest": [6800.0, pd.NA],
            }
        )
        load_mock.return_value = (cached, {"last_update_time": today.isoformat()})
        fetch_mock.return_value = SimpleNamespace(
            dataframe=latest,
            is_chain=False,
            source="AkShare期权",
        )

        item = load_or_fetch_option("I2609P730", count=500, force_refresh=True)

        self.assertEqual(item.dataframe.loc[item.dataframe["date"] == prior_day, "close"].iloc[0], 30.8)
        current = item.dataframe.loc[item.dataframe["date"] == today].iloc[0]
        self.assertEqual(current["close"], 15.6)
        self.assertEqual(current["open_interest"], 6826.0)

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

        item = load_or_fetch_spread(
            ["I2609", "I2701"],
            force_refresh=True,
            market_now=datetime(2026, 7, 3, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(item.status, "已增量更新")
        self.assertEqual(len(item.dataframe), 3)
        same_day = item.dataframe.loc[item.dataframe["date"] == pd.Timestamp("2026-07-02")]
        self.assertEqual(same_day["I2609_close"].iloc[0], 805.0)
        self.assertEqual(len(save_mock.call_args.kwargs["df"]), 3)


class PositionFacadeContractTests(unittest.TestCase):
    EXPECTED_EXPORTS = {
        "DEFAULT_ETF_CODES", "DEFAULT_FUTURES_CONTRACTS", "DEFAULT_OPTION_CODES",
        "DEFAULT_SPREAD_CONTRACTS", "DEFAULT_SPREAD_GROUPS",
        "ETF_AKSHARE_HISTORY_CODES", "ETF_DISPLAY_NAMES",
        "ETF_MIDSESSION_TIMING_REFRESH_SECONDS",
        "ETF_MORNING_TIMING_REFRESH_SECONDS", "ETF_PORTFOLIO_WEIGHTS_PCT",
        "ETF_POSITION_STRATEGIES", "ETF_REALTIME_TIMING_END_TIME",
        "ETF_REALTIME_TIMING_REFRESH_SECONDS", "ETF_TIMING_STRATEGIES",
        "POSITION_INDEX_TIMING_STRATEGIES",
        "POSITION_TIMING_INITIAL_CAPITAL", "POSITION_TIMING_LOT_SIZE",
        "POSITION_TIMING_PARKING_SYMBOL", "POSITION_TIMING_START_DATE",
        "POSITION_TIMING_TRANSACTION_COST", "PositionItem",
        "PositionTimingPerformanceResult", "_adjusted_history_has_overlap_changes",
        "_append_sina_final_close", "_cache_has_expected_trade_date",
        "_fetch_eastmoney_exchange_fund_close", "_fetch_exchange_fund_close",
        "_fetch_sina_exchange_fund_close", "_fetch_sina_exchange_fund_final_close",
        "_fetch_sina_exchange_fund_quote", "_merge_by_date",
        "_merge_current_day_refresh", "_request_sina_realtime_snapshot",
        "apply_etf_realtime_quote", "apply_etf_realtime_quote_to_timing",
        "apply_etf_realtime_quotes_to_items", "build_etf_timing_table",
        "build_position_index_timing_table",
        "build_position_timing_performance",
        "build_recent_etf_operation_guidance", "calculate_512890_parking_snapshot",
        "calculate_etf_timing_snapshot", "etf_afternoon_timing_fetch_ready",
        "etf_cache_has_latest_final_close", "etf_final_close_ready",
        "etf_intraday_quote_ready", "etf_lunch_timing_fetch_ready",
        "etf_lunch_timing_preview_ready", "etf_morning_timing_fetch_ready",
        "etf_morning_timing_preview_ready", "etf_position_decision",
        "etf_realtime_timing_ready", "fetch_tickflow_etf_quotes",
        "filter_current_etf_realtime_quotes", "filter_final_etf_rows",
        "latest_final_etf_trade_date", "load_or_fetch_etf",
        "load_or_fetch_futures_contract", "load_or_fetch_option",
        "load_or_fetch_spread", "load_runtime_etf_quotes",
        "load_runtime_etf_quote_state",
        "normalize_etf_base_code", "parse_position_codes", "parse_spread_groups",
        "refresh_position_derivative_items", "remember_runtime_etf_quotes",
        "refresh_runtime_etf_quotes",
    }
    EXPECTED_SIGNATURES = {
        "fetch_tickflow_etf_quotes": "(codes: 'list[str]', *, api_key: 'str', market_now: 'datetime | None' = None) -> 'dict[str, dict[str, object]]'",
        "refresh_runtime_etf_quotes": "(codes: 'list[str]', *, api_key: 'str', market_now: 'datetime | None' = None) -> 'dict[str, dict[str, object]]'",
        "load_or_fetch_etf": "(code: 'str', *, api_key: 'str' = '', count: 'int' = 5000, adjust: 'str | None' = 'forward_additive', ma_periods: 'list[int] | tuple[int, ...]' = (20, 60, 120, 250), rsi_period: 'int' = 14, base_date: 'str' = '2024-09-24', allow_fetch: 'bool' = True, force_refresh: 'bool' = False, save_to_cache: 'bool' = True, allow_unfinished_session: 'bool' = False, market_now: 'datetime | None' = None) -> 'PositionItem'",
        "load_or_fetch_futures_contract": "(contract: 'str', *, api_key: 'str' = '', count: 'int' = 500, ma_periods: 'list[int] | tuple[int, ...]' = (5, 20, 60), allow_fetch: 'bool' = True, force_refresh: 'bool' = False, save_to_cache: 'bool' = True, realtime_preview: 'bool' = False, market_now: 'datetime | None' = None) -> 'PositionItem'",
        "load_or_fetch_spread": "(contracts: 'list[str]', *, base_contract: 'str | None' = None, api_key: 'str' = '', max_workers: 'int' = 2, allow_fetch: 'bool' = True, force_refresh: 'bool' = False, save_to_cache: 'bool' = True, realtime_preview: 'bool' = False, market_now: 'datetime | None' = None) -> 'PositionItem'",
        "load_or_fetch_option": "(code: 'str', *, period: 'str' = '1d', count: 'int' = 500, ma_periods: 'list[int] | tuple[int, ...]' = (5, 20, 60), allow_fetch: 'bool' = True, force_refresh: 'bool' = False, save_to_cache: 'bool' = True, realtime_preview: 'bool' = False, market_now: 'datetime | None' = None) -> 'PositionItem'",
        "refresh_position_derivative_items": "(items: 'list[PositionItem]', *, api_key: 'str' = '', max_workers: 'int' = 2, option_count: 'int' = 500, market_now: 'datetime | None' = None) -> 'tuple[list[PositionItem], list[str]]'",
        "build_position_index_timing_table": "() -> 'pd.DataFrame'",
    }

    def test_existing_import_surface_and_signatures_are_stable(self):
        self.assertTrue(self.EXPECTED_EXPORTS.issubset(set(position_facade.__all__)))
        self.assertFalse(self.EXPECTED_EXPORTS - set(vars(position_facade)))
        actual = {
            name: str(inspect.signature(getattr(position_facade, name)))
            for name in self.EXPECTED_SIGNATURES
        }
        self.assertEqual(actual, self.EXPECTED_SIGNATURES)

    def test_model_and_runtime_singletons_keep_legacy_identity(self):
        self.assertIs(position_facade.PositionItem, position_models.PositionItem)
        self.assertEqual(position_facade.PositionItem.__module__, position_facade.__name__)
        self.assertIs(
            position_facade._RUNTIME_ETF_QUOTE_CACHE,
            position_runtime._RUNTIME_ETF_QUOTE_CACHE,
        )
        self.assertIs(
            position_facade._RUNTIME_ETF_QUOTE_CACHE_LOCK,
            position_runtime._RUNTIME_ETF_QUOTE_CACHE_LOCK,
        )


if __name__ == "__main__":
    unittest.main()
