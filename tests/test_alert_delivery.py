from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from services import alert_delivery as delivery
from services import price_alerts
from scripts import monitor_position_timing_trades as etf
from scripts import monitor_iron_ore_price as iron


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("ENABLE_FANGTANG", "ENABLE_WECHAT", "REMINDER_DRY_RUN", "REMINDER_NODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REMINDER_STATE_DIR", str(tmp_path))


def enable(monkeypatch):
    monkeypatch.setenv("ENABLE_FANGTANG", "true")
    monkeypatch.setenv("ENABLE_WECHAT", "true")


def test_missing_flags_fail_closed_without_network_or_hermes():
    with patch("requests.post") as post, patch("services.price_alerts.shutil.which") as lookup, \
         patch("services.price_alerts.subprocess.run") as run:
        assert delivery.enabled_channels() == ()
        assert price_alerts.send_serverchan_message("fake", "标题")["skipped"]
        assert price_alerts.send_hermes_weixin_message("标题")["skipped"]
        post.assert_not_called()
        lookup.assert_not_called()
        run.assert_not_called()


@pytest.mark.parametrize("node,fangtang,wechat,expected", [
    ("lightsail", "true", "true", ("fangtang",)),
    ("windows", "false", "true", ("wechat",)),
    ("windows", "true", "false", ("fangtang",)),
])
def test_channel_matrix(monkeypatch, node, fangtang, wechat, expected):
    monkeypatch.setenv("REMINDER_NODE", node)
    monkeypatch.setenv("ENABLE_FANGTANG", fangtang)
    monkeypatch.setenv("ENABLE_WECHAT", wechat)
    assert delivery.enabled_channels() == expected
    with patch.object(etf, "send_serverchan_message") as ft, patch.object(etf, "send_hermes_weixin_message") as wx:
        etf.send_notification_channels("fake", "标题", "正文", keys=["event"])
        assert ft.call_count == int("fangtang" in expected)
        assert wx.call_count == int("wechat" in expected)


def test_dry_run_blocks_all_senders_and_does_not_create_state(monkeypatch, tmp_path):
    enable(monkeypatch)
    monkeypatch.setenv("REMINDER_DRY_RUN", "true")
    callback = Mock()
    assert delivery.DeliveryLedger().deliver(["event"], "fangtang", callback) == "disabled"
    with patch("requests.post") as post, patch("services.price_alerts.subprocess.run") as run:
        price_alerts.send_serverchan_message("fake", "标题")
        price_alerts.send_hermes_weixin_message("标题")
        post.assert_not_called()
        run.assert_not_called()
    callback.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_same_event_once_each_channel_and_new_symbols(monkeypatch):
    enable(monkeypatch)
    send = Mock()
    for channel in ("fangtang", "wechat"):
        for _ in range(2):
            delivery.DeliveryLedger().deliver(["2026-09-10|159501|SELL"], channel, send)
    assert send.call_count == 2
    delivery.DeliveryLedger().deliver(["2026-09-10|159655|SELL"], "fangtang", send)
    delivery.DeliveryLedger().deliver(["2026-09-11|159501|SELL"], "fangtang", send)
    assert send.call_count == 4


def test_real_process_restart_keeps_receipts(monkeypatch):
    enable(monkeypatch)
    command = [sys.executable, "-c", "from services.alert_delivery import DeliveryLedger; print(DeliveryLedger().deliver(['event'], 'fangtang', lambda keys: None))"]
    assert subprocess.check_output(command, text=True).strip() == "sent"
    assert subprocess.check_output(command, text=True).strip() == "duplicate"


def test_concurrent_process_claims_are_atomic(monkeypatch):
    enable(monkeypatch)
    command = [sys.executable, "-c", "from services.alert_delivery import DeliveryLedger; print(DeliveryLedger().deliver(['race'], 'fangtang', lambda keys: None))"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: subprocess.run(command, capture_output=True, text=True), range(4)))
    assert sum(r.stdout.strip() == "sent" for r in outputs) == 1
    assert all(r.stdout.strip() in {"sent", "duplicate"} or "DeliveryUncertain" in r.stderr for r in outputs)


def test_explicit_failure_retries(monkeypatch):
    enable(monkeypatch)
    ledger = delivery.DeliveryLedger()
    with pytest.raises(RuntimeError):
        ledger.deliver(["event"], "fangtang", Mock(side_effect=delivery.DeliveryRejected("API rejected")))
    assert ledger.statuses(["event"], "fangtang")["event"] == "failed"
    assert ledger.deliver(["event"], "fangtang", Mock()) == "sent"


