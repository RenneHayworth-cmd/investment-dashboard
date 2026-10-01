from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from services.market_fallback import MarketSource, fetch_market_fallback, normalize_daily_prices
from services.akshare_sources import exchange_symbol, futures_daily_sources, raw_security_sources


def frame(day="2026-09-30", price=10):
    return pd.DataFrame({"date": [day], "close": [price]})


def fetch(sources, **kwargs):
    return fetch_market_fallback(sources, normalize_daily_prices, date_column="date", price_column="close", **kwargs)


@pytest.mark.parametrize("first", [pd.DataFrame(), None, frame(price=np.inf), frame(price=-1), frame(day="invalid"), pd.DataFrame({"wrong": [1]}), frame(day="2026-09-29")])
def test_bad_or_stale_response_tries_next_then_stops(first):
    backup, unused = Mock(return_value=frame()), Mock()
    result = fetch([MarketSource("主源", lambda: first), MarketSource("备用", backup), MarketSource("未使用", unused)], target_date="2026-09-30")
    backup.assert_called_once()
    unused.assert_not_called()
    assert result.attrs["market_data_source"] == "备用"
    assert len(result.attrs["market_source_failures"]) == 1


def test_all_exceptions_are_reported_without_losing_source_names():
    a, b = Mock(side_effect=TimeoutError("超时")), Mock(side_effect=RuntimeError("连接断开"))
    with pytest.raises(RuntimeError, match="主源：超时.*备用：连接断开"):
        fetch([MarketSource("主源", a), MarketSource("备用", b)])
    a.assert_called_once()
    b.assert_called_once()


def test_all_stale_sources_are_tried_and_newest_partial_is_labelled():
    a, b, c = Mock(return_value=frame("2026-09-28")), Mock(return_value=frame("2026-09-29")), Mock(return_value=frame("2026-09-25"))
    result = fetch([MarketSource("a", a), MarketSource("b", b), MarketSource("c", c)], target_date="2026-09-30")
    assert result.attrs["market_data_source"] == "b"
    assert "未覆盖目标日期" in result.attrs["market_source_warning"]
    assert len(result.attrs["market_source_failures"]) == 3
    assert all(mock.call_count == 1 for mock in (a, b, c))


def test_future_row_cannot_satisfy_requested_day_and_strict_mode_rejects_partial():
    with pytest.raises(RuntimeError, match="日期滞后"):
        fetch([MarketSource("a", lambda: pd.concat([frame("2026-09-29"), frame("2026-10-01")]))], target_date="2026-09-30", allow_partial=False)


def test_valid_target_response_does_not_return_future_unfinished_rows():
    result = fetch([MarketSource("a", lambda: pd.concat([frame("2026-09-30"), frame("2026-10-01")]))], target_date="2026-09-30")
    assert result["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-09-30"]


def test_worthless_option_zero_price_is_allowed_only_with_explicit_option_policy():
    result = fetch([MarketSource("期权", lambda: frame(price=0))], allow_zero_price=True)
    assert result["close"].tolist() == [0]
    with pytest.raises(RuntimeError):
        fetch([MarketSource("股票", lambda: frame(price=0))])


def test_snapshot_fallback_uses_exact_board_members_and_refuses_missing_cap():
    from services import microcap
    ak = Mock()
    ak.stock_board_concept_cons_em.return_value = pd.DataFrame({"代码": ["600455", "920001"], "名称": ["甲", "乙"]})
    ak.stock_zh_a_spot_em.return_value = pd.DataFrame({
        "代码": ["600455", "920001", "000001"], "总市值": [2e9, 1e9, 1e8],
        "最新价": [10, 5, 1], "涨跌幅": [1, -1, 2], "成交量": [100, 0, 200], "成交额": [10000, 0, 20000],
    })
    with patch.dict("sys.modules", {"akshare": ak}), patch.object(microcap, "_fetch_microcap_stocks_eastmoney", side_effect=TimeoutError("主源超时")):
        result = microcap.fetch_microcap_stocks(page_size=2, retries=1)
        assert result["代码"].tolist() == ["920001", "600455"]
        assert result["是否停牌"].tolist() == [True, False]
        ak.stock_board_concept_cons_em.assert_called_with(symbol="BK1158")
        ak.stock_zh_a_spot_em.return_value.loc[0, "总市值"] = np.nan
        with pytest.raises(RuntimeError, match="市值不完整"):
            microcap.fetch_microcap_stocks(page_size=2, retries=1)


