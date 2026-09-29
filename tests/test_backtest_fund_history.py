import unittest
from unittest.mock import patch

import pandas as pd

from services import position_market
from services.fund_analysis import TickFlowNoDataError


def _history(source: str) -> pd.DataFrame:
    frame = pd.DataFrame({"日期": pd.to_datetime(["2026-09-23", "2026-09-24"]), "收盘价": [1.0, 1.1]})
    frame.attrs["position_history_source"] = source
    return frame


class BacktestFundHistoryTests(unittest.TestCase):
    def test_lof_without_tickflow_bars_uses_exchange_history(self):
        with (
            patch.object(position_market, "fetch_tickflow_fund_close", side_effect=TickFlowNoDataError("TickFlow 未返回 164906.SZ 的日线数据。")),
            patch.object(position_market, "_fetch_exchange_fund_close", return_value=_history("东方财富/AkShare")) as exchange,
        ):
            result = position_market.fetch_backtest_fund_close(symbol="164906.SZ", count=100, adjust="forward_additive")
        exchange.assert_called_once()
        self.assertEqual(result.attrs["position_history_source"], "东方财富/AkShare")

    def test_configured_exchange_history_code_skips_tickflow(self):
        with (
            patch.object(position_market, "fetch_tickflow_fund_close") as tickflow,
            patch.object(position_market, "_fetch_exchange_fund_close", return_value=_history("东方财富/AkShare")),
        ):
            position_market.fetch_backtest_fund_close(symbol="161128.SZ", count=100, adjust="forward_additive")
        tickflow.assert_not_called()

    def test_transient_tickflow_error_does_not_switch_source(self):
        with (
            patch.object(position_market, "fetch_tickflow_fund_close", side_effect=RuntimeError("rate limited")),
            patch.object(position_market, "_fetch_exchange_fund_close") as exchange,
        ):
            with self.assertRaises(RuntimeError):
                position_market.fetch_backtest_fund_close(symbol="512890.SH", count=100, adjust="forward_additive")
        exchange.assert_not_called()

    def test_both_sources_failing_reports_each(self):
        with (
            patch.object(position_market, "fetch_tickflow_fund_close", side_effect=TickFlowNoDataError("TickFlow 未返回 X 的日线数据。")),
            patch.object(position_market, "_fetch_exchange_fund_close", side_effect=ValueError("东方财富不支持比例复权")),
        ):
            with self.assertRaises(ValueError) as caught:
                position_market.fetch_backtest_fund_close(symbol="164906.SZ", count=100, adjust="forward")
        self.assertIn("TickFlow", str(caught.exception))
        self.assertIn("备用源", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
