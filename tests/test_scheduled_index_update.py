import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import update_index_ma20_scheduled as scheduled
from services.update_tasks import UpdateResult


class ScheduledIndexUpdateTests(unittest.TestCase):
    def test_dry_run_lists_pending_indexes_without_network_or_writes(self):
        now = datetime(2026, 8, 13, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch.object(
                scheduled,
                "find_pending_post_close_index_names",
                return_value={"恒生科技", "上证指数"},
            ),
            patch.object(
                scheduled,
                "find_pending_futures_current_contract_index_names",
                return_value=set(),
            ),
            patch.object(scheduled, "run_index_ma20_update") as update_mock,
        ):
            exit_code, message = scheduled.run_scheduled_update(market_now=now, dry_run=True)

        self.assertEqual(exit_code, 0)
        self.assertIn("上证指数", message)
        self.assertIn("恒生科技", message)
        update_mock.assert_not_called()

    def test_no_pending_data_skips_network_update(self):
        with (
            patch.object(scheduled, "find_pending_post_close_index_names", return_value=set()),
            patch.object(
                scheduled,
                "find_pending_futures_current_contract_index_names",
                return_value=set(),
            ),
            patch.object(scheduled, "run_index_ma20_update") as update_mock,
        ):
            exit_code, message = scheduled.run_scheduled_update()

        self.assertEqual(exit_code, 0)
        self.assertIn("无需联网", message)
        update_mock.assert_not_called()

    def test_successful_update_uses_four_workers_and_rechecks_pending_scope(self):
        pending = {"上证指数", "恒生科技"}
        with (
            patch.object(
                scheduled,
                "find_pending_post_close_index_names",
                side_effect=[pending, set()],
            ) as pending_mock,
            patch.object(
                scheduled,
                "find_pending_futures_current_contract_index_names",
                side_effect=[set(), set()],
            ),
            patch.object(
                scheduled,
                "run_index_ma20_update",
                return_value=UpdateResult("success", "ok"),
            ) as update_mock,
        ):
            exit_code, message = scheduled.run_scheduled_update(api_key="test-key")

        self.assertEqual(exit_code, 0)
        self.assertIn("已补齐 2/2", message)
        self.assertEqual(update_mock.call_args.kwargs["index_names"], pending)
        self.assertEqual(update_mock.call_args.kwargs["max_workers"], 4)
        self.assertFalse(update_mock.call_args.kwargs["use_fresh_cache"])
        self.assertEqual(update_mock.call_args.kwargs["api_key"], "test-key")
        self.assertEqual(pending_mock.call_args_list[1].kwargs["index_names"], pending)

    def test_remaining_gaps_return_retryable_nonzero_status(self):
        pending = {"上证指数", "恒生科技"}
        with (
            patch.object(
                scheduled,
                "find_pending_post_close_index_names",
                side_effect=[pending, {"恒生科技"}],
            ),
            patch.object(
                scheduled,
                "find_pending_futures_current_contract_index_names",
                side_effect=[set(), set()],
            ),
            patch.object(
                scheduled,
                "run_index_ma20_update",
                return_value=UpdateResult("success", "partial", errors=["港股源暂不可用"]),
            ),
        ):
            exit_code, message = scheduled.run_scheduled_update()

        self.assertEqual(exit_code, 2)
        self.assertIn("仍待补齐：恒生科技", message)
        self.assertIn("港股源暂不可用", message)

    def test_concrete_futures_contract_gap_triggers_and_rechecks_update(self):
        with (
            patch.object(
                scheduled,
                "find_pending_post_close_index_names",
                side_effect=[set(), set()],
            ),
            patch.object(
                scheduled,
                "find_pending_futures_current_contract_index_names",
                side_effect=[{"铁矿石主连"}, set()],
            ),
            patch.object(
                scheduled,
                "run_index_ma20_update",
                return_value=UpdateResult("success", "ok"),
            ) as update_mock,
        ):
            exit_code, message = scheduled.run_scheduled_update()

        self.assertEqual(exit_code, 0)
        self.assertIn("铁矿石主连", message)
        self.assertEqual(update_mock.call_args.kwargs["index_names"], {"铁矿石主连"})

    def test_single_instance_lock_rejects_second_holder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "scheduled.lock"
            with scheduled.single_instance_lock(lock_path) as first:
                with scheduled.single_instance_lock(lock_path) as second:
                    self.assertTrue(first)
                    self.assertFalse(second)

    def test_windows_installer_contains_both_daily_triggers(self):
        scripts_dir = Path(__file__).parents[1] / "scripts"
        installer = (scripts_dir / "install_index_ma20_update_tasks.ps1").read_text(encoding="utf-8")
        wrapper = (scripts_dir / "run_index_ma20_update_task.ps1").read_text(encoding="utf-8")

        self.assertIn('New-ScheduledTaskTrigger -Daily -At "15:10"', installer)
        self.assertIn('New-ScheduledTaskTrigger -Daily -At "16:10"', installer)
        self.assertIn("-MultipleInstances IgnoreNew", installer)
        self.assertIn("update_index_ma20_scheduled.py", wrapper)
        self.assertIn("ReadToEndAsync", wrapper)
        self.assertIn("finally {", wrapper)


if __name__ == "__main__":
    unittest.main()
