import json
from datetime import date, datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

import services.wind_source as wind
from services.market_calendar import get_market_window, previous_trading_day

TZ = ZoneInfo("Asia/Shanghai")


def _sse(payload_text: str, *, status=200):
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": payload_text}], "isError": False}}
    response = Mock(status_code=status, headers={"content-type": "text/event-stream"})
    response.content = f"event: message\r\ndata: {json.dumps(body, ensure_ascii=False)}\r\n\r\n".encode("utf-8")
    response.raise_for_status = Mock()
    return response


def _quote_table(rows):
    columns = ["最新交易日", "交易时间", "最新成交价", "前收盘价", "今日开盘价", "今日最高价", "今日最低价", "成交量", "成交额", "Wind代码"]
    return json.dumps({"data": {"columns": [{"name": name} for name in columns], "rows": rows}, "error": None}, ensure_ascii=False)


class _Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.trust_env = True

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json["params"]["arguments"])
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


@pytest.fixture
def wind_key(monkeypatch):
    monkeypatch.setenv("WIND_API_KEY", "ak_test")


def test_missing_key_never_touches_the_network():
    with patch.object(wind.requests, "Session") as session:
        with pytest.raises(wind.WindSourceError, match="未配置"):
            wind.fetch_wind_quotes("index_data", ["000001.SH"])
    session.assert_not_called()


def test_quotes_drop_unknown_code_and_discard_fuzzy_matches(wind_key):
    rows = [
        ["20260930", "2026-09-30T15:00:06.000+08:00", "3842.19", "3830.45", "3839.25", "3851.22", "3833.09", "1", "2", "000001.SH"],
        # Wind resolved an unknown code to a different security; it must not be used.
        ["20260929", "2026-09-29T16:00:00.000-04:00", "28.73", None, None, None, None, None, None, "SA.N"],
    ]
    session = _Session([_sse("未识别到有效的金融标的:BK1158"), _sse(_quote_table(rows))])
    with patch.object(wind.requests, "Session", return_value=session):
        frame = wind.fetch_wind_quotes("index_data", ["000001.SH", "BK1158", "SA2701.CZC"])
    assert session.calls[0]["windcode"] == "000001.SH,BK1158,SA2701.CZC"
    assert session.calls[1]["windcode"] == "000001.SH,SA2701.CZC"
    assert frame["wind_code"].tolist() == ["000001.SH"]
    row = frame.iloc[0]
    assert row["trade_date"] == date(2026, 9, 30)
    assert row["price"] == pytest.approx(3842.19)
    assert row["quote_time"] == pd.Timestamp("2026-09-30T15:00:06+08:00")


def test_network_failure_pauses_wind_for_later_calls(wind_key):
    session = _Session([requests.ConnectionError("down"), requests.ConnectionError("down")])
    with patch.object(wind.requests, "Session", return_value=session):
        with pytest.raises(wind.WindSourceError, match="请求失败"):
            wind.fetch_wind_quotes("index_data", ["000001.SH"])
        with pytest.raises(wind.WindSourceError, match="暂停调用"):
            wind.fetch_wind_quotes("index_data", ["000001.SH"])
    assert len(session.calls) == 2  # direct + proxy route, then no further request
    assert not wind.wind_available()


def test_daily_bars_keep_the_session_date_and_unadjusted_request(wind_key):
    table = {"data": {"columns": [{"name": n} for n in ("TIME", "OPEN", "MATCH", "HIGH", "LOW")], "rows": [
        ["2026-09-28T00:00:00.000+08:00", "16.16", "16.07", "16.62", "15.68"],
        ["2026-09-29T00:00:00.000+08:00", "16.17", "16.04", "16.44", "15.73"],
    ]}, "error": None}
    session = _Session([_sse(json.dumps(table))])
    with patch.object(wind.requests, "Session", return_value=session):
        bars = wind.fetch_wind_daily_bars("index_data", "VIX.GI", "2026-09-24", "2026-09-30")
    assert session.calls[0]["aftype"] == "2"
    assert bars["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-09-28", "2026-09-29"]
    assert bars["close"].tolist() == [16.07, 16.04]


