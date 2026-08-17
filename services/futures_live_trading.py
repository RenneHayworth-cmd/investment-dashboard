from __future__ import annotations

import calendar
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
from services.market_calendar import get_market_window, is_market_holiday


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


def _parse_statement_cash_flows(raw: pd.DataFrame, statement_month: str) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    external = _section_table(
        raw,
        "期货期权账户出入金明细（单位：人民币）",
        ("发生日期", "入金", "出金"),
    )
    for sequence, record in enumerate(external.to_dict("records"), start=1):
        flow_date = _date_text(record.get("发生日期"))
        if not flow_date:
            continue
        for entry_type in CASH_FLOW_TYPES:
            amount = _number(record.get(entry_type))
            if amount is None or abs(amount) <= 1e-12:
                continue
            rows.append(
                {
                    "source": "月结单",
                    "statement_month": statement_month,
                    "flow_date": flow_date,
                    "entry_type": entry_type,
                    "amount": abs(amount),
                    "notes": _text(record.get("摘要")),
                    "reconciliation_status": "不适用",
                    "source_row": f"cash-{sequence}-{entry_type}",
                }
            )

    title_row = None
    header_row = None
    header_columns: dict[str, int] = {}
    for index in range(len(raw.index)):
        labels = [_clean_label(value) for value in raw.iloc[index].tolist()]
        if "其它资金明细（单位：人民币）" in labels:
            title_row = index
            continue
        if title_row is not None and index > title_row and all(
            label in labels for label in ("发生日期", "类型", "金额")
        ):
            header_row = index
            header_columns = {
                label: labels.index(label)
                for label in ("发生日期", "类型", "金额")
            }
            break
    if header_row is not None:
        fee_sequence = 0
        for index in range(header_row + 1, len(raw.index)):
            row = raw.iloc[index].tolist()
            if not any(pd.notna(value) and str(value).strip() for value in row):
                break
            first = _clean_label(row[header_columns["发生日期"]])
            if "汇总" in first or first in {"期货持仓明细", "期权持仓明细"}:
                break
            flow_date = _date_text(row[header_columns["发生日期"]])
            fee_type = _clean_label(row[header_columns["类型"]])
            amount = _number(row[header_columns["金额"]])
            if not flow_date or amount is None or abs(amount) <= 1e-12:
                continue
            if "交易手续费" in fee_type:
                continue
            fee_sequence += 1
            rows.append(
                {
                    "source": "月结单",
                    "statement_month": statement_month,
                    "flow_date": flow_date,
                    "entry_type": "申报费" if "申报费" in fee_type else "账户费用",
                    "amount": abs(amount),
                    "notes": fee_type,
                    "reconciliation_status": "不适用",
                    "source_row": f"fee-{fee_sequence}",
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "source",
            "statement_month",
            "flow_date",
            "entry_type",
            "amount",
            "notes",
            "reconciliation_status",
            "source_row",
        ],
    )


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
    cash_flows = _parse_statement_cash_flows(report, statement_month)
    declaration_rows = cash_flows[cash_flows["entry_type"].eq("申报费")]
    account["declaration_fee"] = float(
        pd.to_numeric(declaration_rows["amount"], errors="coerce").fillna(0).sum()
    )
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
        if not cash_flows.empty:
            cash_flows["statement_month"] = statement_month
        warnings_list.append(
            f"工作簿交易月份为 {workbook_month}，但文件名及全部成交日期均为 {filename_month}，已按 {filename_month} 导入"
        )
    statement_fee = float(account.get("monthly_fee") or 0)
    detail_fee = float(pd.to_numeric(trades.get("fee"), errors="coerce").fillna(0).sum()) if not trades.empty else 0.0
    account_fee_rows = cash_flows[
        cash_flows["entry_type"].isin(["申报费", "账户费用"])
    ]
    account_level_fee = float(
        pd.to_numeric(account_fee_rows["amount"], errors="coerce").fillna(0).sum()
    )
    reconciled_detail_fee = detail_fee + account_level_fee
    if abs(statement_fee - reconciled_detail_fee) > RECONCILIATION_TOLERANCE:
        warnings_list.append(
            f"账户手续费 {statement_fee:.2f} 与成交明细及申报费 {reconciled_detail_fee:.2f} "
            f"相差 {statement_fee - reconciled_detail_fee:.2f}"
        )
    external_rows = cash_flows[cash_flows["entry_type"].isin(CASH_FLOW_TYPES)]
    parsed_net_flow = float(
        pd.to_numeric(external_rows.loc[external_rows["entry_type"].eq("入金"), "amount"], errors="coerce")
        .fillna(0)
        .sum()
        - pd.to_numeric(external_rows.loc[external_rows["entry_type"].eq("出金"), "amount"], errors="coerce")
        .fillna(0)
        .sum()
    )
    official_net_flow = float(account.get("deposits_withdrawals") or 0)
    if abs(parsed_net_flow - official_net_flow) > RECONCILIATION_TOLERANCE:
        warnings_list.append(
            f"逐日资金流水净额 {parsed_net_flow:.2f} 与当月存取合计 "
            f"{official_net_flow:.2f} 相差 {official_net_flow - parsed_net_flow:.2f}"
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
        cash_flows=cash_flows,
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
    conn.execute(
        "DELETE FROM futures_cash_flows WHERE source='月结单' AND source_file=?",
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
    for record in payload.cash_flows.to_dict("records"):
        source_key = f"{source_file}|{record.get('source_row')}"
        conn.execute(
            """
            INSERT INTO futures_cash_flows (
                source, statement_month, flow_date, entry_type, amount, notes,
                reconciliation_status, matched_statement_flow_id, source_key,
                source_file, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                record.get("source"),
                record.get("statement_month"),
                record.get("flow_date"),
                record.get("entry_type"),
                record.get("amount"),
                record.get("notes"),
                record.get("reconciliation_status"),
                source_key,
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


def _reconcile_manual_cash_flows(conn) -> None:
    conn.execute(
        """
        UPDATE futures_cash_flows
        SET reconciliation_status='手工', matched_statement_flow_id=NULL
        WHERE source='手工'
        """
    )
    latest_row = conn.execute(
        "SELECT MAX(statement_end_date) FROM futures_account_monthly"
    ).fetchone()
    latest_end = str(latest_row[0] or "") if latest_row else ""
    conn.row_factory = __import__("sqlite3").Row
    manual_rows = conn.execute(
        "SELECT * FROM futures_cash_flows WHERE source='手工' ORDER BY id"
    ).fetchall()
    for manual in manual_rows:
        if latest_end and str(manual["flow_date"]) <= latest_end:
            candidates = conn.execute(
                """
                SELECT id FROM futures_cash_flows
                WHERE source='月结单' AND flow_date=? AND entry_type=?
                  AND ABS(amount - ?) < 0.0000001
                """,
                (manual["flow_date"], manual["entry_type"], manual["amount"]),
            ).fetchall()
            if len(candidates) == 1:
                conn.execute(
                    """
                    UPDATE futures_cash_flows
                    SET reconciliation_status='已接管', matched_statement_flow_id=?
                    WHERE id=?
                    """,
                    (int(candidates[0][0]), int(manual["id"])),
                )
            else:
                conn.execute(
                    """
                    UPDATE futures_cash_flows
                    SET reconciliation_status='待核对', matched_statement_flow_id=NULL
                    WHERE id=?
                    """,
                    (int(manual["id"]),),
                )
    conn.row_factory = None


def _reconcile_option_expiry_events(conn) -> None:
    latest_row = conn.execute(
        "SELECT MAX(statement_end_date) FROM futures_account_monthly"
    ).fetchone()
    latest_end = str(latest_row[0] or "") if latest_row else ""
    if not latest_end:
        return
    conn.row_factory = __import__("sqlite3").Row
    events = conn.execute(
        """
        SELECT * FROM futures_option_expiry_events
        WHERE source='手工' AND event_date <= ?
        ORDER BY id
        """,
        (latest_end,),
    ).fetchall()
    for event in events:
        month = str(event["event_date"])[:7]
        option_quantity = conn.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM futures_month_end_positions
            WHERE statement_month=? AND asset_type='期权' AND contract=?
            """,
            (month, event["option_contract"]),
        ).fetchone()[0]
        reconciled = int(option_quantity or 0) == 0
        if reconciled and event["outcome"] == "履约":
            futures_quantity = conn.execute(
                """
                SELECT COALESCE(SUM(quantity), 0)
                FROM futures_month_end_positions
                WHERE statement_month=? AND asset_type='期货'
                  AND contract=? AND side=?
                """,
                (month, event["underlying_contract"], event["futures_side"]),
            ).fetchone()[0]
            reconciled = int(futures_quantity or 0) >= int(event["quantity"])
        conn.execute(
            """
            UPDATE futures_option_expiry_events
            SET reconciliation_status=?
            WHERE id=?
            """,
            ("已接管" if reconciled else "待核对", int(event["id"])),
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
        _reconcile_manual_cash_flows(conn)
        _reconcile_option_expiry_events(conn)
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


def list_futures_cash_flows(*, include_taken_over: bool = True) -> pd.DataFrame:
    init_db()
    where = (
        ""
        if include_taken_over
        else "WHERE NOT (source='手工' AND reconciliation_status='已接管')"
    )
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            f"""
            SELECT * FROM futures_cash_flows
            {where}
            ORDER BY flow_date DESC, id DESC
            """,
            conn,
        )


def _effective_cash_flows(*, as_of: object = None) -> pd.DataFrame:
    rows = list_futures_cash_flows(include_taken_over=False)
    if rows.empty:
        return rows
    account = latest_monthly_account()
    latest_end = str(account["statement_end_date"]) if account else ""
    official = rows[rows["source"].eq("月结单")].copy()
    manual = rows[
        rows["source"].eq("手工")
        & ~rows["reconciliation_status"].eq("已接管")
        & (rows["flow_date"].astype(str) > latest_end)
    ].copy()
    result = pd.concat([official, manual], ignore_index=True)
    cutoff = _date_text(as_of) if as_of is not None else ""
    if cutoff:
        result = result[result["flow_date"].astype(str) <= cutoff]
    return result.sort_values(["flow_date", "id"]).reset_index(drop=True)


def add_manual_cash_flow(
    *,
    flow_date: object,
    entry_type: str,
    amount: float,
    notes: str = "",
) -> int:
    init_db()
    account = latest_monthly_account()
    if account is None:
        raise ValueError("请先导入月结单。")
    normalized_date = _date_text(flow_date)
    if not normalized_date:
        raise ValueError("资金流水日期无效。")
    if pd.Timestamp(normalized_date) <= pd.Timestamp(account["statement_end_date"]):
        raise ValueError(
            f"手工资金流水日期必须晚于最新月结单截止日 {account['statement_end_date']}。"
        )
    if entry_type not in CASH_FLOW_TYPES:
        raise ValueError("资金流水类型只能是入金或出金。")
    normalized_amount = float(amount)
    if normalized_amount <= 0:
        raise ValueError("资金流水金额必须大于0。")
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO futures_cash_flows (
                source, statement_month, flow_date, entry_type, amount, notes,
                reconciliation_status, matched_statement_flow_id, source_key,
                source_file, created_at
            ) VALUES ('手工', NULL, ?, ?, ?, ?, '手工', NULL, NULL, NULL, ?)
            """,
            (
                normalized_date,
                entry_type,
                normalized_amount,
                str(notes or "").strip(),
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def delete_manual_cash_flow(flow_id: int) -> bool:
    init_db()
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT source FROM futures_cash_flows WHERE id=?", (int(flow_id),)
        ).fetchone()
        if row is None:
            return False
        if row[0] != "手工":
            raise ValueError("月结单资金流水为只读记录，不能删除。")
        conn.execute("DELETE FROM futures_cash_flows WHERE id=?", (int(flow_id),))
        conn.commit()
    return True


def list_futures_daily_pnl_overrides() -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            """
            SELECT * FROM futures_daily_pnl_overrides
            ORDER BY trade_date DESC, id DESC
            """,
            conn,
        )


def add_manual_daily_pnl(
    *,
    trade_date: object,
    pnl_amount: float,
    notes: str = "",
) -> int:
    init_db()
    if latest_monthly_account() is None:
        raise ValueError("请先导入月结单。")
    normalized_date = _date_text(trade_date)
    if not normalized_date:
        raise ValueError("交易日期无效。")
    latest_completed = completed_futures_daily_cutoff().strftime("%Y-%m-%d")
    if normalized_date > latest_completed:
        raise ValueError(f"只能补录不晚于 {latest_completed} 的已完成交易日。")
    normalized_pnl = pd.to_numeric(pnl_amount, errors="coerce")
    if pd.isna(normalized_pnl):
        raise ValueError("当日盈亏金额无效。")
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO futures_daily_pnl_overrides (
                trade_date, pnl_amount, source, notes,
                reconciliation_status, formal_pnl, difference, resolution,
                created_at, updated_at
            ) VALUES (?, ?, '同花顺手工', ?, '待确认', NULL, NULL, NULL, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                pnl_amount=excluded.pnl_amount,
                notes=excluded.notes,
                reconciliation_status='待确认',
                formal_pnl=NULL,
                difference=NULL,
                resolution=NULL,
                updated_at=excluded.updated_at
            """,
            (
                normalized_date,
                float(normalized_pnl),
                str(notes or "").strip(),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM futures_daily_pnl_overrides WHERE trade_date=?",
            (normalized_date,),
        ).fetchone()
        conn.commit()
    return int(row[0])


def delete_manual_daily_pnl(record_id: int) -> bool:
    init_db()
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            "DELETE FROM futures_daily_pnl_overrides WHERE id=?",
            (int(record_id),),
        )
        conn.commit()
        return cursor.rowcount > 0


def resolve_manual_daily_pnl(record_id: int, resolution: str) -> bool:
    if resolution not in DAILY_PNL_RESOLUTIONS:
        raise ValueError("核对结果只能是采用手工或采用正式。")
    init_db()
    with closing(get_conn()) as conn:
        row = conn.execute(
            """
            SELECT formal_pnl FROM futures_daily_pnl_overrides
            WHERE id=?
            """,
            (int(record_id),),
        ).fetchone()
        if row is None:
            return False
        if row[0] is None:
            raise ValueError("正式结算盈亏尚未生成，暂不能确认差异。")
        conn.execute(
            """
            UPDATE futures_daily_pnl_overrides
            SET reconciliation_status=?, resolution=?, updated_at=?
            WHERE id=?
            """,
            (
                resolution,
                resolution,
                datetime.now().isoformat(timespec="seconds"),
                int(record_id),
            ),
        )
        conn.commit()
    return True


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


def _build_estimated_positions_base(*, as_of: object = None) -> pd.DataFrame:
    account = latest_monthly_account()
    if account is None:
        return pd.DataFrame()
    official = list_month_end_positions(str(account["statement_month"]))
    trades = list_futures_live_trades(include_taken_over=False)
    manual = _effective_manual_trades(trades, str(account["statement_end_date"]))
    positions, _ = _apply_manual_to_positions(official, manual, as_of=as_of)
    return positions


def _option_contract_parts(contract: str) -> dict[str, object] | None:
    matched = re.match(r"^([A-Z]+)(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$", contract)
    if not matched:
        return None
    product, year_text, month_text, option_type, strike_text = matched.groups()
    return {
        "product": product,
        "year": 2000 + int(year_text),
        "month": int(month_text),
        "option_type": option_type,
        "strike": float(strike_text),
        "underlying_contract": f"{product}{year_text}{month_text}",
    }


def iron_ore_option_expiry_date(contract: str) -> str | None:
    parts = _option_contract_parts(normalize_contract(contract, "期权"))
    if parts is None or parts["product"] != "I":
        return None
    delivery_year = int(parts["year"])
    delivery_month = int(parts["month"])
    if delivery_month == 1:
        expiry_year, expiry_month = delivery_year - 1, 12
    else:
        expiry_year, expiry_month = delivery_year, delivery_month - 1
    market = get_market_window("A股")
    current = date(expiry_year, expiry_month, 1)
    trading_days = 0
    while current.month == expiry_month:
        if current.weekday() < 5 and (market is None or not is_market_holiday(market, current)):
            trading_days += 1
            if trading_days == 12:
                return current.isoformat()
        current += timedelta(days=1)
    return None


def list_option_expiry_events() -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            """
            SELECT * FROM futures_option_expiry_events
            ORDER BY event_date DESC, option_contract, id DESC
            """,
            conn,
        )


