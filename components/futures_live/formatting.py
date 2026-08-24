"""期货实盘页面使用的纯格式化与状态判断函数。"""

from __future__ import annotations

import json

import pandas as pd


def format_money(value: object) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value):,.2f}"


def format_number(value: object, digits: int = 2) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value):,.{digits}f}"


def format_ratio(value: object) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value) * 100:.2f}%"


def decode_warnings(value: object) -> list[str]:
    if not value or pd.isna(value):
        return []
    try:
        parsed = json.loads(str(value))
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except Exception:
        return [str(value)]


def has_unresolved_price_gaps(daily_pnl: pd.DataFrame) -> bool:
    if daily_pnl.empty or "missing_contracts" not in daily_pnl:
        return False
    missing_values = daily_pnl["missing_contracts"].dropna().astype(str)
    return any(
        contract and not contract.endswith("到期处理待确认")
        for value in missing_values
        for contract in value.split("、")
    )
