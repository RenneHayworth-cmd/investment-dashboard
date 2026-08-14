import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import math
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from core import cache, db
from services import correlation_analysis, market_calendar
from scripts import migrate_fund_price_caches


class _FakeCursor:
    lastrowid = 7

    def fetchone(self):
        return None


class _TrackingConnection:
    def __init__(self, fail_method: str):
        self.fail_method = fail_method
        self.closed = False

    def _maybe_fail(self, method: str) -> None:
        if self.fail_method == method:
            raise sqlite3.OperationalError(f"{method} failed")

    def execute(self, *_args, **_kwargs):
        self._maybe_fail("execute")
        return _FakeCursor()

    def executemany(self, *_args, **_kwargs):
        self._maybe_fail("executemany")
        return _FakeCursor()

    def commit(self):
        self._maybe_fail("commit")

    def close(self):
        self.closed = True


class _RowConnection(_TrackingConnection):
    def __init__(self, row):
        super().__init__("")
        self.row = row

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return self.row


class DatabaseResourceTests(unittest.TestCase):
    def test_database_job_success_path_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "cache.db"
            with patch("core.db.DB_PATH", database_path), patch("core.db.ensure_dirs"):
                db.init_db()
                job_id = db.start_job("测试任务")
                db.finish_job(job_id, "success", "完成")
                jobs = db.list_jobs()

        self.assertEqual(jobs.iloc[0]["job_name"], "测试任务")
        self.assertEqual(jobs.iloc[0]["status"], "success")
        self.assertEqual(jobs.iloc[0]["message"], "完成")

    def test_init_db_closes_connection_when_schema_creation_fails(self):
        conn = _TrackingConnection("execute")

        with (
            patch("core.db.ensure_dirs"),
            patch("core.db.get_conn", return_value=conn),
            self.assertRaises(sqlite3.OperationalError),
        ):
            db.init_db()

        self.assertTrue(conn.closed)

    def test_start_job_closes_connection_when_commit_fails(self):
        conn = _TrackingConnection("commit")

        with patch("core.db.get_conn", return_value=conn), self.assertRaises(sqlite3.OperationalError):
            db.start_job("测试任务")

        self.assertTrue(conn.closed)

    def test_save_dataset_closes_connection_when_metadata_write_fails(self):
        conn = _TrackingConnection("execute")
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("core.cache.RAW_DIR", Path(temp_dir)),
                patch("core.cache.get_conn", return_value=conn),
                self.assertRaises(sqlite3.OperationalError),
            ):
                cache.save_dataset(
                    symbol="TEST",
                    name="测试",
                    source="unit",
                    data_type="daily",
                    df=pd.DataFrame({"日期": ["2026-01-01"], "收盘价": [1.0]}),
                )

        self.assertTrue(conn.closed)

    def test_save_dataset_restores_existing_csv_when_metadata_write_fails(self):
        conn = _TrackingConnection("execute")
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir)
            source_dir = raw_dir / "unit"
            source_dir.mkdir()
            file_path = source_dir / "TEST_1d.csv"
            original = b"date,close\n2026-01-01,1\n"
            file_path.write_bytes(original)

            with (
                patch("core.cache.RAW_DIR", raw_dir),
                patch("core.cache.get_conn", return_value=conn),
                self.assertRaises(sqlite3.OperationalError),
            ):
                cache.save_dataset(
                    symbol="TEST",
                    name="测试",
                    source="unit",
                    data_type="daily",
                    df=pd.DataFrame({"date": ["2026-01-02"], "close": [2.0]}),
                )

            self.assertEqual(file_path.read_bytes(), original)
            self.assertFalse(list(source_dir.glob("*.tmp")))
            self.assertFalse(list(source_dir.glob("*.backup")))

    def test_concurrent_dataset_writes_leave_one_complete_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "cache.db"
            first = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=200), "close": [1.0] * 200})
            second = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=200), "close": [2.0] * 200})

            with patch("core.cache.RAW_DIR", root / "raw"), patch("core.db.DB_PATH", database_path):
                db.init_db()

                def save(frame):
                    cache.save_dataset("TEST", "测试", "unit", "daily", frame)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    list(executor.map(save, (first, second)))

                loaded, _ = cache.load_dataset("TEST", "unit", "daily")

            self.assertEqual(len(loaded), 200)
            self.assertIn(set(loaded["close"]), ({1.0}, {2.0}))

    def test_list_datasets_closes_connection_when_query_fails(self):
        conn = _TrackingConnection("")

        with (
            patch("core.cache.get_conn", return_value=conn),
            patch("core.cache.pd.read_sql_query", side_effect=sqlite3.OperationalError("query failed")),
            self.assertRaises(sqlite3.OperationalError),
        ):
            cache.list_datasets()

        self.assertTrue(conn.closed)

    def test_corrupted_cached_csv_is_reported_and_connection_is_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "broken.csv"
            file_path.write_bytes(b"\xff\xfe\x00\x00")
            conn = _RowConnection((str(file_path), "2026-01-01", "2026-01-01T16:00:00", "success"))

            with patch("core.cache.get_conn", return_value=conn), self.assertRaises(UnicodeDecodeError):
                cache.load_dataset("TEST", "unit", "daily")

        self.assertTrue(conn.closed)

    def test_correlation_save_closes_connection_when_insert_fails(self):
        conn = _TrackingConnection("executemany")
        pairs = pd.DataFrame([{"标的A": "A", "标的B": "B", "相关系数r": 0.5}])

        with (
            patch("services.correlation_analysis.init_db"),
            patch("services.correlation_analysis.get_conn", return_value=conn),
            self.assertRaises(sqlite3.OperationalError),
        ):
            correlation_analysis.save_correlation_results(pairs, {"共同日期数": 2})

        self.assertTrue(conn.closed)

    def test_constant_correlation_is_marked_unavailable_and_not_saved(self):
        items = [
            correlation_analysis.CorrelationInput(
                "A", "A", pd.DataFrame({"date": pd.date_range("2026-01-01", periods=3), "close": [1, 1, 1]})
            ),
            correlation_analysis.CorrelationInput(
                "B", "B", pd.DataFrame({"date": pd.date_range("2026-01-01", periods=3), "close": [2, 3, 4]})
            ),
        ]
        result = correlation_analysis.calculate_price_correlation(items)

        self.assertTrue(math.isnan(float(result.pair_table.iloc[0]["相关系数r"])))
        self.assertEqual(result.pair_table.iloc[0]["相关性"], "无法计算")
        self.assertEqual(result.summary["最高相关"], "-")

        get_conn_mock = Mock(return_value=_TrackingConnection(""))
        with (
            patch("services.correlation_analysis.init_db"),
            patch("services.correlation_analysis.get_conn", get_conn_mock),
        ):
            correlation_analysis.save_correlation_results(result.pair_table, result.summary)
        get_conn_mock.assert_not_called()


