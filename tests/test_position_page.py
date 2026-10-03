from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from services.position_analysis import PositionItem, PositionTimingPerformanceResult


PAGE = Path(__file__).parents[1] / "pages" / "2_持仓分析.py"


def _item(
    category: str,
    code: str,
    *,
    cached: bool,
) -> PositionItem:
    if not cached:
        return PositionItem(
            category,
            code,
            code,
            "无缓存",
            dataframe=pd.DataFrame(),
        )
    price_column = "price" if category == "ETF" else "close"
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-20", "2026-08-21"]),
            price_column: [100.0, 101.0],
        }
    )
    metrics = (
        {"最新价": 101.0, "日涨跌(%)": 1.0}
        if category == "ETF"
        else {"最新收盘": 101.0, "日涨跌(%)": 1.0}
    )
    if category == "期货价差":
        metrics = {"最新价差": 10.0, "价差日变化": 1.0}
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-20", "2026-08-21"]),
                "spread_value": [9.0, 10.0],
            }
        )
    return PositionItem(
        category,
        code,
        code,
        "缓存",
        source="本地缓存",
        latest_date="2026-08-21",
        metrics=metrics,
        dataframe=frame,
    )


def _patch_page(stack: ExitStack, *, cached: bool):
    from components.position.runtime_state import _PREVIEWS
    from services import position_runtime
    _PREVIEWS.clear()
    position_runtime._RUNTIME_ETF_QUOTE_CACHE.clear()
    position_runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear()

    class PageDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 21, 10, 30, tzinfo=tz)

    stack.enter_context(patch("components.position.coordinator.datetime", PageDateTime))
    stack.enter_context(patch("components.position.realtime.datetime", PageDateTime))
    stack.enter_context(patch("components.position.realtime.fetch_realtime_index_quotes", return_value={}))
    stack.enter_context(patch("core.db.init_db"))
    etf = stack.enter_context(
        patch(
            "services.position_analysis.load_or_fetch_etf",
            side_effect=lambda code, **kwargs: _item("ETF", code, cached=cached),
        )
    )
    futures = stack.enter_context(
        patch(
            "services.position_analysis.load_or_fetch_futures_contract",
            side_effect=lambda code, **kwargs: _item("期货", code, cached=cached),
        )
    )
    spread = stack.enter_context(
        patch(
            "services.position_analysis.load_or_fetch_spread",
            side_effect=lambda codes, **kwargs: _item(
                "期货价差",
                " - ".join(codes),
                cached=cached,
            ),
        )
    )
    realtime_fetch = stack.enter_context(
        patch(
            "services.position_analysis.fetch_tickflow_etf_quotes",
            side_effect=AssertionError("默认渲染不应获取实时行情"),
        )
    )
    derivative_refresh = stack.enter_context(
        patch(
            "services.position_analysis.refresh_position_derivative_items",
            return_value=([], []),
        )
    )
    stack.enter_context(
        patch("services.position_analysis.load_runtime_etf_quotes", return_value={})
    )
    stack.enter_context(
        patch(
            "services.position_analysis.filter_current_etf_realtime_quotes",
            return_value={},
        )
    )
    stack.enter_context(
        patch("services.position_analysis.etf_intraday_quote_ready", return_value=False)
    )
    for name in (
        "etf_final_close_ready",
        "etf_morning_timing_fetch_ready",
        "etf_morning_timing_preview_ready",
        "etf_lunch_timing_fetch_ready",
        "etf_afternoon_timing_fetch_ready",
        "etf_lunch_timing_preview_ready",
        "etf_realtime_timing_ready",
    ):
        stack.enter_context(
            patch(f"services.position_analysis.{name}", return_value=False)
        )
    stack.enter_context(
        patch(
            "services.position_analysis.latest_final_etf_trade_date",
            return_value=date(2026, 8, 21),
        )
    )
    stack.enter_context(
        patch(
            "services.position_analysis.apply_etf_realtime_quotes_to_items",
            side_effect=lambda items, quotes: items,
        )
    )
    stack.enter_context(
        patch(
            "services.position_analysis.build_etf_timing_table",
            return_value=pd.DataFrame(),
        )
    )
    stack.enter_context(
        patch(
            "services.position_analysis.build_position_index_timing_table",
            return_value=pd.DataFrame(
                {
                    "指数名称": ["微盘股指数", "中证500"],
                    "代码": ["BK1158", "000905"],
                    "择时判断": ["持有", "空仓"],
                }
            ),
        )
    )
    stack.enter_context(
        patch(
            "components.position.realtime.build_recent_position_operation_guidance",
            return_value=pd.DataFrame(),
        )
    )
    stack.enter_context(
        patch(
            "services.position_analysis.build_position_timing_performance",
            return_value=PositionTimingPerformanceResult(
                errors=["测试缓存不足，暂不生成策略曲线。"]
            ),
        )
    )
    return etf, futures, spread, realtime_fetch, derivative_refresh


