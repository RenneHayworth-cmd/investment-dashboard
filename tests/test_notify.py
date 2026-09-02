import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

from services.notify.channels import (
    NtfyBridgeChannel,
    PushPlusChannel,
    ServerChanChannel,
    WeComChannel,
    WindowsToastChannel,
)
from services.notify.engine import NotifyEngine
from services.notify.models import (
    ChannelType,
    DeliveryResult,
    NotificationAction,
    NotificationMessage,
    Priority,
)
from services.notify.server import create_app
from services.notify import send_alert, send_trade_signal


class NotificationModelTests(unittest.TestCase):
    def test_priority_parsing(self):
        self.assertEqual(Priority.from_str("urgent"), Priority.URGENT)
        self.assertEqual(Priority.from_str("5"), Priority.URGENT)
        self.assertEqual(Priority.from_str(Priority.URGENT), Priority.URGENT)
        self.assertEqual(Priority.from_str("high"), Priority.HIGH)
        self.assertEqual(Priority.from_str("4"), Priority.HIGH)
        self.assertEqual(Priority.from_str("low"), Priority.LOW)
        self.assertEqual(Priority.from_str("2"), Priority.LOW)
        self.assertEqual(Priority.from_str(None), Priority.DEFAULT)
        self.assertEqual(Priority.from_str("unknown_val"), Priority.DEFAULT)

    def test_priority_levels(self):
        self.assertGreater(Priority.URGENT.level, Priority.HIGH.level)
        self.assertGreater(Priority.HIGH.level, Priority.DEFAULT.level)
        self.assertGreater(Priority.DEFAULT.level, Priority.LOW.level)
        self.assertGreater(Priority.LOW.level, Priority.MIN.level)

    def test_message_dict_serialization(self):
        msg = NotificationMessage(
            topic="test",
            title="测试标题",
            body="测试正文",
            priority=Priority.HIGH,
            tags=["warning", "test"],
            click_url="http://localhost:8501",
            actions=[NotificationAction(action_type="view", label="查看", url="http://localhost:8501")],
            channels=[ChannelType.SERVERCHAN, ChannelType.NTFY],
        )
        d = msg.to_dict()
        self.assertEqual(d["priority"], "high")
        self.assertEqual(d["channels"], ["serverchan", "ntfy"])
        self.assertEqual(d["actions"][0]["action"], "view")


