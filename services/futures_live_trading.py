from __future__ import annotations

import calendar
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import warnings

import pandas as pd

from core.db import get_conn, init_db
from services.futures_options_analysis import (
    fetch_option_from_akshare,
    normalize_option_symbol,
)
from services.futures_spread import (
    completed_futures_daily_cutoff,
    fetch_futures_daily,
)


DEFAULT_FUTURES_STATEMENT_DIR = Path(
    "/mnt/c/Users/78224/OneDrive/A股/期货交易结算单"
)
STATEMENT_FILE_PATTERN = re.compile(r"^\d+_(\d{4}-\d{2})\.(xls|xlsx)$", re.IGNORECASE)
ASSET_TYPES = ("期货", "期权")
BUY_SELL_VALUES = ("买", "卖")
OPEN_CLOSE_VALUES = ("开", "平")
RECONCILIATION_TOLERANCE = 0.05


@dataclass
class StatementPayload:
    statement_month: str
    statement_end_date: str
    account: dict[str, object]
    positions: pd.DataFrame
    trades: pd.DataFrame
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


def _clean_label(value: object) -> str:
    return re.sub(r"[\s\r\n]+", "", str(value or "")).strip()


def _number(value: object, *, percent: bool = False) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "-", "—"}:
        return None
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1]
    result = pd.to_numeric(text, errors="coerce")
    if pd.isna(result):
        return None
    number = float(result)
    if percent or is_percent:
        number /= 100
    return number


