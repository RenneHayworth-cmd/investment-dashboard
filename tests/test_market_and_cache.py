import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import latest_trade_date_text
from services.index_ma20 import append_eastmoney_latest_index_row, fetch_eastmoney_clist_latest_index_row
from services.market_calendar import get_market_window, is_market_trading_day
from services.position_analysis import _cache_has_expected_trade_date


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
    def test_exchange_holidays_are_not_trading_days(self):
        cases = [
            ("A股", "2026-10-01T12:00:00"),
            ("日本", "2026-01-01T12:00:00"),
            ("韩国", "2026-01-01T12:00:00"),
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


if __name__ == "__main__":
    unittest.main()
