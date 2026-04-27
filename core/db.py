import sqlite3
from datetime import datetime

from core.paths import DB_PATH, ensure_dirs


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    ensure_dirs()
    conn = get_conn()
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
    conn.commit()
    conn.close()


def start_job(job_name: str) -> int:
    conn = get_conn()
    cursor = conn.execute(
        """
        INSERT INTO jobs (job_name, status, started_at)
        VALUES (?, ?, ?)
        """,
        (job_name, "running", datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    job_id = cursor.lastrowid
    conn.close()
    return job_id


def finish_job(job_id: int, status: str, message: str = "") -> None:
    conn = get_conn()
    conn.execute(
        """
        UPDATE jobs
        SET status=?, finished_at=?, message=?
        WHERE id=?
        """,
        (status, datetime.now().isoformat(timespec="seconds"), message, job_id),
    )
    conn.commit()
    conn.close()

