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
    for name in ("ENABLE_FANGTANG", "ENABLE_WECHAT", "ENABLE_WXPUSHER", "REMINDER_DRY_RUN", "REMINDER_NODE",
                 "WXPUSHER_APP_TOKEN", "WXPUSHER_UIDS", "WXPUSHER_TOPIC_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REMINDER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(price_alerts, "WXPUSHER_CONFIG_DIR", tmp_path / "no-config")


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


def test_event_identity_separates_slots_for_scheduled_delivery():
    p = preview()
    key_1345 = etf.notification_events(p, slot="13:45", trade_date="2026-09-10")
    key_1450 = etf.notification_events(p, slot="14:50", trade_date="2026-09-10")
    assert key_1345 != key_1450
    assert key_1345 == [delivery.event_key("2026-09-10", "ETF500K", "13:45")]
    assert key_1450 == [delivery.event_key("2026-09-10", "ETF500K", "14:50")]


def test_each_slot_delivers_full_actions(monkeypatch):
    enable(monkeypatch)
    p = preview()
    keys_1345 = etf.notification_events(p, slot="13:45", trade_date=p.preview_date)
    with patch.object(etf, "send_serverchan_message") as ft, patch.object(etf, "send_hermes_weixin_message"):
        etf.send_notification_channels("fake", "标题", "正文", keys=keys_1345,
            render=lambda pending: etf.render_pending(p, keys_1345, pending, "13:45"))
        p.actions.loc[1] = {**p.actions.iloc[0].to_dict(), "代码": "159655"}
        keys_1450 = etf.notification_events(p, slot="14:50", trade_date=p.preview_date)
        etf.send_notification_channels("fake", "标题", "正文", keys=keys_1450,
            render=lambda pending: etf.render_pending(p, keys_1450, pending, "14:50"))
        assert ft.call_count == 2
        assert "159655" in ft.call_args.args[2]
        assert "159501" in ft.call_args.args[2]


def test_no_action_1454_suppressed_when_1450_already_notified():
    state = etf.PositionTimingNotificationState(
        trade_date="2026-09-10",
        notified_slots=["14:50"],
        no_action_at_1450=True,
        last_outcome="no_action",
    )
    assert etf.should_suppress_notification(state, outcome="no_action", slot="14:54")
    assert not etf.should_suppress_notification(state, outcome="action", slot="14:54")


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
    calendars = [line for line in timer.splitlines() if line.startswith("OnCalendar=")]
    assert calendars == ["OnCalendar=*-*-* 14:50:00 Asia/Shanghai"]
    assert etf.ALERT_SLOTS == ((14, 50),)


# --- WxPusher ---------------------------------------------------------------

WX_CONFIG = price_alerts.WxPusherConfig("AT_secret_token", ("UID_a",), ())


def wx_response(code=1000, items=({"uid": "UID_a", "code": 1000, "status": "创建发送任务成功"},)):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"code": code, "msg": "处理成功", "data": list(items), "success": code == 1000}
    return response


def test_wxpusher_flag_is_off_by_default_and_allowed_on_lightsail(monkeypatch):
    assert "wxpusher" not in delivery.enabled_channels()
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    monkeypatch.setenv("REMINDER_NODE", "lightsail")
    monkeypatch.setenv("ENABLE_WECHAT", "true")
    assert delivery.enabled_channels() == ("wxpusher",)
    monkeypatch.setenv("REMINDER_DRY_RUN", "true")
    assert delivery.enabled_channels() == ()
    monkeypatch.setenv("REMINDER_DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_WXPUSHER", "maybe")
    with pytest.raises(ValueError):
        delivery.enabled_channels()


def test_wxpusher_disabled_never_touches_the_network():
    with patch("requests.post") as post:
        assert price_alerts.send_wxpusher_message(WX_CONFIG, "标题")["skipped"]
    post.assert_not_called()


def test_wxpusher_posts_markdown_with_summary_and_recipients(monkeypatch):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    config = price_alerts.WxPusherConfig("AT_secret_token", ("UID_a", "UID_b"), (7,))
    with patch("requests.post", return_value=wx_response(items=[
        {"uid": "UID_a", "code": 1000}, {"uid": "UID_b", "code": 1000}, {"topicId": 7, "code": 1000},
    ])) as post:
        price_alerts.send_wxpusher_message(config, "ETF均线策略交易提醒 14:50", "- **卖出** 159967")
    (url,), kwargs = post.call_args
    assert url == "https://wxpusher.zjiecode.com/api/send/message"
    body = kwargs["json"]
    assert body == {
        "appToken": "AT_secret_token",
        "content": "## ETF均线策略交易提醒 14:50\n\n- **卖出** 159967",
        "summary": "ETF均线策略交易提醒 14:50",
        "contentType": 3,
        "uids": ["UID_a", "UID_b"],
        "topicIds": [7],
    }
    assert "AT_secret_token" not in url