def _effective_option_expiry_events(*, as_of: object = None) -> pd.DataFrame:
    events = list_option_expiry_events()
    if events.empty:
        return events
    account = latest_monthly_account()
    latest_end = str(account["statement_end_date"]) if account else ""
    result = events[
        events["source"].eq("手工")
        & events["reconciliation_status"].eq("已确认")
        & (events["event_date"].astype(str) > latest_end)
    ].copy()
    cutoff = _date_text(as_of) if as_of is not None else ""
    if cutoff:
        result = result[result["event_date"].astype(str) <= cutoff]
    return result.sort_values(["event_date", "id"]).reset_index(drop=True)


def _apply_option_expiry_events(
    positions: pd.DataFrame,
    events: pd.DataFrame,
) -> pd.DataFrame:
    if positions.empty or events.empty:
        return positions
    states = {
        (str(row["asset_type"]), str(row["contract"]), str(row["side"])): dict(row)
        for row in positions.to_dict("records")
    }
    for event in events.to_dict("records"):
        option_contract = str(event["option_contract"])
        quantity = int(event["quantity"])
        option_keys = [
            key
            for key, state in states.items()
            if key[0] == "期权"
            and key[1] == option_contract
            and int(state.get("estimated_quantity") or 0) > 0
        ]
        remaining = quantity
        for key in option_keys:
            state = states[key]
            available = int(state.get("estimated_quantity") or 0)
            removed = min(available, remaining)
            state["estimated_quantity"] = available - removed
            state["post_month_change"] = (
                int(state["estimated_quantity"]) - int(state.get("official_quantity") or 0)
            )
            remaining -= removed
            if remaining <= 0:
                break
        if remaining > 0:
            raise ValueError(f"{option_contract} 到期数量超过当前预计持仓。")
        if event["outcome"] != "履约":
            continue
        underlying = str(event["underlying_contract"])
        futures_side = str(event["futures_side"])
        strike = float(event["strike"])
        key = ("期货", underlying, futures_side)
        state = states.get(key)
        if state is None:
            account = latest_monthly_account() or {}
            state = {
                "statement_month": account.get("statement_month", ""),
                "statement_end_date": account.get("statement_end_date", ""),
                "asset_type": "期货",
                "contract": underlying,
                "side": futures_side,
                "average_price": strike,
                "previous_settlement": None,
                "settlement_price": None,
                "floating_pnl": None,
                "margin": None,
                "multiplier": _known_multiplier("期货", underlying)
                or _known_multiplier("期权", option_contract),
                "trade_code": "",
                "official_quantity": 0,
                "post_month_change": quantity,
                "estimated_quantity": quantity,
            }
            states[key] = state
        else:
            current_quantity = int(state.get("estimated_quantity") or 0)
            current_average = _number(state.get("average_price")) or 0.0
            new_quantity = current_quantity + quantity
            state["average_price"] = (
                current_average * current_quantity + strike * quantity
            ) / new_quantity
            state["estimated_quantity"] = new_quantity
            state["post_month_change"] = (
                new_quantity - int(state.get("official_quantity") or 0)
            )
    result = pd.DataFrame(states.values())
    return result.sort_values(["asset_type", "contract", "side"]).reset_index(drop=True)


