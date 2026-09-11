"""Fail-closed channel policy and cross-platform SQLite event receipts.

No signal calculations and no transport dependencies at import time.
"""
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


def flag(name: str) -> bool:
    value = os.environ.get(name, "false").strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off", ""}:
        return False
    raise ValueError(f"{name} 必须为 true 或 false")


def channel_enabled(channel: str) -> bool:
    if flag("REMINDER_DRY_RUN"):
        return False
    if channel == "wechat" and os.environ.get("REMINDER_NODE") == "lightsail":
        return False
    return flag({"fangtang": "ENABLE_FANGTANG", "wechat": "ENABLE_WECHAT"}[channel])


def enabled_channels() -> tuple[str, ...]:
    return tuple(c for c in ("fangtang", "wechat") if channel_enabled(c))


def state_dir() -> Path:
    from core.paths import OUTPUT_DIR
    return Path(os.environ.get("REMINDER_STATE_DIR") or OUTPUT_DIR / "alerts")


def event_key(*parts) -> str:
    return "|".join(str(p).replace("|", "_") for p in parts)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()[:20]


class DeliveryUncertain(RuntimeError):
    pass


class DeliveryRejected(RuntimeError):
    """The provider explicitly confirmed rejection (safe to retry later)."""


def ambiguous_failure(exc: Exception) -> bool:
    """Only explicit rejection or failure before connection is retryable."""
    import requests
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return False
    if isinstance(exc, requests.exceptions.HTTPError):
        # A gateway/server failure may occur after downstream acceptance.
        return exc.response is None or exc.response.status_code >= 500 or exc.response.status_code == 408
    if isinstance(exc, DeliveryRejected):
        return False
    # Unknown callback failures may happen after acceptance. Fail closed.
    return True


class DeliveryLedger:
    def __init__(self, path: Path | None = None):
        self.path = (path or state_dir() / "deliveries.sqlite3").resolve()

    def deliver(self, keys: list[str], channel: str, send) -> str:
        """Atomically claim a batch, then call send with only unsent event keys.

        SQLite BEGIN IMMEDIATE works on Windows and Linux. A committed 'sending'
        receipt survives crashes; subsequent runs must reconcile it rather than
        blindly retry. Only provider-confirmed acceptance is recorded as 'sent'.
        """
        if not channel_enabled(channel):
            return "disabled"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (event TEXT NOT NULL, "
                       "channel TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL, "
                       "PRIMARY KEY(event, channel))")
            db.execute("BEGIN IMMEDIATE")
            pending = []
            for key in dict.fromkeys(keys):
                row = db.execute("SELECT status FROM deliveries WHERE event=? AND channel=?",
                                 (key, channel)).fetchone()
                if row and row[0] in {"sending", "uncertain"}:
                    raise DeliveryUncertain("发送结果待核对，已阻止自动重复发送")
                if not row or row[0] != "sent":
                    pending.append(key)
            if not pending:
                return "duplicate"

            def record(status):
                stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                db.executemany("INSERT OR REPLACE INTO deliveries VALUES (?, ?, ?, ?)",
                               [(k, channel, status, stamp) for k in pending])
                db.commit()

            record("sending")
            try:
                send(pending)
            except Exception as exc:
                record("uncertain" if ambiguous_failure(exc) else "failed")
                raise
            record("sent")
            return "sent"

    def statuses(self, keys: list[str], channel: str) -> dict[str, str]:
        """Dry-run inspection never creates or changes the ledger."""
        if not self.path.exists():
            return {k: "new" for k in keys}
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as db:
            return {k: (db.execute("SELECT status FROM deliveries WHERE event=? AND channel=?",
                                  (k, channel)).fetchone() or ("new",))[0] for k in keys}


@contextmanager
def process_lock(path: Path):
    """Short-lived script exclusion on POSIX or native Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            lock = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            lock()
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            unlock()