def test_wxpusher_summary_is_capped_and_oversize_content_is_rejected_before_sending(monkeypatch):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    with patch("requests.post", return_value=wx_response()) as post:
        price_alerts.send_wxpusher_message(WX_CONFIG, "题" * 300, "正文")
        assert len(post.call_args.kwargs["json"]["summary"]) == 100
        post.reset_mock()
        with pytest.raises(delivery.DeliveryRejected):
            price_alerts.send_wxpusher_message(WX_CONFIG, "标题", "字" * 25_000)  # < 40k chars but > 65,535 bytes
        post.assert_not_called()


@pytest.mark.parametrize("config", [
    price_alerts.WxPusherConfig("", ("UID_a",), ()),
    price_alerts.WxPusherConfig("AT_secret_token", (), ()),
])
def test_wxpusher_missing_credentials_or_recipients_are_rejected(monkeypatch, config):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    with patch("requests.post") as post, pytest.raises(delivery.DeliveryRejected):
        price_alerts.send_wxpusher_message(config, "标题", "正文")
    post.assert_not_called()


@pytest.mark.parametrize("response,error", [
    (wx_response(code=1001, items=()), delivery.DeliveryRejected),          # provider refused
    (wx_response(items=({"uid": "UID_a", "code": 1002},)), delivery.DeliveryRejected),  # every recipient refused
    (wx_response(items=({"uid": "UID_a", "code": 1000}, {"uid": "UID_b", "code": 1002})), delivery.DeliveryUncertain),
    (wx_response(items=()), delivery.DeliveryUncertain),                    # accepted but no task detail
])
def test_wxpusher_response_classification(monkeypatch, response, error):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    with patch("requests.post", return_value=response), pytest.raises(error):
        price_alerts.send_wxpusher_message(WX_CONFIG, "标题", "正文")


def test_wxpusher_non_json_answer_is_uncertain(monkeypatch):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    response = wx_response()
    response.json.return_value = ["unexpected"]
    with patch("requests.post", return_value=response), pytest.raises(delivery.DeliveryUncertain):
        price_alerts.send_wxpusher_message(WX_CONFIG, "标题", "正文")


def test_wxpusher_config_reads_env_then_files(monkeypatch, tmp_path):
    (tmp_path / "wxpusher_app_token").write_text("AT_from_file\n", encoding="utf-8")
    (tmp_path / "wxpusher_uids").write_text("UID_file\n", encoding="utf-8")
    from_files = price_alerts.load_wxpusher_config(tmp_path)
    assert (from_files.app_token, from_files.uids, from_files.topic_ids) == ("AT_from_file", ("UID_file",), ())
    monkeypatch.setenv("WXPUSHER_APP_TOKEN", "AT_env")
    monkeypatch.setenv("WXPUSHER_UIDS", "UID_1, UID_2")
    monkeypatch.setenv("WXPUSHER_TOPIC_IDS", "12,34")
    from_env = price_alerts.load_wxpusher_config(tmp_path)
    assert (from_env.app_token, from_env.uids, from_env.topic_ids) == ("AT_env", ("UID_1", "UID_2"), (12, 34))
    monkeypatch.setenv("WXPUSHER_TOPIC_IDS", "abc")
    with pytest.raises(delivery.DeliveryRejected):
        price_alerts.load_wxpusher_config(tmp_path)


def test_position_alert_sends_through_wxpusher_and_ledger_blocks_a_repeat(monkeypatch):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    monkeypatch.setenv("WXPUSHER_APP_TOKEN", "AT_secret_token")
    monkeypatch.setenv("WXPUSHER_UIDS", "UID_a")
    with patch("requests.post", return_value=wx_response()) as post:
        channels, errors = etf.send_notification_channels("", "标题", "正文", keys=["2026-09-29|ETF500K|14:50"])
        assert (channels, errors) == (("WxPusher",), ())
        again, _ = etf.send_notification_channels("", "标题", "正文", keys=["2026-09-29|ETF500K|14:50"])
    assert again == ("WxPusher",)
    assert post.call_count == 1  # second run finds the event already sent
    assert delivery.DeliveryLedger().statuses(["2026-09-29|ETF500K|14:50"], "wxpusher") == {
        "2026-09-29|ETF500K|14:50": "sent"}
    assert delivery.DeliveryLedger().statuses(["2026-09-29|ETF500K|14:50"], "fangtang") == {
        "2026-09-29|ETF500K|14:50": "new"}


