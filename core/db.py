import sqlite3
from contextlib import closing
from datetime import datetime

import pandas as pd

from core.paths import DB_PATH, ensure_dirs


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=30)


def init_db() -> None:
    ensure_dirs()
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT,
                source TEXT NOT NULL,
                data_type TEXT NOT NULL,
                period TEXT DEFAULT '1d',
                file_path TEXT NOT NULL,
                last_trade_date TEXT,
                last_update_time TEXT,
                row_count INTEGER,
                status TEXT,
                error_message TEXT,
                UNIQUE(symbol, source, data_type, period)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                message TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS correlation_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_a TEXT NOT NULL,
                asset_b TEXT NOT NULL,
                correlation REAL NOT NULL,
                strength TEXT,
                start_date TEXT,
                end_date TEXT,
                common_days INTEGER,
                source_summary TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()


def start_job(job_name: str) -> int:
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO jobs (job_name, status, started_at)
            VALUES (?, ?, ?)
            """,
            (job_name, "running", datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        job_id = cursor.lastrowid
    return job_id


def finish_job(job_id: int, status: str, message: str = "") -> None:
    with closing(get_conn()) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status=?, finished_at=?, message=?
            WHERE id=?
            """,
            (status, datetime.now().isoformat(timespec="seconds"), message, job_id),
        )
        conn.commit()


def list_jobs(limit: int = 100) -> pd.DataFrame:
    with closing(get_conn()) as conn:
        df = pd.read_sql_query(
            """
            SELECT id, job_name, status, started_at, finished_at, message
            FROM jobs
            ORDER BY id DESC
            LIMIT ?
            """,
            conn,
            params=(limit,),
        )
    return df