def test_concrete_futures_stale_sina_continues_to_em():
    from services import futures_spread
    ak = Mock()
    ak.futures_zh_daily_sina.return_value = frame("2026-09-29", 700)
    ak.futures_hist_table_em.return_value = pd.DataFrame({"合约代码": ["i2701"]})
    ak.futures_hist_em.return_value = frame("2026-09-30", 710)
    with patch.dict("sys.modules", {"akshare": ak}), patch.dict("os.environ", {"INVESTMENT_DASHBOARD_ALERT_TICKFLOW_TIMEOUT_SECONDS": ""}):
        result = futures_spread.fetch_futures_daily_from_akshare("I2701", target_date="2026-09-30")
    assert result["close"].tolist() == [710]
    ak.futures_hist_em.assert_called_once()


def test_settlement_fallback_uses_exact_exchange_contract_and_never_close_as_settlement():
    from services import futures_live_settlements as market
    ak = Mock()
    ak.futures_zh_daily_sina.return_value = pd.DataFrame({"date": ["2026-09-30"], "close": [700]})
    ak.get_futures_daily.return_value = pd.DataFrame({
        "date": ["2026-09-30", "2026-09-30"], "symbol": ["i2701", "i2705"], "close": [700, 710], "settle": [698, 708],
    })
    with patch.dict("sys.modules", {"akshare": ak}):
        result, source = market._fetch_futures_settlement_history("I2701", start_date="2026-09-30", end_date="2026-09-30")
    assert result["settlement"].tolist() == [698]
    assert "DCE" in source
    ak.get_futures_daily.assert_called_once_with(start_date="20260930", end_date="20260930", market="DCE")


@pytest.mark.parametrize("symbol,expected", [("510500", "sh510500"), ("600455", "sh600455"), ("920001", "bj920001"), ("900901", "sh900901"), ("164906.SZ", "sz164906"), ("000001", "sz000001")])
def test_exchange_codes_do_not_confuse_shanghai_etfs_and_beijing_stocks(symbol, expected):
    assert exchange_symbol(symbol) == expected


@pytest.mark.parametrize("failure", [RuntimeError("限流"), pd.DataFrame(), frame("2026-09-29")])
def test_microcap_any_failure_or_stale_history_uses_tencent(failure):
    from services import microcap_live_market as market
    ak = Mock()
    if isinstance(failure, Exception):
        ak.stock_zh_a_hist.side_effect = failure
    else:
        ak.stock_zh_a_hist.return_value = failure
    ak.stock_zh_a_hist_tx.return_value = frame()
    with patch.dict("sys.modules", {"akshare": ak}):
        result = market._akshare_history("920001", "2026-09-01", "2026-09-30")
    assert result.attrs["source"] == "akshare_tx"
    assert ak.stock_zh_a_hist_tx.call_args.kwargs["symbol"] == "bj920001"
    ak.stock_zh_a_daily.assert_not_called()


def test_microcap_empty_tencent_also_tries_sina():
    from services import microcap_live_market as market
    ak = Mock()
    ak.stock_zh_a_hist.return_value = pd.DataFrame()
    ak.stock_zh_a_hist_tx.return_value = pd.DataFrame()
    ak.stock_zh_a_daily.return_value = frame()
    with patch.dict("sys.modules", {"akshare": ak}), patch.object(market, "_akshare_history_fallback") as direct:
        result = market._akshare_history("600455", "2026-09-01", "2026-09-30")
    assert result.attrs["source"] == "akshare_sina"
    direct.assert_not_called()