@pytest.mark.parametrize("error", [requests.ReadTimeout(), requests.ConnectionError(), TimeoutError()])
def test_ambiguous_delivery_never_blindly_retries(monkeypatch, error):
    enable(monkeypatch)
    with pytest.raises(type(error)):
        delivery.DeliveryLedger().deliver(["event"], "fangtang", Mock(side_effect=error))
    assert delivery.DeliveryLedger().statuses(["event"], "fangtang")["event"] == "uncertain"
    with pytest.raises(delivery.DeliveryUncertain):
        delivery.DeliveryLedger().deliver(["event"], "fangtang", Mock())


def test_process_death_after_claim_prevents_resend(monkeypatch):
    enable(monkeypatch)
    command = [sys.executable, "-c", "import os; from services.alert_delivery import DeliveryLedger; DeliveryLedger().deliver(['crash'], 'fangtang', lambda keys: os._exit(9))"]
    assert subprocess.run(command).returncode == 9
    assert delivery.DeliveryLedger().statuses(["crash"], "fangtang")["crash"] == "sending"
    with pytest.raises(delivery.DeliveryUncertain):
        delivery.DeliveryLedger().deliver(["crash"], "fangtang", Mock())


def test_import_without_fcntl_and_sqlite_operations(monkeypatch):
    enable(monkeypatch)
    source = """import builtins
real_import = builtins.__import__
def no_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('not available on Windows')
    return real_import(name, *args, **kwargs)
builtins.__import__ = no_fcntl
from services.alert_delivery import DeliveryLedger
assert DeliveryLedger().deliver(['windows'], 'fangtang', lambda keys: None) == 'sent'
"""
    subprocess.run([sys.executable, "-c", source], check=True)


def preview():
    return etf.position.PositionTimingTradePreviewResult(
        formal_date="2026-09-09", preview_date="2026-09-10", quote_time="2026-09-10 14:50:00",
        actions=pd.DataFrame([{"操作": "卖出", "代码": "159501", "数量": 100,
                               "基金名称": "测试ETF", "参考价": 2.0, "预计金额": 200, "原因": "择时信号"}]))


def test_event_identity_ignores_quote_price_and_slot_but_separates_quantity_and_phase():
    p = preview()
    key = etf.notification_events(p, slot="14:50", trade_date="2026-09-10")
    p.actions.loc[0, "参考价"] = 3.0
    assert etf.notification_events(p, slot="14:54", trade_date="2026-09-10") == key
    p.actions.loc[0, "数量"] = 200
    assert etf.notification_events(p, slot="14:54", trade_date="2026-09-10") != key
    assert "|preview|2026-09-09|" in key[0]


def test_partial_batch_only_renders_unsent_symbol(monkeypatch):
    enable(monkeypatch)
    p = preview()
    keys = etf.notification_events(p, slot="14:50", trade_date=p.preview_date)
    with patch.object(etf, "send_serverchan_message") as ft, patch.object(etf, "send_hermes_weixin_message"):
        etf.send_notification_channels("fake", "标题", "正文", keys=keys,
            render=lambda pending: etf.render_pending(p, keys, pending, "14:50"))
        p.actions.loc[1] = {**p.actions.iloc[0].to_dict(), "代码": "159655"}
        keys = etf.notification_events(p, slot="14:54", trade_date=p.preview_date)
        etf.send_notification_channels("fake", "标题", "正文", keys=keys,
            render=lambda pending: etf.render_pending(p, keys, pending, "14:54"))
        assert ft.call_count == 2
        assert "159655" in ft.call_args.args[2]
        assert "159501" not in ft.call_args.args[2]


def test_no_action_1454_deduplicated_per_channel():
    p = replace(preview(), actions=pd.DataFrame())
    assert etf.notification_events(p, slot="14:50", trade_date=p.preview_date) == etf.notification_events(p, slot="14:54", trade_date=p.preview_date)


@pytest.mark.parametrize("script", [etf, iron])
def test_test_notification_dry_run_never_sends(monkeypatch, script):
    enable(monkeypatch)
    args = SimpleNamespace(dry_run=True, test_notification=True, force=True)
    with patch.object(script, "parse_args", return_value=args), patch.object(script, "configure_logging"), \
         patch("requests.post") as post, patch("services.price_alerts.subprocess.run") as run:
        assert script.main() == 0
        post.assert_not_called()
        run.assert_not_called()


def test_iron_disabled_does_not_advance_threshold_state(monkeypatch):
    monkeypatch.setenv("ENABLE_FANGTANG", "true")
    args = SimpleNamespace(dry_run=False, test_notification=False, force=True)
    with patch.object(iron, "parse_args", return_value=args), patch.object(iron, "configure_logging"), \
         patch.object(iron, "process_price_alert") as process, patch.object(iron, "_fetch_futures_quote") as fetch:
        assert iron.main() == 0
        process.assert_not_called()
        fetch.assert_not_called()


