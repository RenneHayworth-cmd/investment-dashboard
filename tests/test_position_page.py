from contextlib import ExitStack
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from services.position_analysis import PositionItem


PAGE = Path(__file__).parents[1] / "pages" / "5_持仓分析.py"


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
            side_effect=AssertionError("点击加载前不应刷新期货或价差"),
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
            "services.position_analysis.build_recent_etf_operation_guidance",
            return_value=pd.DataFrame(),
        )
    )
    return etf, futures, spread, realtime_fetch, derivative_refresh


class PositionPageSmokeTests(unittest.TestCase):
    def test_default_render_is_cache_first_and_network_disabled(self):
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
        self.assertTrue(all(not call.kwargs["allow_fetch"] for call in etf.call_args_list))
        self.assertTrue(
            all(not call.kwargs["allow_fetch"] for call in futures.call_args_list)
        )
        self.assertTrue(
            all(not call.kwargs["allow_fetch"] for call in spread.call_args_list)
        )
        realtime_fetch.assert_not_called()
        derivative_refresh.assert_not_called()
        self.assertFalse(app.session_state["position_updates_enabled"])

    def test_cached_etf_futures_and_spreads_render_without_network(self):
        with ExitStack() as stack:
            etf, futures, spread, realtime_fetch, derivative_refresh = _patch_page(
                stack,
                cached=True,
            )
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(
            [item.label for item in app.text_area],
            ["ETF持仓", "期货持仓", "期货价差"],
        )
        self.assertEqual(app.text_area[1].value, "I2609")
        self.assertEqual(app.checkbox[0].label, "强制重新检查已是最新的ETF缓存")
        self.assertFalse(app.checkbox[0].value)
        self.assertEqual(app.checkbox[1].label, "更新后保存到本地缓存")
        self.assertTrue(app.checkbox[1].value)
        self.assertTrue(any(call.args[0] == "I2609" for call in futures.call_args_list))
        self.assertEqual(
            [call.args[0] for call in spread.call_args_list],
            [["I2609", "I2705"], ["IM2609", "IM2703"]],
        )
        self.assertTrue(etf.call_args_list)
        realtime_fetch.assert_not_called()
        derivative_refresh.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
