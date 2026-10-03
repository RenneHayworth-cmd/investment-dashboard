"""ETF实盘按资产类别汇总盈亏：ETF、LOF套利、可转债、现金管理。"""

from __future__ import annotations

import pandas as pd

from services.live_price_history import is_convertible_bond

CATEGORY_ORDER = ("ETF", "LOF套利", "可转债", "现金管理", "其他")
CATEGORY_COLUMNS = ["类别", "标的数", "已实现盈亏", "未实现盈亏", "利息分红等", "累计盈亏"]


def classify_symbol(symbol: object) -> str:
    code = str(symbol or "").strip()
    if not code:
        return "其他"
    if is_convertible_bond(code):
        return "可转债"
    if code.startswith("511"):
        return "现金管理"
    if code.startswith(("16", "50")):
        return "LOF套利"
    return "ETF"


def _flow_category(row) -> str:
    symbol = str(row.symbol or "").strip() if pd.notna(row.symbol) else ""
    if symbol:
        return classify_symbol(symbol)
    if row.entry_type == "利息" or "逆回购" in str(row.notes or ""):
        return "现金管理"
    return "其他"


def summarize_live_pnl_by_category(symbol_history: pd.DataFrame, cash_flows: pd.DataFrame) -> pd.DataFrame:
    """标的买卖盈亏按代码归类，利息、分红、兑息等非交易收支按关联代码或性质归类。"""
    totals = {
        category: {"标的数": 0, "已实现盈亏": 0.0, "未实现盈亏": 0.0, "利息分红等": 0.0}
        for category in CATEGORY_ORDER
    }
    if symbol_history is not None and not symbol_history.empty:
        for row in symbol_history.itertuples(index=False):
            category = classify_symbol(row.symbol)
            totals[category]["标的数"] += 1
            totals[category]["已实现盈亏"] += float(pd.to_numeric(row.realized_pnl, errors="coerce") or 0.0)
            totals[category]["未实现盈亏"] += float(pd.to_numeric(row.unrealized_pnl, errors="coerce") or 0.0)
    if cash_flows is not None and not cash_flows.empty:
        income = cash_flows[cash_flows["entry_type"].isin(["现金分红", "利息", "其他收入", "其他支出"])]
        for row in income.itertuples(index=False):
            sign = -1.0 if row.entry_type == "其他支出" else 1.0
            totals[_flow_category(row)]["利息分红等"] += sign * float(row.amount)
    rows = []
    for category in CATEGORY_ORDER:
        values = totals[category]
        total = values["已实现盈亏"] + values["未实现盈亏"] + values["利息分红等"]
        if values["标的数"] == 0 and abs(total) < 0.005:
            continue
        rows.append({"类别": category, **values, "累计盈亏": total})
    result = pd.DataFrame(rows, columns=CATEGORY_COLUMNS)
    if not result.empty:
        total_row = {"类别": "合计", "标的数": int(result["标的数"].sum())}
        for column in CATEGORY_COLUMNS[2:]:
            total_row[column] = float(result[column].sum())
        result = pd.concat([result, pd.DataFrame([total_row])], ignore_index=True)
    return result


__all__ = ["CATEGORY_ORDER", "classify_symbol", "summarize_live_pnl_by_category"]
