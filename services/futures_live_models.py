from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

import pandas as pd

DEFAULT_FUTURES_STATEMENT_DIR = Path(
    "/mnt/c/Users/78224/OneDrive/A股/期货交易结算单"
)


STATEMENT_FILE_PATTERN = re.compile(r"^\d+_(\d{4}-\d{2})\.(xls|xlsx)$", re.IGNORECASE)


ASSET_TYPES = ("期货", "期权")


BUY_SELL_VALUES = ("买", "卖")


OPEN_CLOSE_VALUES = ("开", "平")


CASH_FLOW_TYPES = ("入金", "出金")


OPTION_EXPIRY_OUTCOMES = ("作废", "履约")


DAILY_PNL_RESOLUTIONS = ("采用手工", "采用正式")


RECONCILIATION_TOLERANCE = 0.05


@dataclass
class StatementPayload:
    statement_month: str
    statement_end_date: str
    account: dict[str, object]
    positions: pd.DataFrame
    trades: pd.DataFrame
    cash_flows: pd.DataFrame
    warnings: list[str]


@dataclass
class StatementSyncResult:
    scanned: int
    imported: int
    skipped: int
    failed: int
    warnings: list[str]
    errors: list[str]


def configured_statement_dir(value: str | os.PathLike[str] | None = None) -> Path:
    raw = str(value or os.environ.get("FUTURES_STATEMENT_DIR") or "").strip()
    if not raw:
        return DEFAULT_FUTURES_STATEMENT_DIR
    windows_match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if windows_match:
        drive, tail = windows_match.groups()
        raw = f"/mnt/{drive.lower()}/{tail.replace('\\', '/')}"
    return Path(raw).expanduser()


def discover_statement_files(directory: str | os.PathLike[str] | None = None) -> list[Path]:
    root = configured_statement_dir(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"结算单目录不存在：{root}")
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file() and STATEMENT_FILE_PATTERN.match(path.name)
    )


def normalize_contract(value: object, asset_type: str | None = None) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        raise ValueError("合约代码不能为空。")
    base = text.split(".", 1)[0]
    if asset_type == "期权" or re.search(r"[-_][CP][-_]?\d+$", base):
        matched = re.match(r"^([A-Z]+\d{4})[-_]?([CP])[-_]?(\d+)$", base)
        if matched:
            return "".join(matched.groups())
    return base.replace("-", "").replace("_", "")