@pytest.mark.parametrize("contract,expected", [
    ("I2701", "I2701.DCE"), ("IM2610", "IM2610.CFE"), ("AU2612", "AU2612.SHF"),
    ("SC2611", "SC2611.INE"), ("SA2701", "SA701.CZC"), ("LC2701", "LC2701.GFE"), ("I0", None),
])
def test_futures_contract_codes(contract, expected):
    assert wind.wind_futures_contract_code(contract) == expected


@pytest.mark.parametrize("code,expected", [("600519", "600519.SH"), ("000001", "000001.SZ"), ("920002", "920002.BJ"), ("430047", "430047.BJ")])
def test_stock_codes(code, expected):
    assert wind.wind_stock_code(code) == expected


def _completed_sessions(target: date, count: int) -> list[date]:
    market = get_market_window("A股")
    days, current = [target], target
    while len(days) < count:
        current = previous_trading_day(market, current)
        days.append(current)
    return sorted(days)


def test_index_supplement_appends_only_missing_completed_sessions(wind_key):
    from services.index_config import INDEX_CONFIG
    from services.index_update_orchestration import supplement_index_raw_from_wind

    target = date(2026, 9, 29)
    sessions = _completed_sessions(target, 21)
    finalized = pd.DataFrame({"trade_date": pd.to_datetime(sessions[:-1]), "close": 100.0})
    primary = pd.DataFrame({"trade_date": pd.to_datetime([sessions[-2]]), "close": [101.0]})
    bars = pd.DataFrame({"date": pd.to_datetime([sessions[-2], target]), "close": [999.0, 102.0]})
    with patch("services.wind_source.fetch_wind_daily_bars", return_value=bars) as fetch:
        merged, added, error = supplement_index_raw_from_wind(
            INDEX_CONFIG["上证指数"], primary, finalized, market_name="A股", target_date=target, source_days=30,
        )
    fetch.assert_called_once()
    assert (added, error) == (1, "")
    by_date = merged.set_index(merged["trade_date"].dt.date)["close"]
    assert by_date[sessions[-2]] == 101.0  # the primary row wins over Wind
    assert by_date[target] == 102.0


def test_index_supplement_skips_complete_history_and_main_continuous_futures(wind_key):
    from services.index_config import INDEX_CONFIG
    from services.index_update_orchestration import supplement_index_raw_from_wind

    target = date(2026, 9, 29)
    complete = pd.DataFrame({"trade_date": pd.to_datetime(_completed_sessions(target, 21)), "close": 100.0})
    gap = complete.iloc[:-1]
    with patch("services.wind_source.fetch_wind_daily_bars") as fetch:
        assert supplement_index_raw_from_wind(
            INDEX_CONFIG["上证指数"], None, complete, market_name="A股", target_date=target, source_days=30,
        )[1] == 0
        assert supplement_index_raw_from_wind(
            INDEX_CONFIG["铁矿石主连"], None, gap, market_name="A股", target_date=target, source_days=30,
        )[1] == 0
    fetch.assert_not_called()


def test_realtime_index_wind_quotes_reject_stale_sessions_and_futures_previous_close(wind_key):
    from services.index_realtime import _fetch_wind_index_quotes

    frame = pd.DataFrame([
        {"wind_code": "000001.SH", "trade_date": date(2026, 9, 30), "quote_time": pd.Timestamp("2026-09-30T10:00:00+08:00"), "price": 3842.0, "previous_close": 3830.0},
        {"wind_code": "000300.SH", "trade_date": date(2026, 9, 29), "quote_time": pd.Timestamp("2026-09-29T15:00:00+08:00"), "price": 4000.0, "previous_close": 3990.0},
        {"wind_code": "I.DCE", "trade_date": date(2026, 9, 30), "quote_time": pd.Timestamp("2026-09-30T10:00:00+08:00"), "price": 702.5, "previous_close": 699.0},
    ])
    with patch("services.wind_source.fetch_wind_quotes", return_value=frame):
        quotes, error = _fetch_wind_index_quotes(
            ["上证指数", "沪深300", "铁矿石主连", "微盘股"], now=datetime(2026, 9, 30, 10, 1, tzinfo=TZ),
        )
    assert error == ""
    assert set(quotes) == {"上证指数", "铁矿石主连"}
    assert quotes["上证指数"]["change_pct"] == pytest.approx((3842 / 3830 - 1) * 100)
    assert quotes["铁矿石主连"]["previous_close"] is None
    assert quotes["上证指数"]["source"] == "万得Wind"


