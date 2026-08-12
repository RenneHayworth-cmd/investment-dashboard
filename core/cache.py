from contextlib import closing
from contextlib import contextmanager
from datetime import datetime
import fcntl
import os
from pathlib import Path
import shutil
import tempfile

import pandas as pd

from core.db import get_conn
from core.paths import RAW_DIR


def _dataset_paths(symbol: str, source: str, period: str) -> tuple[Path, Path]:
    source_dir = RAW_DIR / source
    safe_symbol = symbol.replace("/", "_").replace("\\", "_")
    file_path = source_dir / f"{safe_symbol}_{period}.csv"
    return file_path, file_path.with_suffix(file_path.suffix + ".lock")


@contextmanager
def _dataset_lock(lock_path: Path, *, exclusive: bool):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(
            lock_file.fileno(),
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def latest_trade_date_text(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""
    for date_column in ("trade_date", "日期", "date", "datetime"):
        if date_column not in df.columns:
            continue
        dates = pd.to_datetime(df[date_column], errors="coerce").dropna()
        if not dates.empty:
            return str(dates.max().date())
    return ""


def save_dataset(
    symbol: str,
    name: str,
    source: str,
    data_type: str,
    df: pd.DataFrame,
    period: str = "1d",
) -> Path:
    file_path, lock_path = _dataset_paths(symbol, source, period)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    backup_path: Path | None = None
    last_trade_date = latest_trade_date_text(df)

    try:
        with tempfile.NamedTemporaryFile(
            dir=file_path.parent,
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
        df.to_csv(temp_path, index=False, encoding="utf-8-sig")
        with temp_path.open("rb") as persisted:
            os.fsync(persisted.fileno())

        with _dataset_lock(lock_path, exclusive=True):
            existed = file_path.exists()
            if existed:
                with tempfile.NamedTemporaryFile(
                    dir=file_path.parent,
                    prefix=f".{file_path.name}.",
                    suffix=".backup",
                    delete=False,
                ) as backup_file:
                    backup_path = Path(backup_file.name)
                shutil.copy2(file_path, backup_path)

            os.replace(temp_path, file_path)
            temp_path = None
            try:
                with closing(get_conn()) as conn:
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
            except Exception:
                if backup_path is not None and backup_path.exists():
                    os.replace(backup_path, file_path)
                    backup_path = None
                elif not existed:
                    file_path.unlink(missing_ok=True)
                raise
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if backup_path is not None:
            backup_path.unlink(missing_ok=True)
    return file_path


def load_dataset(symbol: str, source: str, data_type: str, period: str = "1d"):
    _expected_path, lock_path = _dataset_paths(symbol, source, period)
    with _dataset_lock(lock_path, exclusive=False):
        with closing(get_conn()) as conn:
            row = conn.execute(
                """
                SELECT file_path, last_trade_date, last_update_time, status
                FROM datasets
                WHERE symbol=? AND source=? AND data_type=? AND period=?
                """,
                (symbol, source, data_type, period),
            ).fetchone()

        if not row:
            return None, None

        file_path, last_trade_date, last_update_time, status = row
        if status != "success" or not Path(file_path).exists():
            return None, {"last_trade_date": last_trade_date, "last_update_time": last_update_time}

        meta = {"last_trade_date": last_trade_date, "last_update_time": last_update_time}
        return pd.read_csv(file_path), meta


def list_datasets() -> pd.DataFrame:
    with closing(get_conn()) as conn:
        df = pd.read_sql_query(
            """
            SELECT symbol, name, source, data_type, period, last_trade_date,
                   last_update_time, row_count, status, file_path
            FROM datasets
            ORDER BY last_update_time DESC
            """,
            conn,
        )
    return df