def build_estimated_positions(*, as_of: object = None) -> pd.DataFrame:
    positions = _build_estimated_positions_base(as_of=as_of)
    events = _effective_option_expiry_events(as_of=as_of)
    return _apply_option_expiry_events(positions, events)


def list_option_expiry_candidates(*, as_of: object = None) -> pd.DataFrame:
    target = _date_text(as_of) if as_of is not None else completed_futures_daily_cutoff().strftime("%Y-%m-%d")
    positions = _build_estimated_positions_base(as_of=target)
    if positions.empty:
        return pd.DataFrame()
    confirmed = list_option_expiry_events()
    confirmed_keys = set(
        zip(
            confirmed.get("event_date", pd.Series(dtype=str)).astype(str),
            confirmed.get("option_contract", pd.Series(dtype=str)).astype(str),
        )
    )
    cached = load_daily_closes()
    rows: list[dict[str, object]] = []
    option_positions = positions[
        positions["asset_type"].eq("期权")
        & pd.to_numeric(positions["estimated_quantity"], errors="coerce").fillna(0).gt(0)
    ]
    for position in option_positions.to_dict("records"):
        contract = str(position["contract"])
        parts = _option_contract_parts(contract)
        expiry_date = iron_ore_option_expiry_date(contract)
        if parts is None or not expiry_date or (expiry_date, contract) in confirmed_keys:
            continue
        underlying = str(parts["underlying_contract"])
        settlement_rows = cached[
            cached["asset_type"].eq("期货")
            & cached["contract"].eq(underlying)
            & cached["trade_date"].eq(expiry_date)
            & cached["settlement_price"].notna()
        ]
        settlement = (
            float(settlement_rows.iloc[-1]["settlement_price"])
            if not settlement_rows.empty
            else None
        )
        if expiry_date > target:
            status = "待到期"
        elif settlement is None:
            status = "等待结算价"
        else:
            status = "待确认"
        strike = float(parts["strike"])
        is_put = parts["option_type"] == "P"
        in_the_money = (
            settlement is not None
            and ((is_put and settlement < strike) or (not is_put and settlement > strike))
        )
        option_side = str(position["side"])
        futures_side = (
            "空" if is_put and option_side == "多" else
            "多" if is_put else
            "多" if option_side == "多" else "空"
        )
        rows.append(
            {
                "option_contract": contract,
                "option_side": option_side,
                "quantity": int(position["estimated_quantity"]),
                "expiry_date": expiry_date,
                "underlying_contract": underlying,
                "strike": strike,
                "settlement_price": settlement,
                "expected_outcome": (
                    "待结算"
                    if settlement is None
                    else "履约" if in_the_money else "作废"
                ),
                "expected_futures_side": futures_side if in_the_money else "",
                "status": status,
            }
        )
    return pd.DataFrame(rows).sort_values(["expiry_date", "option_contract"]).reset_index(drop=True) if rows else pd.DataFrame()


