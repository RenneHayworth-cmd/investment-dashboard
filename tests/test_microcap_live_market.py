import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from services import microcap_live_market as market


class MicrocapLiveMarketTests(unittest.TestCase):
    def test_akshare_history_retries_direct_after_proxy_error(self):
        from requests.exceptions import ProxyError

        ak = Mock()
        ak.stock_zh_a_hist.side_effect = ProxyError("proxy unavailable")
        ak.stock_zh_a_hist_tx.side_effect = RuntimeError("Tencent unavailable")
        fresh = pd.DataFrame({"date": ["2026-09-24"], "close": [10.5]})
        with (
            patch.dict("sys.modules", {"akshare": ak}),
            patch.object(market, "_akshare_history_fallback", return_value=fresh) as direct,
        ):
            result = market._akshare_history("600455", "2026-09-22", "2026-09-24")
        direct.assert_called_once_with("600455", "2026-09-22", "2026-09-24")
        self.assertEqual(result.to_dict("records"), [{"date": "2026-09-24", "close": 10.5}])

    def test_akshare_proxy_error_uses_tencent_daily_source_and_marks_cache_source(self):
        from requests.exceptions import ProxyError

        ak = Mock()
        ak.stock_zh_a_hist.side_effect = ProxyError("EastMoney proxy unavailable")
        ak.stock_zh_a_hist_tx.return_value = pd.DataFrame({
            "date": ["2026-09-24"], "close": [10.5],
        })
        with patch.dict("sys.modules", {"akshare": ak}):
            result = market._akshare_history("600455", "2026-09-22", "2026-09-24")
        ak.stock_zh_a_hist_tx.assert_called_once_with(
            symbol="sh600455", start_date="20260922", end_date="20260924",
            adjust="", timeout=12,
        )
        self.assertEqual(result.attrs["source"], "akshare_tx")

    def test_akshare_fallback_tries_without_proxy_and_parses_unadjusted_close(self):
        session = Mock()
        session.get.return_value.json.return_value = {
            "data": {"klines": ["2026-09-24,10,10.5,11,9,100,1000,0,0,0,0"]}
        }
        with patch.object(market.requests, "Session", return_value=session):
            result = market._akshare_history_fallback("600455", "2026-09-22", "2026-09-24")
        self.assertFalse(session.trust_env)
        session.get.assert_called_once()
        self.assertIn("push2his.eastmoney.com/api/qt/stock/kline/get", session.get.call_args.args[0])
        self.assertEqual(session.get.call_args.kwargs["params"]["secid"], "1.600455")
        self.assertEqual(result.to_dict("records"), [{"date": "2026-09-24", "close": 10.5}])
        session.close.assert_called_once()

    def test_formal_history_is_append_only_and_unadjusted(self):
        old = pd.DataFrame({"date": ["2026-09-22"], "close": [10.0]})
        fresh = pd.DataFrame({"trade_date": ["2026-09-22", "2026-09-23"], "close": [99.0, 11.0]})
        saved, loaded = [], []
        def load(symbol, source, data_type, period="1d"):
            loaded.append((symbol, source, data_type, period))
            return (old if source == "tickflow" else None, None)
        with (
            patch("services.microcap_live_market.load_dataset", side_effect=load),
            patch("services.microcap_live_market.latest_settled_trade_date", return_value=pd.Timestamp("2026-09-23").date()),
            patch("services.microcap_live_market._tickflow_history", return_value=fresh),
            patch("services.microcap_live_market.save_dataset", side_effect=lambda *args, **kwargs: saved.append(args)),
        ):
            histories, failures = market.load_microcap_histories(
                ["600000"], start_date="2026-09-01", allow_fetch=True,
                market_now=datetime(2026, 9, 24, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertEqual(histories["600000"].to_dict("records"), [
            {"date": "2026-09-22", "close": 10.0}, {"date": "2026-09-23", "close": 11.0}
        ])
        self.assertFalse(failures)
        self.assertEqual(saved[0][0], "microcap_live_600000")
        self.assertEqual(saved[0][1], "600000")
        self.assertEqual(saved[0][3], market.DATA_TYPE)
        self.assertEqual({item[0] for item in loaded}, {"microcap_live_600000"})

    def test_missing_official_close_keeps_old_cache_and_reports_both_sources(self):
        old = pd.DataFrame({"date": ["2026-09-22"], "close": [10.0]})
        with (
            patch("services.microcap_live_market.load_dataset", side_effect=lambda *args, **kwargs: (old if args[1] == "tickflow" else None, None)),
            patch("services.microcap_live_market.latest_settled_trade_date", return_value=pd.Timestamp("2026-09-23").date()),
            patch("services.microcap_live_market._tickflow_history", side_effect=TimeoutError("tickflow")),
            patch("services.microcap_live_market._akshare_history", side_effect=TimeoutError("akshare")),
        ):
            histories, failures = market.load_microcap_histories(
                ["600000"], allow_fetch=True,
                market_now=datetime(2026, 9, 24, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertEqual(histories["600000"]["date"].tolist(), ["2026-09-22"])
        self.assertIn("TickFlow", failures["600000"])
        self.assertIn("AkShare", failures["600000"])

    def test_rate_limited_tickflow_uses_akshare_formal_close(self):
        old = pd.DataFrame({"date": ["2026-09-22"], "close": [10.0]})
        fresh = pd.DataFrame({"日期": ["2026-09-23"], "收盘价": [10.5]})
        fresh.attrs["source"] = "akshare_tx"
        saved = []
        loaded_sources = []
        def load(_symbol, source, *_args, **_kwargs):
            loaded_sources.append(source)
            return (old if source == "tickflow" else None, None)
        with (
            patch("services.microcap_live_market.load_dataset", side_effect=load),
            patch("services.microcap_live_market.latest_settled_trade_date", return_value=pd.Timestamp("2026-09-23").date()),
            patch("services.microcap_live_market._tickflow_history", side_effect=RuntimeError("RateLimitError")),
            patch("services.microcap_live_market._akshare_history", return_value=fresh),
            patch("services.microcap_live_market.save_dataset", side_effect=lambda *args, **kwargs: saved.append(args)),
        ):
            histories, failures = market.load_microcap_histories(
                ["600455"], allow_fetch=True,
                market_now=datetime(2026, 9, 24, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertFalse(failures)
        self.assertEqual(histories["600455"].to_dict("records"), [
            {"date": "2026-09-22", "close": 10.0}, {"date": "2026-09-23", "close": 10.5},
        ])
        self.assertIn("akshare_tx", loaded_sources)
        self.assertEqual(saved[0][2], "akshare_tx")

    def test_intraday_quote_is_transient_and_never_saved(self):
        current = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch("services.microcap_live_market._is_open", return_value=True),
            patch("services.microcap_live_market._tickflow_quotes", return_value={
                "600000": {"price": 10.5, "quote_time": current, "source": "TickFlow", "status": "实时"}
            }),
            patch("services.microcap_live_market.save_dataset") as save_mock,
        ):
            quotes, failures = market.fetch_microcap_realtime_quotes(["600000"], api_key="test", market_now=current)
        self.assertEqual(quotes["600000"]["price"], 10.5)
        self.assertFalse(failures)
        save_mock.assert_not_called()

    def test_beijing_listings_use_batch_akshare_snapshot(self):
        current = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch("services.microcap_live_market._is_open", return_value=True),
            patch("services.microcap_live_market._tickflow_quotes", return_value={}),
            patch("services.microcap_live_market._eastmoney_quotes", return_value={}),
            patch("services.microcap_live_market._akshare_bj_quotes", return_value={
                "830799": {"price": 10.2, "quote_time": current, "source": "AkShare全市场实时快照", "status": "实时"}
            }) as bj_mock,
            patch("services.microcap_live_market._akshare_quote") as ordinary_mock,
        ):
            quotes, failures = market.fetch_microcap_realtime_quotes(["830799"], market_now=current)
        bj_mock.assert_called_once_with(["830799"], current)
        ordinary_mock.assert_not_called()
        self.assertIn("830799", quotes)
        self.assertFalse(failures)

    def test_successful_akshare_fallback_does_not_report_resolved_tickflow_error(self):
        current = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch("services.microcap_live_market._is_open", return_value=True),
            patch("services.microcap_live_market._tickflow_quotes", side_effect=TimeoutError("offline")),
            patch("services.microcap_live_market._eastmoney_quotes", return_value={}),
            patch("services.microcap_live_market._akshare_quote", return_value={
                "price": 10.1, "quote_time": current, "source": "AkShare", "status": "实时"
            }),
        ):
            quotes, failures = market.fetch_microcap_realtime_quotes(["600000"], api_key="key", market_now=current)
        self.assertEqual(quotes["600000"]["source"], "AkShare")
        self.assertFalse(failures)

    def test_intraday_uses_akshare_when_tickflow_does_not_cover_symbol(self):
        current = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch("services.microcap_live_market._is_open", return_value=True),
            patch("services.microcap_live_market._tickflow_quotes", return_value={}),
            patch("services.microcap_live_market._eastmoney_quotes", return_value={}),
            patch("services.microcap_live_market._akshare_quote", return_value={
                "price": 10.1, "quote_time": current, "source": "AkShare", "status": "实时"
            }),
        ):
            quotes, failures = market.fetch_microcap_realtime_quotes(["600000"], api_key="test", market_now=current)
        self.assertEqual(quotes["600000"]["source"], "AkShare")
        self.assertFalse(failures)


    def test_tickflow_quotes_are_batched_five_at_a_time_and_survive_a_failed_batch(self):
        current = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        codes = [f"{600000 + i:06d}" for i in range(12)]
        calls = []

        class Quotes:
            def get(self, symbols, as_dataframe):
                calls.append(list(symbols))
                if len(symbols) > 5:
                    raise ValueError("标的数量超限")
                if "600005.SH" in symbols:
                    raise TimeoutError("batch down")
                return pd.DataFrame({"symbol": symbols, "last_price": [10.0] * len(symbols)})

        class Client:
            def __init__(self, **kwargs):
                self.quotes = Quotes()

            def close(self):
                pass

        with (
            patch("tickflow.TickFlow", Client),
            patch("services.position_analysis._tickflow_quote_datetime", return_value=current),
        ):
            result = market._tickflow_quotes(codes, "key", current)
        self.assertEqual([len(batch) for batch in calls], [5, 5, 2])
        self.assertEqual(sorted(result), codes[:5] + codes[10:])  # the failing middle batch is dropped only

    def test_eastmoney_batch_tries_direct_connection_before_proxy(self):
        current = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        stamp = int(current.timestamp())
        routes = []

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"diff": [{"f2": 11.18, "f12": "300220", "f124": stamp},
                                          {"f2": "-", "f12": "600455", "f124": stamp}]}}

        class Session:
            def __init__(self):
                self.trust_env = True

            def get(self, url, **kwargs):
                routes.append(self.trust_env)
                if not self.trust_env:
                    raise ConnectionError("direct blocked")
                self.params = kwargs["params"]
                return Response()

            def close(self):
                pass

        with patch.object(market.requests, "Session", Session):
            result = market._eastmoney_quotes(["300220", "600455"], current)
        self.assertEqual(routes[:len(market.EASTMONEY_ULIST_URLS)], [False] * len(market.EASTMONEY_ULIST_URLS))
        self.assertIs(routes[-1], True)
        self.assertEqual(list(result), ["300220"])  # "-" (no trade) is skipped
        self.assertEqual(result["300220"]["price"], 11.18)
        self.assertIn("代理", result["300220"]["source"])
        self.assertEqual(market._eastmoney_secid("600455"), "1.600455")
        self.assertEqual(market._eastmoney_secid("300220"), "0.300220")

    def test_eastmoney_fills_symbols_tickflow_missed_without_akshare(self):
        current = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch("services.microcap_live_market._is_open", return_value=True),
            patch("services.microcap_live_market._tickflow_quotes", return_value={
                "600000": {"price": 10.0, "quote_time": current, "source": "TickFlow实时行情", "status": "实时"}}),
            patch("services.microcap_live_market._eastmoney_quotes", return_value={
                "300220": {"price": 11.0, "quote_time": current, "source": "东方财富实时行情（直连）", "status": "实时"}}) as em,
            patch("services.microcap_live_market._akshare_quote") as ak_mock,
        ):
            quotes, failures = market.fetch_microcap_realtime_quotes(["600000", "300220"], api_key="k", market_now=current)
        em.assert_called_once_with(["300220"], current)
        ak_mock.assert_not_called()
        self.assertEqual(set(quotes), {"600000", "300220"})
        self.assertFalse(failures)

    def test_failure_messages_are_short(self):
        current = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        long_error = ConnectionError("HTTPSConnectionPool(host='push2.eastmoney.com') " + "f1%2C" * 200)
        with (
            patch("services.microcap_live_market._is_open", return_value=True),
            patch("services.microcap_live_market._tickflow_quotes", side_effect=long_error),
            patch("services.microcap_live_market._eastmoney_quotes", side_effect=long_error),
            patch("services.microcap_live_market._akshare_quote", side_effect=long_error),
        ):
            quotes, failures = market.fetch_microcap_realtime_quotes(["600000"], api_key="k", market_now=current)
        self.assertFalse(quotes)
        self.assertLess(len(failures["600000"]), 400)
        self.assertIn("东方财富", failures["600000"])

    def test_target_date_switches_to_current_session_at_1505(self):
        tz = ZoneInfo("Asia/Shanghai")
        # 2026-09-25 is the Mid-Autumn holiday, so the previous session is 09-24.
        self.assertEqual(market.microcap_target_date(datetime(2026, 9, 28, 15, 4, tzinfo=tz)), "2026-09-24")
        self.assertEqual(market.microcap_target_date(datetime(2026, 9, 28, 15, 5, tzinfo=tz)), "2026-09-28")

    def _load_until(self, history):
        return lambda _symbol, source, *_args, **_kwargs: (history if source == "tickflow" else None, None)

    def test_post_close_snapshot_supplies_current_session_close(self):
        tz = ZoneInfo("Asia/Shanghai")
        cached = pd.DataFrame({"date": ["2026-09-23", "2026-09-24"], "close": [4.25, 4.3]})
        saved = []
        with (
            patch("services.microcap_live_market.load_dataset", side_effect=self._load_until(cached)),
            patch("services.microcap_live_market.save_dataset", side_effect=lambda *args, **kwargs: saved.append(args)),
            patch("services.microcap_live_market._tickflow_history") as daily_tick,
            patch("services.microcap_live_market._akshare_history") as daily_ak,
            patch("services.microcap_live_market._tickflow_quotes", return_value={}),
            patch("services.microcap_live_market._eastmoney_quotes", return_value={
                "002316": {"price": 4.36, "quote_time": datetime(2026, 9, 28, 15, 5, 30, tzinfo=tz)},
            }),
        ):
            histories, failures = market.load_microcap_histories(
                ["002316"], allow_fetch=True, market_now=datetime(2026, 9, 28, 15, 6, tzinfo=tz),
            )
        self.assertFalse(failures)
        self.assertEqual(histories["002316"].iloc[-1].to_dict(), {"date": "2026-09-28", "close": 4.36})
        daily_tick.assert_not_called()
        daily_ak.assert_not_called()
        self.assertEqual([item[2] for item in saved], ["eastmoney_close_snapshot"])

    def test_daily_bar_wins_over_snapshot_for_same_date(self):
        bars = pd.DataFrame({"date": ["2026-09-24", "2026-09-28"], "close": [4.3, 4.35]})
        snap = pd.DataFrame({"date": ["2026-09-28"], "close": [4.36]})
        load = lambda _symbol, source, *_args, **_kwargs: (
            {"tickflow": bars, "eastmoney_close_snapshot": snap}.get(source), None
        )
        with patch("services.microcap_live_market.load_dataset", side_effect=load):
            histories, _ = market.load_microcap_histories(
                ["002316"], market_now=datetime(2026, 9, 28, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertEqual(histories["002316"].iloc[-1]["close"], 4.35)

    def test_snapshot_is_not_used_when_an_earlier_session_is_missing(self):
        tz = ZoneInfo("Asia/Shanghai")
        cached = pd.DataFrame({"date": ["2026-09-23"], "close": [4.25]})
        with (
            patch("services.microcap_live_market.load_dataset", side_effect=self._load_until(cached)),
            patch("services.microcap_live_market.save_dataset"),
            patch("services.microcap_live_market._tickflow_history", return_value=cached),
            patch("services.microcap_live_market._akshare_history", side_effect=TimeoutError("akshare")),
            patch("services.microcap_live_market._eastmoney_quotes") as snapshot,
        ):
            histories, failures = market.load_microcap_histories(
                ["002316"], allow_fetch=True, market_now=datetime(2026, 9, 28, 15, 6, tzinfo=tz),
            )
        snapshot.assert_not_called()
        self.assertEqual(histories["002316"]["date"].tolist(), ["2026-09-23"])
        self.assertIn("002316", failures)

    def test_pre_close_snapshot_is_rejected(self):
        tz = ZoneInfo("Asia/Shanghai")
        cached = pd.DataFrame({"date": ["2026-09-24"], "close": [4.3]})
        with (
            patch("services.microcap_live_market.load_dataset", side_effect=self._load_until(cached)),
            patch("services.microcap_live_market.save_dataset"),
            patch("services.microcap_live_market._tickflow_history", return_value=cached),
            patch("services.microcap_live_market._akshare_history", return_value=cached),
            patch("services.microcap_live_market._tickflow_quotes", return_value={}),
            patch("services.microcap_live_market._eastmoney_quotes", return_value={
                "002316": {"price": 4.34, "quote_time": datetime(2026, 9, 28, 14, 59, 57, tzinfo=tz)},
            }),
        ):
            histories, failures = market.load_microcap_histories(
                ["002316"], allow_fetch=True, market_now=datetime(2026, 9, 28, 15, 6, tzinfo=tz),
            )
        self.assertEqual(histories["002316"]["date"].max(), "2026-09-24")
        self.assertIn("缺少目标日2026-09-28", failures["002316"])


if __name__ == "__main__":
    unittest.main()
