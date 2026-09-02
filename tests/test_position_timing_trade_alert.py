from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.monitor_position_timing_trades import (
    PositionTimingNotificationState,
    alert_slot,
    format_notification,
    load_notification_state,
    save_notification_state,
    send_notification_channels,
    should_suppress_notification,
)
from services import position_analysis as position
from services import position_performance


class PositionTimingTradeAlertTests(unittest.TestCase):
    def setUp(self):
        self.market_now = datetime(2026, 8, 28, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.required_codes = list(position.ETF_PORTFOLIO_WEIGHTS_PCT) + [
            position.POSITION_TIMING_PARKING_SYMBOL
        ]
        formal_dates = position_performance._expected_a_share_sessions(
            pd.Timestamp(position.POSITION_TIMING_START_DATE),
            pd.Timestamp("2026-08-27"),
        )
        self.items = [
            position.PositionItem(
                category="ETF",
                code=code,
                name=position.ETF_DISPLAY_NAMES.get(code, code),
                status="缓存",
                dataframe=pd.DataFrame(
                    {
                        "date": formal_dates,
                        "price": [1.0 + index / 100 for index in range(len(formal_dates))],
                    }
                ),
                formal_history_valid=True,
            )
            for code in self.required_codes
        ]
        self.quotes = {
            code: {
                "symbol": code,
                "price": 2.0,
                "quote_time": self.market_now,
            }
            for code in self.required_codes
        }

    @staticmethod
    def _trade(
        date: str,
        source: str,
        symbol: str,
        operation: str,
        quantity: int,
        reason: str,
    ) -> dict[str, object]:
        return {
            "日期": pd.Timestamp(date),
            "标的名称": source,
            "代码": source,
            "配置比例(%)": 10.0,
            "交易标的": symbol,
            "交易标的名称": position.ETF_DISPLAY_NAMES.get(symbol, symbol),
            "操作": operation,
            "成交价": 2.0,
            "份额": quantity,
            "成交金额": quantity * 2.0,
            "手续费": 0.0,
            "现金余额": 0.0,
            "原因": reason,
        }

    def test_preview_nets_strategy_sleeves_into_executable_share_changes(self):
        formal_trades = pd.DataFrame(
            [
                self._trade("2026-08-20", "510500", "510500", "买入", 1000, "历史买入"),
                self._trade("2026-08-20", "510500", "512890", "买入", 500, "历史承接"),
            ]
        )
        preview_trades = pd.concat(
            [
                formal_trades,
                pd.DataFrame(
                    [
                        self._trade("2026-08-28", "510500", "510500", "卖出", 400, "信号转为空仓"),
                        self._trade("2026-08-28", "159967", "159967", "买入", 300, "信号重新买入"),
                        self._trade("2026-08-28", "510500", "512890", "卖出", 500, "退出承接"),
                        self._trade("2026-08-28", "159552", "512890", "买入", 200, "转入承接"),
                    ]
                ),
            ],
            ignore_index=True,
        )

        with patch.object(
            position_performance,
            "_run_fixed_position_timing_backtest",
            side_effect=[
                SimpleNamespace(trades=formal_trades),
                SimpleNamespace(trades=preview_trades),
            ],
        ):
            result = position.build_position_timing_trade_preview(
                self.items,
                self.quotes,
                market_now=self.market_now,
            )

        self.assertEqual(result.errors, [])
        actions = {
            (row.操作, row.代码): row.数量
            for row in result.actions.itertuples(index=False)
        }
        self.assertEqual(
            actions,
            {
                ("卖出", "510500"): 400,
                ("卖出", "512890"): 300,
                ("买入", "159967"): 300,
            },
        )
        self.assertEqual(result.actions.iloc[0]["操作"], "卖出")
        self.assertIn("510500袖套", result.actions.loc[result.actions["代码"].eq("512890"), "原因"].iloc[0])

    def test_preview_rejects_missing_same_day_quote(self):
        quotes = dict(self.quotes)
        del quotes["513880"]
        result = position.build_position_timing_trade_preview(
            self.items,
            quotes,
            market_now=self.market_now,
        )
        self.assertTrue(result.errors)
        self.assertIn("513880", result.errors[0])
        self.assertTrue(result.actions.empty)

    def test_notification_slots_and_state_round_trip(self):
        expected_slots = ("09:45", "11:45", "14:45", "14:50", "14:54")
        for slot in expected_slots:
            hour, minute = (int(value) for value in slot.split(":"))
            self.assertEqual(
                alert_slot(self.market_now.replace(hour=hour, minute=minute)),
                slot,
            )
        self.assertIsNone(
            alert_slot(self.market_now.replace(hour=14, minute=51))
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            state = PositionTimingNotificationState(
                trade_date="2026-08-28",
                notified_slots=["14:50"],
                no_action_at_1450=True,
                last_outcome="no_action",
                last_notification_at="2026-08-28T14:50:00+08:00",
            )
            save_notification_state(state, path)
            self.assertEqual(load_notification_state(path), state)

    def test_no_action_message_states_that_later_checks_continue(self):
        preview = position.PositionTimingTradePreviewResult(
            formal_date="2026-08-27",
            preview_date="2026-08-28",
            quote_time="2026-08-28 14:50:02",
        )
        title, description, outcome = format_notification(preview, slot="14:50")
        self.assertEqual(outcome, "no_action")
        self.assertIn("今日无需操作", title)
        self.assertIn("14:54仍会继续检查行情", description)
        self.assertIn("不再重复", description)

        later_title, later_description, later_outcome = format_notification(
            preview,
            slot="14:54",
        )
        self.assertEqual(later_outcome, "no_action")
        self.assertIn("14:54当前无需操作", later_title)
        self.assertIn("后续时点仍会继续检查", later_description)

    def test_1450_no_action_only_suppresses_later_no_action_messages(self):
        state = PositionTimingNotificationState(
            trade_date="2026-08-28",
            notified_slots=["14:50"],
            no_action_at_1450=True,
            last_outcome="no_action",
        )

        self.assertTrue(
            should_suppress_notification(
                state,
                outcome="no_action",
                slot="14:54",
            )
        )
        self.assertFalse(
            should_suppress_notification(
                state,
                outcome="no_action",
                slot="11:45",
            )
        )
        self.assertFalse(
            should_suppress_notification(
                state,
                outcome="action",
                slot="14:54",
            )
        )

    def test_notification_channels_send_serverchan_and_hermes(self):
        with (
            patch(
                "scripts.monitor_position_timing_trades.send_serverchan_message"
            ) as serverchan_mock,
            patch(
                "scripts.monitor_position_timing_trades.send_hermes_weixin_message"
            ) as hermes_mock,
        ):
            sent_channels, errors = send_notification_channels(
                "SCT_TEST",
                "测试标题",
                "测试正文",
            )

        self.assertEqual(sent_channels, ("Server酱", "Hermes微信"))
        self.assertEqual(errors, ())
        serverchan_mock.assert_called_once_with("SCT_TEST", "测试标题", "测试正文")
        hermes_mock.assert_called_once_with("测试标题", "测试正文")

    def test_notification_channel_failure_does_not_block_other_channel(self):
        with (
            patch(
                "scripts.monitor_position_timing_trades.send_serverchan_message",
                side_effect=RuntimeError("serverchan unavailable"),
            ),
            patch(
                "scripts.monitor_position_timing_trades.send_hermes_weixin_message"
            ) as hermes_mock,
        ):
            sent_channels, errors = send_notification_channels(
                "SCT_TEST",
                "测试标题",
                "测试正文",
            )

        self.assertEqual(sent_channels, ("Hermes微信",))
        self.assertEqual(len(errors), 1)
        self.assertIn("Server酱推送失败", errors[0])
        hermes_mock.assert_called_once_with("测试标题", "测试正文")


if __name__ == "__main__":
    unittest.main()
