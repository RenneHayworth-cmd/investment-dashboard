import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from services import microcap_live_market as market

REAL_TENCENT_QUOTES = market._tencent_quotes


@pytest.fixture(autouse=True)
def _no_tencent_network():
    """Tencent sits between EastMoney and AkShare; tests opt in by patching it themselves."""
    with patch.object(market, "_tencent_quotes", return_value={}):
        yield


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
            patch("services.microcap.load_microcap_constituent_snapshots", return_value=(pd.DataFrame(), None)),
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
            patch("services.microcap.load_microcap_constituent_snapshots", return_value=(pd.DataFrame(), None)),
        ):
            histories, failures = market.load_microcap_histories(
                ["002316"], allow_fetch=True, market_now=datetime(2026, 9, 28, 15, 6, tzinfo=tz),
            )
        self.assertEqual(histories["002316"]["date"].max(), "2026-09-24")
        self.assertIn("缺少目标日2026-09-28", failures["002316"])


if __name__ == "__main__":
    unittest.main()


def test_quote_phase_covers_lunch_and_after_close():
    tz = ZoneInfo("Asia/Shanghai")
    at = lambda h, m, day=29: market.quote_phase(datetime(2026, 9, day, h, m, tzinfo=tz))
    assert at(9, 15) is None            # before the open
    assert at(10, 0) == "session"
    assert at(11, 30) == "lunch"
    assert at(12, 59) == "lunch"
    assert at(13, 0) == "session"
    assert at(15, 0) == "after_close"
    assert at(16, 30) == "after_close"
    assert at(12, 0, day=25) is None    # Mid-Autumn holiday
    assert at(12, 0, day=27) is None    # Sunday


