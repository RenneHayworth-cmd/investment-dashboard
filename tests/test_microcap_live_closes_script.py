from datetime import datetime
from unittest.mock import patch

import pandas as pd

import scripts.update_microcap_live_closes as script

TRADES = pd.DataFrame({"symbol": ["688021.SH", "300635"], "trade_date": ["2026-09-23", "2026-09-24"]})
EMPTY = pd.DataFrame()


def _run(now: datetime, histories, argv=()):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    with patch.object(script, "datetime", FixedClock), \
         patch.object(script, "list_microcap_trades", return_value=TRADES), \
         patch.object(script, "list_microcap_cash_flows", return_value=EMPTY), \
         patch.object(script, "list_microcap_position_adjustments", return_value=EMPTY), \
         patch.object(script, "init_db"), \
         patch.object(script, "start_job", return_value=1), \
         patch.object(script, "finish_job") as finish, \
         patch.object(script, "load_microcap_histories", side_effect=histories) as load, \
         patch("sys.argv", ["update_microcap_live_closes.py", *argv]):
        return script.main(), load, finish


AFTER_CLOSE = datetime(2026, 9, 29, 15, 10, tzinfo=script.TZ)


def test_skips_before_settlement_and_on_holidays_without_touching_the_cache():
    for now in (datetime(2026, 9, 29, 14, 0, tzinfo=script.TZ), datetime(2026, 10, 1, 15, 10, tzinfo=script.TZ)):
        code, load, _ = _run(now, AssertionError("must not load"))
        assert code == 0
        load.assert_not_called()


def test_complete_cache_makes_no_network_request():
    code, load, finish = _run(AFTER_CLOSE, [({}, {})])
    assert code == 0
    assert load.call_count == 1 and load.call_args.kwargs["allow_fetch"] is False
    finish.assert_not_called()


def test_missing_closes_fetch_once_for_the_ledger_codes():
    missing = {"688021": "正式收盘数据截止2026-09-28，缺少目标日2026-09-29"}
    code, load, finish = _run(AFTER_CLOSE, [({}, missing), ({}, {})])
    assert code == 0
    assert load.call_args_list[1].args[0] == ["300635", "688021"]
    assert load.call_args_list[1].kwargs["allow_fetch"] is True
    assert load.call_args_list[1].kwargs["start_date"] == "2026-09-23"
    assert finish.call_args.args[1] == "success"


def test_remaining_gaps_fail_so_the_retry_triggers_run_and_dry_run_stays_offline():
    missing = {"688021": "缺少目标日2026-09-29"}
    code, _, finish = _run(AFTER_CLOSE, [({}, missing), ({}, missing)])
    assert code == 1
    assert finish.call_args.args[1] == "failed"

    code, load, _ = _run(AFTER_CLOSE, [({}, missing)], argv=("--dry-run",))
    assert code == 0 and load.call_count == 1
