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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_key TEXT UNIQUE,
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                fee_rate_pct REAL NOT NULL,
                strategy TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_statement_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                file_mtime_ns INTEGER NOT NULL,
                file_hash TEXT NOT NULL,
                statement_month TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                status TEXT NOT NULL,
                warnings TEXT,
                error_message TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_account_monthly (
                statement_month TEXT PRIMARY KEY,
                statement_end_date TEXT NOT NULL,
                previous_balance REAL,
                deposits_withdrawals REAL,
                monthly_pnl REAL,
                total_premium REAL,
                monthly_fee REAL,
                declaration_fee REAL NOT NULL DEFAULT 0,
                ending_balance REAL,
                customer_equity REAL,
                cash_equity REAL,
                frozen_funds REAL,
                margin REAL,
                floating_pnl REAL,
                available_funds REAL,
                risk_ratio REAL,
                additional_margin REAL,
                source_file TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                warnings TEXT
            )
            """
        )
        account_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(futures_account_monthly)")
        }
        if "declaration_fee" not in account_columns:
            conn.execute(
                "ALTER TABLE futures_account_monthly "
                "ADD COLUMN declaration_fee REAL NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_month_end_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                statement_month TEXT NOT NULL,
                statement_end_date TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                contract TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                average_price REAL,
                previous_settlement REAL,
                settlement_price REAL,
                floating_pnl REAL,
                margin REAL,
                multiplier REAL,
                trade_code TEXT,
                source_file TEXT NOT NULL,
                UNIQUE(statement_month, asset_type, contract, side)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_live_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                statement_month TEXT,
                trade_date TEXT NOT NULL,
                trade_time TEXT,
                asset_type TEXT NOT NULL,
                contract TEXT NOT NULL,
                broker_trade_id TEXT,
                buy_sell TEXT NOT NULL,
                open_close TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                turnover REAL,
                multiplier REAL,
                fee REAL NOT NULL DEFAULT 0,
                close_pnl REAL,
                strategy TEXT,
                notes TEXT,
                reconciliation_status TEXT NOT NULL DEFAULT '不适用',
                matched_statement_trade_id INTEGER,
                source_file TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(matched_statement_trade_id) REFERENCES futures_live_trades(id)
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_futures_statement_trade_key
            ON futures_live_trades(source_file, asset_type, broker_trade_id)
            WHERE source='月结单' AND broker_trade_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_daily_closes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_type TEXT NOT NULL,
                contract TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                close_price REAL NOT NULL,
                settlement_price REAL,
                source TEXT NOT NULL,
                settlement_source TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(asset_type, contract, trade_date)
            )
            """
        )
        close_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(futures_daily_closes)")
        }
        if "settlement_price" not in close_columns:
            conn.execute(
                "ALTER TABLE futures_daily_closes ADD COLUMN settlement_price REAL"
            )
        if "settlement_source" not in close_columns:
            conn.execute(
                "ALTER TABLE futures_daily_closes ADD COLUMN settlement_source TEXT"
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_futures_live_trades_date
            ON futures_live_trades(trade_date, contract)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_futures_positions_month
            ON futures_month_end_positions(statement_month, contract)
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