def _integer(value: object) -> int:
    number = _number(value)
    return int(round(number or 0))


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _date_text(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else pd.Timestamp(parsed).strftime("%Y-%m-%d")


def _time_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    text = str(value).strip()
    matched = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", text)
    return matched.group(1) if matched else text


def _read_sheet(path: Path, sheet_name: str) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        return pd.read_excel(path, sheet_name=sheet_name, header=None, dtype=object)


def _value_after_label(raw: pd.DataFrame, label: str) -> object:
    target = _clean_label(label)
    for row_index in range(len(raw.index)):
        for column_index in range(len(raw.columns)):
            if _clean_label(raw.iat[row_index, column_index]) != target:
                continue
            for next_column in range(column_index + 1, len(raw.columns)):
                value = raw.iat[row_index, next_column]
                if pd.notna(value) and str(value).strip():
                    return value
    return None


def _deduplicate_headers(values: list[object]) -> list[str]:
    counts: dict[str, int] = {}
    headers: list[str] = []
    for value in values:
        base = _clean_label(value) or "未命名"
        counts[base] = counts.get(base, 0) + 1
        headers.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return headers


def _table_after_header(
    raw: pd.DataFrame,
    required_headers: tuple[str, ...],
    *,
    start_row: int = 0,
) -> pd.DataFrame:
    normalized_required = {_clean_label(item) for item in required_headers}
    header_row = None
    for index in range(start_row, len(raw.index)):
        labels = {_clean_label(value) for value in raw.iloc[index].tolist() if _clean_label(value)}
        if normalized_required.issubset(labels):
            header_row = index
            break
    if header_row is None:
        raise ValueError(f"未找到表头：{', '.join(required_headers)}")

    headers = _deduplicate_headers(raw.iloc[header_row].tolist())
    records: list[list[object]] = []
    for index in range(header_row + 1, len(raw.index)):
        row = raw.iloc[index].tolist()
        first = _clean_label(row[0] if row else "")
        if first == "合计":
            break
        if not any(pd.notna(value) and str(value).strip() for value in row):
            if records:
                break
            continue
        records.append(row)
    return pd.DataFrame(records, columns=headers)


def _section_table(raw: pd.DataFrame, title: str, required_headers: tuple[str, ...]) -> pd.DataFrame:
    target = _clean_label(title)
    start_row = 0
    for index in range(len(raw.index)):
        labels = {_clean_label(value) for value in raw.iloc[index].tolist() if _clean_label(value)}
        if target in labels:
            start_row = index + 1
            break
    else:
        return pd.DataFrame()
    try:
        return _table_after_header(raw, required_headers, start_row=start_row)
    except ValueError:
        return pd.DataFrame()


def _statement_end_date(statement_month: str) -> str:
    year, month = (int(part) for part in statement_month.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def _parse_account(raw: pd.DataFrame, statement_month: str) -> dict[str, object]:
    fields = {
        "previous_balance": "上月结存",
        "deposits_withdrawals": "当月存取合计",
        "monthly_pnl": "当月盈亏",
        "total_premium": "当月总权利金",
        "monthly_fee": "当月手续费",
        "ending_balance": "当月结存",
        "customer_equity": "客户权益",
        "cash_equity": "实有货币资金",
        "frozen_funds": "冻结资金",
        "margin": "保证金占用",
        "floating_pnl": "浮动盈亏",
        "available_funds": "可用资金",
        "additional_margin": "追加保证金",
    }
    account = {key: _number(_value_after_label(raw, label)) for key, label in fields.items()}
    account["risk_ratio"] = _number(_value_after_label(raw, "风险度"), percent=True)
    account["statement_month"] = statement_month
    account["statement_end_date"] = _statement_end_date(statement_month)
    required = ("customer_equity", "monthly_fee", "margin", "available_funds")
    missing = [key for key in required if account.get(key) is None]
    if missing:
        raise ValueError(f"账户资金字段缺失：{', '.join(missing)}")
    return account


def _parse_declaration_fee(raw: pd.DataFrame) -> float:
    header_row = None
    header_columns: dict[str, int] = {}
    for index in range(len(raw.index)):
        labels = [_clean_label(value) for value in raw.iloc[index].tolist()]
        if all(label in labels for label in ("发生日期", "类型", "金额")):
            header_row = index
            header_columns = {label: labels.index(label) for label in ("发生日期", "类型", "金额")}
            break
    if header_row is None:
        return 0.0

    total = 0.0
    for index in range(header_row + 1, len(raw.index)):
        row = raw.iloc[index].tolist()
        first = _clean_label(row[header_columns["发生日期"]])
        fee_type = _clean_label(row[header_columns["类型"]])
        if first == "期货持仓汇总":
            break
        if first == "合计" or "申报费" not in fee_type:
            continue
        amount = _number(row[header_columns["金额"]])
        if amount is not None:
            total += abs(amount)
    return total


def _position_multiplier(
    *,
    average_price: float | None,
    settlement_price: float | None,
    quantity: int,
    floating_pnl: float | None,
) -> float | None:
    if not average_price or settlement_price is None or quantity <= 0 or floating_pnl is None:
        return None
    denominator = abs(settlement_price - average_price) * quantity
    if denominator <= 1e-12:
        return None
    candidate = abs(floating_pnl) / denominator
    rounded = round(candidate)
    return float(rounded) if rounded > 0 and abs(candidate - rounded) <= 0.05 else None


def _parse_positions(report_raw: pd.DataFrame, statement_month: str) -> pd.DataFrame:
    statement_end = _statement_end_date(statement_month)
    rows: list[dict[str, object]] = []
    futures = _section_table(report_raw, "期货持仓汇总", ("合约", "买持仓", "卖持仓"))
    for record in futures.to_dict("records"):
        contract = normalize_contract(record.get("合约"), "期货")
        previous = _number(record.get("昨结算价"))
        settlement = _number(record.get("今结算价"))
        total_float = _number(record.get("浮动盈亏"))
        long_quantity = _integer(record.get("买持仓"))
        short_quantity = _integer(record.get("卖持仓"))
        margin = _number(record.get("交易保证金"))
        for side, quantity, price in (
            ("多", long_quantity, _number(record.get("买均价"))),
            ("空", short_quantity, _number(record.get("卖均价"))),
        ):
            if quantity <= 0:
                continue
            side_float = None
            if total_float is not None and not (long_quantity > 0 and short_quantity > 0):
                side_float = total_float
            multiplier = _position_multiplier(
                average_price=price,
                settlement_price=settlement,
                quantity=quantity,
                floating_pnl=side_float,
            )
            rows.append(
                {
                    "statement_month": statement_month,
                    "statement_end_date": statement_end,
                    "asset_type": "期货",
                    "contract": contract,
                    "side": side,
                    "quantity": quantity,
                    "average_price": price,
                    "previous_settlement": previous,
                    "settlement_price": settlement,
                    "floating_pnl": side_float,
                    "margin": margin if side == "多" or long_quantity == 0 else 0.0,
                    "multiplier": multiplier,
                    "trade_code": "",
                }
            )

    options = _section_table(
        report_raw,
        "期权持仓汇总",
        ("品种合约", "买持仓", "卖持仓", "今结算价"),
    )
    for record in options.to_dict("records"):
        contract = normalize_contract(record.get("品种合约"), "期权")
        for side, quantity, price in (
            ("多", _integer(record.get("买持仓")), _number(record.get("买均价"))),
            ("空", _integer(record.get("卖持仓")), _number(record.get("卖均价"))),
        ):
            if quantity <= 0:
                continue
            rows.append(
                {
                    "statement_month": statement_month,
                    "statement_end_date": statement_end,
                    "asset_type": "期权",
                    "contract": contract,
                    "side": side,
                    "quantity": quantity,
                    "average_price": price,
                    "previous_settlement": _number(record.get("昨结算价")),
                    "settlement_price": _number(record.get("今结算价")),
                    "floating_pnl": None,
                    "margin": _number(record.get("交易保证金")),
                    "multiplier": None,
                    "trade_code": _text(record.get("交易编码")),
                }
            )
    return pd.DataFrame(rows)


def _trade_multiplier(turnover: float | None, price: float | None, quantity: int) -> float | None:
    if turnover is None or price is None or price <= 0 or quantity <= 0:
        return None
    candidate = abs(turnover) / (price * quantity)
    rounded = round(candidate)
    return float(rounded) if rounded > 0 and abs(candidate - rounded) <= 0.05 else None


def _parse_futures_trades(raw: pd.DataFrame, statement_month: str) -> list[dict[str, object]]:
    table = _table_after_header(raw, ("交易日期", "合约", "成交序号", "开/平"))
    rows: list[dict[str, object]] = []
    for record in table.to_dict("records"):
        trade_date = _date_text(record.get("实际成交日期") or record.get("交易日期"))
        price = _number(record.get("成交价"))
        quantity = _integer(record.get("手数"))
        if not trade_date or price is None or quantity <= 0:
            continue
        turnover = _number(record.get("成交额"))
        rows.append(
            {
                "source": "月结单",
                "statement_month": statement_month,
                "trade_date": trade_date,
                "trade_time": _time_text(record.get("成交时间")),
                "asset_type": "期货",
                "contract": normalize_contract(record.get("合约"), "期货"),
                "broker_trade_id": _text(record.get("成交序号")) or None,
                "buy_sell": _text(record.get("买/卖")),
                "open_close": _text(record.get("开/平")) or "未提供",
                "price": price,
                "quantity": quantity,
                "turnover": turnover,
                "multiplier": _trade_multiplier(turnover, price, quantity),
                "fee": _number(record.get("手续费")) or 0.0,
                "close_pnl": _number(record.get("平仓盈亏")),
                "strategy": "",
                "notes": "",
                "reconciliation_status": "不适用",
            }
        )
    return rows


def _parse_option_trades(raw: pd.DataFrame, statement_month: str) -> list[dict[str, object]]:
    table = _table_after_header(raw, ("日期", "品种合约", "流水号", "买/卖"))
    rows: list[dict[str, object]] = []
    for record in table.to_dict("records"):
        trade_date = _date_text(record.get("成交日期") or record.get("日期"))
        price = _number(record.get("权利金单价"))
        quantity = _integer(record.get("成交量"))
        if not trade_date or price is None or quantity <= 0:
            continue
        raw_turnover = _number(record.get("权利金"))
        turnover = abs(raw_turnover) if raw_turnover is not None else None
        rows.append(
            {
                "source": "月结单",
                "statement_month": statement_month,
                "trade_date": trade_date,
                "trade_time": _time_text(record.get("成交时间")),
                "asset_type": "期权",
                "contract": normalize_contract(record.get("品种合约"), "期权"),
                "broker_trade_id": _text(record.get("流水号")) or None,
                "buy_sell": _text(record.get("买/卖")),
                "open_close": "未提供",
                "price": price,
                "quantity": quantity,
                "turnover": turnover,
                "multiplier": _trade_multiplier(turnover, price, quantity),
                "fee": _number(record.get("手续费")) or 0.0,
                "close_pnl": None,
                "strategy": "",
                "notes": "月结单未提供期权开平与平仓盈亏",
                "reconciliation_status": "不适用",
            }
        )
    return rows


def parse_statement(path: str | os.PathLike[str]) -> StatementPayload:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"结算单不存在：{source}")
    report = _read_sheet(source, "客户交易结算月报")
    month_value = _value_after_label(report, "交易月份")
    month_match = re.search(r"(\d{4})[-/年](\d{1,2})", str(month_value or ""))
    if not month_match:
        raise ValueError("未能从工作簿读取交易月份。")
    workbook_month = f"{int(month_match.group(1)):04d}-{int(month_match.group(2)):02d}"
    statement_month = workbook_month
    account = _parse_account(report, statement_month)
    account["declaration_fee"] = _parse_declaration_fee(report)
    positions = _parse_positions(report, statement_month)
    futures_raw = _read_sheet(source, "成交明细")
    options_raw = _read_sheet(source, "期权成交明细")
    trade_rows = _parse_futures_trades(futures_raw, statement_month)
    trade_rows.extend(_parse_option_trades(options_raw, statement_month))
    trades = pd.DataFrame(trade_rows)

    warnings_list: list[str] = []
    filename_match = STATEMENT_FILE_PATTERN.match(source.name)
    filename_month = filename_match.group(1) if filename_match else ""
    dated_months = set()
    if not trades.empty:
        dated_months = set(
            pd.to_datetime(trades["trade_date"], errors="coerce")
            .dropna()
            .dt.strftime("%Y-%m")
            .tolist()
        )
    if filename_month and filename_month != workbook_month and dated_months == {filename_month}:
        statement_month = filename_month
        account["statement_month"] = statement_month
        account["statement_end_date"] = _statement_end_date(statement_month)
        if not positions.empty:
            positions["statement_month"] = statement_month
            positions["statement_end_date"] = _statement_end_date(statement_month)
        if not trades.empty:
            trades["statement_month"] = statement_month
        warnings_list.append(
            f"工作簿交易月份为 {workbook_month}，但文件名及全部成交日期均为 {filename_month}，已按 {filename_month} 导入"
        )
    statement_fee = float(account.get("monthly_fee") or 0)
    detail_fee = float(pd.to_numeric(trades.get("fee"), errors="coerce").fillna(0).sum()) if not trades.empty else 0.0
    declaration_fee = float(account.get("declaration_fee") or 0)
    reconciled_detail_fee = detail_fee + declaration_fee
    if abs(statement_fee - reconciled_detail_fee) > RECONCILIATION_TOLERANCE:
        warnings_list.append(
            f"账户手续费 {statement_fee:.2f} 与成交明细及申报费 {reconciled_detail_fee:.2f} "
            f"相差 {statement_fee - reconciled_detail_fee:.2f}"
        )
    futures_positions = positions[positions.get("asset_type").eq("期货")] if not positions.empty else positions
    position_float = float(pd.to_numeric(futures_positions.get("floating_pnl"), errors="coerce").fillna(0).sum()) if not futures_positions.empty else 0.0
    account_float = float(account.get("floating_pnl") or 0)
    if abs(account_float - position_float) > RECONCILIATION_TOLERANCE:
        warnings_list.append(
            f"账户浮动盈亏 {account_float:.2f} 与期货持仓汇总 {position_float:.2f} 相差 {account_float - position_float:.2f}"
        )
    position_margin = float(pd.to_numeric(positions.get("margin"), errors="coerce").fillna(0).sum()) if not positions.empty else 0.0
    account_margin = float(account.get("margin") or 0)
    if abs(account_margin - position_margin) > RECONCILIATION_TOLERANCE:
        warnings_list.append(
            f"账户保证金 {account_margin:.2f} 与持仓汇总 {position_margin:.2f} 相差 {account_margin - position_margin:.2f}"
        )
    return StatementPayload(
        statement_month=statement_month,
        statement_end_date=_statement_end_date(statement_month),
        account=account,
        positions=positions,
        trades=trades,
        warnings=warnings_list,
    )


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _insert_statement_payload(conn, path: Path, payload: StatementPayload, file_hash: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    source_file = str(path.resolve())
    conn.execute("DELETE FROM futures_account_monthly WHERE source_file=?", (source_file,))
    conn.execute("DELETE FROM futures_month_end_positions WHERE source_file=?", (source_file,))
    conn.execute(
        "DELETE FROM futures_live_trades WHERE source='月结单' AND source_file=?",
        (source_file,),
    )
    account = payload.account
    conn.execute(
        """
        INSERT OR REPLACE INTO futures_account_monthly (
            statement_month, statement_end_date, previous_balance,
            deposits_withdrawals, monthly_pnl, total_premium, monthly_fee,
            declaration_fee, ending_balance, customer_equity, cash_equity, frozen_funds, margin,
            floating_pnl, available_funds, risk_ratio, additional_margin,
            source_file, imported_at, warnings
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.statement_month,
            payload.statement_end_date,
            account.get("previous_balance"),
            account.get("deposits_withdrawals"),
            account.get("monthly_pnl"),
            account.get("total_premium"),
            account.get("monthly_fee"),
            account.get("declaration_fee"),
            account.get("ending_balance"),
            account.get("customer_equity"),
            account.get("cash_equity"),
            account.get("frozen_funds"),
            account.get("margin"),
            account.get("floating_pnl"),
            account.get("available_funds"),
            account.get("risk_ratio"),
            account.get("additional_margin"),
            source_file,
            now,
            _json_list(payload.warnings),
        ),
    )
    for record in payload.positions.to_dict("records"):
        conn.execute(
            """
            INSERT INTO futures_month_end_positions (
                statement_month, statement_end_date, asset_type, contract, side,
                quantity, average_price, previous_settlement, settlement_price,
                floating_pnl, margin, multiplier, trade_code, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.statement_month,
                payload.statement_end_date,
                record.get("asset_type"),
                record.get("contract"),
                record.get("side"),
                record.get("quantity"),
                record.get("average_price"),
                record.get("previous_settlement"),
                record.get("settlement_price"),
                record.get("floating_pnl"),
                record.get("margin"),
                record.get("multiplier"),
                record.get("trade_code"),
                source_file,
            ),
        )
    for record in payload.trades.to_dict("records"):
        conn.execute(
            """
            INSERT INTO futures_live_trades (
                source, statement_month, trade_date, trade_time, asset_type,
                contract, broker_trade_id, buy_sell, open_close, price, quantity,
                turnover, multiplier, fee, close_pnl, strategy, notes,
                reconciliation_status, source_file, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("source"),
                record.get("statement_month"),
                record.get("trade_date"),
                record.get("trade_time"),
                record.get("asset_type"),
                record.get("contract"),
                record.get("broker_trade_id"),
                record.get("buy_sell"),
                record.get("open_close"),
                record.get("price"),
                record.get("quantity"),
                record.get("turnover"),
                record.get("multiplier"),
                record.get("fee"),
                record.get("close_pnl"),
                record.get("strategy"),
                record.get("notes"),
                record.get("reconciliation_status"),
                source_file,
                now,
            ),
        )
    stat = path.stat()
    conn.execute(
        """
        INSERT INTO futures_statement_imports (
            file_path, file_name, file_size, file_mtime_ns, file_hash,
            statement_month, imported_at, status, warnings, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '成功', ?, '')
        ON CONFLICT(file_path) DO UPDATE SET
            file_name=excluded.file_name,
            file_size=excluded.file_size,
            file_mtime_ns=excluded.file_mtime_ns,
            file_hash=excluded.file_hash,
            statement_month=excluded.statement_month,
            imported_at=excluded.imported_at,
            status='成功',
            warnings=excluded.warnings,
            error_message=''
        """,
        (
            source_file,
            path.name,
            stat.st_size,
            stat.st_mtime_ns,
            file_hash,
            payload.statement_month,
            now,
            _json_list(payload.warnings),
        ),
    )


def _backfill_position_multipliers(conn) -> None:
    rows = conn.execute(
        """
        SELECT asset_type, contract, multiplier
        FROM futures_live_trades
        WHERE multiplier IS NOT NULL AND multiplier > 0
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[float]] = {}
    for asset_type, contract, multiplier in rows:
        grouped.setdefault((asset_type, contract), []).append(float(multiplier))
    for (asset_type, contract), values in grouped.items():
        median = float(pd.Series(values).median())
        conn.execute(
            """
            UPDATE futures_month_end_positions
            SET multiplier=?
            WHERE asset_type=? AND contract=? AND multiplier IS NULL
            """,
            (median, asset_type, contract),
        )


def _manual_match_candidates(conn, manual) -> list[int]:
    if manual["broker_trade_id"]:
        rows = conn.execute(
            """
            SELECT id FROM futures_live_trades
            WHERE source='月结单' AND asset_type=? AND broker_trade_id=?
            """,
            (manual["asset_type"], manual["broker_trade_id"]),
        ).fetchall()
        if rows:
            return [int(row[0]) for row in rows]
    params: list[object] = [
        manual["trade_date"],
        manual["asset_type"],
        manual["contract"],
        manual["buy_sell"],
        manual["price"],
        manual["quantity"],
    ]
    time_clause = ""
    if manual["trade_time"]:
        time_clause = " AND trade_time=?"
        params.append(manual["trade_time"])
    rows = conn.execute(
        f"""
        SELECT id FROM futures_live_trades
        WHERE source='月结单' AND trade_date=? AND asset_type=? AND contract=?
          AND buy_sell=? AND ABS(price - ?) < 0.0000001 AND quantity=?
          {time_clause}
        """,
        tuple(params),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _reconcile_manual_trades(conn) -> None:
    conn.execute(
        """
        UPDATE futures_live_trades
        SET reconciliation_status='手工', matched_statement_trade_id=NULL
        WHERE source='手工'
        """
    )
    conn.row_factory = __import__("sqlite3").Row
    manual_rows = conn.execute(
        "SELECT * FROM futures_live_trades WHERE source='手工' ORDER BY id"
    ).fetchall()
    for manual in manual_rows:
        candidates = _manual_match_candidates(conn, manual)
        if len(candidates) == 1:
            conn.execute(
                """
                UPDATE futures_live_trades
                SET reconciliation_status='已接管', matched_statement_trade_id=?
                WHERE id=?
                """,
                (candidates[0], manual["id"]),
            )
        elif len(candidates) > 1:
            conn.execute(
                """
                UPDATE futures_live_trades
                SET reconciliation_status='待核对', matched_statement_trade_id=NULL
                WHERE id=?
                """,
                (manual["id"],),
            )
    conn.row_factory = None


def sync_statements(
    directory: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
) -> StatementSyncResult:
    init_db()
    files = discover_statement_files(directory)
    imported = skipped = failed = 0
    all_warnings: list[str] = []
    errors: list[str] = []
    with closing(get_conn()) as conn:
        for path in files:
            source_file = str(path.resolve())
            current_hash = _file_hash(path)
            existing = conn.execute(
                "SELECT file_hash, status FROM futures_statement_imports WHERE file_path=?",
                (source_file,),
            ).fetchone()
            if not force and existing and existing[0] == current_hash and existing[1] == "成功":
                skipped += 1
                continue
            try:
                payload = parse_statement(path)
                conn.execute("SAVEPOINT import_statement")
                _insert_statement_payload(conn, path, payload, current_hash)
                conn.execute("RELEASE SAVEPOINT import_statement")
                imported += 1
                all_warnings.extend(f"{path.name}：{item}" for item in payload.warnings)
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT import_statement")
                    conn.execute("RELEASE SAVEPOINT import_statement")
                except Exception:
                    pass
                failed += 1
                errors.append(f"{path.name}：{exc}")
                if existing is None:
                    stat = path.stat()
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO futures_statement_imports (
                            file_path, file_name, file_size, file_mtime_ns, file_hash,
                            statement_month, imported_at, status, warnings, error_message
                        ) VALUES (?, ?, ?, ?, ?, '', ?, '失败', '[]', ?)
                        """,
                        (
                            source_file,
                            path.name,
                            stat.st_size,
                            stat.st_mtime_ns,
                            current_hash,
                            datetime.now().isoformat(timespec="seconds"),
                            str(exc),
                        ),
                    )
            conn.commit()
        _backfill_position_multipliers(conn)
        _reconcile_manual_trades(conn)
        conn.commit()
    return StatementSyncResult(
        scanned=len(files),
        imported=imported,
        skipped=skipped,
        failed=failed,
        warnings=all_warnings,
        errors=errors,
    )


def list_statement_imports() -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            """
            SELECT file_name, statement_month, imported_at, status, warnings, error_message
            FROM futures_statement_imports
            ORDER BY statement_month DESC, file_name DESC
            """,
            conn,
        )


def list_monthly_accounts() -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            "SELECT * FROM futures_account_monthly ORDER BY statement_month",
            conn,
        )


def latest_monthly_account() -> dict[str, object] | None:
    accounts = list_monthly_accounts()
    return None if accounts.empty else accounts.iloc[-1].to_dict()


def list_month_end_positions(statement_month: str | None = None) -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        if statement_month is None:
            row = conn.execute("SELECT MAX(statement_month) FROM futures_account_monthly").fetchone()
            statement_month = row[0] if row else None
        if not statement_month:
            return pd.DataFrame()
        return pd.read_sql_query(
            """
            SELECT * FROM futures_month_end_positions
            WHERE statement_month=?
            ORDER BY asset_type, contract, side
            """,
            conn,
            params=(statement_month,),
        )


def list_futures_live_trades(*, include_taken_over: bool = True) -> pd.DataFrame:
    init_db()
    where = "" if include_taken_over else "WHERE NOT (source='手工' AND reconciliation_status='已接管')"
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            f"""
            SELECT * FROM futures_live_trades
            {where}
            ORDER BY trade_date DESC, COALESCE(trade_time, '') DESC, id DESC
            """,
            conn,
        )


def _effective_manual_trades(trades: pd.DataFrame, statement_end: str) -> pd.DataFrame:
    if trades.empty:
        return trades
    result = trades[
        trades["source"].eq("手工")
        & ~trades["reconciliation_status"].eq("已接管")
        & (pd.to_datetime(trades["trade_date"], errors="coerce") > pd.Timestamp(statement_end))
    ].copy()
    return result.sort_values(["trade_date", "trade_time", "id"], na_position="last")


def _known_multiplier(asset_type: str, contract: str) -> float | None:
    init_db()
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT multiplier FROM futures_live_trades
            WHERE asset_type=? AND contract=? AND multiplier IS NOT NULL AND multiplier > 0
            UNION ALL
            SELECT multiplier FROM futures_month_end_positions
            WHERE asset_type=? AND contract=? AND multiplier IS NOT NULL AND multiplier > 0
            """,
            (asset_type, contract, asset_type, contract),
        ).fetchall()
    if not rows:
        return None
    return float(pd.Series([row[0] for row in rows], dtype="float64").median())


def _apply_manual_to_positions(
    official: pd.DataFrame,
    manual: pd.DataFrame,
    *,
    as_of: object = None,
) -> tuple[pd.DataFrame, dict[int, float]]:
    keys = ["asset_type", "contract", "side"]
    states: dict[tuple[str, str, str], dict[str, object]] = {}
    for record in official.to_dict("records"):
        key = (record["asset_type"], record["contract"], record["side"])
        states[key] = {
            **record,
            "official_quantity": int(record.get("quantity") or 0),
            "quantity": int(record.get("quantity") or 0),
        }
    calculated_close_pnl: dict[int, float] = {}
    cutoff = pd.to_datetime(as_of, errors="coerce") if as_of is not None else pd.NaT
    for trade in manual.to_dict("records"):
        trade_date = pd.to_datetime(trade.get("trade_date"), errors="coerce")
        if pd.notna(cutoff) and (pd.isna(trade_date) or trade_date.normalize() > cutoff.normalize()):
            continue
        asset_type = str(trade["asset_type"])
        contract = str(trade["contract"])
        buy_sell = str(trade["buy_sell"])
        open_close = str(trade["open_close"])
        quantity = int(trade["quantity"])
        price = float(trade["price"])
        multiplier = _number(trade.get("multiplier")) or _known_multiplier(asset_type, contract)
        if open_close == "开":
            side = "多" if buy_sell == "买" else "空"
            key = (asset_type, contract, side)
            state = states.setdefault(
                key,
                {
                    "asset_type": asset_type,
                    "contract": contract,
                    "side": side,
                    "statement_month": official["statement_month"].max() if not official.empty else "",
                    "statement_end_date": official["statement_end_date"].max() if not official.empty else "",
                    "official_quantity": 0,
                    "quantity": 0,
                    "average_price": None,
                    "previous_settlement": None,
                    "settlement_price": None,
                    "floating_pnl": None,
                    "margin": None,
                    "multiplier": multiplier,
                    "trade_code": "",
                },
            )
            current_quantity = int(state.get("quantity") or 0)
            current_average = _number(state.get("average_price")) or 0.0
            new_quantity = current_quantity + quantity
            state["average_price"] = (
                (current_average * current_quantity + price * quantity) / new_quantity
            )
            state["quantity"] = new_quantity
            state["multiplier"] = multiplier or state.get("multiplier")
        elif open_close == "平":
            side = "多" if buy_sell == "卖" else "空"
            key = (asset_type, contract, side)
            state = states.get(key)
            available = int(state.get("quantity") or 0) if state else 0
            if quantity > available:
                raise ValueError(f"{contract} {side}仓最多可平 {available} 手。")
            average = _number(state.get("average_price")) or 0.0
            used_multiplier = multiplier or _number(state.get("multiplier"))
            if used_multiplier:
                pnl = (
                    (price - average) * quantity * used_multiplier
                    if side == "多"
                    else (average - price) * quantity * used_multiplier
                )
                calculated_close_pnl[int(trade["id"])] = float(pnl)
            state["quantity"] = available - quantity
            state["multiplier"] = used_multiplier
        else:
            raise ValueError(f"手工成交开平标志无效：{open_close}")

    rows = []
    for state in states.values():
        official_quantity = int(state.get("official_quantity") or 0)
        estimated_quantity = int(state.get("quantity") or 0)
        if official_quantity <= 0 and estimated_quantity <= 0:
            continue
        state["official_quantity"] = official_quantity
        state["post_month_change"] = estimated_quantity - official_quantity
        state["estimated_quantity"] = estimated_quantity
        rows.append(state)
    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.drop(columns=["quantity"], errors="ignore").sort_values(keys).reset_index(drop=True)
    return result, calculated_close_pnl


def build_estimated_positions(*, as_of: object = None) -> pd.DataFrame:
    account = latest_monthly_account()
    if account is None:
        return pd.DataFrame()
    official = list_month_end_positions(str(account["statement_month"]))
    trades = list_futures_live_trades(include_taken_over=False)
    manual = _effective_manual_trades(trades, str(account["statement_end_date"]))
    positions, _ = _apply_manual_to_positions(official, manual, as_of=as_of)
    return positions


def add_manual_trade(
    *,
    trade_date: object,
    trade_time: str = "",
    asset_type: str,
    contract: str,
    buy_sell: str,
    open_close: str,
    price: float,
    quantity: int,
    turnover: float | None = None,
    fee: float = 0,
    close_pnl: float | None = None,
    broker_trade_id: str = "",
    strategy: str = "",
    notes: str = "",
) -> int:
    init_db()
    account = latest_monthly_account()
    if account is None:
        raise ValueError("请先导入月结单。")
    normalized_date = _date_text(trade_date)
    if not normalized_date:
        raise ValueError("成交日期无效。")
    if pd.Timestamp(normalized_date) <= pd.Timestamp(account["statement_end_date"]):
        raise ValueError(f"手工成交日期必须晚于最新月结单截止日 {account['statement_end_date']}。")
    if asset_type not in ASSET_TYPES:
        raise ValueError("资产类型只能是期货或期权。")
    if buy_sell not in BUY_SELL_VALUES or open_close not in OPEN_CLOSE_VALUES:
        raise ValueError("买卖或开平标志无效。")
    price = float(price)
    quantity = int(quantity)
    fee = float(fee)
    if price <= 0 or quantity <= 0:
        raise ValueError("成交价格和数量必须大于0。")
    if fee < 0:
        raise ValueError("手续费不能为负数。")
    normalized_contract = normalize_contract(contract, asset_type)
    normalized_turnover = None if turnover is None or float(turnover) <= 0 else float(turnover)
    multiplier = _trade_multiplier(normalized_turnover, price, quantity)
    multiplier = multiplier or _known_multiplier(asset_type, normalized_contract)
    if multiplier is None:
        raise ValueError("无法确认合约乘数，请填写成交额或权利金。")
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO futures_live_trades (
                source, statement_month, trade_date, trade_time, asset_type,
                contract, broker_trade_id, buy_sell, open_close, price, quantity,
                turnover, multiplier, fee, close_pnl, strategy, notes,
                reconciliation_status, source_file, created_at
            ) VALUES ('手工', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '手工', NULL, ?)
            """,
            (
                normalized_date,
                _time_text(trade_time),
                asset_type,
                normalized_contract,
                str(broker_trade_id or "").strip() or None,
                buy_sell,
                open_close,
                price,
                quantity,
                normalized_turnover,
                multiplier,
                fee,
                None if close_pnl is None else float(close_pnl),
                str(strategy or "").strip(),
                str(notes or "").strip(),
                now,
            ),
        )
        trade_id = int(cursor.lastrowid)
        conn.commit()
    try:
        build_estimated_positions()
    except Exception:
        with closing(get_conn()) as conn:
            conn.execute("DELETE FROM futures_live_trades WHERE id=? AND source='手工'", (trade_id,))
            conn.commit()
        raise
    return trade_id


def delete_manual_trade(trade_id: int) -> bool:
    init_db()
    account = latest_monthly_account()
    if account is None:
        return False
    official = list_month_end_positions(str(account["statement_month"]))
    all_trades = list_futures_live_trades(include_taken_over=False)
    remaining_manual = _effective_manual_trades(
        all_trades[all_trades["id"].ne(int(trade_id))],
        str(account["statement_end_date"]),
    )
    try:
        _apply_manual_to_positions(official, remaining_manual)
    except Exception:
        raise ValueError("删除后会造成后续平仓超过可用持仓，不能删除该记录。")
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT source FROM futures_live_trades WHERE id=?", (int(trade_id),)
        ).fetchone()
        if row is None:
            return False
        if row[0] != "手工":
            raise ValueError("月结单成交为只读记录，不能删除。")
        conn.execute("DELETE FROM futures_live_trades WHERE id=?", (int(trade_id),))
        conn.commit()
    return True


def load_daily_closes(asset_type: str | None = None, contract: str | None = None) -> pd.DataFrame:
    init_db()
    clauses: list[str] = []
    params: list[object] = []
    if asset_type:
        clauses.append("asset_type=?")
        params.append(asset_type)
    if contract:
        clauses.append("contract=?")
        params.append(normalize_contract(contract, asset_type))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            f"""
            SELECT asset_type, contract, trade_date, close_price,
                   settlement_price, source, settlement_source, updated_at
            FROM futures_daily_closes {where}
            ORDER BY trade_date, asset_type, contract
            """,
            conn,
            params=tuple(params),
        )


def _save_daily_close_frame(asset_type: str, contract: str, data: pd.DataFrame, source: str) -> int:
    if data is None or data.empty:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    saved = 0
    with closing(get_conn()) as conn:
        for record in data.to_dict("records"):
            trade_date = _date_text(record.get("date"))
            close = _number(record.get("close"))
            if not trade_date or close is None or close <= 0:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO futures_daily_closes (
                    asset_type, contract, trade_date, close_price, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (asset_type, contract, trade_date, close, source, now),
            )
            saved += max(cursor.rowcount, 0)
        conn.commit()
    return saved


def _infer_previous_settlement(
    latest_price: object,
    change_pct: object,
    *,
    tick_size: float = 0.1,
) -> float | None:
    latest = _number(latest_price)
    change = _number(change_pct)
    if latest is None or latest <= 0 or change is None or change <= -100:
        return None
    estimate = latest / (1 + change / 100)
    center = round(estimate / tick_size)
    candidates = [tick_size * value for value in range(center - 3, center + 4) if value > 0]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda value: abs(round((latest - value) / value * 100, 2) - change),
    )
    implied = round((latest - best) / best * 100, 2)
    return round(best, 10) if abs(implied - change) <= 0.01 else None


def _save_settlement_price(
    asset_type: str,
    contract: str,
    trade_date: str,
    settlement_price: float,
    source: str,
) -> int:
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            """
            UPDATE futures_daily_closes
            SET settlement_price=?, settlement_source=?, updated_at=?
            WHERE asset_type=? AND contract=? AND trade_date=?
            """,
            (
                settlement_price,
                source,
                datetime.now().isoformat(timespec="seconds"),
                asset_type,
                contract,
                trade_date,
            ),
        )
        conn.commit()
        return max(cursor.rowcount, 0)


def _update_position_settlements(
    positions: pd.DataFrame,
    target_date: str,
    *,
    force: bool = False,
    market_now: datetime | None = None,
) -> dict[str, object]:
    cached = load_daily_closes()
    contracts = positions[["asset_type", "contract"]].drop_duplicates()
    needed: list[tuple[str, str]] = []
    skipped = 0
    for asset_type, contract in contracts.itertuples(index=False, name=None):
        existing = cached[
            cached["asset_type"].eq(asset_type)
            & cached["contract"].eq(contract)
            & cached["trade_date"].eq(target_date)
        ]
        if (
            not force
            and not existing.empty
            and pd.notna(existing.iloc[-1].get("settlement_price"))
        ):
            skipped += 1
        else:
            needed.append((asset_type, contract))
    if not needed:
        return {"updated": 0, "skipped": skipped, "errors": []}

    import akshare as ak

    updated = 0
    errors: list[str] = []
    target_compact = target_date.replace("-", "")
    cffex_contracts = {
        contract
        for asset_type, contract in needed
        if asset_type == "期货" and re.match(r"^(IC|IF|IH|IM|T|TF|TS|TL)\d", contract)
    }
    cffex_daily = pd.DataFrame()
    if cffex_contracts:
        try:
            cffex_daily = ak.get_futures_daily(
                start_date=target_compact,
                end_date=target_compact,
                market="CFFEX",
            )
        except Exception as exc:
            errors.extend(f"{contract}结算价：{exc}" for contract in sorted(cffex_contracts))

    option_chains: dict[str, pd.DataFrame] = {}
    current_day = (market_now or datetime.now()).date()
    for asset_type, contract in needed:
        try:
            settlement = None
            source = ""
            if asset_type == "期货" and contract in cffex_contracts:
                rows = cffex_daily[
                    cffex_daily["symbol"].astype(str).str.upper().eq(contract)
                ] if not cffex_daily.empty else pd.DataFrame()
                if not rows.empty:
                    settlement = _number(rows.iloc[-1].get("settle"))
                    source = "中金所日行情结算价"
            elif asset_type == "期货":
                raw = ak.futures_zh_daily_sina(symbol=contract.lower())
                raw_dates = pd.to_datetime(raw.get("date"), errors="coerce")
                rows = raw[raw_dates.dt.strftime("%Y-%m-%d").eq(target_date)]
                if not rows.empty:
                    settlement = _number(rows.iloc[-1].get("settle"))
                    source = "新浪期货日线结算价"
            else:
                matched = re.match(r"^([A-Z]+\d{4})[CP]\d+$", contract)
                if not matched or not contract.startswith("I"):
                    raise RuntimeError("当前仅支持铁矿石期权结算价。")
                if pd.Timestamp(target_date).date() >= current_day:
                    raise RuntimeError("当日结算价需在下一交易日行情发布后确认。")
                underlying = matched.group(1).lower()
                if underlying not in option_chains:
                    option_chains[underlying] = ak.option_commodity_contract_table_sina(
                        symbol="铁矿石期权",
                        contract=underlying,
                    )
                chain = option_chains[underlying]
                rows = chain[
                    chain["看跌合约-看跌期权合约"]
                    .astype(str)
                    .str.upper()
                    .eq(contract)
                ]
                if not rows.empty:
                    row = rows.iloc[-1]
                    settlement = _infer_previous_settlement(
                        row.get("看跌合约-最新价"),
                        row.get("看跌合约-涨跌"),
                    )
                    source = "新浪期权链反推昨结算价"
            if settlement is None or settlement <= 0:
                raise RuntimeError("未取得有效结算价。")
            updated += _save_settlement_price(
                asset_type,
                contract,
                target_date,
                settlement,
                source,
            )
        except Exception as exc:
            message = f"{contract}结算价：{exc}"
            if message not in errors:
                errors.append(message)
    return {"updated": updated, "skipped": skipped, "errors": errors}


def update_position_daily_closes(
    *,
    api_key: str = "",
    force: bool = False,
    market_now: datetime | None = None,
) -> dict[str, object]:
    positions = build_estimated_positions()
    if positions.empty:
        return {"updated": 0, "skipped": 0, "errors": [], "target_date": None}
    positions = positions[
        pd.to_numeric(positions["estimated_quantity"], errors="coerce")
        .fillna(0)
        .gt(0)
    ]
    if positions.empty:
        return {"updated": 0, "skipped": 0, "errors": [], "target_date": None}
    target = completed_futures_daily_cutoff(market_now).strftime("%Y-%m-%d")
    cached = load_daily_closes()
    updated = skipped = 0
    errors: list[str] = []
    contracts = positions[["asset_type", "contract"]].drop_duplicates()
    for asset_type, contract in contracts.itertuples(index=False, name=None):
        existing = cached[
            cached["asset_type"].eq(asset_type) & cached["contract"].eq(contract)
        ]
        latest = existing["trade_date"].max() if not existing.empty else None
        if not force and latest is not None and str(latest) >= target:
            skipped += 1
            continue
        try:
            if asset_type == "期权":
                option_symbol = normalize_option_symbol(contract)
                data, source, is_chain = fetch_option_from_akshare(
                    option_symbol,
                    "1d",
                    5000,
                    prefer_realtime_snapshot=False,
                    market_now=market_now,
                )
                if is_chain:
                    raise RuntimeError("期权行情返回了期权链而不是合约日线。")
            else:
                data = fetch_futures_daily(
                    contract,
                    api_key=api_key,
                    prefer_realtime_snapshot=False,
                    market_now=market_now,
                )
                source = "TickFlow/AkShare期货日线"
            updated += _save_daily_close_frame(asset_type, contract, data, source)
        except Exception as exc:
            errors.append(f"{contract}：{exc}")
    settlement_result = _update_position_settlements(
        positions,
        target,
        force=force,
        market_now=market_now,
    )
    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "settlement_updated": settlement_result["updated"],
        "settlement_skipped": settlement_result["skipped"],
        "settlement_errors": settlement_result["errors"],
        "target_date": target,
    }


def _position_prices_for_date(
    positions: pd.DataFrame,
    valuation_date: str,
    *,
    price_column: str = "close_price",
) -> dict[tuple[str, str], float]:
    cached = load_daily_closes()
    prices: dict[tuple[str, str], float] = {}
    for asset_type, contract in positions[["asset_type", "contract"]].drop_duplicates().itertuples(index=False, name=None):
        rows = cached[
            cached["asset_type"].eq(asset_type)
            & cached["contract"].eq(contract)
            & cached["trade_date"].eq(valuation_date)
        ]
        if not rows.empty and pd.notna(rows.iloc[-1].get(price_column)):
            prices[(asset_type, contract)] = float(rows.iloc[-1][price_column])
    return prices


def _option_cashflow(trades: pd.DataFrame) -> pd.Series:
    turnover = pd.to_numeric(trades["turnover"], errors="coerce").fillna(
        pd.to_numeric(trades["price"], errors="coerce")
        * pd.to_numeric(trades["quantity"], errors="coerce")
        * pd.to_numeric(trades["multiplier"], errors="coerce")
    )
    return turnover.where(trades["buy_sell"].eq("卖"), -turnover)


def _trades_with_calculated_manual_close_pnl() -> pd.DataFrame:
    account = latest_monthly_account()
    trades = list_futures_live_trades(include_taken_over=False)
    if account is None or trades.empty:
        return trades
    official = list_month_end_positions(str(account["statement_month"]))
    manual = _effective_manual_trades(trades, str(account["statement_end_date"]))
    _, calculated = _apply_manual_to_positions(official, manual)
    for trade_id, value in calculated.items():
        mask = trades["id"].eq(trade_id) & trades["close_pnl"].isna()
        trades.loc[mask, "close_pnl"] = value
    return trades


def build_current_position_pnl(
    *,
    as_of: object = None,
    valuation_mode: str = "close",
) -> pd.DataFrame:
    if valuation_mode not in {"close", "settlement"}:
        raise ValueError("估值口径必须是 close 或 settlement。")
    price_column = "settlement_price" if valuation_mode == "settlement" else "close_price"
    account = latest_monthly_account()
    positions = build_estimated_positions(as_of=as_of)
    if account is None or positions.empty:
        return pd.DataFrame()
    effective_trades = _trades_with_calculated_manual_close_pnl()
    statement_end = str(account["statement_end_date"])
    valuation_date = statement_end
    requested_date = _date_text(as_of) if as_of is not None else ""
    cached = load_daily_closes()
    active_contracts = positions.loc[
        pd.to_numeric(positions["estimated_quantity"], errors="coerce").fillna(0).gt(0),
        ["asset_type", "contract"],
    ].drop_duplicates()
    if requested_date and requested_date > statement_end:
        valuation_date = requested_date
    elif not cached.empty and not active_contracts.empty:
        common_dates: set[str] | None = None
        for asset_type, contract in active_contracts.itertuples(index=False, name=None):
            dates = set(
                cached.loc[
                    cached["asset_type"].eq(asset_type)
                    & cached["contract"].eq(contract)
                    & cached["trade_date"].gt(statement_end),
                    ["trade_date", price_column],
                ].astype(str)
                .loc[lambda frame: frame[price_column].ne("nan"), "trade_date"]
            )
            common_dates = dates if common_dates is None else common_dates & dates
        if common_dates:
            valuation_date = max(common_dates)
    prices = _position_prices_for_date(
        positions,
        valuation_date,
        price_column=price_column,
    )
    effective_trades = effective_trades[
        pd.to_datetime(effective_trades["trade_date"], errors="coerce")
        <= pd.Timestamp(valuation_date)
    ].copy()
    rows: list[dict[str, object]] = []
    for position in positions.to_dict("records"):
        asset_type = str(position["asset_type"])
        contract = str(position["contract"])
        side = str(position["side"])
        quantity = int(position["estimated_quantity"])
        average = _number(position.get("average_price"))
        multiplier = _number(position.get("multiplier")) or _known_multiplier(asset_type, contract)
        latest = prices.get((asset_type, contract))
        if latest is None and valuation_date == statement_end:
            latest = _number(position.get("settlement_price"))
            row_date = statement_end
        elif latest is None:
            row_date = valuation_date
        else:
            contract_closes = cached[
                cached["asset_type"].eq(asset_type)
                & cached["contract"].eq(contract)
                & cached["trade_date"].eq(valuation_date)
            ]
            row_date = valuation_date
        previous = None
        contract_closes = cached[
            cached["asset_type"].eq(asset_type)
            & cached["contract"].eq(contract)
            & (cached["trade_date"] < row_date)
            & cached[price_column].notna()
        ]
        if not contract_closes.empty:
            previous = float(contract_closes.iloc[-1][price_column])
        elif row_date == statement_end:
            previous = _number(position.get("previous_settlement"))
        floating = None
        daily = None
        if quantity > 0 and average is not None and latest is not None and multiplier is not None:
            direction = 1 if side == "多" else -1
            floating = (latest - average) * quantity * multiplier * direction
            if previous is not None:
                daily = (latest - previous) * quantity * multiplier * direction
        contract_trades = effective_trades[
            effective_trades["asset_type"].eq(asset_type)
            & effective_trades["contract"].eq(contract)
        ].copy()
        fee = float(pd.to_numeric(contract_trades.get("fee"), errors="coerce").fillna(0).sum()) if not contract_trades.empty else 0.0
        if asset_type == "期货":
            realized = float(pd.to_numeric(contract_trades.get("close_pnl"), errors="coerce").fillna(0).sum()) if not contract_trades.empty else 0.0
        else:
            cashflow = float(_option_cashflow(contract_trades).sum()) if not contract_trades.empty else 0.0
            open_basis = 0.0
            if quantity > 0 and average is not None and multiplier is not None:
                open_basis = average * quantity * multiplier * (1 if side == "空" else -1)
            realized = cashflow - open_basis
        rows.append(
            {
                "asset_type": asset_type,
                "contract": contract,
                "side": side,
                "official_quantity": int(position["official_quantity"]),
                "post_month_change": int(position["post_month_change"]),
                "estimated_quantity": quantity,
                "average_price": average,
                "latest_close": latest,
                "valuation_price": latest,
                "valuation_mode": valuation_mode,
                "valuation_date": row_date,
                "multiplier": multiplier,
                "daily_pnl": daily,
                "realized_pnl": realized,
                "floating_pnl": floating,
                "fee": fee,
                "net_pnl": None if floating is None else realized + floating - fee,
            }
        )
    return pd.DataFrame(rows)


def build_contract_pnl_history(
    *,
    as_of: object = None,
    valuation_mode: str = "close",
) -> pd.DataFrame:
    trades = _trades_with_calculated_manual_close_pnl()
    cutoff = pd.to_datetime(as_of, errors="coerce") if as_of is not None else pd.NaT
    if pd.notna(cutoff) and not trades.empty:
        trades = trades[
            pd.to_datetime(trades["trade_date"], errors="coerce") <= pd.Timestamp(cutoff)
        ].copy()
    current = build_current_position_pnl(
        as_of=as_of,
        valuation_mode=valuation_mode,
    )
    if trades.empty and current.empty:
        return pd.DataFrame()
    current_lookup = {
        (row.asset_type, row.contract, row.side): row
        for row in current.itertuples(index=False)
    }
    rows: list[dict[str, object]] = []
    for (asset_type, contract), group in trades.groupby(["asset_type", "contract"], dropna=False):
        first_date = group["trade_date"].min()
        last_date = group["trade_date"].max()
        fee = float(pd.to_numeric(group["fee"], errors="coerce").fillna(0).sum())
        has_unknown_open_close = group["open_close"].eq("未提供").any()
        open_quantity = (
            pd.NA
            if has_unknown_open_close
            else int(pd.to_numeric(group.loc[group["open_close"].eq("开"), "quantity"], errors="coerce").fillna(0).sum())
        )
        close_quantity = (
            pd.NA
            if has_unknown_open_close
            else int(pd.to_numeric(group.loc[group["open_close"].eq("平"), "quantity"], errors="coerce").fillna(0).sum())
        )
        matching_current = [row for key, row in current_lookup.items() if key[:2] == (asset_type, contract)]
        if asset_type == "期货":
            realized = float(pd.to_numeric(group["close_pnl"], errors="coerce").fillna(0).sum())
        else:
            cashflow = float(_option_cashflow(group).sum())
            open_basis = 0.0
            for row in matching_current:
                if row.estimated_quantity and pd.notna(row.average_price) and pd.notna(row.multiplier):
                    open_basis += (
                        float(row.average_price)
                        * int(row.estimated_quantity)
                        * float(row.multiplier)
                        * (1 if row.side == "空" else -1)
                    )
            realized = cashflow - open_basis
        floating_values = [row.floating_pnl for row in matching_current if pd.notna(row.floating_pnl)]
        floating = float(sum(floating_values)) if floating_values else (0.0 if not matching_current else None)
        long_quantity = sum(int(row.estimated_quantity) for row in matching_current if row.side == "多")
        short_quantity = sum(int(row.estimated_quantity) for row in matching_current if row.side == "空")
        valuation_dates = [str(row.valuation_date) for row in matching_current if row.valuation_date]
        rows.append(
            {
                "asset_type": asset_type,
                "contract": contract,
                "status": "持仓中" if long_quantity or short_quantity else "已平仓",
                "first_trade_date": first_date,
                "last_trade_date": last_date,
                "open_quantity": open_quantity,
                "close_quantity": close_quantity,
                "long_quantity": long_quantity,
                "short_quantity": short_quantity,
                "realized_pnl": realized,
                "floating_pnl": floating,
                "fee": fee,
                "net_pnl": None if floating is None else realized + floating - fee,
                "valuation_date": max(valuation_dates) if valuation_dates else "",
            }
        )
    return pd.DataFrame(rows).sort_values(["asset_type", "status", "contract"]).reset_index(drop=True)


def summarize_futures_live_pnl(
    *,
    as_of: object = None,
    valuation_mode: str = "close",
    include_declaration_fee: bool = True,
) -> dict[str, object]:
    current = build_current_position_pnl(
        as_of=as_of,
        valuation_mode=valuation_mode,
    )
    history = build_contract_pnl_history(
        as_of=as_of,
        valuation_mode=valuation_mode,
    )
    if history.empty:
        return {
            "daily_pnl": None,
            "realized_pnl": 0.0,
            "floating_pnl": 0.0,
            "fee": 0.0,
            "net_pnl": 0.0,
            "valuation_date": None,
        }
    accounts = list_monthly_accounts()
    cutoff = pd.to_datetime(as_of, errors="coerce") if as_of is not None else pd.NaT
    if pd.notna(cutoff) and not accounts.empty:
        accounts = accounts[
            pd.to_datetime(accounts["statement_end_date"], errors="coerce") <= pd.Timestamp(cutoff)
        ].copy()
    official_fee = float(pd.to_numeric(accounts.get("monthly_fee"), errors="coerce").fillna(0).sum()) if not accounts.empty else 0.0
    declaration_fee = float(pd.to_numeric(accounts.get("declaration_fee"), errors="coerce").fillna(0).sum()) if not accounts.empty else 0.0
    account = latest_monthly_account()
    manual_fee = 0.0
    if account is not None:
        trades = list_futures_live_trades(include_taken_over=False)
        manual = _effective_manual_trades(trades, str(account["statement_end_date"]))
        if pd.notna(cutoff) and not manual.empty:
            manual = manual[
                pd.to_datetime(manual["trade_date"], errors="coerce") <= pd.Timestamp(cutoff)
            ]
        manual_fee = float(pd.to_numeric(manual.get("fee"), errors="coerce").fillna(0).sum()) if not manual.empty else 0.0
    total_fee = official_fee + manual_fee
    if not include_declaration_fee:
        total_fee -= declaration_fee
    realized = float(pd.to_numeric(history["realized_pnl"], errors="coerce").fillna(0).sum())
    floating = float(pd.to_numeric(history["floating_pnl"], errors="coerce").fillna(0).sum())
    return {
        "daily_pnl": float(pd.to_numeric(current.get("daily_pnl"), errors="coerce").sum(min_count=1)) if not current.empty else None,
        "realized_pnl": realized,
        "floating_pnl": floating,
        "fee": total_fee,
        "declaration_fee": declaration_fee,
        "unallocated_fee": total_fee
        - float(pd.to_numeric(history["fee"], errors="coerce").fillna(0).sum())
        - (declaration_fee if include_declaration_fee else 0.0),
        "net_pnl": realized + floating - total_fee,
        "valuation_date": current["valuation_date"].max() if not current.empty else None,
        "valuation_mode": valuation_mode,
    }


def build_daily_account_pnl() -> pd.DataFrame:
    accounts = list_monthly_accounts()
    if accounts.empty:
        return pd.DataFrame()
    trades = list_futures_live_trades(include_taken_over=False)
    rows: list[dict[str, object]] = []
    cumulative_fees = 0.0
    cumulative_futures_realized = 0.0
    cumulative_option_cashflow = 0.0
    for account in accounts.to_dict("records"):
        month = str(account["statement_month"])
        month_trades = trades[trades["statement_month"].eq(month)]
        cumulative_fees += float(account.get("monthly_fee") or 0)
        futures_month = month_trades[month_trades["asset_type"].eq("期货")] if not month_trades.empty else month_trades
        options_month = month_trades[month_trades["asset_type"].eq("期权")] if not month_trades.empty else month_trades
        cumulative_futures_realized += float(pd.to_numeric(futures_month.get("close_pnl"), errors="coerce").fillna(0).sum()) if not futures_month.empty else 0.0
        cumulative_option_cashflow += float(_option_cashflow(options_month).sum()) if not options_month.empty else 0.0
        positions = list_month_end_positions(month)
        option_market_value = 0.0
        option_open_basis = 0.0
        for position in positions[positions["asset_type"].eq("期权")].to_dict("records"):
            multiplier = _number(position.get("multiplier")) or _known_multiplier("期权", position["contract"])
            settlement = _number(position.get("settlement_price"))
            average = _number(position.get("average_price"))
            if multiplier and settlement is not None and average is not None:
                sign = 1 if position["side"] == "多" else -1
                option_market_value += sign * settlement * int(position["quantity"]) * multiplier
                option_open_basis += -sign * average * int(position["quantity"]) * multiplier
        realized = cumulative_futures_realized + cumulative_option_cashflow - option_open_basis
        floating = float(account.get("floating_pnl") or 0) + option_open_basis + option_market_value
        net = realized + floating - cumulative_fees
        rows.append(
            {
                "date": account["statement_end_date"],
                "realized_pnl": realized,
                "floating_pnl": floating,
                "fee": cumulative_fees,
                "net_pnl": net,
                "source": "月结单",
            }
        )
    latest_account = accounts.iloc[-1].to_dict()
    latest_positions = build_estimated_positions()
    active_contracts = latest_positions.loc[
        pd.to_numeric(latest_positions.get("estimated_quantity"), errors="coerce").fillna(0).gt(0),
        ["asset_type", "contract"],
    ].drop_duplicates() if not latest_positions.empty else pd.DataFrame()
    closes = load_daily_closes()
    common_dates: set[str] | None = None
    if not active_contracts.empty and not closes.empty:
        for asset_type, contract in active_contracts.itertuples(index=False, name=None):
            dates = set(
                closes.loc[
                    closes["asset_type"].eq(asset_type)
                    & closes["contract"].eq(contract)
                    & closes["trade_date"].gt(str(latest_account["statement_end_date"])),
                    "trade_date",
                ].astype(str)
            )
            common_dates = dates if common_dates is None else common_dates & dates
    for valuation_date in sorted(common_dates or set()):
        current = summarize_futures_live_pnl(as_of=valuation_date)
        if current.get("valuation_date") != valuation_date:
            continue
        rows.append(
            {
                "date": valuation_date,
                "realized_pnl": current["realized_pnl"],
                "floating_pnl": current["floating_pnl"],
                "fee": current["fee"],
                "net_pnl": current["net_pnl"],
                "source": "正式收盘估值",
            }
        )
    result = pd.DataFrame(rows).drop_duplicates("date", keep="last").sort_values("date")
    result["daily_pnl"] = pd.to_numeric(result["net_pnl"], errors="coerce").diff()
    return result.reset_index(drop=True)
