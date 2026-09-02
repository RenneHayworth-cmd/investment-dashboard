import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from streamlit.testing.v1 import AppTest

from components.live_record.dashboard import _load_shared_position_quotes


class LiveRecordPageSmokeTests(unittest.TestCase):
    def test_shared_holdings_quotes_merge_process_and_session_state(self):
        market_now = datetime(2026, 8, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        process_quotes = {
            "159501": {
                "price": 1.2,
                "quote_time": market_now.replace(hour=9, minute=58),
            }
        }
        session_quotes = {
            "159501": {
                "price": 1.3,
                "quote_time": market_now,
            },
            "510500": {
                "price": 6.1,
                "quote_time": market_now,
            },
        }

        with patch(
            "components.live_record.dashboard.load_runtime_etf_quotes",
            return_value=process_quotes,
        ):
            result = _load_shared_position_quotes(
                market_now=market_now,
                session_quotes=session_quotes,
            )

        self.assertEqual(result["159501"]["price"], 1.3)
        self.assertEqual(result["510500"]["price"], 6.1)

    def test_default_empty_render_is_cache_first_and_never_fetches(self):
        page_path = Path(__file__).parents[1] / "pages" / "6_实盘记录.py"
        with (
            patch.dict("os.environ", {"TICKFLOW_API_KEY": "test_key"}),
            patch("core.db.init_db"),
            patch("services.live_trading.list_live_trades", return_value=pd.DataFrame()),
            patch("components.live_record.dashboard.list_live_trades", return_value=pd.DataFrame()),
            patch("components.live_record.dashboard.list_live_cash_flows", return_value=pd.DataFrame()),
            patch(
                "components.live_record.dashboard.refresh_runtime_etf_quotes",
                return_value={},
            ),
            patch("services.position_analysis.load_or_fetch_etf") as fetch_mock,
        ):
            app = AppTest.from_file(str(page_path), default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        fetch_mock.assert_not_called()
        subheaders = [item.value for item in app.subheader]
        self.assertIn("实盘账户", subheaders)
        self.assertIn("每日正式收盘盈亏", subheaders)
        self.assertIn("新增成交", subheaders)
        self.assertIn("新增资金流水", subheaders)
        self.assertIn("资金流水明细", subheaders)
        self.assertIn("历史盈亏", subheaders)
        self.assertEqual(app.number_input[0].label, "数量")
        self.assertEqual(app.number_input[0].value, 0)
        self.assertEqual(app.number_input[1].label, "成交价格")
        self.assertEqual(app.number_input[1].value, 0.0)
        self.assertEqual(app.number_input[2].label, "手续费率(%)")
        self.assertEqual(app.number_input[2].value, 0.006)
        self.assertNotIn("TickFlow API Key", [item.label for item in app.text_input])
        self.assertNotIn("实时行情", [item.label for item in app.expander])

    def test_cached_holding_render_keeps_both_price_reads_network_disabled(self):
        page_path = Path(__file__).parents[1] / "pages" / "6_实盘记录.py"
        trades = pd.DataFrame(
            [
                {
                    "id": 1,
                    "trade_date": "2026-08-01",
                    "symbol": "159501",
                    "name": "纳指ETF",
                    "side": "买入",
                    "price": 1.2,
                    "quantity": 100,
                    "fee_rate_pct": 0.006,
                    "strategy": "",
                    "notes": "",
                    "created_at": "2026-08-01 15:00:00",
                }
            ]
        )
        cached_item = SimpleNamespace(
            dataframe=pd.DataFrame(),
            latest_date="2026-08-22",
            error=None,
            status="缓存",
        )
        with (
            patch.dict("os.environ", {"TICKFLOW_API_KEY": "test_key"}),
            patch("core.db.init_db"),
            patch("services.live_trading.list_live_trades", return_value=trades),
            patch("components.live_record.dashboard.list_live_trades", return_value=trades),
            patch("components.live_record.dashboard.list_live_cash_flows", return_value=pd.DataFrame()),
            patch("services.live_trading.live_close_refresh_due", return_value=False),
            patch("components.live_record.dashboard.live_close_refresh_due", return_value=False),
            patch(
                "components.live_record.dashboard.refresh_runtime_etf_quotes",
                return_value={},
            ) as realtime_mock,
            patch(
                "services.position_analysis.load_or_fetch_etf",
                return_value=cached_item,
            ) as fetch_mock,
            patch(
                "components.live_record.dashboard.load_or_fetch_etf",
                return_value=cached_item,
            ) as dashboard_fetch_mock,
        ):
            app = AppTest.from_file(str(page_path), default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(fetch_mock.call_count, 1)
        self.assertEqual(dashboard_fetch_mock.call_count, 1)
        self.assertEqual(
            [call.kwargs["allow_fetch"] for call in dashboard_fetch_mock.call_args_list],
            [False],
        )
        self.assertEqual(
            [call.kwargs["save_to_cache"] for call in fetch_mock.call_args_list],
            [False],
        )
        realtime_mock.assert_called_once()
        self.assertEqual(realtime_mock.call_args.args[0], ["159501"])

    def test_initialized_account_renders_summary_chart_and_detail_tabs(self):
        page_path = Path(__file__).parents[1] / "pages" / "6_实盘记录.py"
        trades = pd.DataFrame(
            [
                {
                    "id": 1,
                    "trade_date": "2026-08-25",
                    "trade_time": "10:00:00",
                    "symbol": "159501",
                    "name": "纳指ETF",
                    "side": "买入",
                    "price": 1.2,
                    "quantity": 100,
                    "fee_rate_pct": 0.006,
                    "strategy": "",
                    "notes": "",
                    "created_at": "2026-08-25 10:00:00",
                }
            ]
        )
        cash_flows = pd.DataFrame(
            [
                {
                    "id": 1,
                    "flow_date": "2026-08-25",
                    "flow_time": None,
                    "entry_type": "期初资金",
                    "amount": 10_000.0,
                    "symbol": None,
                    "notes": "",
                    "created_at": "2026-08-25 09:00:00",
                }
            ]
        )
        history = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-25", "2026-08-26"]),
                "price": [1.2, 1.3],
            }
        )
        cached_item = SimpleNamespace(
            dataframe=history,
            latest_date="2026-08-26",
            error=None,
            status="缓存",
        )
        with (
            patch.dict("os.environ", {"TICKFLOW_API_KEY": "test_key"}),
            patch("core.db.init_db"),
            patch("services.live_trading.list_live_trades", return_value=trades),
            patch("services.live_trading.list_live_cash_flows", return_value=cash_flows),
            patch("components.live_record.dashboard.list_live_trades", return_value=trades),
            patch("components.live_record.dashboard.list_live_cash_flows", return_value=cash_flows),
            patch("components.live_record.dashboard.live_close_refresh_due", return_value=False),
            patch("components.live_record.dashboard.refresh_runtime_etf_quotes", return_value={}),
            patch("components.live_record.dashboard.load_or_fetch_etf", return_value=cached_item),
            patch("services.position_analysis.load_or_fetch_etf", return_value=cached_item),
        ):
            app = AppTest.from_file(str(page_path), default_timeout=20).run()
            scope_radio = next(item for item in app.radio if item.label == "收益口径")
            app = scope_radio.set_value("持仓口径").run()

        self.assertEqual(list(app.exception), [])
        self.assertGreaterEqual(len(app.get("plotly_chart")), 1)
        self.assertIn("实盘账户", [item.value for item in app.subheader])
        tab_labels = [item.label for item in app.tabs]
        self.assertIn("持仓情况", tab_labels)
        self.assertIn("标的详情", tab_labels)
        self.assertIn("交易明细", tab_labels)


if __name__ == "__main__":
    unittest.main()
