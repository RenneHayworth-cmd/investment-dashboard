"""实盘记录页面通用数值格式化。"""

import pandas as pd


def money(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):,.2f}"


def format_live_number(value: object, digits: int = 2, prefix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{prefix}{float(value):,.{digits}f}"


__all__ = ["format_live_number", "money"]
