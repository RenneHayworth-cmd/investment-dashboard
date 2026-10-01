"""EastMoney quote/kline hosts refuse requests; the 15:00 minute point still gives the close.

Seen 2026-09-29: 中证红利低波 stuck at 09-24 (official CSIndex rows discarded because the
EastMoney quote failed) and 微盘股 stuck at 09-28 (every kline route 503/dropped).
"""
from datetime import date
from unittest.mock import Mock, patch

import pandas as pd
import pytest
import requests

from services import index_sources_akshare as akshare_sources
from services import index_sources_eastmoney as em


def _trend_response(points):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"data": {"trends": points}}
    return response


class _DeadSession:
    trust_env = False

    def get(self, *_args, **_kwargs):
        raise requests.ConnectionError("Remote end closed connection without response")


def test_trend_close_accepts_only_the_1500_point_and_rotates_hosts():
    calls = []

    def get(url, **_kwargs):
        calls.append(url.split("/api")[0])
        if len(calls) < 3:
            raise requests.ConnectionError("dropped")
        return _trend_response(["2026-09-29 14:59,3808.10", "2026-09-29 15:00,3809.04"])

    with patch("requests.get", side_effect=get):
        assert em.fetch_eastmoney_trend_close("90.BK1158") == (pd.Timestamp("2026-09-29"), 3809.04)
    assert calls == ["https://push2his.eastmoney.com", "http://push2his.eastmoney.com", "https://91.push2his.eastmoney.com"]

    with patch("requests.get", return_value=_trend_response(["2026-09-29 13:45,3801.00"])):
        assert em.fetch_eastmoney_trend_close("90.BK1158") is None  # intraday, not a close

    with patch("requests.get", side_effect=requests.ConnectionError("dropped")) as get:
        assert em.fetch_eastmoney_trend_close("90.BK1158") is None
    assert get.call_count == 8


def test_quote_row_falls_back_to_the_1500_close_for_a_shares_only():
    history = pd.DataFrame({"trade_date": pd.to_datetime(["2026-09-24", "2026-09-28"]), "close": [10800.0, 10826.46]})
    with patch("requests.Session", return_value=_DeadSession()), \
         patch.object(em, "fetch_eastmoney_trend_close", return_value=(pd.Timestamp("2026-09-29"), 10819.28)):
        result = em.append_eastmoney_quote_row(history, "2.H30269")
    assert result["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-09-24", "2026-09-28", "2026-09-29"]
    assert result.iloc[-1]["close"] == 10819.28

    with patch("requests.Session", return_value=_DeadSession()), \
         patch.object(em, "fetch_eastmoney_trend_close", return_value=None):
        unchanged = em.append_eastmoney_quote_row(history, "2.H30269")
    assert len(unchanged) == 2

    with patch("requests.Session", return_value=_DeadSession()), \
         patch.object(em, "fetch_eastmoney_trend_close") as trend:
        em.append_eastmoney_quote_row(history, "124.HSTECH")
    trend.assert_not_called()  # Hong Kong closes at 16:00; the 15:00 rule does not apply


def test_h30269_keeps_official_rows_when_eastmoney_has_no_same_day_value():
    official = pd.DataFrame({"日期": [date(2026, 9, 24), date(2026, 9, 28)], "收盘": [10800.0, 10826.46]})
    ak = Mock()
    ak.stock_zh_index_hist_csindex.return_value = official
    with patch.dict("sys.modules", {"akshare": ak}), \
         patch.object(akshare_sources, "append_eastmoney_quote_row", side_effect=lambda df, *_a, **_k: df):
        export = akshare_sources.get_index_data_from_akshare_csindex("H30269", "中证红利低波", days=30)
    dates = pd.to_datetime(export["日期"] if "日期" in export.columns else export.iloc[:, 0]).dt.strftime("%Y-%m-%d").tolist()
    assert "2026-09-28" in dates  # previously the whole batch was discarded and 09-28 never saved
    # Stale official history is retained after all compatible daily adapters run.
    ak.stock_zh_index_daily.assert_called_once()


def test_board_kline_failure_uses_the_1500_close_as_one_row():
    with patch("requests.Session", return_value=_DeadSession()), \
         patch.object(em, "get_index_data_from_akshare_eastmoney_fallback", side_effect=RuntimeError("akshare down")), \
         patch.object(em, "fetch_eastmoney_trend_close", return_value=(pd.Timestamp("2026-09-29"), 3809.04)):
        export = em.get_index_data_from_eastmoney_kline("90.BK1158", "微盘股", days=10, fqt="1", akshare_board_symbol="BK1158")
    raw = em.extract_raw_from_export_df(export, "微盘股")
    assert raw["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-09-29"]
    assert raw.iloc[0]["close"] == 3809.04


