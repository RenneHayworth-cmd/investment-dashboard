from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from components.position import runtime_state
from services import position_runtime as runtime


def at(hour, minute=0, *, day=21):
    return datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def isolated_runtime():
    runtime._RUNTIME_ETF_QUOTE_CACHE.clear()
    runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear()
    runtime_state._PREVIEWS.clear()
    yield
    runtime._RUNTIME_ETF_QUOTE_CACHE.clear()
    runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear()
    runtime_state._PREVIEWS.clear()


def quotes_for(codes, *, market_now, **kwargs):
    return {code: {"price": 1.0, "quote_time": market_now} for code in codes}


def refresh(now):
    return runtime.refresh_runtime_etf_quotes(
        ["159501", "518850"], api_key="test-only", market_now=now,
    )


@pytest.mark.parametrize("start", [at(9, 30), at(10, 30), at(13), at(14, 50)])
def test_all_etf_trading_bands_refresh_at_two_minutes_and_reuse_earlier_reruns(start):
    with patch.object(runtime, "fetch_tickflow_etf_quotes", side_effect=quotes_for) as fetch:
        first = refresh(start)
        assert refresh(start + timedelta(seconds=119)) == first
        fetch.assert_called_once()
        result = refresh(start + timedelta(seconds=120))
        assert fetch.call_count == 2
        assert result["159501"]["quote_time"] == start + timedelta(seconds=120)


def test_failed_intraday_batch_keeps_previous_quotes_and_backs_off_ten_minutes():
    with patch.object(runtime, "fetch_tickflow_etf_quotes", side_effect=quotes_for):
        previous = refresh(at(10, 30))
    with patch.object(runtime, "fetch_tickflow_etf_quotes", side_effect=RuntimeError("限流")) as fetch:
        with pytest.raises(RuntimeError, match="限流"):
            refresh(at(10, 32))
        assert refresh(at(10, 34)) == previous
        assert refresh(at(10, 40)) == previous
        fetch.assert_called_once()
    with patch.object(runtime, "fetch_tickflow_etf_quotes", side_effect=quotes_for) as fetch:
        assert refresh(at(10, 42))["159501"]["quote_time"] == at(10, 42)
        fetch.assert_called_once()


@pytest.mark.parametrize("start,interval", [
    (at(9, 30), 600), (at(10, 30), 1800), (at(13), 1800), (at(14, 50), 120),
])
def test_auxiliary_quotes_keep_their_original_cadence(start, interval):
    state = {
        "trade_date": start.date().isoformat(),
        "band": runtime._runtime_quote_refresh_band(start)[0],
        "last_attempt": start.replace(tzinfo=None).isoformat(),
        "success": True,
    }
    assert not runtime.auxiliary_quote_refresh_due(start + timedelta(seconds=interval - 1), state)
    assert runtime.auxiliary_quote_refresh_due(start + timedelta(seconds=interval), state)


def test_auxiliary_lunch_success_is_reused_and_failures_keep_ten_minute_retry():
    state = {
        "trade_date": "2026-08-21", "band": "午间",
        "last_attempt": "2026-08-21T11:35:00", "success": True,
    }
    assert not runtime.auxiliary_quote_refresh_due(at(12, 50), state)
    assert runtime.auxiliary_quote_refresh_due(at(13), state)
    state["success"] = False
    assert not runtime.auxiliary_quote_refresh_due(at(11, 44), state)
    assert runtime.auxiliary_quote_refresh_due(at(11, 45), state)
    assert not runtime.auxiliary_quote_refresh_due(at(16), state)


def test_complete_lunch_batch_is_reused_until_afternoon_trading_resumes():
    with patch.object(runtime, "fetch_tickflow_etf_quotes", side_effect=quotes_for) as fetch:
        first = refresh(at(11, 35))
        later = refresh(at(12, 50))
        assert first == later
        fetch.assert_called_once()
        afternoon = refresh(at(13, 0))
        assert fetch.call_count == 2
        assert afternoon["159501"]["quote_time"] == at(13)


def test_partial_lunch_batch_does_not_count_cached_morning_prices_as_success():
    with patch.object(runtime, "fetch_tickflow_etf_quotes", side_effect=quotes_for):
        refresh(at(10, 30))
    partial = {"159501": {"price": 1.1, "quote_time": at(11, 35)}}
    with patch.object(runtime, "fetch_tickflow_etf_quotes", return_value=partial) as fetch:
        result = refresh(at(11, 35))
        assert result["518850"]["quote_time"] == at(10, 30)
        assert runtime.load_runtime_etf_quote_state()["last_success_scope"] == ["159501"]
        refresh(at(11, 40))
        fetch.assert_called_once()
    with patch.object(runtime, "fetch_tickflow_etf_quotes", side_effect=quotes_for) as fetch:
        result = refresh(at(11, 45))
        assert result["518850"]["quote_time"] == at(11, 45)
        refresh(at(12, 30))
        fetch.assert_called_once()


@pytest.mark.parametrize("now", [at(8, 30), at(15), at(15, 2), at(16), at(10, day=22)])
def test_quote_refresh_never_fetches_outside_a_share_quote_hours(now):
    with patch.object(runtime, "fetch_tickflow_etf_quotes") as fetch:
        assert runtime.refresh_runtime_etf_quotes(
            ["159501"], api_key="test-only", market_now=now, force=True,
        ) == {}
        fetch.assert_not_called()


def test_preview_reopens_share_snapshots_without_mutating_each_other():
    first = {}
    runtime_state.restore_previews(first, ("ETF",), at(11, 35))
    first["position_etf_lunch_timing_preview"] = {"quotes": {"159501": {"price": 1.0}}}
    first["position_derivative_realtime_preview"] = {"items": {"I2701": "quote"}}
    runtime_state.remember_previews(first, at(11, 35))
    reopened = {}
    runtime_state.restore_previews(reopened, ("ETF",), at(12, 35))
    assert reopened["position_derivative_realtime_preview"] == first["position_derivative_realtime_preview"]
    reopened["position_etf_lunch_timing_preview"]["quotes"]["159501"]["price"] = 2.0
    third = {}
    runtime_state.restore_previews(third, ("ETF",), at(12, 40))
    assert third["position_etf_lunch_timing_preview"]["quotes"]["159501"]["price"] == 1.0


def test_preview_scope_changes_and_new_trade_dates_clear_old_previews():
    session = {}
    runtime_state.restore_previews(session, ("ETF",), at(11, 35))
    session["position_etf_lunch_timing_preview"] = {"quotes": {"159501": {"price": 1.0}}}
    session["position_etf_auto_final_last_attempt"] = "2026-08-21T15:06:00"
    runtime_state.remember_previews(session, at(15, 6))
    runtime_state.restore_previews(session, ("other ETF",), at(16))
    assert "position_etf_lunch_timing_preview" not in session
    runtime_state.restore_previews(session, ("ETF",), at(16))
    assert "position_etf_lunch_timing_preview" in session
    runtime_state.restore_previews(session, ("ETF",), at(9, 30, day=24))
    assert "position_etf_lunch_timing_preview" not in session
    assert "position_etf_auto_final_last_attempt" not in session