class ChannelTests(unittest.TestCase):
    @patch("requests.post")
    def test_serverchan_channel(self, post_mock):
        resp = Mock()
        resp.json.return_value = {"code": 0, "message": "success"}
        post_mock.return_value = resp

        channel = ServerChanChannel(sendkey="SCT123456")
        self.assertTrue(channel.is_configured())

        msg = NotificationMessage(title="告警", body="内容", tags=["⚠️"])
        res = channel.send(msg)
        self.assertTrue(res.success)
        post_mock.assert_called_once()
        args, kwargs = post_mock.call_args
        self.assertIn("https://sctapi.ftqq.com/SCT123456.send", args[0])
        self.assertEqual(kwargs["json"]["title"], "[⚠️] 告警")

    @patch("requests.post")
    def test_ntfy_bridge_channel(self, post_mock):
        resp = Mock()
        resp.headers = {"content-type": "application/json"}
        resp.json.return_value = {"id": "123"}
        post_mock.return_value = resp

        channel = NtfyBridgeChannel(server_url="https://ntfy.sh", default_topic="my_alerts")
        self.assertTrue(channel.is_configured())

        msg = NotificationMessage(
            topic="trades",
            title="买入信号",
            body="中证500突破",
            priority=Priority.URGENT,
            tags=["trade", "510500"],
            click_url="http://localhost:8501/5_持仓分析",
            actions=[NotificationAction("view", "打开看板", "http://localhost:8501")],
        )
        res = channel.send(msg)
        self.assertTrue(res.success)
        args, kwargs = post_mock.call_args
        self.assertEqual(args[0], "https://ntfy.sh/trades")
        self.assertEqual(kwargs["headers"]["Priority"], "5")
        self.assertEqual(kwargs["headers"]["Tags"], "trade,510500")
        self.assertEqual(kwargs["headers"]["Click"], "http://localhost:8501/5_持仓分析")
        self.assertIn("view, 打开看板, http://localhost:8501", kwargs["headers"]["Actions"])

    @patch("requests.post")
    def test_pushplus_channel(self, post_mock):
        resp = Mock()
        resp.json.return_value = {"code": 200, "msg": "success"}
        post_mock.return_value = resp

        channel = PushPlusChannel(token="token_abc")
        self.assertTrue(channel.is_configured())

        msg = NotificationMessage(title="PushPlus测试", body="正文\n第二行")
        res = channel.send(msg)
        self.assertTrue(res.success)
        self.assertIn("正文<br/>第二行", post_mock.call_args[1]["json"]["content"])

    @patch("requests.post")
    def test_wecom_channel(self, post_mock):
        resp = Mock()
        resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
        post_mock.return_value = resp

        channel = WeComChannel(webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=123")
        self.assertTrue(channel.is_configured())

        msg = NotificationMessage(title="企业微信通知", body="告警内容")
        res = channel.send(msg)
        self.assertTrue(res.success)

    @patch("subprocess.run")
    def test_windows_toast_channel(self, subproc_mock):
        proc = Mock()
        proc.returncode = 0
        subproc_mock.return_value = proc

        channel = WindowsToastChannel()
        self.assertTrue(channel.is_configured())

        msg = NotificationMessage(title="桌面弹窗", body="行情剧烈变动")
        res = channel.send(msg)
        self.assertTrue(res.success)
        subproc_mock.assert_called_once()


class NotifyEngineTests(unittest.TestCase):
    def test_engine_dispatch_and_storage(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_notifications.db"
            engine = NotifyEngine(db_path=db_path)

            mock_serverchan = Mock()
            mock_serverchan.is_configured.return_value = True
            mock_serverchan.send.return_value = DeliveryResult("serverchan", True, "ok")

            mock_ntfy = Mock()
            mock_ntfy.is_configured.return_value = True
            mock_ntfy.send.return_value = DeliveryResult("ntfy", True, "ok")

            engine.register_channel(ChannelType.SERVERCHAN, mock_serverchan)
            engine.register_channel(ChannelType.NTFY, mock_ntfy)

            results = engine.send(
                title="铁矿石破位",
                body="低于730",
                topic="alerts",
                priority=Priority.HIGH,
                tags=["warning"],
            )

            self.assertTrue(all(r.success for r in results))
            mock_serverchan.send.assert_called_once()
            mock_ntfy.send.assert_called_once()

            history = engine.get_history(topic="alerts")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["title"], "铁矿石破位")
            self.assertEqual(history[0]["priority"], "high")
            self.assertEqual(history[0]["tags"], ["warning"])

    def test_trade_signal_helper(self):
        with patch("services.notify.get_default_engine") as get_engine_mock:
            engine_mock = Mock()
            engine_mock.send.return_value = [DeliveryResult("serverchan", True, "ok")]
            get_engine_mock.return_value = engine_mock

            send_trade_signal(
                symbol="159967",
                name="创成长ETF",
                action="转入512890",
                price=0.772,
                details="跌破MA25下轨",
            )

            engine_mock.send.assert_called_once()
            call_kwargs = engine_mock.send.call_args[1]
            self.assertIn("🛡️", call_kwargs["title"])
            self.assertIn("创成长ETF(159967)", call_kwargs["title"])
            self.assertIn("0.772", call_kwargs["body"])
            self.assertEqual(call_kwargs["topic"], "trades")


class NotifyHttpServerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        db_path = Path(self.td.name) / "test_api_notify.db"
        self.engine = NotifyEngine(db_path=db_path)
        
        mock_serverchan = Mock()
        mock_serverchan.is_configured.return_value = True
        mock_serverchan.send.return_value = DeliveryResult("serverchan", True, "ok")
        self.engine.register_channel(ChannelType.SERVERCHAN, mock_serverchan)

        self.app = create_app(engine=self.engine)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.td.cleanup()

    def test_health_check(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_ntfy_style_post_text(self):
        headers = {
            "Title": urllib.parse.quote("期货价格预警"),
            "Priority": "urgent",
            "Tags": "warning,iron_ore",
            "Click": "http://localhost:8501/futures",
            "Actions": "view, Open Dashboard, http://localhost:8501",
        }
        resp = self.client.post("/alerts", content="铁矿石I2701跌破730元/吨".encode("utf-8"), headers=headers)
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["topic"], "alerts")
        self.assertEqual(payload["priority"], "urgent")
        self.assertTrue(payload["delivered"])

        hist_resp = self.client.get("/alerts/json")
        self.assertEqual(hist_resp.status_code, 200)
        history = hist_resp.json()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["title"], "期货价格预警")
        self.assertEqual(history[0]["tags"], ["warning", "iron_ore"])

    def test_json_payload_post(self):
        payload = {
            "title": "ETF择时买入",
            "message": "标普500ETF突破MA25上轨",
            "priority": "high",
            "tags": ["trade", "159655"],
        }
        resp = self.client.post("/trades", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["delivered"])

        hist_resp = self.client.get("/trades/json")
        history = hist_resp.json()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["title"], "ETF择时买入")
