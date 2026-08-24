import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest


class LiveRecordPageSmokeTests(unittest.TestCase):
    def test_default_empty_render_is_cache_first_and_never_fetches(self):
        page_path = Path(__file__).parents[1] / "pages" / "6_实盘记录.py"
        with (
            patch("core.db.init_db"),
            patch("services.live_trading.list_live_trades", return_value=pd.DataFrame()),
            patch("services.position_analysis.load_or_fetch_etf") as fetch_mock,
        ):
            app = AppTest.from_file(str(page_path), default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        fetch_mock.assert_not_called()
        self.assertEqual(
            [item.value for item in app.subheader],
            ["当前实盘持仓", "每日收盘盈亏", "新增成交", "成交明细", "历史盈亏"],
        )
        self.assertEqual(app.number_input[0].label, "数量")
        self.assertEqual(app.number_input[0].value, 0)
        self.assertEqual(app.number_input[1].label, "成交价格")
        self.assertEqual(app.number_input[1].value, 0.0)
        self.assertEqual(app.number_input[2].label, "手续费率(%)")
        self.assertEqual(app.number_input[2].value, 0.006)

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
            patch("core.db.init_db"),
            patch("services.live_trading.list_live_trades", return_value=trades),
            patch("services.live_trading.live_close_refresh_due", return_value=False),
            patch(
                "services.position_analysis.load_or_fetch_etf",
                return_value=cached_item,
            ) as fetch_mock,
        ):
            app = AppTest.from_file(str(page_path), default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertEqual(
            [call.kwargs["allow_fetch"] for call in fetch_mock.call_args_list],
            [False, False],
        )
        self.assertEqual(
            [call.kwargs["save_to_cache"] for call in fetch_mock.call_args_list],
            [True, False],
        )


if __name__ == "__main__":
    unittest.main()