def test_board_kline_failure_still_raises_without_a_completed_close():
    with patch("requests.Session", return_value=_DeadSession()), \
         patch.object(em, "get_index_data_from_akshare_eastmoney_fallback", side_effect=RuntimeError("akshare down")), \
         patch.object(em, "fetch_eastmoney_trend_close", return_value=None):
        with pytest.raises(RuntimeError, match="akshare down"):
            em.get_index_data_from_eastmoney_kline("90.BK1158", "微盘股", days=10, fqt="1", akshare_board_symbol="BK1158")
    with patch("requests.Session", return_value=_DeadSession()), \
         patch.object(em, "get_index_data_from_akshare_eastmoney_fallback", side_effect=RuntimeError("hk down")), \
         patch.object(em, "fetch_eastmoney_trend_close") as trend:
        with pytest.raises(RuntimeError):
            em.get_index_data_from_eastmoney_kline("124.HSTECH", "恒生科技", days=10, fqt="0", akshare_hk_em_symbol="HSTECH")
    trend.assert_not_called()


def _cst(text):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Asia/Shanghai"))


class _QuoteSession:
    trust_env = False

    def __init__(self, data):
        self.data = data

    def get(self, *_args, **_kwargs):
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = {"data": self.data}
        return response


def test_a_lunch_quote_is_never_the_close_of_a_finished_session():
    # 2026-09-29: push2delay still served the 11:30 BK1158 value (3826.14) after 15:00.
    assert em._stale_session_quote("A股", _cst("2026-09-29 11:30:00"))
    assert not em._stale_session_quote("A股", _cst("2026-09-29 15:00:03"))
    assert em._stale_session_quote("港股", _cst("2026-09-29 15:59:00"))

    lunch = int(_cst("2026-09-29 11:30:00").timestamp())
    closed = int(_cst("2026-09-29 15:00:03").timestamp())
    board = lambda stamp: {"diff": [{"f12": "BK1158", "f2": 3826.14 if stamp == lunch else 3809.04, "f124": stamp}]}
    with patch("requests.Session", return_value=_QuoteSession(board(lunch))):
        assert em.fetch_eastmoney_clist_latest_index_row(board_symbol="BK1158") is None
    with patch("requests.Session", return_value=_QuoteSession(board(closed))):
        row = em.fetch_eastmoney_clist_latest_index_row(board_symbol="BK1158")
    assert row.iloc[0]["close"] == 3809.04

    history = pd.DataFrame({"trade_date": pd.to_datetime(["2026-09-28"]), "close": [3743.27]})
    with patch("requests.Session", return_value=_QuoteSession({"f43": 3826.14, "f86": lunch})), \
         patch.object(em, "fetch_eastmoney_trend_close", return_value=(pd.Timestamp("2026-09-29"), 3809.04)):
        result = em.append_eastmoney_quote_row(history, "90.BK1158")
    assert result.iloc[-1]["close"] == 3809.04  # stale quote dropped, 15:00 minute close used


def _export(index_name, days):
    return pd.DataFrame({"日期": [f"2026-09-{day:02d}" for day in days], f"{index_name}_收盘价": [3800.0 + day for day in days]})


@patch("services.index_history._latest_completed_date_for_market", return_value=date(2026, 9, 30))
def test_lagging_tickflow_history_continues_to_akshare(_target):
    from services import index_history

    with patch.object(index_history, "get_index_data_from_tickflow", return_value=_export("上证指数", [28, 29])), \
            patch.object(index_history, "fetch_index_from_source", return_value=_export("上证指数", [29, 30])) as source:
        result = index_history.fetch_one_index("上证指数", {"tickflow_symbol": "000001.SH", "market_group": "A股"}, api_key="k")

    source.assert_called_once()
    assert result["日期"].tolist()[-1] == "2026-09-30"
    assert result.attrs["market_data_source"] == "AkShare"


@patch("services.index_history._latest_completed_date_for_market", return_value=date(2026, 9, 30))
def test_current_tickflow_history_skips_akshare(_target):
    from services import index_history

    with patch.object(index_history, "get_index_data_from_tickflow", return_value=_export("上证指数", [29, 30])), \
            patch.object(index_history, "fetch_index_from_source") as source:
        result = index_history.fetch_one_index("上证指数", {"tickflow_symbol": "000001.SH", "market_group": "A股"}, api_key="k")

    source.assert_not_called()
    assert result["日期"].tolist()[-1] == "2026-09-30"


@patch("services.index_history._latest_completed_date_for_market", return_value=date(2026, 9, 30))
def test_all_lagging_index_sources_keep_newest_history_with_warning(_target):
    from services import index_history

    with patch.object(index_history, "get_index_data_from_tickflow", return_value=_export("上证指数", [28, 29])), \
            patch.object(index_history, "fetch_index_from_source", side_effect=RuntimeError("akshare down")):
        result = index_history.fetch_one_index("上证指数", {"tickflow_symbol": "000001.SH", "market_group": "A股"}, api_key="k")

    assert result["日期"].tolist()[-1] == "2026-09-29"
    assert "所有接口均未覆盖目标日期" in result.attrs["market_source_warning"]
