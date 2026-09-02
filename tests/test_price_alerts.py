import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from services.price_alerts import (
    load_price_alert_state,
    process_price_alert,
    send_hermes_weixin_message,
    send_serverchan_message,
    serverchan_endpoint,
)


class PriceAlertTests(unittest.TestCase):
    @patch("services.price_alerts.subprocess.run")
    def test_hermes_weixin_message_uses_cli_home_channel(self, run_mock):
        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"success": True, "platform": "weixin"}),
            stderr="",
        )

        with patch.dict("os.environ", {"HERMES_SEND_BIN": "/test/hermes"}):
            result = send_hermes_weixin_message("测试\n标题", "正文")

        self.assertTrue(result["success"])
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:5], ["/test/hermes", "send", "--to", "weixin", "--json"])
        self.assertEqual(command[5], "测试 标题\n\n正文")
        self.assertFalse(run_mock.call_args.kwargs["check"])

    def test_serverchan_endpoint_supports_turbo_and_sc3_keys(self):
        self.assertEqual(
            serverchan_endpoint("SCT123"),
            "https://sctapi.ftqq.com/SCT123.send",
        )
        self.assertEqual(
            serverchan_endpoint("sctp12345tabc"),
            "https://12345.push.ft07.com/send/sctp12345tabc.send",
        )

    @patch("requests.post")
    def test_serverchan_message_checks_success_code(self, post_mock):
        response = Mock()
        response.json.return_value = {"code": 0, "message": "success"}
        post_mock.return_value = response

        result = send_serverchan_message("SCT_TEST", "测试\n标题", "正文")

        response.raise_for_status.assert_called_once_with()
        self.assertEqual(result["code"], 0)
        request = post_mock.call_args
        self.assertEqual(request.kwargs["json"]["title"], "测试 标题")
        self.assertEqual(request.kwargs["timeout"], 10)

    def test_alert_fires_once_until_price_recovers(self):
        now = datetime(2026, 7, 24, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        notifications = []
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            notify = lambda title, description: notifications.append((title, description))

            first = process_price_alert(
                price=729.8,
                threshold=730,
                contract="I2609",
                checked_at=now,
                state_path=state_path,
                notify=notify,
            )
            repeated = process_price_alert(
                price=728.0,
                threshold=730,
                contract="I2609",
                checked_at=now,
                state_path=state_path,
                notify=notify,
            )
            rearmed = process_price_alert(
                price=731.0,
                threshold=730,
                contract="I2609",
                checked_at=now,
                state_path=state_path,
                notify=notify,
            )
            second = process_price_alert(
                price=729.0,
                threshold=730,
                contract="I2609",
                checked_at=now,
                state_path=state_path,
                notify=notify,
            )

            self.assertEqual(first.status, "alerted")
            self.assertEqual(repeated.status, "below_suppressed")
            self.assertEqual(rearmed.status, "rearmed")
            self.assertEqual(second.status, "alerted")
            self.assertEqual(len(notifications), 2)
            self.assertTrue(load_price_alert_state(state_path).below_threshold)

    def test_failed_notification_does_not_suppress_retry(self):
        now = datetime(2026, 7, 24, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"

            with self.assertRaisesRegex(RuntimeError, "send failed"):
                process_price_alert(
                    price=729,
                    threshold=730,
                    contract="I2609",
                    checked_at=now,
                    state_path=state_path,
                    notify=Mock(side_effect=RuntimeError("send failed")),
                )

            self.assertFalse(load_price_alert_state(state_path).below_threshold)


if __name__ == "__main__":
    unittest.main()
