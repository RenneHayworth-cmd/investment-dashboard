from __future__ import annotations

import calendar
from datetime import date, datetime
import os
from pathlib import Path
import re
import warnings

import pandas as pd

from services.futures_live_models import (
    CASH_FLOW_TYPES,
    RECONCILIATION_TOLERANCE,
    STATEMENT_FILE_PATTERN,
    StatementPayload,
    normalize_contract,
)

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