class MarketCalendarFallbackTests(unittest.TestCase):
    def test_static_calendar_warns_once_outside_covered_years(self):
        market = market_calendar.get_market_window("日本")
        market_calendar._get_exchange_calendar.cache_clear()
        market_calendar._warn_static_calendar_coverage.cache_clear()

        with (
            patch("services.market_calendar.xcals", None),
            self.assertLogs("services.market_calendar", level="WARNING") as logs,
        ):
            self.assertFalse(market_calendar.is_market_holiday(market, date(2027, 1, 4)))
            self.assertFalse(market_calendar.is_market_holiday(market, date(2027, 1, 5)))

        coverage_logs = [line for line in logs.output if "静态休市日仅覆盖" in line]
        self.assertEqual(len(coverage_logs), 1)


class PageReliabilityTests(unittest.TestCase):
    def test_fund_cache_migration_defaults_to_read_only_preview(self):
        rows = [
            {
                "用途": "持仓择时",
                "代码": "159545",
                "复权": "forward_additive",
                "缓存键": "fund_close_v2_159545.SZ_forward_additive",
                "现状": "待建立",
            }
        ]
        with (
            patch.object(migrate_fund_price_caches, "build_preview", return_value=rows),
            patch.object(migrate_fund_price_caches, "apply_migration") as apply_mock,
            patch("builtins.print"),
        ):
            result = migrate_fund_price_caches.main([])

        self.assertEqual(result, 0)
        apply_mock.assert_not_called()

    def test_fund_cache_migration_repairs_existing_invalid_v2_cache(self):
        rows = [
            {
                "用途": "持仓择时",
                "代码": "159545",
                "复权": "forward_additive",
                "缓存键": "fund_close_v2_159545.SZ_forward_additive",
                "现状": "已存在",
            }
        ]
        invalid_item = Mock(
            status="缓存待校验",
            formal_history_valid=False,
            error="缓存标签错误",
            dataframe=pd.DataFrame(),
            latest_date="",
        )
        repaired_item = Mock(
            status="已更新",
            formal_history_valid=True,
            error="",
            dataframe=pd.DataFrame({"date": [pd.Timestamp("2026-08-12")]}),
            latest_date="2026-08-12",
        )
        with (
            patch.object(
                migrate_fund_price_caches,
                "load_or_fetch_etf",
                side_effect=[invalid_item, repaired_item],
            ) as load_mock,
            patch.object(
                migrate_fund_price_caches,
                "validate_159545_acceptance",
                return_value=["159545固定验收通过。"],
            ),
            patch("builtins.print"),
        ):
            result = migrate_fund_price_caches.apply_migration(
                rows,
                api_key="environment-key",
                count=5000,
            )

        self.assertEqual(result, 0)
        self.assertFalse(load_mock.call_args_list[0].kwargs["allow_fetch"])
        self.assertFalse(load_mock.call_args_list[0].kwargs["save_to_cache"])
        self.assertTrue(load_mock.call_args_list[1].kwargs["allow_fetch"])
        self.assertTrue(load_mock.call_args_list[1].kwargs["save_to_cache"])

    def test_analysis_pages_use_explicit_adjustment_labels_and_live_none(self):
        root = Path(__file__).parents[1]
        for page_name in (
            "2_A股分析.py",
            "3_策略回测.py",
            "4_相关性分析.py",
            "5_持仓分析.py",
            "7_美股分析.py",
        ):
            source = (root / "pages" / page_name).read_text(encoding="utf-8")
            self.assertIn("FUND_ADJUSTMENT_OPTIONS", source)
        live_source = (root / "pages" / "6_实盘记录.py").read_text(encoding="utf-8")
        self.assertIn("adjust=FUND_ADJUST_NONE", live_source)
    def test_position_page_imports_realtime_timing_end_constant(self):
        source = (Path(__file__).parents[1] / "pages" / "5_持仓分析.py").read_text(encoding="utf-8")

        self.assertIn("ETF_REALTIME_TIMING_END_TIME,", source)
        self.assertIn("market_now.time() >= ETF_REALTIME_TIMING_END_TIME", source)

    def test_analysis_pages_keep_last_source_in_session_state(self):
        root = Path(__file__).parents[1]
        a_share = (root / "pages" / "2_A股分析.py").read_text(encoding="utf-8")
        us_stock = (root / "pages" / "7_美股分析.py").read_text(encoding="utf-8")

        self.assertIn('analysis_state_key = "a_share_analysis_source"', a_share)
        self.assertIn('analysis_state_key = "us_stock_analysis_source"', us_stock)
        self.assertIn("if save_to_cache and fresh_analysis", a_share)
        self.assertIn("if save_to_cache and fresh_analysis", us_stock)

    def test_task_page_has_manual_formal_update_and_chinese_columns(self):
        source = (Path(__file__).parents[1] / "pages" / "9_任务与数据.py").read_text(encoding="utf-8")

        self.assertIn("更新缺失的正式指数数据", source)
        self.assertIn("确认更新并复核", source)
        self.assertIn("build_pending_index_update_preview", source)
        self.assertIn("verify_updated_index_data", source)
        self.assertIn('"last_update_time": "更新时间"', source)
        self.assertIn('str.replace("T", " ", regex=False)', source)


if __name__ == "__main__":
    unittest.main()