def test_etf_quotes_fall_back_to_wind_when_tickflow_raises(wind_key):
    import services.position_runtime as runtime

    client = Mock()
    client.quotes.get.side_effect = RuntimeError("tickflow down")
    frame = pd.DataFrame([{"wind_code": "510500.SH", "trade_date": date(2026, 9, 30),
                           "quote_time": pd.Timestamp("2026-09-30T10:00:00+08:00"), "price": 7.472, "previous_close": 7.476}])
    with patch.object(runtime, "_tickflow_quote_client", return_value=client), \
         patch.object(runtime, "_fetch_tencent_fund_quotes", return_value=({}, "腾讯实时行情：无报价")), \
         patch.object(runtime, "fetch_sina_exchange_fund_quotes", return_value=({}, "新浪财经：无报价")), \
         patch("services.wind_source.fetch_wind_quotes", return_value=frame) as fetch:
        quotes = runtime.fetch_tickflow_etf_quotes(["510500"], api_key="key", market_now=datetime(2026, 9, 30, 10, 1, tzinfo=TZ))
    fetch.assert_called_once_with("fund_data", ["510500.SH"])
    assert quotes["510500"]["price"] == 7.472
    assert quotes["510500"]["source"] == "万得Wind"


def test_etf_quote_error_names_every_failed_source(wind_key):
    import services.position_runtime as runtime

    client = Mock()
    client.quotes.get.side_effect = RuntimeError("tickflow down")
    with patch.object(runtime, "_tickflow_quote_client", return_value=client), \
         patch.object(runtime, "_fetch_tencent_fund_quotes", return_value=({}, "腾讯实时行情：无报价")), \
         patch.object(runtime, "fetch_sina_exchange_fund_quotes", return_value=({}, "新浪财经：无报价")), \
         patch("services.wind_source.fetch_wind_quotes", side_effect=wind.WindSourceError("万得Wind请求失败")):
        with pytest.raises(ValueError, match="TickFlow：tickflow down；腾讯实时行情：无报价；新浪财经：无报价；万得Wind"):
            runtime.fetch_tickflow_etf_quotes(["510500"], api_key="key", market_now=datetime(2026, 9, 30, 10, 1, tzinfo=TZ))


def test_microcap_wind_quotes_run_before_akshare_and_skip_untraded_stocks(wind_key):
    import services.microcap_live_market as market

    frame = pd.DataFrame([
        {"wind_code": "000001.SZ", "trade_date": date(2026, 9, 30), "quote_time": pd.Timestamp("2026-09-30T10:00:00+08:00"), "price": 12.3, "volume": 100.0},
        {"wind_code": "600000.SH", "trade_date": date(2026, 9, 30), "quote_time": pd.Timestamp("2026-09-30T10:00:00+08:00"), "price": 8.8, "volume": 0.0},
    ])
    akshare_quote = Mock(return_value=None)
    with patch.object(market, "_tickflow_quotes", return_value={}), \
         patch.object(market, "_eastmoney_quotes", side_effect=RuntimeError("blocked")), \
         patch.object(market, "_tencent_quotes", return_value={}), \
         patch.object(market, "_akshare_quote", akshare_quote), \
         patch("services.wind_source.fetch_wind_quotes", return_value=frame):
        quotes, failures = market.fetch_microcap_realtime_quotes(
            ["000001", "600000"], market_now=datetime(2026, 9, 30, 10, 1, tzinfo=TZ),
        )
    assert quotes["000001"]["source"] == "万得Wind实时行情"
    assert "600000" not in quotes  # zero volume: suspended, left to the next source
    akshare_quote.assert_called_once()
    assert "600000" in failures