def test_quotes_are_fetched_during_lunch():
    current = datetime(2026, 9, 29, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    lunch_close = {"600000": {"price": 10.5, "quote_time": current.replace(hour=11, minute=30), "source": "t", "status": "实时"}}
    with patch("services.microcap_live_market._tickflow_quotes", return_value=lunch_close) as tickflow:
        quotes, failures = market.fetch_microcap_realtime_quotes(["600000"], api_key="k", market_now=current)
    tickflow.assert_called_once()
    assert quotes["600000"]["price"] == 10.5 and not failures


def test_no_quote_request_before_the_open():
    with patch("services.microcap_live_market._tickflow_quotes") as tickflow:
        quotes, failures = market.fetch_microcap_realtime_quotes(
            ["600000"], api_key="k", market_now=datetime(2026, 9, 29, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    tickflow.assert_not_called()
    assert (quotes, failures) == ({}, {})


def _bk1158_snapshot(day, taken, rows):
    return pd.DataFrame([
        {"快照日期": day, "快照时间": taken, "代码": code, "最新价": price, "是否停牌": halted}
        for code, price, halted in rows
    ])


def test_bk1158_constituent_snapshot_is_the_last_close_fallback():
    tz = ZoneInfo("Asia/Shanghai")
    close_at = datetime(2026, 9, 29, 15, 0, tzinfo=tz)
    snap = _bk1158_snapshot("2026-09-29", "2026-09-29 15:05:19", [
        ("688021", 20.27, False), ("300635", 11.9, True), ("002316", 4.4, False),
    ])
    with (
        patch("services.microcap_live_market._tickflow_quotes", return_value={
            "002316": {"price": 4.36, "quote_time": datetime(2026, 9, 29, 15, 0, 3, tzinfo=tz)},
        }),
        patch("services.microcap_live_market._eastmoney_quotes", side_effect=ConnectionError("down")),
        patch("services.microcap.load_microcap_constituent_snapshots", return_value=(snap, None)),
    ):
        result, error = market._close_snapshots(["002316", "300635", "688021", "688067"], "k", close_at, close_at)
    assert result == {
        "002316": {"price": 4.36, "cache_source": "tickflow_close_snapshot"},  # network quote keeps priority
        "688021": {"price": 20.27, "cache_source": "bk1158_close_snapshot"},
    }  # 300635 suspended and 688067 absent are left to the daily bars
    assert "东方财富" in error


def test_bk1158_snapshot_must_be_taken_on_the_day_after_the_close():
    tz = ZoneInfo("Asia/Shanghai")
    close_at = datetime(2026, 9, 24, 15, 0, tzinfo=tz)
    for taken in ("2026-09-24 14:30:00", "2026-09-26 19:56:27"):  # intraday, or re-taken days later
        snap = _bk1158_snapshot("2026-09-24", taken, [("688021", 20.22, False)])
        with patch("services.microcap.load_microcap_constituent_snapshots", return_value=(snap, None)):
            assert market._bk1158_snapshot_closes(["688021"], close_at) == {}



def _tencent_payload(rows):
    """rows: (prefix, code, price, volume, stamp) -> a GBK Tencent batch body."""
    lines = []
    for prefix, code, price, volume, stamp in rows:
        fields = ["1", "名称", code, str(price), "4.50", "4.60", str(volume)] + ["0"] * 23 + [stamp] + ["0"] * 10
        lines.append(f'v_{prefix}{code}="{"~".join(fields)}";')
    return "\n".join(lines).encode("gbk")


def test_tencent_rows_keep_only_stocks_traded_today():
    tz = ZoneInfo("Asia/Shanghai")
    current = datetime(2026, 9, 30, 15, 6, tzinfo=tz)
    body = _tencent_payload([
        ("sz", "002316", 4.95, 120000, "20260930150003"),
        ("bj", "920799", 30.36, 19020, "20260930150000"),
        ("bj", "830799", 34.28, 0, "20260930090000"),     # delisted old code: no trade today
        ("sh", "688021", 19.84, 5000, "20260929150000"),   # yesterday's quote
        ("sz", "600000", 9.48, 5000, "20260930150000"),    # wrong exchange for the code
    ])
    rows = market._tencent_rows(body, ["002316", "920799", "830799", "688021", "600000"], current, "直连")
    assert sorted(rows) == ["002316", "920799"]
    assert rows["002316"]["price"] == 4.95
    assert rows["002316"]["quote_time"] == datetime(2026, 9, 30, 15, 0, 3, tzinfo=tz)
    assert rows["002316"]["source"] == "腾讯实时行情（直连）"
    assert [market._tencent_symbol(c) for c in ("600000", "900901", "002316", "300635", "920799", "430047")] == [
        "sh600000", "sh900901", "sz002316", "sz300635", "bj920799", "bj430047"]


def test_tencent_batch_tries_direct_connection_before_proxy():
    current = datetime(2026, 9, 30, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    routes, urls = [], []

    class Response:
        content = _tencent_payload([("sz", "002316", 4.8, 100, "20260930095958")])

        def raise_for_status(self):
            pass

    class Session:
        def __init__(self):
            self.trust_env = True

        def get(self, url, **kwargs):
            routes.append(self.trust_env)
            urls.append(url)
            if not self.trust_env:
                raise ConnectionError("direct blocked")
            return Response()

        def close(self):
            pass

    with patch.object(market.requests, "Session", Session):
        result = REAL_TENCENT_QUOTES(["002316", "688021"], current)
    assert routes == [False, True]
    assert urls[0] == "https://qt.gtimg.cn/q=sz002316,sh688021"
    assert list(result) == ["002316"] and "代理" in result["002316"]["source"]

    with patch.object(market.requests, "Session", Session), \
         patch.object(Session, "get", side_effect=ConnectionError("both down")):
        with pytest.raises(ConnectionError):
            REAL_TENCENT_QUOTES(["002316"], current)


def test_tencent_fills_what_tickflow_and_eastmoney_missed_before_akshare():
    current = datetime(2026, 9, 30, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    tencent = {"300635": {"price": 11.8, "quote_time": current, "source": "腾讯实时行情（直连）", "status": "实时"}}
    with (
        patch("services.microcap_live_market._tickflow_quotes", side_effect=TimeoutError("rate limited")),
        patch("services.microcap_live_market._eastmoney_quotes", side_effect=ConnectionError("down")),
        patch("services.microcap_live_market._tencent_quotes", return_value=tencent) as tx,
        patch("services.microcap_live_market._akshare_quote") as ak_mock,
    ):
        quotes, failures = market.fetch_microcap_realtime_quotes(["300635"], api_key="k", market_now=current)
    tx.assert_called_once_with(["300635"], current)
    ak_mock.assert_not_called()
    assert quotes == tencent and not failures


def test_tencent_close_snapshot_sits_between_eastmoney_and_bk1158():
    tz = ZoneInfo("Asia/Shanghai")
    close_at = datetime(2026, 9, 30, 15, 0, tzinfo=tz)
    snap = _bk1158_snapshot("2026-09-30", "2026-09-30 15:05:19", [("688021", 19.9, False), ("300635", 11.8, False)])
    with (
        patch("services.microcap_live_market._tickflow_quotes", return_value={}),
        patch("services.microcap_live_market._eastmoney_quotes", side_effect=ConnectionError("down")),
        patch("services.microcap_live_market._tencent_quotes", return_value={
            "002316": {"price": 4.95, "quote_time": datetime(2026, 9, 30, 15, 0, 3, tzinfo=tz)},
            "688021": {"price": 19.84, "quote_time": datetime(2026, 9, 30, 14, 59, 58, tzinfo=tz)},  # before the close
        }) as tx,
        patch("services.microcap.load_microcap_constituent_snapshots", return_value=(snap, None)),
    ):
        result, error = market._close_snapshots(["002316", "688021"], "k", close_at, close_at)
    tx.assert_called_once_with(["002316", "688021"], close_at)
    assert result == {
        "002316": {"price": 4.95, "cache_source": "tencent_close_snapshot"},
        "688021": {"price": 19.9, "cache_source": "bk1158_close_snapshot"},
    }
    assert "东方财富" in error
    assert market.HISTORY_SOURCES.index("tencent_close_snapshot") < market.HISTORY_SOURCES.index("bk1158_close_snapshot")