def test_position_alert_wxpusher_failure_is_reported_without_the_token(monkeypatch):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    monkeypatch.setenv("ENABLE_FANGTANG", "true")
    monkeypatch.setenv("WXPUSHER_APP_TOKEN", "AT_secret_token")
    monkeypatch.setenv("WXPUSHER_UIDS", "UID_a")
    with patch.object(etf, "send_serverchan_message") as fangtang, \
         patch("requests.post", side_effect=requests.ConnectionError("AT_secret_token boom")):
        channels, errors = etf.send_notification_channels("SCT_x", "标题", "正文", keys=["event"])
    assert channels == ("Server酱",)               # the other channel is unaffected
    assert errors and "WxPusher推送失败" in errors[0] and "AT_secret_token" not in errors[0]
    fangtang.assert_called_once()
    assert delivery.DeliveryLedger().statuses(["event"], "wxpusher")["event"] == "uncertain"


def test_position_alert_redacts_the_wxpusher_token(monkeypatch):
    monkeypatch.setenv("WXPUSHER_APP_TOKEN", "AT_secret_token")
    assert "AT_secret_token" not in etf.safe_text("请求失败 AT_secret_token")


def test_wxpusher_spt_uses_simple_push_endpoint_without_uids(monkeypatch):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    config = price_alerts.WxPusherConfig("SPT_secret_simple_token")
    assert config.is_spt
    spt_ok = wx_response(items=({"spt": "SPT_secret_simple_token", "uid": None, "code": 1000, "status": "创建发送任务成功"},))
    with patch("requests.post", return_value=spt_ok) as post:
        price_alerts.send_wxpusher_message(config, "ETF均线策略交易提醒测试", "这是一条测试")
    (url,), kwargs = post.call_args
    assert url == "https://wxpusher.zjiecode.com/api/send/message/simple-push"
    assert kwargs["json"] == {
        "content": "## ETF均线策略交易提醒测试\n\n这是一条测试",
        "summary": "ETF均线策略交易提醒测试",
        "contentType": 3,
        "spt": "SPT_secret_simple_token",
    }
    assert "SPT_secret_simple_token" not in url


def test_wxpusher_spt_rejection_is_classified_like_app_token(monkeypatch):
    monkeypatch.setenv("ENABLE_WXPUSHER", "true")
    config = price_alerts.WxPusherConfig("SPT_secret_simple_token")
    with patch("requests.post", return_value=wx_response(code=1001, items=())), pytest.raises(delivery.DeliveryRejected):
        price_alerts.send_wxpusher_message(config, "标题", "正文")


def test_iron_ore_defaults_to_700_with_a_state_file_per_threshold():
    assert iron.DEFAULT_THRESHOLD == 700.0
    assert iron.state_path(700.0).name == "iron_ore_below_700.json"
    assert iron.lock_path(700.0).name == "iron_ore_below_700.lock"
    assert iron.state_path(730.0) != iron.state_path(700.0)
    assert iron.state_path(692.5).name == "iron_ore_below_692.5.json"


def test_lowering_the_threshold_starts_armed_instead_of_inheriting_below_state(tmp_path):
    # The 730 regime left the price "already below" (notified on 2026-09-11, never recovered).
    old_state = tmp_path / iron.state_path(730.0).name
    price_alerts.save_price_alert_state(old_state, price_alerts.PriceAlertState(below_threshold=True, last_price=702.0))
    when = datetime(2026, 9, 29, 10, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    notify = Mock()
    # Same state file with the new threshold would swallow this crossing (proves the hazard) ...
    inherited = price_alerts.process_price_alert(
        price=699.0, threshold=700.0, contract="I2701", checked_at=when, state_path=old_state, notify=notify)
    assert inherited.status == "below_suppressed" and not notify.called
    # ... whereas the per-threshold file starts armed and alerts on the first crossing below 700.
    fresh = price_alerts.process_price_alert(
        price=699.0, threshold=700.0, contract="I2701", checked_at=when,
        state_path=tmp_path / iron.state_path(700.0).name, notify=notify)
    assert fresh.status == "alerted"
    assert "700" in notify.call_args.args[0]
    assert price_alerts.process_price_alert(
        price=695.0, threshold=700.0, contract="I2701", checked_at=when,
        state_path=tmp_path / iron.state_path(700.0).name, notify=notify).status == "below_suppressed"