def test_microcap_wind_close_snapshot_requires_post_close_time(wind_key):
    import services.microcap_live_market as market

    frame = pd.DataFrame([
        {"wind_code": "000001.SZ", "trade_date": date(2026, 9, 30), "quote_time": pd.Timestamp("2026-09-30T15:00:03+08:00"), "price": 12.3, "volume": 100.0},
        {"wind_code": "000002.SZ", "trade_date": date(2026, 9, 30), "quote_time": pd.Timestamp("2026-09-30T14:59:00+08:00"), "price": 9.1, "volume": 100.0},
    ])
    current = datetime(2026, 9, 30, 15, 10, tzinfo=TZ)
    close_at = datetime(2026, 9, 30, 15, 0, tzinfo=TZ)
    with patch.object(market, "_tickflow_quotes", return_value={}), \
         patch.object(market, "_eastmoney_quotes", return_value={}), \
         patch.object(market, "_tencent_quotes", return_value={}), \
         patch.object(market, "_bk1158_snapshot_closes", return_value={}), \
         patch("services.wind_source.fetch_wind_quotes", return_value=frame):
        snapshots, _ = market._close_snapshots(["000001", "000002"], "", current, close_at)
    assert snapshots == {"000001": {"price": 12.3, "cache_source": "wind_close_snapshot"}}


def _taoxi_frame(codes, *, volume=100.0):
    return pd.DataFrame([
        {"wind_code": wind.wind_stock_code(code), "trade_date": date(2026, 9, 30),
         "quote_time": pd.Timestamp("2026-09-30T10:00:00+08:00"), "price": 11.0, "previous_close": 10.0,
         "open": 10.2, "volume": volume, "amount": 1000.0}
        for code in codes
    ])


def test_taoxi_wind_rows_feed_the_strict_twenty_member_calculation(wind_key):
    from services.taoxi_microcap_index import TaoxiDataError, _calculate_intraday_quote, _wind_constituent_rows

    codes = [f"{600000 + index:06d}" for index in range(20)]
    current = datetime(2026, 9, 30, 10, 1, tzinfo=TZ)
    with patch("services.wind_source.fetch_wind_quotes", return_value=_taoxi_frame(codes)):
        quote = _calculate_intraday_quote(_wind_constituent_rows(codes), codes, 1000.0, current, "万得Wind")
    assert quote["price"] == pytest.approx(1100.0)
    with patch("services.wind_source.fetch_wind_quotes", return_value=_taoxi_frame(codes[:19])):
        with pytest.raises(TaoxiDataError, match="缺少"):
            _calculate_intraday_quote(_wind_constituent_rows(codes), codes, 1000.0, current, "万得Wind")


def test_futures_contract_daily_uses_wind_after_lagging_sources_but_not_for_main_continuous(wind_key):
    import services.futures_spread as futures_spread
    from services.market_fallback import MarketSource

    stale = pd.DataFrame({"date": pd.to_datetime(["2026-09-29"]), "close": [699.0]})
    fresh = pd.DataFrame({"date": pd.to_datetime(["2026-09-29", "2026-09-30"]), "close": [699.0, 702.5]})
    registry = lambda _ak, contract: [MarketSource("新浪", lambda: stale.copy())]
    with patch("services.akshare_sources.futures_daily_sources", side_effect=registry), \
         patch.object(futures_spread, "_fetch_futures_daily_from_sina_direct", side_effect=RuntimeError("down")), \
         patch("services.wind_source.fetch_wind_daily_bars", return_value=fresh) as fetch:
        result = futures_spread.fetch_futures_daily_from_akshare("I2701", target_date="2026-09-30")
        assert result.attrs["market_data_source"] == "万得Wind"
        assert fetch.call_args.args[:2] == ("index_data", "I2701.DCE")
        fetch.reset_mock()
        main = futures_spread.fetch_futures_daily_from_akshare("I0", target_date="2026-09-30")
    fetch.assert_not_called()
    assert main["date"].max() == pd.Timestamp("2026-09-29")
