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


@patch.dict("os.environ", {"ENABLE_FANGTANG": "true", "ENABLE_WECHAT": "true", "ENABLE_WXPUSHER": "false",
                           "REMINDER_DRY_RUN": "false", "REMINDER_NODE": "windows"})
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


class LadderAlertTests(unittest.TestCase):
    """Alert at 700, then 695, 690, ...; hovering around a level never re-alerts."""

    def _replay(self, prices, *, step):
        notifications = []
        statuses = []
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            for minute, price in enumerate(prices):
                result = process_price_alert(
                    price=price,
                    threshold=700,
                    contract="I2701",
                    checked_at=datetime(2026, 9, 29, 10, minute % 60, tzinfo=ZoneInfo("Asia/Shanghai")),
                    state_path=state_path,
                    notify=lambda title, description: notifications.append((title, description)),
                    step=step,
                )
                statuses.append(result.status)
        return notifications, statuses

    # 2026-09-29 around 11:00: 699.5 / 700.0 flips, which produced 7 alerts in 30 minutes.
    OSCILLATION = [701.0, 699.5, 700.0, 699.5, 700.0, 699.5, 700.5, 699.5, 700.0, 699.5, 703.0, 699.0]

    def titles(self, notifications):
        return [title for title, _ in notifications]

    def test_oscillation_around_700_alerts_once(self):
        notifications, statuses = self._replay(self.OSCILLATION, step=5)
        self.assertEqual(self.titles(notifications), ["铁矿石主连跌破 700 元/吨"])
        self.assertNotIn("rearmed", statuses)
        self.assertIn("下一次提醒：跌破 695 元/吨", notifications[0][1])
        self.assertIn("回升到 705 元/吨及以上", notifications[0][1])

    def test_each_lower_level_alerts_once(self):
        prices = [699.5, 697.0, 695.0, 694.5, 695.5, 694.5, 692.0, 689.5, 690.5, 689.0]
        notifications, _ = self._replay(prices, step=5)
        self.assertEqual(
            self.titles(notifications),
            ["铁矿石主连跌破 700 元/吨", "铁矿石主连跌破 695 元/吨", "铁矿石主连跌破 690 元/吨"],
        )

    def test_gap_through_several_levels_sends_one_alert_for_the_lowest(self):
        notifications, _ = self._replay([701.0, 688.0, 686.0, 684.5], step=5)
        self.assertEqual(self.titles(notifications), ["铁矿石主连跌破 690 元/吨", "铁矿石主连跌破 685 元/吨"])

    def test_recovery_to_705_resets_the_ladder(self):
        prices = [699.0, 694.0, 702.0, 699.0, 705.0, 699.5]
        notifications, statuses = self._replay(prices, step=5)
        self.assertEqual(statuses, ["alerted", "alerted", "rearm_pending", "below_suppressed", "rearmed", "alerted"])
        self.assertEqual(
            self.titles(notifications),
            ["铁矿石主连跌破 700 元/吨", "铁矿石主连跌破 695 元/吨", "铁矿石主连跌破 700 元/吨"],
        )

    def test_step_zero_keeps_the_single_level_behaviour(self):
        notifications, _ = self._replay(self.OSCILLATION, step=0)
        self.assertEqual(len(notifications), 6)  # every drop below 700 after touching 700 again

    def test_state_from_before_the_ladder_resumes_at_the_next_level(self):
        # The live state file written today has below_threshold=true and no alert_level.
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps({"below_threshold": True, "last_price": 699.5}), encoding="utf-8")
            notifications = []
            for price in (699.0, 700.0, 694.5):
                process_price_alert(
                    price=price, threshold=700, contract="I2701",
                    checked_at=datetime(2026, 9, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    state_path=state_path, step=5,
                    notify=lambda title, description: notifications.append(title),
                )
            self.assertEqual(notifications, ["铁矿石主连跌破 695 元/吨"])
            self.assertEqual(load_price_alert_state(state_path).alert_level, 695.0)


if __name__ == "__main__":
    unittest.main()
