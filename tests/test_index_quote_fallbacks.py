"""Fallbacks for the overseas server, where EastMoney quote hosts return 502 or drop connections."""
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

from services import index_realtime as realtime
from services.index_sources_eastmoney import get_index_data_from_eastmoney_kline

SH = ZoneInfo("Asia/Shanghai")
TENCENT_CSI500 = (
    'v_sh000905="1~中证500~000905~7435.45~7402.30~7388.11~72183857~0~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0'
    '~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~~20260929112921~33.15~0.45~7443.73~7386.14~";'
)


class _DeadSession:
    trust_env = False

    def get(self, *_args, **_kwargs):
        raise requests.ConnectionError("Remote end closed connection without response")


def _response(*, content: bytes = b"", payload=None):
    response = Mock()
    response.raise_for_status = Mock()
    response.content = content
    response.json.return_value = payload
    return response


@patch("requests.Session", return_value=_DeadSession())
def test_exchange_index_falls_back_to_tencent_when_eastmoney_fails(_session):
    with patch("requests.get", return_value=_response(content=TENCENT_CSI500.encode("gbk"))) as get:
        quote = realtime._fetch_eastmoney_quote("中证500", "1.000905")
    assert get.call_args.args[0] == "https://qt.gtimg.cn/q=sh000905"
    assert quote == {
        "price": 7435.45, "previous_close": 7402.30, "change_pct": 0.45, "volume": None, "position": None,
        "quote_time": datetime(2026, 9, 29, 11, 29, 21, tzinfo=SH), "source": "腾讯行情",
    }


def test_tencent_rejects_a_different_symbol_or_bad_payload():
    wrong_code = TENCENT_CSI500.replace("~000905~", "~000852~")
    for body in (wrong_code, 'v_sh000905="";', "garbage"):
        with patch("requests.get", return_value=_response(content=body.encode("gbk"))):
            assert realtime._fetch_tencent_index_quote("1.000905", "Asia/Shanghai") is None
    assert realtime._fetch_tencent_index_quote("90.BK1158", "Asia/Shanghai") is None  # boards are not on Tencent
    assert realtime._fetch_tencent_index_quote("124.HSTECH", "Asia/Shanghai") is None


def test_us_eastmoney_quote_rejects_composite_for_nasdaq100():
    wrong = _response(payload={"data": {"f57": "NDX", "f58": "纳斯达克", "f43": 26800}})
    session = Mock()
    session.get.return_value = wrong
    with patch("requests.Session", return_value=session):
        assert realtime._fetch_eastmoney_quote("纳斯达克100", realtime.EASTMONEY_QUOTE_SECIDS["纳斯达克100"]) is None


def test_us_eastmoney_quote_uses_exact_nasdaq100_identity():
    right = _response(payload={"data": {"f57": "NDX100", "f58": "纳斯达克100", "f43": 30350,
                                         "f60": 30408.5, "f86": 1790866800}})
    session = Mock()
    session.get.return_value = right
    with patch("requests.Session", return_value=session):
        quote = realtime._fetch_eastmoney_quote("纳斯达克100", realtime.EASTMONEY_QUOTE_SECIDS["纳斯达克100"])
    assert session.get.call_args.kwargs["params"]["secid"] == "100.NDX100"
    assert quote["price"] == 30350


@patch("requests.Session", return_value=_DeadSession())
def test_board_falls_back_to_eastmoney_minute_trend_rotating_hosts(_session):
    ok = _response(payload={"data": {"preClose": 3743.27, "trends": ["2026-09-29 11:28,3824.10", "2026-09-29 11:29,3825.79"]}})
    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        if len(calls) < 3:
            raise requests.ConnectionError("dropped")
        return ok

    with patch("requests.get", side_effect=get):
        quote = realtime._fetch_eastmoney_quote("微盘股", "90.BK1158")
    assert [u.split("/api")[0] for u in calls] == [
        "https://push2his.eastmoney.com", "http://push2his.eastmoney.com", "https://91.push2his.eastmoney.com"]
    assert quote["price"] == 3825.79
    assert quote["previous_close"] == 3743.27
    assert quote["change_pct"] == pytest.approx((3825.79 / 3743.27 - 1) * 100)
    assert quote["quote_time"] == datetime(2026, 9, 29, 11, 29, tzinfo=SH)
    assert quote["source"] == "东方财富分时"


@patch("requests.Session", return_value=_DeadSession())
def test_board_quote_is_none_when_every_route_fails(_session):
    with patch("requests.get", side_effect=requests.ConnectionError("dropped")) as get:
        assert realtime._fetch_eastmoney_quote("微盘股", "90.BK1158") is None
    assert get.call_count == 8  # 4 hosts x https/http, nothing else is tried for a board


def test_daily_kline_tries_http_after_https_fails():
    seen = []

    class Session:
        trust_env = False

        def get(self, url, **_kwargs):
            seen.append(url.split("/api")[0])
            if url.startswith("https://"):
                raise requests.ConnectionError("dropped")
            return _response(payload={"data": {"klines": ["2026-09-24,1,3768.12", "2026-09-28,1,3743.27"]}})

    with patch("requests.Session", return_value=Session()), \
         patch("services.index_sources_eastmoney.append_eastmoney_latest_index_row", side_effect=lambda _ak, df, *_a, **_k: df):
        result = get_index_data_from_eastmoney_kline("90.BK1158", "微盘股", days=5, fqt="1", akshare_board_symbol="BK1158")
    assert seen == ["https://push2his.eastmoney.com", "http://push2his.eastmoney.com"]
    assert result is not None and not result.empty
