from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import services.position_runtime as runtime

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 9, 10, 1, tzinfo=TZ)


def _tencent_payload(code, prefix, price, previous, volume, stamp):
    fields = [""] * 40
    fields[2], fields[3], fields[4], fields[6], fields[30] = code, price, previous, volume, stamp
    return f'v_{prefix}{code}="{"~".join(fields)}";'


def test_tencent_fund_quotes_keep_only_traded_same_day_rows():
    content = "\n".join([
        _tencent_payload("161125", "sz", "3.281", "3.275", "1200", "20261009100030"),
        _tencent_payload("512890", "sh", "1.196", "1.196", "0", "20261009100030"),  # 今日未成交
        _tencent_payload("511360", "sh", "114.01", "114.00", "300", "20260930150000"),  # 旧日期
    ]).encode("gbk")
    response = Mock(content=content)
    response.raise_for_status = Mock()
    session = Mock()
    session.get.return_value = response
    with patch("requests.Session", return_value=session):
        quotes, error = runtime._fetch_tencent_fund_quotes(["161125.SZ", "512890.SH", "511360.SH"], market_now=NOW)

    assert error == ""
    assert set(quotes) == {"161125"}
    assert quotes["161125"]["source"] == "腾讯实时行情"
    assert session.get.call_args.args[0].endswith("sz161125,sh512890,sh511360")


def test_each_fallback_only_fills_codes_still_missing():
    client = Mock()
    client.quotes.get.return_value = pd.DataFrame(
        [{"symbol": "512890.SH", "last_price": 1.2, "prev_close": 1.19, "timestamp": int(NOW.timestamp() * 1000)}]
    )
    tencent = Mock(return_value=({"161125": {"price": 3.28, "source": "腾讯实时行情"}}, ""))
    sina = Mock(return_value=({"161128": {"price": 7.6, "source": "新浪财经实时"}}, ""))
    with (
        patch.object(runtime, "_tickflow_quote_client", return_value=client),
        patch.object(runtime, "_tickflow_quote_datetime", return_value=NOW),
        patch.object(runtime, "_fetch_tencent_fund_quotes", tencent),
        patch.object(runtime, "fetch_sina_exchange_fund_quotes", sina),
        patch.object(runtime, "_fetch_wind_etf_quotes", return_value=({}, "")) as wind,
    ):
        quotes = runtime.fetch_tickflow_etf_quotes(["512890", "161125", "161128"], api_key="key", market_now=NOW)

    assert quotes["512890"]["source"] == "TickFlow"
    assert tencent.call_args.args[0] == ["161125.SZ", "161128.SZ"]
    assert sina.call_args.args[0] == ["161128.SZ"]
    wind.assert_not_called()
    assert set(quotes) == {"512890", "161125", "161128"}


def test_without_tickflow_key_fallbacks_still_provide_quotes():
    tencent = Mock(return_value=({"510500": {"price": 7.4, "source": "腾讯实时行情"}}, ""))
    with (
        patch.object(runtime, "_tickflow_quote_client") as client,
        patch.object(runtime, "_fetch_tencent_fund_quotes", tencent),
    ):
        quotes = runtime.fetch_tickflow_etf_quotes(["510500"], api_key="", market_now=NOW)

    client.assert_not_called()
    assert quotes["510500"]["price"] == 7.4
