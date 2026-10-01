from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from services import index_realtime as realtime


def cst(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai"))


def quote(value, price=100.0):
    return {"price": price, "quote_time": cst(value), "source": "测试"}


@pytest.fixture(autouse=True)
def isolated_transient_quotes():
    realtime._RUNTIME_QUOTE_CACHE.clear()
    realtime._RUNTIME_LUNCH_QUOTE_KEYS.clear()
    yield
    realtime._RUNTIME_QUOTE_CACHE.clear()
    realtime._RUNTIME_LUNCH_QUOTE_KEYS.clear()


@pytest.mark.parametrize("name,clock,active,requested", [
    ("沪深300", "09:29", False, False),
    ("沪深300", "11:30", False, True),
    ("沪深300", "13:00", True, True),
    ("沪深300", "15:00", False, False),
    ("恒生科技", "11:45", True, True),
    ("恒生科技", "12:00", False, True),
    ("恒生科技", "13:00", True, True),
    ("恒生科技", "15:30", True, True),
    ("恒生科技", "16:00", False, False),
    ("日经225", "08:00", True, True),
    ("日经225", "10:30", False, True),
    ("日经225", "11:30", True, True),
    ("日经225", "14:30", False, False),
    ("韩国KOSPI", "12:00", True, True),
    ("韩国KOSPI", "14:30", False, False),
    ("铁矿石主连", "10:15", False, False),
    ("铁矿石主连", "10:30", True, True),
    ("铁矿石主连", "13:15", False, True),
    ("铁矿石主连", "13:30", True, True),
    ("中证500期货主连", "13:00", True, True),
    ("中证500期货主连", "21:30", False, False),
])
def test_request_selection_uses_each_markets_own_session(name, clock, active, requested):
    now = cst(f"2026-07-23 {clock}")
    names, _ = realtime.manual_quote_request_names(now=now)
    assert realtime.quote_is_active_for_display(name, now=now) is active
    assert (name in names) is requested


@pytest.mark.parametrize("name,clock", [
    ("沪深300", "12:15"), ("恒生科技", "12:15"),
    ("日经225", "10:45"), ("铁矿石主连", "13:15"),
])
def test_lunch_success_survives_a_new_browser_session(name, clock):
    now = cst(f"2026-07-23 {clock}")
    names, keys = realtime.manual_quote_request_names(now=now)
    assert name in names
    prices = {name: quote(f"2026-07-23 {clock}")}
    realtime.remember_runtime_realtime_quotes(prices)
    realtime.remember_runtime_lunch_quotes(prices, keys, now=now)
    reopened_names, _ = realtime.manual_quote_request_names(set(), now=now)
    assert name not in reopened_names
    tomorrow_names, _ = realtime.manual_quote_request_names(now=cst(f"2026-07-24 {clock}"))
    assert name in tomorrow_names


@pytest.mark.parametrize("bad_quote", [
    quote("2026-07-22 11:30"), quote("2026-07-23 09:30"),
    quote("2026-07-23 11:30", price=0), {"price": 100.0},
])
def test_invalid_lunch_response_does_not_disable_future_retries(bad_quote):
    now = cst("2026-07-23 12:00")
    _, keys = realtime.manual_quote_request_names(now=now)
    completed = realtime.remember_runtime_lunch_quotes({"沪深300": bad_quote}, keys, now=now)
    assert not completed
    names, _ = realtime.manual_quote_request_names(now=now)
    assert "沪深300" in names


@pytest.mark.parametrize("name,quote_clock,now_clock", [
    ("沪深300", "14:59", "15:02"), ("恒生科技", "15:59", "16:02"),
    ("日经225", "14:29", "14:32"), ("韩国KOSPI", "14:29", "14:32"),
    ("铁矿石主连", "14:59", "15:02"),
])
def test_after_close_keep_last_quote_until_formal_close_is_confirmed(name, quote_clock, now_clock):
    prices = quote(f"2026-07-23 {quote_clock}")
    now = cst(f"2026-07-23 {now_clock}")
    assert realtime.quote_is_visible_for_manual_display(name, prices, now=now, formal_trade_date="2026-07-22")
    assert not realtime.quote_is_visible_for_manual_display(name, prices, now=now, formal_trade_date="2026-07-23")
    names, _ = realtime.manual_quote_request_names(now=now)
    assert name not in names


def test_us_quote_retention_uses_new_york_date_across_china_midnight():
    prices = quote("2026-07-23 03:59")  # 07-22 15:59 New York
    now = cst("2026-07-23 04:02")
    assert realtime.quote_is_visible_for_manual_display("标普500", prices, now=now, formal_trade_date="2026-07-21")
    assert not realtime.quote_is_visible_for_manual_display("标普500", prices, now=now, formal_trade_date="2026-07-22")
    assert not realtime.quote_is_visible_for_manual_display("标普500", prices, now=cst("2026-07-23 22:00"))


@pytest.mark.parametrize("day,before,opened,closed", [
    ("2026-07-23", "21:29", "21:30", "2026-07-24 04:00"),
    ("2026-11-12", "22:29", "22:30", "2026-11-13 05:00"),
])
def test_us_open_and_close_follow_daylight_saving_time(day, before, opened, closed):
    assert not realtime.quote_is_active_for_display("标普500", now=cst(f"{day} {before}"))
    assert realtime.quote_is_active_for_display("标普500", now=cst(f"{day} {opened}"))
    assert not realtime.quote_is_active_for_display("标普500", now=cst(closed))


def test_futures_friday_quote_survives_midnight_and_cannot_replace_friday_close():
    prices = quote("2026-07-17 23:45")
    saturday = cst("2026-07-18 01:00")
    assert realtime.quote_is_visible_for_manual_display("沪金主连", prices, now=saturday, formal_trade_date="2026-07-17")
    assert realtime.quote_is_visible_for_manual_display("沪金主连", prices, now=cst("2026-07-18 03:00"), formal_trade_date="2026-07-17")
    assert not realtime.quote_is_visible_for_manual_display("沪金主连", prices, now=cst("2026-07-20 16:00"), formal_trade_date="2026-07-20")


@pytest.mark.parametrize("value", ["2026-09-30 21:30", "2026-10-01 01:00", "2026-04-03 21:30", "2026-04-04 01:00"])
def test_holiday_eve_suspends_evening_and_following_midnight_futures(value):
    names, _ = realtime.manual_quote_request_names(now=cst(value))
    assert not names.intersection(realtime.FUTURES_QUOTE_SYMBOLS)


@pytest.mark.parametrize("name,now,previous,target", [
    ("恒生科技", "2026-12-24 12:09", "2026-12-23", "2026-12-23"),
    ("恒生科技", "2026-12-24 12:10", "2026-12-23", "2026-12-24"),
    ("标普500", "2026-11-28 02:09", "2026-11-25", "2026-11-25"),
    ("标普500", "2026-11-28 02:10", "2026-11-25", "2026-11-27"),
])
def test_half_day_stops_quotes_and_waits_for_its_own_close_confirmation_delay(name, now, previous, target):
    names, keys = realtime.manual_quote_request_names(now=cst(now))
    assert name not in names
    assert name not in keys  # A half day close is not a lunch break.
    assert realtime._daily_update_target(name, now=cst(now)).isoformat() == target


def test_completed_formal_caches_skip_network_work_independently_of_other_markets():
    now = cst("2026-07-23 15:10")
    selected = {"沪深300", "恒生科技", "日经225", "标普500"}

    def history(symbol, *_args):
        name = next(name for name in selected if realtime.raw_cache_symbol(name, realtime.INDEX_CONFIG[name]) == symbol)
        day = "2026-07-22" if name in {"恒生科技", "标普500"} else "2026-07-23"
        return pd.DataFrame({"trade_date": [day], "close": [100.0]}), {}

    with patch.object(realtime, "load_dataset", side_effect=history), patch.object(realtime, "missing_recent_market_trade_dates", return_value=[]):
        assert not realtime.find_pending_post_close_index_names(now=now, index_names=selected)
        pending = realtime.find_pending_post_close_index_names(now=cst("2026-07-23 16:10"), index_names=selected)
        assert pending == {"恒生科技"}


def test_china_holiday_does_not_suspend_other_markets():
    names, _ = realtime.manual_quote_request_names(now=cst("2026-09-25 10:00"))
    assert "沪深300" not in names
    assert "恒生科技" in names
    assert "日经225" in names
    assert "韩国KOSPI" not in names


def test_old_browser_snapshot_does_not_mask_newer_process_quote():
    newest = quote("2026-07-23 11:30", price=102.0)
    old = quote("2026-07-23 10:00", price=101.0)
    realtime.remember_runtime_realtime_quotes({"沪深300": newest})
    merged = realtime.load_runtime_realtime_quotes({"沪深300": old})
    assert merged["沪深300"]["price"] == 102.0
    merged["沪深300"]["price"] = 999.0
    assert realtime.load_runtime_realtime_quotes()["沪深300"]["price"] == 102.0
    realtime.remember_runtime_realtime_quotes({"沪深300": old})
    assert realtime.load_runtime_realtime_quotes()["沪深300"]["price"] == 102.0


def test_confirmed_cache_dates_do_not_use_raw_history_or_another_futures_contract():
    confirmed = pd.DataFrame({"trade_date": ["2026-07-22"], "close": [100.0]})
    concrete = pd.DataFrame({"trade_date": ["2026-07-21"], "close": [101.0]})
    with (
        patch.object(realtime, "load_dataset", return_value=(confirmed, {})) as load,
        patch("services.index_update_persistence.load_futures_current_contract_history", return_value=("I2701", concrete)),
    ):
        dates = realtime.load_index_formal_trade_dates(["沪深300", "铁矿石主连"])
    assert dates["沪深300"] == pd.Timestamp("2026-07-22")
    assert dates["铁矿石主连"] == pd.Timestamp("2026-07-21")
    load.assert_called_once()
    assert load.call_args.args[1] == "index_final_history"


def test_main_series_correction_gap_keeps_the_last_quote_until_corrected_close_arrives():
    def history(_symbol, source, _kind):
        day = "2026-09-30" if source == "index_final_history" else "2026-09-29"
        return pd.DataFrame({"trade_date": [day], "close": [100.0]}), {}

    with (
        patch.object(realtime, "load_dataset", side_effect=history),
        patch("services.index_update_persistence.load_futures_current_contract_history", return_value=(None, None)),
    ):
        dates = realtime.load_index_formal_trade_dates(["原油主连"])
    assert dates["原油主连"] == pd.Timestamp("2026-09-29")


def fixed_time_page_source(day, hour, minute):
    from pathlib import Path
    source = (Path(__file__).parents[1] / "pages" / "1_指数监控.py").read_text()
    clock = f'''from datetime import datetime as _datetime
class datetime(_datetime):
    @classmethod
    def now(cls, tz=None):
        return _datetime({day.year}, {day.month}, {day.day}, {hour}, {minute}, tzinfo=tz)
'''
    return source.replace("from datetime import datetime\n", clock, 1)


def test_page_reopens_and_deduplicates_lunch_but_updates_still_open_markets():
    from streamlit.testing.v1 import AppTest
    source = fixed_time_page_source(cst("2026-07-23"), 12, 15)

    def batch(**kwargs):
        return {name: {"price": 100.0, "quote_time": kwargs["now"]} for name in kwargs["force_index_names"]}

    with (
        patch("core.db.init_db"),
        patch.object(realtime, "fetch_realtime_index_quotes", side_effect=batch) as fetch,
        patch.object(realtime, "find_pending_post_close_index_names", return_value=set()),
        patch.object(realtime, "fetch_futures_main_contract_names", return_value={}),
        patch("services.update_tasks.find_pending_futures_current_contract_index_names", return_value=set()),
        patch("services.update_tasks.run_index_ma20_update") as daily_update,
    ):
        first = AppTest.from_string(source, default_timeout=30).run()
        assert not first.exception
        fetch.assert_not_called()
        first.button[0].click().run()
        assert not first.exception
        first_names = fetch.call_args.kwargs["force_index_names"]
        assert {"沪深300", "恒生科技", "铁矿石主连"}.issubset(first_names)
        assert fetch.call_count == 1
        reopened = AppTest.from_string(source, default_timeout=30).run()
        assert not reopened.exception
        assert fetch.call_count == 1
        reopened.button[0].click().run()
        assert not reopened.exception
        assert fetch.call_count == 2
        assert fetch.call_args.kwargs["force_index_names"] == {"日经225", "韩国KOSPI"}
        daily_update.assert_not_called()


def test_page_after_all_markets_close_reads_current_caches_without_fetching():
    from streamlit.testing.v1 import AppTest
    source = fixed_time_page_source(cst("2026-09-30"), 16, 20)
    with (
        patch("core.db.init_db"),
        patch.object(realtime, "fetch_realtime_index_quotes") as fetch,
        patch.object(realtime, "find_pending_post_close_index_names", return_value=set()),
        patch("services.update_tasks.find_pending_futures_current_contract_index_names", return_value=set()),
        patch("services.update_tasks.run_index_ma20_update") as daily_update,
    ):
        first = AppTest.from_string(source, default_timeout=30).run()
        assert not first.exception
        first.button[0].click().run()
        assert not first.exception
        reopened = AppTest.from_string(source, default_timeout=30).run()
        assert not reopened.exception
        reopened.button[0].click().run()
        assert not reopened.exception
        fetch.assert_not_called()
        daily_update.assert_not_called()


def test_closed_futures_contract_gap_is_filled_without_requesting_a_live_quote():
    from streamlit.testing.v1 import AppTest
    from services.update_tasks import UpdateResult
    source = fixed_time_page_source(cst("2026-09-30"), 16, 20)
    with (
        patch("core.db.init_db"),
        patch.object(realtime, "fetch_realtime_index_quotes") as fetch,
        patch.object(realtime, "find_pending_post_close_index_names", return_value=set()),
        patch("services.update_tasks.find_pending_futures_current_contract_index_names", return_value={"铁矿石主连"}),
        patch("services.update_tasks.run_index_ma20_update", return_value=UpdateResult("success", "补齐当前合约正式收盘")) as update,
    ):
        app = AppTest.from_string(source, default_timeout=30).run()
        assert not app.exception
        app.button[0].click().run()
        assert not app.exception
        fetch.assert_not_called()
        update.assert_called_once()
        assert update.call_args.kwargs["index_names"] == {"铁矿石主连"}
