import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from services.fund_analysis import (
    FUND_ADJUSTMENT_VALUES,
    FUND_ADJUST_NONE,
    _fetch_eastmoney_fund_nav_page,
    _request_eastmoney_page,
    analyze_fund_nav,
    fetch_tickflow_fund_close,
    fetch_eastmoney_fund_name,
    normalize_fund_adjustment,
    normalize_nav_dataframe,
)


class TickFlowAdjustmentTests(unittest.TestCase):
    @patch("services.fund_analysis.fetch_tickflow_instrument_name", return_value="测试ETF")
    def test_tickflow_receives_each_explicit_adjustment_mode(self, _name_mock):
        klines = Mock()
        klines.get.return_value = pd.DataFrame(
            {
                "trade_date": ["2026-08-11", "2026-08-12"],
                "open": [1.0, 1.1],
                "close": [1.1, 1.2],
            }
        )
        client = SimpleNamespace(klines=klines)

        class FakeTickFlow:
            def __new__(cls, *args, **kwargs):
                return client

            @classmethod
            def free(cls):
                return client

        with patch.dict(sys.modules, {"tickflow": SimpleNamespace(TickFlow=FakeTickFlow)}):
            for mode in sorted(FUND_ADJUSTMENT_VALUES):
                result = fetch_tickflow_fund_close(
                    "159545.SZ",
                    api_key="test-key",
                    count=2,
                    adjust=mode,
                )
                self.assertTrue(result["_adjust_mode"].eq(mode).all())

            legacy_none = fetch_tickflow_fund_close(
                "159545.SZ",
                api_key="test-key",
                count=2,
                adjust=None,
            )

        passed_modes = [call.kwargs["adjust"] for call in klines.get.call_args_list]
        self.assertEqual(passed_modes[:-1], sorted(FUND_ADJUSTMENT_VALUES))
        self.assertEqual(passed_modes[-1], FUND_ADJUST_NONE)
        self.assertTrue(legacy_none["_adjust_mode"].eq(FUND_ADJUST_NONE).all())

    def test_adjustment_validation_rejects_unknown_values(self):
        self.assertEqual(normalize_fund_adjustment(None), FUND_ADJUST_NONE)
        with self.assertRaisesRegex(ValueError, "不支持的复权方式"):
            normalize_fund_adjustment("qfq")


class EastMoneyResponseTests(unittest.TestCase):
    @patch("services.fund_analysis.requests.get")
    def test_page_request_rejects_non_object_json(self, get_mock):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = []
        get_mock.return_value = response

        with self.assertRaisesRegex(ValueError, "响应格式异常"):
            _request_eastmoney_page("512890", page_index=1, page_size=20)

    @patch("services.fund_analysis._request_eastmoney_page")
    def test_nav_page_rejects_missing_data_field(self, request_mock):
        request_mock.return_value = {"ErrCode": 0, "TotalCount": 1}

        with self.assertRaisesRegex(ValueError, "Data"):
            _fetch_eastmoney_fund_nav_page("512890", page_index=1, page_size=20)

    @patch("services.fund_analysis.requests.get", side_effect=RuntimeError("network unavailable"))
    def test_name_lookup_logs_fallback_without_changing_return_value(self, _get_mock):
        with self.assertLogs("services.fund_analysis", level="INFO") as logs:
            result = fetch_eastmoney_fund_name("512890")

        self.assertEqual(result, "512890")
        self.assertTrue(any("沿用基金代码" in line for line in logs.output))

    @patch("services.fund_analysis.requests.get")
    def test_page_request_keeps_explicit_timeout(self, get_mock):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ErrCode": 0, "TotalCount": 0, "Data": {"LSJZList": []}}
        get_mock.return_value = response

        _request_eastmoney_page("512890", page_index=1, page_size=20)

        self.assertEqual(get_mock.call_args.kwargs["timeout"], 15)


class FundAnalysisCalculationTests(unittest.TestCase):
    def test_normalize_accepts_chinese_columns_and_removes_invalid_rows(self):
        raw = pd.DataFrame(
            {
                "净值日期": ["2026-01-02", "invalid", "2026-01-05"],
                "累计净值": [1.0, 2.0, "1.1"],
                "基金名称": ["测试基金", "测试基金", "测试基金"],
            }
        )

        name, normalized = normalize_nav_dataframe(raw)

        self.assertEqual(name, "测试基金")
        self.assertEqual(normalized["date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-01-02", "2026-01-05"])
        self.assertEqual(normalized["price"].tolist(), [1.0, 1.1])

    def test_normalize_rejects_empty_input(self):
        with self.assertRaisesRegex(ValueError, "没有可分析的数据"):
            normalize_nav_dataframe(pd.DataFrame())

    def test_analysis_preserves_drawdown_dates_and_depth(self):
        data = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]),
                "price": [100.0, 120.0, 90.0, 120.0],
            }
        )

        result = analyze_fund_nav(data, "测试基金", ma_periods=(2,), rsi_period=2)

        self.assertEqual(result.summary["最大回撤(%)"], -25.0)
        self.assertEqual(result.summary["最大回撤峰值日"], "2026-01-05")
        self.assertEqual(result.summary["最大回撤谷底日"], "2026-01-06")
        self.assertEqual(result.summary["最大回撤修复日"], "2026-01-07")
        self.assertTrue(result.summary["最大回撤是否已修复"])

    def test_one_year_rolling_annual_return_uses_252_trading_days(self):
        data = pd.DataFrame(
            {
                "date": pd.bdate_range("2025-01-02", periods=253),
                "price": [100.0 + (100.0 * index / 252) for index in range(253)],
            }
        )

        result = analyze_fund_nav(data, "测试基金", ma_periods=(20,))

        self.assertEqual(result.summary["滚动年化类型"], "一年滚动年化收益率(%)")
        self.assertEqual(result.summary["一年滚动年化收益率(%)"], 100.0)


if __name__ == "__main__":
    unittest.main()
