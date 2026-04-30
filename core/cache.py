from datetime import datetime
from pathlib import Path

import pandas as pd

from core.db import get_conn
from core.paths import RAW_DIR


def save_dataset(
    symbol: str,
    name: str,
    source: str,
    data_type: str,
    df: pd.DataFrame,
    period: str = "1d",
) -> Path:
    source_dir = RAW_DIR / source
    source_dir.mkdir(parents=True, exist_ok=True)

    safe_symbol = symbol.replace("/", "_").replace("\\", "_")
    file_path = source_dir / f"{safe_symbol}_{period}.csv"
    df.to_csv(file_path, index=False, encoding="utf-8-sig")

    last_trade_date = ""
    for date_column in ("trade_date", "日期"):
        if date_column in df.columns and not df.empty:
            last_trade_date = str(pd.to_datetime(df[date_column]).max().date())
            break

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO datasets (
            symbol, name, source, data_type, period, file_path,
            last_trade_date, last_update_time, row_count, status, error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, source, data_type, period)
        DO UPDATE SET
            name=excluded.name,
            file_path=excluded.file_path,
            last_trade_date=excluded.last_trade_date,
            last_update_time=excluded.last_update_time,
            row_count=excluded.row_count,
            status=excluded.status,
            error_message=excluded.error_message
        """,
        (
            symbol,
            name,
            source,
            data_type,
            period,
            str(file_path),
            last_trade_date,
            datetime.now().isoformat(timespec="seconds"),
            len(df),
            "success",
            None,
        ),
    )
    conn.commit()
    conn.close()
    return file_path


def load_dataset(symbol: str, source: str, data_type: str, period: str = "1d"):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT file_path, last_trade_date, last_update_time, status
        FROM datasets
        WHERE symbol=? AND source=? AND data_type=? AND period=?
        """,
        (symbol, source, data_type, period),
    ).fetchone()
    conn.close()

    if not row:
        return None, None

    file_path, last_trade_date, last_update_time, status = row
    if status != "success" or not Path(file_path).exists():
        return None, {"last_trade_date": last_trade_date, "last_update_time": last_update_time}

    meta = {"last_trade_date": last_trade_date, "last_update_time": last_update_time}
    return pd.read_csv(file_path), meta


def list_datasets() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query(
        """
        SELECT symbol, name, source, data_type, period, last_trade_date,
               last_update_time, row_count, status, file_path
        FROM datasets
        ORDER BY last_update_time DESC
        """,
        conn,
    )
    conn.close()
    return df