def confirm_option_expiry_event(
    *,
    option_contract: str,
    outcome: str,
    quantity: int | None = None,
    notes: str = "",
) -> int:
    if outcome not in OPTION_EXPIRY_OUTCOMES:
        raise ValueError("到期结果只能是作废或履约。")
    normalized_contract = normalize_contract(option_contract, "期权")
    candidates = list_option_expiry_candidates()
    matching = candidates[candidates["option_contract"].eq(normalized_contract)] if not candidates.empty else candidates
    if matching.empty:
        raise ValueError("当前没有可确认的该期权到期记录。")
    candidate = matching.iloc[0]
    if candidate["status"] != "待确认":
        raise ValueError("正式结算价尚未就绪，暂不能确认到期结果。")
    confirmed_quantity = int(quantity or candidate["quantity"])
    if confirmed_quantity != int(candidate["quantity"]):
        raise ValueError("同一期权合约需一次确认全部预计持仓手数。")
    futures_side = str(candidate["expected_futures_side"])
    if outcome == "履约" and not futures_side:
        parts = _option_contract_parts(normalized_contract) or {}
        is_put = parts.get("option_type") == "P"
        option_side = str(candidate["option_side"])
        futures_side = (
            "空" if is_put and option_side == "多" else
            "多" if is_put else
            "多" if option_side == "多" else "空"
        )
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO futures_option_expiry_events (
                source, event_date, option_contract, outcome, quantity,
                underlying_contract, futures_side, strike, settlement_price,
                reconciliation_status, source_file, notes, created_at
            ) VALUES ('手工', ?, ?, ?, ?, ?, ?, ?, ?, '已确认', NULL, ?, ?)
            ON CONFLICT(source, event_date, option_contract) DO UPDATE SET
                outcome=excluded.outcome,
                quantity=excluded.quantity,
                underlying_contract=excluded.underlying_contract,
                futures_side=excluded.futures_side,
                strike=excluded.strike,
                settlement_price=excluded.settlement_price,
                reconciliation_status='已确认',
                notes=excluded.notes,
                created_at=excluded.created_at
            """,
            (
                candidate["expiry_date"],
                normalized_contract,
                outcome,
                confirmed_quantity,
                candidate["underlying_contract"] if outcome == "履约" else None,
                futures_side if outcome == "履约" else None,
                float(candidate["strike"]),
                float(candidate["settlement_price"]),
                str(notes or "").strip(),
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)


def delete_manual_option_expiry_event(event_id: int) -> bool:
    init_db()
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT source FROM futures_option_expiry_events WHERE id=?",
            (int(event_id),),
        ).fetchone()
        if row is None:
            return False
        if row[0] != "手工":
            raise ValueError("月结单到期记录为只读记录，不能删除。")
        conn.execute(
            "DELETE FROM futures_option_expiry_events WHERE id=?", (int(event_id),)
        )
        conn.commit()
    return True


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


def _save_daily_close_frame(
    asset_type: str,
    contract: str,
    data: pd.DataFrame,
    source: str,
    *,
    max_trade_date: str | None = None,
) -> int:
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
            if max_trade_date and trade_date > max_trade_date:
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
              AND settlement_price IS NULL
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


def _save_daily_settlement_frame(
    asset_type: str,
    contract: str,
    data: pd.DataFrame,
    source: str,
    *,
    min_trade_date: str | None = None,
    max_trade_date: str | None = None,
) -> dict[str, object]:
    if data is None or data.empty:
        return {"updated": 0, "conflicts": []}
    now = datetime.now().isoformat(timespec="seconds")
    updated = 0
    conflicts: list[str] = []
    with closing(get_conn()) as conn:
        for record in data.to_dict("records"):
            trade_date = _date_text(record.get("date"))
            settlement = _number(record.get("settlement"))
            close = _number(record.get("close"))
            if (
                not trade_date
                or settlement is None
                or settlement < 0
                or (asset_type == "期货" and settlement <= 0)
            ):
                continue
            if min_trade_date and trade_date < min_trade_date:
                continue
            if max_trade_date and trade_date > max_trade_date:
                continue
            existing = conn.execute(
                """
                SELECT close_price, settlement_price
                FROM futures_daily_closes
                WHERE asset_type=? AND contract=? AND trade_date=?
                """,
                (asset_type, contract, trade_date),
            ).fetchone()
            if existing is None:
                if close is None or close < 0:
                    continue
                conn.execute(
                    """
                    INSERT INTO futures_daily_closes (
                        asset_type, contract, trade_date, close_price,
                        settlement_price, source, settlement_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_type,
                        contract,
                        trade_date,
                        close,
                        settlement,
                        source,
                        source,
                        now,
                    ),
                )
                updated += 1
                continue
            existing_settlement = _number(existing[1])
            if existing_settlement is None:
                conn.execute(
                    """
                    UPDATE futures_daily_closes
                    SET settlement_price=?, settlement_source=?, updated_at=?
                    WHERE asset_type=? AND contract=? AND trade_date=?
                      AND settlement_price IS NULL
                    """,
                    (
                        settlement,
                        source,
                        now,
                        asset_type,
                        contract,
                        trade_date,
                    ),
                )
                updated += 1
            elif abs(existing_settlement - settlement) > 1e-8:
                conflicts.append(
                    f"{contract} {trade_date} 已有结算价 {existing_settlement:g}，"
                    f"新来源返回 {settlement:g}，已保留原值"
                )
        conn.commit()
    return {"updated": updated, "conflicts": conflicts}


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
            not existing.empty
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
            updated += _save_daily_close_frame(
                asset_type,
                contract,
                data,
                source,
                max_trade_date=target,
            )
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


def _historical_contract_requirements(
    *, market_now: datetime | None = None
) -> pd.DataFrame:
    trades = list_futures_live_trades(include_taken_over=False)
    if trades.empty:
        return pd.DataFrame()
    trades = trades.copy()
    trades["trade_date"] = pd.to_datetime(trades["trade_date"], errors="coerce")
    trades = trades.dropna(subset=["trade_date"])
    latest_target = completed_futures_daily_cutoff(market_now).strftime("%Y-%m-%d")
    current = build_estimated_positions()
    active_keys = set()
    if not current.empty:
        active = current[
            pd.to_numeric(current["estimated_quantity"], errors="coerce").fillna(0).gt(0)
        ]
        active_keys = set(zip(active["asset_type"], active["contract"]))
    requirements: dict[tuple[str, str], dict[str, object]] = {}
    for (asset_type, contract), group in trades.groupby(["asset_type", "contract"]):
        first_date = group["trade_date"].min().strftime("%Y-%m-%d")
        last_trade_date = group["trade_date"].max().strftime("%Y-%m-%d")
        target_date = latest_target if (asset_type, contract) in active_keys else last_trade_date
        if asset_type == "期权":
            expiry = iron_ore_option_expiry_date(str(contract))
            if expiry:
                target_date = max(last_trade_date, min(expiry, latest_target))
        requirements[(asset_type, contract)] = {
            "asset_type": asset_type,
            "contract": contract,
            "first_date": first_date,
            "target_date": target_date,
        }
    option_rows = [
        row for row in requirements.values() if row["asset_type"] == "期权"
    ]
    for row in option_rows:
        parts = _option_contract_parts(str(row["contract"]))
        if parts is None or parts["product"] != "I":
            continue
        key = ("期货", str(parts["underlying_contract"]))
        existing = requirements.get(key)
        if existing is None:
            requirements[key] = {
                "asset_type": "期货",
                "contract": key[1],
                "first_date": row["first_date"],
                "target_date": row["target_date"],
            }
        else:
            existing["first_date"] = min(
                str(existing["first_date"]), str(row["first_date"])
            )
            existing["target_date"] = max(
                str(existing["target_date"]), str(row["target_date"])
            )
    rows = list(requirements.values())
    return pd.DataFrame(rows).sort_values(["asset_type", "contract"]).reset_index(drop=True)


def update_traded_contract_daily_closes(
    *,
    api_key: str = "",
    force: bool = False,
    market_now: datetime | None = None,
) -> dict[str, object]:
    requirements = _historical_contract_requirements(market_now=market_now)
    if requirements.empty:
        return {"updated": 0, "skipped": 0, "errors": [], "contracts": 0}
    cached = load_daily_closes()
    updated = skipped = 0
    errors: list[str] = []
    for requirement in requirements.to_dict("records"):
        asset_type = str(requirement["asset_type"])
        contract = str(requirement["contract"])
        existing = cached[
            cached["asset_type"].eq(asset_type) & cached["contract"].eq(contract)
        ]
        has_coverage = (
            not existing.empty
            and str(existing["trade_date"].min()) <= str(requirement["first_date"])
            and str(existing["trade_date"].max()) >= str(requirement["target_date"])
        )
        if has_coverage and not force:
            skipped += 1
            continue
        try:
            if asset_type == "期权":
                data, source, is_chain = fetch_option_from_akshare(
                    normalize_option_symbol(contract),
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
            updated += _save_daily_close_frame(
                asset_type,
                contract,
                data,
                source,
                max_trade_date=str(requirement["target_date"]),
            )
        except Exception as exc:
            errors.append(f"{contract}：{exc}")
    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "contracts": len(requirements),
    }