class PositionPageSmokeTests(unittest.TestCase):
    def test_quote_error_stays_visible_while_failed_batch_is_in_cooldown(self):
        class TradingDateTime(datetime):
            current = datetime(2026, 8, 21, 10, 30)

            @classmethod
            def now(cls, tz=None):
                return cls.current.replace(tzinfo=tz)

        with ExitStack() as stack:
            _, _, _, quotes, _ = _patch_page(stack, cached=True)
            stack.enter_context(patch.dict("os.environ", {"TICKFLOW_API_KEY": "test-only"}))
            for module in ("coordinator", "realtime"):
                stack.enter_context(patch(f"components.position.{module}.datetime", TradingDateTime))
            for name in ("etf_intraday_quote_ready", "etf_morning_timing_fetch_ready", "etf_morning_timing_preview_ready"):
                stack.enter_context(patch(f"services.position_analysis.{name}", return_value=True))
            quotes.side_effect = lambda codes, **kwargs: {
                code: {"price": 102.0, "quote_time": kwargs["market_now"]} for code in codes
            }
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(app.exception), [])
            quotes.side_effect = RuntimeError("TickFlow限流")
            TradingDateTime.current = datetime(2026, 8, 21, 10, 32)
            app.run()
            TradingDateTime.current = datetime(2026, 8, 21, 10, 34)
            app.run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(any("TickFlow限流" in warning.value for warning in app.warning))
            self.assertEqual(quotes.call_count, 2)

    def test_reopening_during_lunch_reuses_successful_quote_batches(self):
        from services import position_analysis as position

        class LunchDateTime(datetime):
            current = datetime(2026, 8, 21, 11, 35)

            @classmethod
            def now(cls, tz=None):
                return cls.current.replace(tzinfo=tz)

        with ExitStack() as stack:
            _, _, _, quotes, derivatives = _patch_page(stack, cached=True)
            stack.enter_context(patch.dict("os.environ", {"TICKFLOW_API_KEY": "test-only"}))
            for module in ("coordinator", "realtime"):
                stack.enter_context(patch(f"components.position.{module}.datetime", LunchDateTime))
            for name in ("etf_intraday_quote_ready", "etf_lunch_timing_fetch_ready", "etf_lunch_timing_preview_ready"):
                stack.enter_context(patch(f"services.position_analysis.{name}", return_value=True))
            quotes.side_effect = lambda codes, **kwargs: {
                code: {"symbol": code, "price": 102.0, "quote_time": kwargs["market_now"]}
                for code in codes
            }
            derivatives.side_effect = lambda items, **kwargs: (
                [item for item in items if item.category != "ETF"], []
            )
            indices = stack.enter_context(patch(
                "components.position.realtime.fetch_realtime_index_quotes",
                side_effect=lambda **kwargs: {
                    name: {"price": 100.0, "quote_time": kwargs["now"]}
                    for name in position.POSITION_INDEX_TIMING_STRATEGIES
                },
            ))
            first = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(first.exception), [])
            LunchDateTime.current = datetime(2026, 8, 21, 12, 15)
            reopened = AppTest.from_file(str(PAGE), default_timeout=20).run()

        self.assertEqual(list(reopened.exception), [])
        quotes.assert_called_once()
        derivatives.assert_called_once()
        indices.assert_called_once()
        self.assertEqual(
            reopened.session_state["position_etf_lunch_timing_preview"]["fetched_at"],
            "2026-08-21T11:35:00",
        )

    def test_reopening_outside_quote_hours_never_forces_realtime_refresh(self):
        for hour, minute, intraday in ((8, 30, False), (15, 2, True), (16, 0, False)):
            with self.subTest(hour=hour, minute=minute), ExitStack() as stack:
                etf, futures, spreads, quotes, derivatives = _patch_page(stack, cached=True)

                class ClosedDateTime(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return datetime(2026, 8, 21, hour, minute, tzinfo=tz)

                for module in ("coordinator", "realtime"):
                    stack.enter_context(patch(f"components.position.{module}.datetime", ClosedDateTime))
                stack.enter_context(patch("services.position_analysis.etf_intraday_quote_ready", return_value=intraday))
                stack.enter_context(patch("services.position_analysis.etf_final_close_ready", return_value=hour >= 16))
                quote_refresh = stack.enter_context(patch("services.position_analysis.refresh_runtime_etf_quotes"))
                close_fetch = stack.enter_context(patch("components.position.realtime.fetch_audited_close"))
                first = AppTest.from_file(str(PAGE), default_timeout=20).run()
                second = AppTest.from_file(str(PAGE), default_timeout=20).run()

                self.assertEqual(list(first.exception), [])
                self.assertEqual(list(second.exception), [])
                quotes.assert_not_called()
                quote_refresh.assert_not_called()
                derivatives.assert_not_called()
                close_fetch.assert_not_called()
                for loader in (etf, futures, spreads):
                    self.assertTrue(all(not call.kwargs.get("force_refresh", False) for call in loader.call_args_list))

    def test_reopened_partial_lunch_batch_retries_after_ten_minutes(self):
        class LunchDateTime(datetime):
            current = datetime(2026, 8, 21, 11, 35)

            @classmethod
            def now(cls, tz=None):
                return cls.current.replace(tzinfo=tz)

        batches = []

        def fetch_quotes(codes, **kwargs):
            batches.append(list(codes))
            selected = codes[:-1] if len(batches) == 1 else codes
            return {
                code: {"price": 102.0, "quote_time": kwargs["market_now"]}
                for code in selected
            }

        with ExitStack() as stack:
            _, _, _, quotes, _ = _patch_page(stack, cached=True)
            quotes.side_effect = fetch_quotes
            stack.enter_context(patch.dict("os.environ", {"TICKFLOW_API_KEY": "test-only"}))
            for module in ("coordinator", "realtime"):
                stack.enter_context(patch(f"components.position.{module}.datetime", LunchDateTime))
            for name in ("etf_intraday_quote_ready", "etf_lunch_timing_fetch_ready", "etf_lunch_timing_preview_ready"):
                stack.enter_context(patch(f"services.position_analysis.{name}", return_value=True))
            first = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(first.exception), [])
            self.assertEqual(quotes.call_count, 1)
            self.assertLess(
                len(first.session_state["position_etf_lunch_timing_preview"]["received_scope"]),
                len(batches[0]),
            )
            LunchDateTime.current = datetime(2026, 8, 21, 11, 40)
            reopened = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(reopened.exception), [])
            self.assertEqual(quotes.call_count, 1)
            LunchDateTime.current = datetime(2026, 8, 21, 11, 45)
            retry = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(retry.exception), [])
            self.assertEqual(quotes.call_count, 2)
            self.assertEqual(
                set(retry.session_state["position_etf_lunch_timing_preview"]["received_scope"]),
                set(batches[0]),
            )

    def test_missing_formal_close_still_backfills_once_and_reopen_reuses_it(self):
        from dataclasses import replace

        class ClosedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 21, 16, 0, tzinfo=tz)

        completed = set()

        def load_etf(code, **kwargs):
            item = _item("ETF", code, cached=True)
            return item if code in completed else replace(item, latest_date="2026-08-20")

        def fetch_close(code, **kwargs):
            completed.add(code)
            return _item("ETF", code, cached=True)

        with ExitStack() as stack:
            etf, _, _, quotes, derivatives = _patch_page(stack, cached=True)
            etf.side_effect = load_etf
            for module in ("coordinator", "realtime"):
                stack.enter_context(patch(f"components.position.{module}.datetime", ClosedDateTime))
            stack.enter_context(patch("services.position_analysis.etf_final_close_ready", return_value=True))
            close_fetch = stack.enter_context(patch("components.position.realtime.fetch_audited_close", side_effect=fetch_close))
            first = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(first.exception), [])
            self.assertGreater(close_fetch.call_count, 0)
            self.assertEqual(close_fetch.call_count, len(completed))
            close_fetch.reset_mock()
            reopened = AppTest.from_file(str(PAGE), default_timeout=20).run()

        self.assertEqual(list(reopened.exception), [])
        close_fetch.assert_not_called()
        quotes.assert_not_called()
        derivatives.assert_not_called()

    def test_timing_performance_component_renders_metrics_chart_and_detail(self):
        daily = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                "每日盈亏": [0.0, 100.0],
                "每日收益率(%)": [0.0, 0.02],
                "累计盈亏": [0.0, 100.0],
                "累计收益率(%)": [0.0, 0.02],
                "净值": [1.0, 1.0002],
                "账户资产": [500_000.0, 500_100.0],
                "持仓市值": [225_000.0, 225_100.0],
                "现金": [275_000.0, 275_000.0],
            }
        )
        result = PositionTimingPerformanceResult(
            daily=daily,
            positions=pd.DataFrame(
                {
                    "基金名称": ["纳指ETF嘉实"],
                    "代码": ["159501"],
                    "持仓数量": [100],
                    "成本价": [2.0],
                    "最新价": [2.1],
                    "持仓市值": [210.0],
                    "当日盈亏": [10.0],
                    "当日收益率(%)": [5.0],
                    "当日收益基数": [200.0],
                    "浮动盈亏": [10.0],
                    "账户权重(%)": [0.04],
                    "来源袖套": ["159501"],
                    "累计手续费": [0.01],
                }
            ),
            trades=pd.DataFrame(
                {
                    "标的名称": ["纳指ETF嘉实"],
                    "代码": ["159501"],
                    "配置比例(%)": [15.0],
                    "日期": pd.to_datetime(["2026-08-05"]),
                    "交易标的": ["159501"],
                    "交易标的名称": ["纳指ETF嘉实"],
                    "操作": ["买入"],
                    "成交价": [2.0],
                    "份额": [100],
                    "成交金额": [200.0],
                    "手续费": [0.01],
                    "本次交易盈亏金额": [pd.NA],
                    "本次交易盈亏率(%)": [pd.NA],
                    "现金余额": [74_999.99],
                    "原因": ["开始日新买入信号"],
                }
            ),
            summary={
                "最新估值日期": "2026-08-06",
                "策略资产": 500_100.0,
                "累计盈亏": 100.0,
                "累计收益率(%)": 0.02,
                "当前净值": 1.0002,
                "当前持仓市值": 225_100.0,
                "当前现金": 275_000.0,
                "当前仓位比例(%)": 45.01,
                "初始手续费": 10.0,
                "后续交易费用": 1.0,
                "正式数据截止日": "2026-08-06",
            },
        )
        source = """
from components.position.performance import render_position_timing_performance
render_position_timing_performance([])
"""
        with patch(
            "services.position_analysis.build_position_timing_performance",
            return_value=result,
        ):
            app = AppTest.from_string(source, default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(
            [item.value for item in app.subheader],
            ["50万元ETF均线策略每日盈亏", "50万元ETF策略收益日历"],
        )
        self.assertEqual(
            [item.label for item in app.tabs],
            ["策略持仓情况", "策略交易明细", "每日盈亏明细"],
        )
        segmented_labels = [item.label for item in app.segmented_control]
        self.assertIn("时间范围", segmented_labels)
        self.assertIn("统计周期", segmented_labels)
        self.assertIn("显示口径", segmented_labels)
        self.assertEqual(len(app.get("plotly_chart")), 1)
        position_tables = [
            item.value
            for item in app.markdown
            if "position-data-table" in item.value
        ]
        self.assertEqual(len(position_tables), 1)
        self.assertIn("正式收盘", position_tables[0])
        self.assertIn("当日盈亏", position_tables[0])
        self.assertIn("2.100", position_tables[0])
        self.assertIn("2.000", position_tables[0])
        self.assertIn("合计", position_tables[0])

    def test_timing_performance_chart_uses_trading_day_category_axis(self):
        source = (
            Path(__file__).parents[1]
            / "components"
            / "position"
            / "performance.py"
        ).read_text(encoding="utf-8")

        self.assertIn('type="category"', source)
        self.assertIn("categoryarray=chart_dates.tolist()", source)
        self.assertIn("周末和节假日已自动跳过", source)

    def test_timing_performance_tables_show_prices_with_three_decimals(self):
        source = (
            Path(__file__).parents[1]
            / "components"
            / "position"
            / "performance.py"
        ).read_text(encoding="utf-8")

        self.assertIn('position_number_cell(row["成本价"], digits=3)', source)
        self.assertIn('position_number_cell(row["最新价"], digits=3)', source)
        self.assertIn(
            'position_pnl_cell(row["当日盈亏"], row["当日收益率(%)"])',
            source,
        )
        self.assertIn(
            '"成交价": st.column_config.NumberColumn(format="%.3f")',
            source,
        )
        self.assertIn(
            "render_position_table(headers, rows, total_cells=total_cells",
            source,
        )

    def test_strategy_positions_overlay_realtime_quote_for_display_only(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from components.position.performance import (
            _overlay_strategy_positions_with_realtime,
        )

        formal = pd.DataFrame(
            {
                "基金名称": ["纳指ETF嘉实"],
                "代码": ["159501"],
                "持仓数量": [100],
                "成本价": [2.0],
                "最新价": [2.1],
                "持仓市值": [210.0],
                "当日盈亏": [5.0],
                "当日收益率(%)": [2.5],
                "当日收益基数": [200.0],
                "浮动盈亏": [10.0],
                "账户权重(%)": [17.36],
                "来源袖套": ["159501"],
                "累计手续费": [0.01],
            }
        )
        display, summary = _overlay_strategy_positions_with_realtime(
            formal,
            {
                "159501.SZ": {
                    "price": 2.2,
                    "previous_close": 2.1,
                    "quote_time": "2026-08-31 14:54:03+08:00",
                }
            },
            formal_cash=1_000.0,
            market_now=datetime(2026, 8, 31, 14, 54, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(float(formal.iloc[0]["最新价"]), 2.1)
        self.assertEqual(float(display.iloc[0]["最新价"]), 2.2)
        self.assertAlmostEqual(float(display.iloc[0]["持仓市值"]), 220.0)
        self.assertAlmostEqual(float(display.iloc[0]["当日盈亏"]), 10.0)
        self.assertAlmostEqual(float(display.iloc[0]["当日收益率(%)"]), 10 / 210 * 100)
        self.assertAlmostEqual(float(display.iloc[0]["浮动盈亏"]), 20.0)
        self.assertEqual(display.iloc[0]["行情状态"], "实时")
        self.assertEqual(display.iloc[0]["行情时间"], "2026-08-31 14:54:03")
        self.assertEqual(summary["实时行情数量"], 1)
        self.assertEqual(float(summary["策略资产"]), 1_220.0)

    def test_strategy_positions_receive_existing_shared_quotes(self):
        source = (
            Path(__file__).parents[1]
            / "components"
            / "position"
            / "realtime.py"
        ).read_text(encoding="utf-8")

        self.assertIn("realtime_quotes=active_preview_quotes", source)
        self.assertIn("market_now=market_now", source)

    def test_default_render_automatically_loads_holdings(self):
        with ExitStack() as stack:
            etf, futures, spread, realtime_fetch, derivative_refresh = _patch_page(
                stack,
                cached=False,
            )
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(app.button[0].label, "加载持仓信息")
        self.assertFalse(app.button[0].value)
        self.assertTrue(etf.call_args_list)
        self.assertTrue(futures.call_args_list)
        self.assertTrue(spread.call_args_list)
        self.assertTrue(any(call.kwargs["allow_fetch"] for call in etf.call_args_list))
        self.assertTrue(
            any(call.kwargs["allow_fetch"] for call in futures.call_args_list)
        )
        self.assertTrue(
            any(call.kwargs["allow_fetch"] for call in spread.call_args_list)
        )
        realtime_fetch.assert_not_called()
        derivative_refresh.assert_not_called()
        self.assertTrue(app.session_state["position_updates_enabled"])
        subheaders = [item.value for item in app.subheader]
        self.assertNotIn("实盘账户", subheaders)
        self.assertTrue(subheaders[0].startswith("分析数据状态 · "))
        self.assertEqual(
            [item.label for item in app.metric[:4]],
            ["分析标的", "可用数据", "缺失缓存", "获取失败"],
        )
        self.assertLess(subheaders.index("ETF择时状态"), subheaders.index("指数择时参考"))
        self.assertLess(subheaders.index("指数择时参考"), subheaders.index("近一周操作指引"))
        self.assertLess(
            subheaders.index("近一周操作指引"),
            subheaders.index("50万元ETF均线策略每日盈亏"),
        )

    def test_cached_etf_futures_and_spreads_render_without_network(self):
        with ExitStack() as stack:
            etf, futures, spread, realtime_fetch, derivative_refresh = _patch_page(
                stack,
                cached=True,
            )
            app = AppTest.from_file(str(PAGE), default_timeout=20)
            app.session_state["position_initial_load_completed"] = True
            app.session_state["position_updates_enabled"] = True
            app.run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(
            [item.label for item in app.text_area],
            ["ETF持仓", "期货持仓", "期货价差"],
        )
        self.assertEqual(app.text_area[1].value, "I2701")
        self.assertEqual(app.checkbox[0].label, "强制重新检查已是最新的ETF缓存")
        self.assertFalse(app.checkbox[0].value)
        self.assertEqual(app.checkbox[1].label, "更新后保存到本地缓存")
        self.assertTrue(app.checkbox[1].value)
        self.assertTrue(any(call.args[0] == "I2701" for call in futures.call_args_list))
        self.assertEqual(
            [call.args[0] for call in spread.call_args_list],
            [["I2701", "I2705"], ["IM2610", "IM2703"]],
        )
        self.assertTrue(etf.call_args_list)
        realtime_fetch.assert_not_called()
        derivative_refresh.assert_not_called()

    def test_load_click_refreshes_derivatives_once_when_formal_cache_is_current(self):
        with ExitStack() as stack:
            _, _, _, _, derivative_refresh = _patch_page(stack, cached=True)
            stack.enter_context(patch("services.position_analysis.etf_intraday_quote_ready", return_value=True))
            stack.enter_context(patch("services.position_analysis.refresh_runtime_etf_quotes", return_value={}))
            derivative_refresh.side_effect = None
            derivative_refresh.return_value = ([], [])
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()
            derivative_refresh.assert_called_once()
            derivative_refresh.reset_mock()

            app.button[0].click().run()
            app.run()

        self.assertEqual(list(app.exception), [])
        derivative_refresh.assert_called_once()
        refreshed_items = derivative_refresh.call_args.args[0]
        self.assertEqual(
            [item.category for item in refreshed_items if item.category != "ETF"],
            ["期货", "期货价差", "期货价差"],
        )
        self.assertEqual(app.session_state["position_derivative_refresh_request"], 1)
        self.assertEqual(app.session_state["position_derivative_refresh_consumed"], 1)

    def test_open_during_trading_loads_quotes_once_and_reruns_reuse_cache(self):
        with ExitStack() as stack:
            etf, futures, spread, _, derivative_refresh = _patch_page(stack, cached=True)
            stack.enter_context(
                patch("services.position_analysis.etf_intraday_quote_ready", return_value=True)
            )
            quote_refresh = stack.enter_context(
                patch("services.position_analysis.refresh_runtime_etf_quotes", return_value={})
            )
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(app.exception), [])
            quote_refresh.assert_called_once()
            derivative_refresh.assert_called_once()
            self.assertTrue(all(not call.kwargs.get("force_refresh", False) for call in etf.call_args_list))
            etf.reset_mock()
            futures.reset_mock()
            spread.reset_mock()

            app.run()

        self.assertEqual(list(app.exception), [])
        quote_refresh.assert_called_once()
        derivative_refresh.assert_called_once()
        for loader in (etf, futures, spread):
            self.assertTrue(loader.call_args_list)
            self.assertTrue(all(not call.kwargs["allow_fetch"] for call in loader.call_args_list))

    def test_fresh_cards_render_before_history_backfill_on_open_and_manual_load(self):
        from components.position.cards_tables import render_position_cards
        from services.position_runtime import apply_etf_realtime_quotes_to_items

        events = []
        quote = {"159967": {"price": 111.0, "quote_time": "2026-09-30 10:10:00"}}

        def load_etf(code, **kwargs):
            if kwargs["allow_fetch"]:
                events.append("history")
            return _item("ETF", code, cached=True)

        def draw_cards(items):
            item = next(item for item in items if item.code == "159967")
            events.append(("cards", item.metrics["最新价"]))
            render_position_cards(items)

        with ExitStack() as stack:
            etf, _, _, _, _ = _patch_page(stack, cached=True)
            etf.side_effect = load_etf
            stack.enter_context(patch("services.position_analysis.etf_intraday_quote_ready", return_value=True))
            stack.enter_context(patch("services.position_analysis.refresh_runtime_etf_quotes", return_value=quote))
            stack.enter_context(patch("services.position_analysis.remember_runtime_etf_quotes"))
            stack.enter_context(patch("services.position_analysis.filter_current_etf_realtime_quotes", return_value=quote))
            stack.enter_context(patch("services.position_analysis.apply_etf_realtime_quotes_to_items", side_effect=apply_etf_realtime_quotes_to_items))
            stack.enter_context(patch("components.position.coordinator.render_position_cards", side_effect=draw_cards))
            stack.enter_context(patch("components.position.realtime.render_position_cards", side_effect=draw_cards))
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(app.exception), [])
            self.assertLess(events.index(("cards", 101.0)), events.index("history"))
            self.assertLess(events.index(("cards", 111.0)), events.index("history"))
            events.clear()

            app.button[0].click().run()

        self.assertEqual(list(app.exception), [])
        self.assertLess(events.index(("cards", 111.0)), events.index("history"))

    def test_etf_two_minute_refresh_preserves_derivative_cadence(self):
        class TradingDateTime(datetime):
            current = datetime(2026, 8, 21, 10, 30)

            @classmethod
            def now(cls, tz=None):
                return cls.current.replace(tzinfo=tz)

        with ExitStack() as stack:
            _, _, _, _, derivative_refresh = _patch_page(stack, cached=True)
            for module in ("coordinator", "realtime"):
                stack.enter_context(patch(f"components.position.{module}.datetime", TradingDateTime))
            stack.enter_context(patch("services.position_analysis.etf_intraday_quote_ready", return_value=True))
            stack.enter_context(patch.dict("os.environ", {"TICKFLOW_API_KEY": "test-only"}))
            derivative_refresh.side_effect = None
            derivative_refresh.return_value = ([], [])
            stack.enter_context(
                patch(
                    "services.position_analysis.etf_morning_timing_fetch_ready",
                    return_value=True,
                )
            )
            quotes = stack.enter_context(
                patch(
                    "services.position_analysis.refresh_runtime_etf_quotes",
                    return_value={},
                )
            )
            stack.enter_context(
                patch(
                    "components.position.realtime.fetch_realtime_index_quotes",
                    side_effect=lambda **kwargs: {
                        name: {"price": 100.0, "quote_time": kwargs["now"]}
                        for name in kwargs["force_index_names"]
                    },
                )
            )
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()
            self.assertEqual(list(app.exception), [])
            quotes.reset_mock()
            derivative_refresh.reset_mock()
            TradingDateTime.current = datetime(2026, 8, 21, 10, 32)
            app.run()
            self.assertEqual(list(app.exception), [])
            derivative_refresh.assert_not_called()
            quotes.assert_called_once()
            TradingDateTime.current = datetime(2026, 8, 21, 11, 0)
            app.run()

        self.assertEqual(list(app.exception), [])
        derivative_refresh.assert_called_once()

    def test_cached_option_detail_component_is_network_free(self):
        source = """
import pandas as pd
from components.position.details import render_position_detail
from services.position_analysis import PositionItem

item = PositionItem(
    "期权",
    "I2609P730",
    "铁矿石看跌期权",
    "缓存",
    source="本地缓存",
    latest_date="2026-08-21",
    metrics={"最新收盘": 16.0, "日涨跌(%)": 1.0},
    dataframe=pd.DataFrame({
        "date": pd.to_datetime(["2026-08-20", "2026-08-21"]),
        "close": [15.0, 16.0],
        "volume": [100, 120],
        "open_interest": [500, 510],
    }),
)
render_position_detail(item)
"""
        with patch("services.position_analysis.load_or_fetch_option") as load_option:
            app = AppTest.from_string(source, default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(
            [item.label for item in app.tabs],
            ["走势", "成交持仓", "摘要", "数据"],
        )
        load_option.assert_not_called()

    def test_etf_timing_table_renders_multiline_headers(self):
        source = """
import pandas as pd
from components.position.cards_tables import render_etf_timing_table

df = pd.DataFrame([{
    "ETF名称": "红利低波",
    "代码": "512890",
    "组合权重": "35%",
    "最新价": 1.025,
    "当日涨跌幅(%)": 0.35,
    "偏离率(%)": 0.69,
    "状态转换时间": "2026-09-11",
    "区间涨幅(%)": 2.35,
    "上一状态转换时间": "2026-08-15",
    "上一区间涨幅(%)": 5.12,
    "数据截止日": "2026-09-11",
    "择时判断": "持有",
}])
render_etf_timing_table(df, value_formatter=lambda col, val: str(val))
"""
        app = AppTest.from_string(source, default_timeout=20).run()
        self.assertEqual(list(app.exception), [])
        markdown_body = app.markdown[0].value
        self.assertIn("<th>当日涨跌<br>幅(%)</th>", markdown_body)
        self.assertIn("<th>偏离率<br>(%)</th>", markdown_body)
        self.assertIn("<th>状态转换<br>时间</th>", markdown_body)
        self.assertIn("<th>区间涨幅<br>(%)</th>", markdown_body)
        self.assertIn("<th>上一状态<br>转换时间</th>", markdown_body)
        self.assertIn("<th>上一区间<br>涨幅(%)</th>", markdown_body)
        self.assertIn("<th>数据<br>截止日</th>", markdown_body)
        self.assertIn("<th>组合<br>权重</th>", markdown_body)
        self.assertIn("<th>ETF名称</th>", markdown_body)


if __name__ == "__main__":
    unittest.main()
