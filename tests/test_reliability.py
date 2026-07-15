import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core import cache, db
from services import correlation_analysis, market_calendar


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


if __name__ == "__main__":
    unittest.main()