def _fetch_futures_settlement_history(
    contract: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[pd.DataFrame, str]:
    import akshare as ak

    raw = ak.futures_zh_daily_sina(symbol=contract.lower())
    source = "新浪期货日线结算价"
    if raw is None or raw.empty:
        raise RuntimeError(f"{source}未返回数据。")
    required = {"date", "close", "settle"}
    if not required.issubset(raw.columns):
        raise RuntimeError("新浪期货日线缺少日期、收盘价或结算价字段。")
    result = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["date"], errors="coerce"),
            "close": pd.to_numeric(raw["close"], errors="coerce"),
            "settlement": pd.to_numeric(raw["settle"], errors="coerce"),
        }
    ).dropna(subset=["date", "settlement"])
    return result, source


def _fetch_cffex_settlement_history(
    contracts: set[str],
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, str]:
    from io import BytesIO, StringIO
    import zipfile

    import requests

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    months = pd.period_range(start=start, end=end, freq="M")
    rows: list[dict[str, object]] = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for month in months:
        month_text = month.strftime("%Y%m")
        response = requests.get(
            f"http://www.cffex.com.cn/sj/historysj/{month_text}/zip/{month_text}.zip",
            headers=headers,
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            for file_name in archive.namelist():
                matched = re.search(r"(\d{8})_1\.csv$", file_name)
                if not matched:
                    continue
                trade_date = pd.Timestamp(matched.group(1))
                if trade_date < start or trade_date > end:
                    continue
                with archive.open(file_name) as file_object:
                    raw = pd.read_csv(
                        StringIO(file_object.read().decode("gb2312"))
                    )
                if raw.shape[1] < 10:
                    continue
                raw_symbols = raw.iloc[:, 0].astype(str).str.strip().str.upper()
                symbols = raw_symbols.map(
                    lambda value: normalize_contract(
                        value,
                        "期权" if re.search(r"-[CP]-", value) else "期货",
                    )
                )
                selected = raw[symbols.isin(contracts)]
                for selected_index in selected.index:
                    rows.append(
                        {
                            "date": trade_date,
                            "contract": symbols.loc[selected_index],
                            "close": _number(raw.loc[selected_index].iloc[8]),
                            "settlement": _number(raw.loc[selected_index].iloc[9]),
                        }
                    )
    result = pd.DataFrame(rows)
    if result.empty:
        raise RuntimeError("中金所历史日行情未返回所需合约。")
    return result, "中金所历史日行情结算价"


def _parse_dce_option_settlement_payload(
    payload: object,
    trade_date: str,
    contracts: set[str],
) -> pd.DataFrame:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("大商所期权日行情返回格式异常。")
    rows: list[dict[str, object]] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        try:
            contract = normalize_contract(item.get("contractId"), "期权")
        except ValueError:
            continue
        if contract not in contracts:
            continue
        settlement = _number(item.get("clearPrice"))
        close = _number(item.get("close"))
        if settlement is None or settlement < 0:
            continue
        rows.append(
            {
                "date": trade_date,
                "contract": contract,
                "close": close,
                "settlement": settlement,
            }
        )
    return pd.DataFrame(rows)


def _fetch_dce_option_settlements_for_date(
    trade_date: str,
    contracts: set[str],
) -> tuple[pd.DataFrame, str]:
    import requests

    request_payload = {
        "contractId": "",
        "lang": "zh",
        "optionSeries": "",
        "statisticsType": 0,
        "tradeDate": trade_date.replace("-", ""),
        "tradeType": "2",
        "varietyId": "i",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.dce.com.cn/",
    }
    failures: list[str] = []
    for url in (
        "https://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes",
        "http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes",
    ):
        try:
            response = requests.post(
                url,
                json=request_payload,
                headers=headers,
                timeout=(3.05, 8),
            )
            response.raise_for_status()
            frame = _parse_dce_option_settlement_payload(
                response.json(), trade_date, contracts
            )
            return frame, "大商所期权日行情结算价"
        except Exception as exc:
            failures.append(str(exc))
    detail = failures[-1] if failures else "未知错误"
    raise RuntimeError(f"大商所期权日行情不可用：{detail}")


def update_traded_contract_daily_settlements(
    *,
    force: bool = False,
    market_now: datetime | None = None,
) -> dict[str, object]:
    requirements = _historical_contract_requirements(market_now=market_now)
    if requirements.empty:
        return {
            "updated": 0,
            "skipped": 0,
            "errors": [],
            "conflicts": [],
            "contracts": 0,
        }
    cached = load_daily_closes()
    existing_settlements = {
        (str(row.asset_type), str(row.contract), str(row.trade_date))
        for row in cached.itertuples(index=False)
        if pd.notna(row.settlement_price)
    }
    updated = skipped = 0
    errors: list[str] = []
    conflicts: list[str] = []

    option_requirements: list[dict[str, object]] = []
    cffex_requirements: list[dict[str, object]] = []
    for requirement in requirements.to_dict("records"):
        asset_type = str(requirement["asset_type"])
        contract = str(requirement["contract"])
        first_date = str(requirement["first_date"])
        target_date = str(requirement["target_date"])
        if asset_type == "期权" and re.match(r"^(IO|HO|MO)\d", contract):
            cffex_requirements.append(requirement)
            continue
        if asset_type == "期权":
            option_requirements.append(requirement)
            continue
        if re.match(r"^(IC|IF|IH|IM|T|TF|TS|TL)\d", contract):
            cffex_requirements.append(requirement)
            continue
        required_days = _futures_trading_dates(
            pd.Timestamp(first_date).date(), pd.Timestamp(target_date).date()
        )
        missing_days = [
            day
            for day in required_days
            if (asset_type, contract, day) not in existing_settlements
        ]
        if not missing_days and not force:
            skipped += 1
            continue
        try:
            frame, source = _fetch_futures_settlement_history(
                contract,
                start_date=first_date,
                end_date=target_date,
            )
            saved = _save_daily_settlement_frame(
                asset_type,
                contract,
                frame,
                source,
                min_trade_date=first_date,
                max_trade_date=target_date,
            )
            updated += int(saved["updated"])
            conflicts.extend(saved["conflicts"])
        except Exception as exc:
            errors.append(f"{contract}历史结算价：{exc}")

    pending_cffex: list[dict[str, object]] = []
    for requirement in cffex_requirements:
        asset_type = str(requirement["asset_type"])
        contract = str(requirement["contract"])
        required_days = _futures_trading_dates(
            pd.Timestamp(requirement["first_date"]).date(),
            pd.Timestamp(requirement["target_date"]).date(),
        )
        missing_days = [
            day
            for day in required_days
            if (asset_type, contract, day) not in existing_settlements
        ]
        if not missing_days and not force:
            skipped += 1
        else:
            pending_cffex.append(requirement)
    if pending_cffex:
        cffex_contracts = {str(item["contract"]) for item in pending_cffex}
        cffex_start = min(str(item["first_date"]) for item in pending_cffex)
        cffex_end = max(str(item["target_date"]) for item in pending_cffex)
        try:
            cffex_frame, cffex_source = _fetch_cffex_settlement_history(
                cffex_contracts,
                cffex_start,
                cffex_end,
            )
            for requirement in pending_cffex:
                asset_type = str(requirement["asset_type"])
                contract = str(requirement["contract"])
                contract_frame = cffex_frame[cffex_frame["contract"].eq(contract)]
                saved = _save_daily_settlement_frame(
                    asset_type,
                    contract,
                    contract_frame,
                    cffex_source,
                    min_trade_date=str(requirement["first_date"]),
                    max_trade_date=str(requirement["target_date"]),
                )
                updated += int(saved["updated"])
                conflicts.extend(saved["conflicts"])
                if contract_frame.empty:
                    errors.append(f"{contract}历史结算价：中金所未返回该合约。")
        except Exception as exc:
            errors.append(f"中金所历史结算价：{exc}")

    option_days: dict[str, set[str]] = {}
    for requirement in option_requirements:
        contract = str(requirement["contract"])
        parts = _option_contract_parts(contract)
        if parts is None or parts["product"] != "I":
            errors.append(f"{contract}历史结算价：当前仅支持铁矿石期权。")
            continue
        first_date = str(requirement["first_date"])
        target_date = str(requirement["target_date"])
        required_days = _futures_trading_dates(
            pd.Timestamp(first_date).date(), pd.Timestamp(target_date).date()
        )
        missing_days = [
            day
            for day in required_days
            if ("期权", contract, day) not in existing_settlements
        ]
        if not missing_days and not force:
            skipped += 1
            continue
        for day in required_days if force else missing_days:
            option_days.setdefault(day, set()).add(contract)

    for day in sorted(option_days):
        contracts = option_days[day]
        try:
            frame, source = _fetch_dce_option_settlements_for_date(day, contracts)
        except Exception as exc:
            errors.append(f"铁矿石期权 {day} 起历史结算价：{exc}")
            break
        returned = set(frame["contract"].astype(str)) if not frame.empty else set()
        for contract in sorted(contracts):
            contract_frame = frame[frame["contract"].eq(contract)] if not frame.empty else frame
            if contract not in returned:
                errors.append(f"{contract} {day}：大商所未返回正式结算价。")
                continue
            saved = _save_daily_settlement_frame(
                "期权",
                contract,
                contract_frame,
                source,
                min_trade_date=day,
                max_trade_date=day,
            )
            updated += int(saved["updated"])
            conflicts.extend(saved["conflicts"])
    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "conflicts": conflicts,
        "contracts": len(requirements),
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


def _futures_trading_dates(start: date, end: date) -> list[str]:
    market = get_market_window("A股")
    days: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and (market is None or not is_market_holiday(market, current)):
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _daily_fee_adjustments(
    accounts: pd.DataFrame,
    trades: pd.DataFrame,
    cash_flows: pd.DataFrame,
    trading_dates: list[str],
) -> dict[str, float]:
    adjustments: dict[str, float] = {}
    for account in accounts.to_dict("records"):
        month = str(account["statement_month"])
        official_trades = trades[
            trades["source"].eq("月结单") & trades["statement_month"].eq(month)
        ]
        detail_fee = float(
            pd.to_numeric(official_trades.get("fee"), errors="coerce").fillna(0).sum()
        ) if not official_trades.empty else 0.0
        month_account_fees = cash_flows[
            cash_flows["source"].eq("月结单")
            & cash_flows["statement_month"].eq(month)
            & cash_flows["entry_type"].isin(["申报费", "账户费用"])
        ]
        explicit_account_fee = float(
            pd.to_numeric(month_account_fees.get("amount"), errors="coerce").fillna(0).sum()
        ) if not month_account_fees.empty else 0.0
        residual = float(account.get("monthly_fee") or 0) - detail_fee - explicit_account_fee
        if abs(residual) <= RECONCILIATION_TOLERANCE:
            continue
        eligible = [day for day in trading_dates if day.startswith(month)]
        if eligible:
            adjustments[max(eligible)] = adjustments.get(max(eligible), 0.0) + residual
    return adjustments


def build_daily_account_pnl(
    *,
    as_of: object = None,
    valuation_mode: str = "close",
) -> pd.DataFrame:
    if valuation_mode not in {"close", "settlement"}:
        raise ValueError("估值口径必须是 close 或 settlement。")
    accounts = list_monthly_accounts()
    trades = list_futures_live_trades(include_taken_over=False)
    if accounts.empty or trades.empty:
        return pd.DataFrame()
    trades = trades.copy()
    trades["trade_date"] = pd.to_datetime(trades["trade_date"], errors="coerce")
    trades = trades.dropna(subset=["trade_date"]).sort_values(
        ["trade_date", "trade_time", "id"], na_position="last"
    )
    cash_flows = _effective_cash_flows(as_of=as_of)
    start_candidates = [trades["trade_date"].min().date()]
    if not cash_flows.empty:
        first_flow = pd.to_datetime(cash_flows["flow_date"], errors="coerce").min()
        if pd.notna(first_flow):
            start_candidates.append(first_flow.date())
    target = (
        pd.Timestamp(_date_text(as_of)).date()
        if as_of is not None and _date_text(as_of)
        else completed_futures_daily_cutoff().date()
    )
    start = min(start_candidates)
    if target < start:
        return pd.DataFrame()
    trading_dates = _futures_trading_dates(start, target)
    if not trading_dates:
        return pd.DataFrame()

    cached = load_daily_closes()
    close_lookup = {
        (str(row.asset_type), str(row.contract), str(row.trade_date)): float(row.close_price)
        for row in cached.itertuples(index=False)
        if pd.notna(row.close_price)
    }
    settlement_lookup = {
        (str(row.asset_type), str(row.contract), str(row.trade_date)): float(row.settlement_price)
        for row in cached.itertuples(index=False)
        if pd.notna(row.settlement_price)
    }
    valuation_lookup = close_lookup if valuation_mode == "close" else settlement_lookup
    trade_groups = {
        day.strftime("%Y-%m-%d"): group
        for day, group in trades.groupby(trades["trade_date"].dt.normalize())
    }
    flow_groups: dict[str, pd.DataFrame] = {}
    if not cash_flows.empty:
        dated_flows = cash_flows.copy()
        dated_flows["_valuation_date"] = dated_flows["flow_date"].astype(str).map(
            lambda flow_date: next(
                (day for day in trading_dates if day >= flow_date),
                None,
            )
        )
        dated_flows = dated_flows.dropna(subset=["_valuation_date"])
        flow_groups = {
            str(day): group.drop(columns=["_valuation_date"])
            for day, group in dated_flows.groupby("_valuation_date")
        }
    event_rows = list_option_expiry_events()
    event_rows = event_rows[
        event_rows["reconciliation_status"].isin(["已确认", "已接管"])
    ] if not event_rows.empty else event_rows
    event_groups = {
        str(day): group
        for day, group in event_rows.groupby(event_rows["event_date"].astype(str))
    } if not event_rows.empty else {}
    fee_adjustments = _daily_fee_adjustments(
        accounts, trades, cash_flows, trading_dates
    )
    latest_statement_end = str(accounts.iloc[-1]["statement_end_date"])

    futures_states: dict[tuple[str, str], dict[str, float | int]] = {}
    option_states: dict[str, dict[str, float | int]] = {}
    futures_realized = 0.0
    option_realized = 0.0
    cumulative_fee = 0.0
    cumulative_net_flow = 0.0
    rows: list[dict[str, object]] = []

    def add_futures_position(
        contract: str,
        side: str,
        quantity: int,
        price: float,
        multiplier: float,
    ) -> None:
        key = (contract, side)
        state = futures_states.setdefault(
            key, {"quantity": 0, "average": 0.0, "multiplier": multiplier}
        )
        current_quantity = int(state["quantity"])
        new_quantity = current_quantity + quantity
        state["average"] = (
            float(state["average"]) * current_quantity + price * quantity
        ) / new_quantity
        state["quantity"] = new_quantity
        state["multiplier"] = multiplier

    def process_expiry(
        contract: str,
        quantity: int,
        outcome: str,
        underlying: str | None,
        futures_side: str | None,
        strike: float,
    ) -> None:
        nonlocal option_realized
        state = option_states.get(contract)
        if state is None or int(state["quantity"]) == 0:
            return
        signed_quantity = int(state["quantity"])
        closed_quantity = min(abs(signed_quantity), quantity)
        multiplier = float(state["multiplier"])
        option_realized += (
            -float(state["average"]) * closed_quantity * multiplier
            if signed_quantity > 0
            else float(state["average"]) * closed_quantity * multiplier
        )
        state["quantity"] = (
            signed_quantity - closed_quantity
            if signed_quantity > 0
            else signed_quantity + closed_quantity
        )
        if outcome == "履约" and underlying and futures_side:
            add_futures_position(
                underlying, futures_side, closed_quantity, strike, multiplier
            )

    for day in trading_dates:
        day_trades = trade_groups.get(day, pd.DataFrame())
        day_trade_fee = 0.0
        for trade in day_trades.to_dict("records"):
            asset_type = str(trade["asset_type"])
            contract = str(trade["contract"])
            quantity = int(trade["quantity"])
            price = float(trade["price"])
            multiplier = _number(trade.get("multiplier")) or _known_multiplier(
                asset_type, contract
            )
            if multiplier is None:
                continue
            day_trade_fee += float(trade.get("fee") or 0)
            if asset_type == "期货":
                if trade["open_close"] == "开":
                    side = "多" if trade["buy_sell"] == "买" else "空"
                    add_futures_position(contract, side, quantity, price, multiplier)
                    continue
                side = "多" if trade["buy_sell"] == "卖" else "空"
                state = futures_states.get((contract, side))
                available = int(state["quantity"]) if state else 0
                closed_quantity = min(quantity, available)
                supplied_pnl = _number(trade.get("close_pnl"))
                if supplied_pnl is not None:
                    futures_realized += supplied_pnl
                elif state is not None and closed_quantity > 0:
                    futures_realized += (
                        (price - float(state["average"])) * closed_quantity * multiplier
                        if side == "多"
                        else (float(state["average"]) - price) * closed_quantity * multiplier
                    )
                if state is not None:
                    state["quantity"] = max(0, available - quantity)
                continue

            state = option_states.setdefault(
                contract, {"quantity": 0, "average": 0.0, "multiplier": multiplier}
            )
            signed_quantity = int(state["quantity"])
            delta = quantity if trade["buy_sell"] == "买" else -quantity
            if signed_quantity == 0 or signed_quantity * delta > 0:
                new_quantity = signed_quantity + delta
                state["average"] = (
                    float(state["average"]) * abs(signed_quantity) + price * abs(delta)
                ) / abs(new_quantity)
                state["quantity"] = new_quantity
                state["multiplier"] = multiplier
                continue
            closed_quantity = min(abs(signed_quantity), abs(delta))
            option_realized += (
                (price - float(state["average"])) * closed_quantity * multiplier
                if signed_quantity > 0
                else (float(state["average"]) - price) * closed_quantity * multiplier
            )
            new_quantity = signed_quantity + delta
            if signed_quantity * new_quantity < 0:
                state["average"] = price
            elif new_quantity == 0:
                state["average"] = 0.0
            state["quantity"] = new_quantity
            state["multiplier"] = multiplier

        pending_expiry: list[str] = []
        explicit_events = event_groups.get(day, pd.DataFrame())
        explicit_contracts: set[str] = set()
        for event in explicit_events.to_dict("records"):
            explicit_contracts.add(str(event["option_contract"]))
            process_expiry(
                str(event["option_contract"]),
                int(event["quantity"]),
                str(event["outcome"]),
                _text(event.get("underlying_contract")) or None,
                _text(event.get("futures_side")) or None,
                float(event.get("strike") or 0),
            )
        for contract, state in option_states.items():
            if int(state["quantity"]) == 0:
                continue
            expiry_date = iron_ore_option_expiry_date(contract)
            if not expiry_date or expiry_date > day or contract in explicit_contracts:
                continue
            parts = _option_contract_parts(contract) or {}
            underlying = str(parts.get("underlying_contract") or "")
            settlement = settlement_lookup.get(("期货", underlying, expiry_date))
            settlement = settlement or close_lookup.get(("期货", underlying, expiry_date))
            if expiry_date > latest_statement_end or settlement is None:
                pending_expiry.append(contract)
                continue
            strike = float(parts.get("strike") or 0)
            is_put = parts.get("option_type") == "P"
            in_the_money = (
                (is_put and settlement < strike)
                or (not is_put and settlement > strike)
            )
            signed_quantity = int(state["quantity"])
            option_side = "多" if signed_quantity > 0 else "空"
            futures_side = (
                "空" if is_put and option_side == "多" else
                "多" if is_put else
                "多" if option_side == "多" else "空"
            )
            process_expiry(
                contract,
                abs(signed_quantity),
                "履约" if in_the_money else "作废",
                underlying if in_the_money else None,
                futures_side if in_the_money else None,
                strike,
            )

        day_flows = flow_groups.get(day, pd.DataFrame())
        external = day_flows[day_flows["entry_type"].isin(CASH_FLOW_TYPES)] if not day_flows.empty else day_flows
        net_flow = 0.0
        if not external.empty:
            net_flow = float(
                pd.to_numeric(external.loc[external["entry_type"].eq("入金"), "amount"], errors="coerce").fillna(0).sum()
                - pd.to_numeric(external.loc[external["entry_type"].eq("出金"), "amount"], errors="coerce").fillna(0).sum()
            )
        account_fee = 0.0
        if not day_flows.empty:
            account_fee = float(
                pd.to_numeric(
                    day_flows.loc[
                        day_flows["entry_type"].isin(["申报费", "账户费用"]),
                        "amount",
                    ],
                    errors="coerce",
                ).fillna(0).sum()
            )
        cumulative_net_flow += net_flow
        if valuation_mode == "settlement":
            cumulative_fee += day_trade_fee
        else:
            cumulative_fee += day_trade_fee + account_fee + fee_adjustments.get(day, 0.0)

        floating = 0.0
        missing: list[str] = [f"{contract}到期处理待确认" for contract in pending_expiry]
        for (contract, side), state in futures_states.items():
            quantity = int(state["quantity"])
            if quantity <= 0:
                continue
            valuation_price = valuation_lookup.get(("期货", contract, day))
            if valuation_price is None:
                missing.append(contract)
                continue
            direction = 1 if side == "多" else -1
            floating += (
                (valuation_price - float(state["average"]))
                * quantity
                * float(state["multiplier"])
                * direction
            )
        for contract, state in option_states.items():
            signed_quantity = int(state["quantity"])
            if signed_quantity == 0:
                continue
            valuation_price = valuation_lookup.get(("期权", contract, day))
            if valuation_price is None:
                missing.append(contract)
                continue
            direction = 1 if signed_quantity > 0 else -1
            floating += (
                (valuation_price - float(state["average"]))
                * abs(signed_quantity)
                * float(state["multiplier"])
                * direction
            )
        complete = not missing
        realized = futures_realized + option_realized
        net_pnl = realized + floating - cumulative_fee if complete else pd.NA
        economic_equity = cumulative_net_flow + float(net_pnl) if complete else pd.NA
        rows.append(
            {
                "date": day,
                "realized_pnl": realized,
                "floating_pnl": floating if complete else pd.NA,
                "fee": cumulative_fee,
                "net_pnl": net_pnl,
                "net_cash_flow": net_flow,
                "cumulative_net_cash_flow": cumulative_net_flow,
                "economic_equity": economic_equity,
                "source": (
                    "数据不完整"
                    if not complete
                    else (
                        "正式收盘估值"
                        if valuation_mode == "close"
                        else "正式结算估值"
                    )
                ),
                "status": "完整" if complete else "数据不完整",
                "confirmation_status": (
                    "正式" if day <= latest_statement_end else "待月结单确认"
                ),
                "missing_contracts": "、".join(sorted(set(missing))),
            }
        )

    result = pd.DataFrame(rows)
    if valuation_mode == "settlement":
        return _apply_manual_daily_pnl_overrides(
            result,
            latest_statement_end=latest_statement_end,
        )
    result["daily_pnl"] = pd.NA
    result["return_base"] = pd.NA
    result["daily_return_pct"] = pd.NA
    previous_complete = False
    previous_net = 0.0
    previous_equity = 0.0
    for index, row in result.iterrows():
        if row["status"] != "完整" or pd.isna(row["net_pnl"]):
            previous_complete = False
            continue
        if index == 0:
            daily_pnl = float(row["net_pnl"])
            return_base = max(float(row["net_cash_flow"]), 0.0)
        elif previous_complete:
            daily_pnl = float(row["net_pnl"]) - previous_net
            return_base = previous_equity + max(float(row["net_cash_flow"]), 0.0)
        else:
            previous_net = float(row["net_pnl"])
            previous_equity = float(row["economic_equity"])
            previous_complete = True
            continue
        result.at[index, "daily_pnl"] = daily_pnl
        result.at[index, "return_base"] = return_base
        result.at[index, "daily_return_pct"] = (
            daily_pnl / return_base * 100 if return_base > 0 else pd.NA
        )
        previous_net = float(row["net_pnl"])
        previous_equity = float(row["economic_equity"])
        previous_complete = True
    return result.reset_index(drop=True)


def _apply_manual_daily_pnl_overrides(
    result: pd.DataFrame,
    *,
    latest_statement_end: str,
) -> pd.DataFrame:
    if result.empty:
        return result
    overrides = list_futures_daily_pnl_overrides()
    override_by_date = {
        str(row["trade_date"]): row
        for row in overrides.to_dict("records")
    } if not overrides.empty else {}
    result = result.copy()
    result["formal_net_pnl"] = result["net_pnl"]
    result["formal_economic_equity"] = result["economic_equity"]
    result["formal_daily_pnl"] = pd.NA
    result["manual_daily_pnl"] = pd.NA
    result["difference"] = pd.NA
    result["reconciliation_status"] = ""
    result["daily_pnl"] = pd.NA
    result["return_base"] = pd.NA
    result["daily_return_pct"] = pd.NA

    previous_effective_net: float | None = None
    previous_effective_equity: float | None = None
    previous_day_has_cumulative = False
    confirmation_paused = False
    reconciliation_updates: list[tuple[float, float, str, str | None, str, int]] = []

    for index, row in result.iterrows():
        day = str(row["date"])
        formal_complete = row["status"] == "完整" and pd.notna(row["formal_net_pnl"])
        formal_daily: float | None = None
        if formal_complete:
            formal_net = float(row["formal_net_pnl"])
            if index == 0:
                formal_daily = formal_net
            elif previous_day_has_cumulative and previous_effective_net is not None:
                formal_daily = formal_net - previous_effective_net
            if formal_daily is not None:
                result.at[index, "formal_daily_pnl"] = formal_daily

        override = override_by_date.get(day)
        manual_pnl = float(override["pnl_amount"]) if override is not None else None
        resolution = (
            str(override.get("resolution"))
            if override is not None and str(override.get("resolution")) in DAILY_PNL_RESOLUTIONS
            else None
        )
        reconciliation_status = ""
        difference: float | None = None
        if manual_pnl is not None:
            result.at[index, "manual_daily_pnl"] = manual_pnl
        if manual_pnl is not None and formal_daily is not None:
            difference = formal_daily - manual_pnl
            if abs(difference) <= RECONCILIATION_TOLERANCE:
                reconciliation_status = "已一致"
                resolution = "采用正式"
            elif resolution in DAILY_PNL_RESOLUTIONS:
                reconciliation_status = resolution
            else:
                reconciliation_status = "待核对"
            reconciliation_updates.append(
                (
                    formal_daily,
                    difference,
                    reconciliation_status,
                    resolution,
                    datetime.now().isoformat(timespec="seconds"),
                    int(override["id"]),
                )
            )
            result.at[index, "difference"] = difference
            result.at[index, "reconciliation_status"] = reconciliation_status
        elif manual_pnl is not None:
            reconciliation_status = "待确认"
            result.at[index, "reconciliation_status"] = reconciliation_status

        chosen_daily: float | None = None
        use_manual = False
        if manual_pnl is not None:
            use_manual = formal_daily is None or resolution != "采用正式"
            chosen_daily = manual_pnl if use_manual else formal_daily
        elif formal_daily is not None:
            chosen_daily = formal_daily

        can_extend = index == 0 or (
            previous_day_has_cumulative and previous_effective_net is not None
        )
        if chosen_daily is not None and can_extend:
            if index == 0:
                effective_net = chosen_daily
                return_base = max(float(row["net_cash_flow"]), 0.0)
            else:
                effective_net = float(previous_effective_net) + chosen_daily
                return_base = float(previous_effective_equity) + max(
                    float(row["net_cash_flow"]), 0.0
                )
            effective_equity = float(row["cumulative_net_cash_flow"]) + effective_net
            result.at[index, "net_pnl"] = effective_net
            result.at[index, "economic_equity"] = effective_equity
            result.at[index, "daily_pnl"] = chosen_daily
            result.at[index, "return_base"] = return_base
            result.at[index, "daily_return_pct"] = (
                chosen_daily / return_base * 100 if return_base > 0 else pd.NA
            )
            if use_manual:
                result.at[index, "source"] = "同花顺手工"
                result.at[index, "status"] = "手工估算"
            previous_effective_net = effective_net
            previous_effective_equity = effective_equity
            previous_day_has_cumulative = True
        elif chosen_daily is not None and use_manual:
            result.at[index, "daily_pnl"] = chosen_daily
            result.at[index, "status"] = "手工估算"
            if formal_complete:
                result.at[index, "net_pnl"] = float(row["formal_net_pnl"])
                result.at[index, "economic_equity"] = float(
                    row["formal_economic_equity"]
                )
                result.at[index, "source"] = "同花顺手工日收益/正式结算累计"
                previous_effective_net = float(row["formal_net_pnl"])
                previous_effective_equity = float(row["formal_economic_equity"])
                previous_day_has_cumulative = True
            else:
                result.at[index, "net_pnl"] = pd.NA
                result.at[index, "economic_equity"] = pd.NA
                result.at[index, "source"] = "同花顺手工"
                previous_day_has_cumulative = False
        elif formal_complete:
            previous_effective_net = float(row["formal_net_pnl"])
            previous_effective_equity = float(row["formal_economic_equity"])
            previous_day_has_cumulative = True
        else:
            result.at[index, "net_pnl"] = pd.NA
            result.at[index, "economic_equity"] = pd.NA
            previous_day_has_cumulative = False

        unresolved = manual_pnl is not None and (
            formal_daily is None or reconciliation_status == "待核对"
        )
        confirmation_paused = confirmation_paused or unresolved
        if confirmation_paused:
            result.at[index, "confirmation_status"] = (
                "待核对" if unresolved else "待前序核对"
            )
        elif day <= latest_statement_end:
            result.at[index, "confirmation_status"] = "正式"
        else:
            result.at[index, "confirmation_status"] = "待月结单确认"

    if reconciliation_updates:
        with closing(get_conn()) as conn:
            conn.executemany(
                """
                UPDATE futures_daily_pnl_overrides
                SET formal_pnl=?, difference=?, reconciliation_status=?,
                    resolution=?, updated_at=?
                WHERE id=?
                """,
                reconciliation_updates,
            )
            conn.commit()
    return result.reset_index(drop=True)


def build_futures_daily_returns(daily_pnl: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "daily_pnl", "return_base", "daily_return_pct", "status"}
    if daily_pnl is None or daily_pnl.empty or not required.issubset(daily_pnl.columns):
        return pd.DataFrame()
    result = daily_pnl[
        daily_pnl["status"].isin(["完整", "手工估算"])
        & pd.to_numeric(daily_pnl["daily_pnl"], errors="coerce").notna()
    ].copy()
    if result.empty:
        return pd.DataFrame()
    result = result.rename(
        columns={"daily_pnl": "pnl_amount", "daily_return_pct": "return_pct"}
    )
    columns = ["date", "pnl_amount", "return_base", "return_pct", "source"]
    if "confirmation_status" in result.columns:
        columns.append("confirmation_status")
    return result[columns].reset_index(drop=True)