def test_additive_etf_never_calls_tencent_or_unchecked_sina():
    from services import position_market as market
    ak = Mock()
    ak.fund_lof_hist_em.return_value = pd.DataFrame({"日期": ["2026-09-30"], "收盘": [10]})
    with patch.dict("sys.modules", {"akshare": ak}), \
         patch.object(market, "_fetch_eastmoney_exchange_fund_close", side_effect=RuntimeError("断开")), \
         patch.object(market, "_append_sina_final_close", side_effect=lambda frame, **kwargs: frame):
        result = market._fetch_exchange_fund_close(symbol="161128.SZ", count=500, adjust="forward_additive", market_now=datetime(2026, 9, 30, 16, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert result["收盘价"].tolist() == [10]
    assert ak.fund_lof_hist_em.call_args.kwargs["adjust"] == "qfq"
    ak.stock_zh_a_hist_tx.assert_not_called()
    ak.fund_etf_hist_sina.assert_not_called()


def test_futures_exact_contract_uses_em_but_main_keeps_same_vendor_series():
    ak = Mock()
    ak.futures_zh_daily_sina.return_value = pd.DataFrame()
    ak.futures_hist_table_em.return_value = pd.DataFrame({"合约代码": ["i2701", "i2705"]})
    ak.futures_hist_em.return_value = pd.DataFrame({"时间": ["2026-09-30"], "收盘": [700]})
    result = fetch(futures_daily_sources(ak, "i2701"))
    assert result["close"].tolist() == [700]
    ak.futures_hist_em.assert_called_once_with(symbol="i2701", period="daily")
    assert all("futures_hist_em" not in source.name for source in futures_daily_sources(ak, "I0"))


def test_nav_uses_cumulative_indicator_when_primary_disconnects():
    from services import fund_analysis
    ak = Mock()
    ak.fund_open_fund_info_em.return_value = pd.DataFrame({"净值日期": ["2026-09-29"], "累计净值": [1.2]})
    with patch.dict("sys.modules", {"akshare": ak}), patch.object(fund_analysis, "_fetch_eastmoney_fund_nav", side_effect=TimeoutError("超时")):
        result = fund_analysis.fetch_eastmoney_fund_nav("000001")
    ak.fund_open_fund_info_em.assert_called_once_with(symbol="000001", indicator="累计净值走势")
    assert result["累计净值"].tolist() == [1.2]


def test_annual_etf_fallback_removes_unfinished_future_rows():
    from services import annual_etf_market
    ak = Mock()
    ak.stock_zh_a_hist_tx.return_value = pd.DataFrame()
    ak.fund_etf_hist_em.return_value = pd.concat([frame("2026-09-29"), frame("2026-09-30")])
    with patch.dict("sys.modules", {"akshare": ak}), patch("services.market_calendar.latest_settled_trade_date", return_value=date(2026, 9, 29)):
        result = annual_etf_market.fetch_annual_etf_raw_history(SimpleNamespace(symbol="510500.SH"))
    assert result["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-09-29"]
    assert ak.stock_zh_a_hist_tx.call_args.kwargs["symbol"] == "sh510500"


def test_us_unadjusted_history_falls_back_and_resolves_actual_em_code():
    from services import us_stock_analysis as market
    ak = Mock()
    ak.stock_us_daily.return_value = pd.DataFrame()
    ak.stock_us_spot_em.return_value = pd.DataFrame({"代码": ["105.MSFT", "106.ABC"]})
    ak.stock_us_hist.return_value = frame("2026-09-29", 100)
    with patch.dict("sys.modules", {"akshare": ak}), patch.object(market, "_fetch_tickflow_us_daily", side_effect=RuntimeError("限流")):
        result = market.fetch_tickflow_us_daily("MSFT.US", count=100, adjust="none")
    assert result["收盘价"].tolist() == [100]
    ak.stock_us_hist.assert_called_once_with(symbol="105.MSFT", period="daily", adjust="")


def test_us_adjusted_history_never_substitutes_unadjusted_or_ratio_prices():
    from services import us_stock_analysis as market
    ak = Mock()
    with patch.dict("sys.modules", {"akshare": ak}), patch.object(market, "_fetch_tickflow_us_daily", side_effect=RuntimeError("限流")):
        with pytest.raises(RuntimeError, match="TickFlow"):
            market.fetch_tickflow_us_daily("MSFT.US", count=100, adjust="forward_additive")
    ak.stock_us_daily.assert_not_called()
    ak.stock_us_hist.assert_not_called()