def test_general_fangtang_channel_cannot_bypass_disable():
    from services.notify.channels import ServerChanChannel
    from services.notify.models import NotificationMessage
    with patch("requests.post") as post:
        assert not ServerChanChannel("fake").send(NotificationMessage(title="test")).success
        post.assert_not_called()


def test_windows_lock_branch(monkeypatch, tmp_path):
    path = tmp_path / "windows.lock"
    msvcrt = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock())
    with patch.dict(sys.modules, {"msvcrt": msvcrt}), patch.object(delivery.os, "name", "nt"):
        with delivery.process_lock(path) as acquired:
            assert acquired
    assert [call.args[1:] for call in msvcrt.locking.call_args_list] == [(2, 1), (0, 1)]


def test_failed_channel_retries_without_resending_success(monkeypatch):
    enable(monkeypatch)
    with patch.object(etf, "send_serverchan_message") as ft, \
         patch.object(etf, "send_hermes_weixin_message", side_effect=delivery.DeliveryRejected("rejected")):
        channels, errors = etf.send_notification_channels("fake", "标题", "正文", keys=["partial"])
        assert channels == ("Server酱",) and errors
    with patch.object(etf, "send_serverchan_message") as ft, patch.object(etf, "send_hermes_weixin_message") as wx:
        channels, errors = etf.send_notification_channels("fake", "标题", "正文", keys=["partial"])
        ft.assert_not_called()
        wx.assert_called_once()
        assert not errors


def test_connect_timeout_can_retry(monkeypatch):
    enable(monkeypatch)
    with pytest.raises(requests.ConnectTimeout):
        delivery.DeliveryLedger().deliver(["connect"], "fangtang", Mock(side_effect=requests.ConnectTimeout()))
    assert delivery.DeliveryLedger().deliver(["connect"], "fangtang", Mock()) == "sent"


def test_lightsail_entry_hard_disables_hermes(monkeypatch):
    enable(monkeypatch)
    from scripts import run_lightsail_etf_reminder as runner
    with patch.object(etf, "main", side_effect=lambda: int(delivery.channel_enabled("wechat"))) as main:
        assert runner.main() == 0
        main.assert_called_once()
    assert os.environ["ENABLE_WECHAT"] == "false"


def test_real_service_results_match_dry_run_body(monkeypatch, capsys):
    enable(monkeypatch)
    now = datetime(2026, 9, 10, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    codes = list(etf.position.ETF_PORTFOLIO_WEIGHTS_PCT) + [etf.position.POSITION_TIMING_PARKING_SYMBOL]
    dates = pd.bdate_range("2026-05-01", "2026-09-09")
    items = [etf.position.PositionItem("ETF", code, code, "缓存",
                dataframe=pd.DataFrame({"date": dates, "price": [1.0]*len(dates)}),
                formal_history_valid=True) for code in codes]
    quotes = {code: {"price": 2.0, "quote_time": now} for code in codes}
    direct = etf.position.build_position_timing_trade_preview(items, quotes, market_now=now)
    assert not direct.errors and not direct.actions.empty
    expected_title, expected_body, _ = etf.format_notification(direct, slot="14:50")
    monkeypatch.setenv("TICKFLOW_API_KEY", "fake")
    with patch.object(etf, "parse_args", return_value=SimpleNamespace(force=False, dry_run=True, test_notification=False)), \
         patch.object(etf, "configure_logging"), patch.object(etf, "datetime") as clock, \
         patch.object(etf, "single_instance_lock", return_value=nullcontext(True)), \
         patch.object(etf, "load_notification_state", return_value=etf.PositionTimingNotificationState()), \
         patch.object(etf, "_load_formal_items", return_value=items), \
         patch.object(etf.position, "fetch_tickflow_etf_quotes", return_value=quotes), \
         patch.object(etf, "send_notification_channels") as send:
        clock.now.return_value = now
        assert etf.main() == 0
        send.assert_not_called()
    output = capsys.readouterr().out
    assert expected_title in output and expected_body in output
    for key in etf.notification_events(direct, slot="14:50", trade_date="2026-09-10"):
        assert key in output


def test_api_rejection_is_not_success(monkeypatch):
    enable(monkeypatch)
    response = Mock()
    response.json.return_value = {"code": 400, "message": "rejected"}
    with patch("requests.post", return_value=response):
        with pytest.raises(delivery.DeliveryRejected):
            price_alerts.send_serverchan_message("fake", "标题")


def test_systemd_scope_is_etf_only():
    units = Path("deploy/systemd")
    assert {p.name for p in units.iterdir()} == {"position-etf-reminder.service", "position-etf-reminder.timer"}
    timer = (units / "position-etf-reminder.timer").read_text()
    for slot in ("09:45", "11:45", "14:45", "14:50", "14:54"):
        assert f"OnCalendar=*-*-* {slot}:00 Asia/Shanghai" in timer
