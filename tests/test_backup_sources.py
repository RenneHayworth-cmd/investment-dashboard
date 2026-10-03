from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def wind_key(monkeypatch):
    from services.wind_source import reset_wind_cooldown

    monkeypatch.setenv("WIND_API_KEY", "test-key")
    reset_wind_cooldown()
    yield
    reset_wind_cooldown()


@pytest.mark.parametrize(
    ("contract", "expected"),
    [
        ("I2701P700", "I2701-P-700.DCE"),
        ("i2609p730", "I2609-P-730.DCE"),
        ("MO2606P5400", "MO2606-P-5400.CFE"),
        ("MO2612-P-6000", "MO2612-P-6000.CFE"),
        ("SR701C5000", "SR701-C-5000.CZC"),
        ("IM2612", None),
    ],
)
def test_wind_option_contract_code(contract, expected):
    from services.wind_source import wind_option_contract_code

    assert wind_option_contract_code(contract) == expected


def test_bond_daily_falls_back_to_tencent_then_wind(wind_key):
    import akshare as ak

    import services.live_price_history as history

    wind_bars = pd.DataFrame({"date": pd.to_datetime(["2026-09-29", "2026-09-30"]), "close": [149.1, 150.6]})
    with (
        patch.object(ak, "bond_zh_hs_cov_daily", side_effect=RuntimeError("新浪失败")),
        patch.object(ak, "stock_zh_a_hist_tx", side_effect=RuntimeError("腾讯失败")),
        patch("services.wind_source.fetch_wind_daily_bars", return_value=wind_bars) as wind,
    ):
        result = history._fetch_bond_daily("123282", target_date=date(2026, 9, 30))

    assert wind.call_args.args[:2] == ("stock_data", "123282.SZ")
    assert result.iloc[-1].tolist() == ["2026-09-30", 150.6]


def test_futures_quote_falls_back_to_wind(wind_key):
    import services.futures_live_realtime as realtime

    frame = pd.DataFrame([{"wind_code": "I2701.DCE", "trade_date": date(2026, 10, 9),
                           "quote_time": pd.Timestamp("2026-10-09T10:00:00+08:00"), "price": 705.0}])
    with (
        patch("services.futures_spread._fetch_futures_spot_from_sina_direct", side_effect=RuntimeError("新浪失败")),
        patch("akshare.futures_zh_spot", side_effect=RuntimeError("AkShare失败")),
        patch("services.wind_source.fetch_wind_quotes", return_value=frame) as wind,
    ):
        quote = realtime._fetch_futures_quote("I2701")

    wind.assert_called_once_with("index_data", ["I2701.DCE"])
    assert (quote["price"], quote["source"]) == (705.0, "万得Wind")


def test_option_quote_falls_back_to_wind(wind_key):
    import services.futures_live_realtime as realtime

    frame = pd.DataFrame([{"wind_code": "I2701-P-700.DCE", "trade_date": date(2026, 10, 9),
                           "quote_time": pd.Timestamp("2026-10-09T10:00:00+08:00"), "price": 18.6}])
    with (
        patch("services.futures_options_analysis.append_option_spot_row", side_effect=RuntimeError("期权链失败")),
        patch("services.wind_source.fetch_wind_quotes", return_value=frame),
    ):
        quote = realtime._fetch_option_quote("I2701P700", datetime(2026, 10, 9, 10, 0, tzinfo=TZ))

    assert (quote["price"], quote["source"]) == (18.6, "万得Wind")


def test_option_daily_uses_wind_when_sina_lags(wind_key):
    import services.futures_live_prices as prices

    stale = pd.DataFrame({"date": pd.to_datetime(["2026-09-29"]), "close": [21.0]})
    bars = pd.DataFrame({"date": pd.to_datetime(["2026-09-29", "2026-09-30"]), "close": [21.0, 18.6]})
    with (
        patch.object(prices, "fetch_option_from_akshare", return_value=(stale, "新浪", False)),
        patch("services.wind_source.fetch_wind_daily_bars", return_value=bars) as wind,
    ):
        data, source = prices._fetch_option_daily("I2701P700", "2026-09-30", None)

    assert wind.call_args.args[:2] == ("index_data", "I2701-P-700.DCE")
    assert source == "万得Wind期权日线"
    assert data["close"].iloc[-1] == 18.6


def test_option_daily_keeps_current_sina_data_without_calling_wind(wind_key):
    import services.futures_live_prices as prices

    current = pd.DataFrame({"date": pd.to_datetime(["2026-09-30"]), "close": [18.6]})
    with (
        patch.object(prices, "fetch_option_from_akshare", return_value=(current, "新浪", False)),
        patch("services.wind_source.fetch_wind_daily_bars") as wind,
    ):
        data, source = prices._fetch_option_daily("I2701P700", "2026-09-30", None)

    wind.assert_not_called()
    assert source == "新浪"
