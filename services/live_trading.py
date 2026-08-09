from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
import re

import pandas as pd

from core.db import get_conn, init_db


LIVE_TRADE_SIDES = ("买入", "卖出")
LIVE_TRADE_COLUMNS = [
    "id",
    "trade_date",
    "symbol",
    "name",
    "side",
    "price",
    "quantity",
    "fee_rate_pct",
    "strategy",
    "notes",
    "created_at",
]


def normalize_live_trade_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if match:
        return match.group(1)
    if not text:
        raise ValueError("请输入标的代码。")
    return text


def _normalized_trade_date(value: date | datetime | str | pd.Timestamp) -> str:
    trade_date = pd.to_datetime(value, errors="coerce")
    if pd.isna(trade_date):
        raise ValueError("成交日期无效。")
    return pd.Timestamp(trade_date).strftime("%Y-%m-%d")


def add_live_trade(
    *,
    trade_date: date | datetime | str | pd.Timestamp,
    symbol: str,
    name: str,
    side: str,
    price: float,
    quantity: int,
    fee_rate_pct: float,
    strategy: str = "",
    notes: str = "",
    record_key: str | None = None,
) -> tuple[int, bool]:
    init_db()
    normalized_symbol = normalize_live_trade_symbol(symbol)
    normalized_name = str(name or "").strip() or normalized_symbol
    normalized_side = str(side or "").strip()
    if normalized_side not in LIVE_TRADE_SIDES:
        raise ValueError("成交方向只能是买入或卖出。")
    price = float(price)
    quantity = int(quantity)
    fee_rate_pct = float(fee_rate_pct)
    if price <= 0:
        raise ValueError("成交价格必须大于0。")
    if quantity <= 0:
        raise ValueError("成交数量必须大于0。")
    if fee_rate_pct < 0:
        raise ValueError("手续费率不能为负数。")
    normalized_trade_date = _normalized_trade_date(trade_date)

    with closing(get_conn()) as conn:
        if normalized_side == "卖出":
            current_quantity = conn.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN side='买入' THEN quantity ELSE -quantity END), 0)
                FROM live_trades
                WHERE symbol=? AND trade_date<=?
                """,
                (normalized_symbol, normalized_trade_date),
            ).fetchone()[0]
            if int(current_quantity) < quantity:
                raise ValueError(
                    f"卖出数量超过当前记录持仓，最多可卖出 {int(current_quantity)} 份。"
                )

        created_at = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO live_trades (
                record_key, trade_date, symbol, name, side, price, quantity,
                fee_rate_pct, strategy, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record_key).strip() if record_key else None,
                normalized_trade_date,
                normalized_symbol,
                normalized_name,
                normalized_side,
                price,
                quantity,
                fee_rate_pct,
                str(strategy or "").strip(),
                str(notes or "").strip(),
                created_at,
            ),
        )
        created = cursor.rowcount > 0
        if created:
            trade_id = int(cursor.lastrowid)
        elif record_key:
            row = conn.execute(
                "SELECT id FROM live_trades WHERE record_key=?",
                (str(record_key).strip(),),
            ).fetchone()
            trade_id = int(row[0])
        else:
            raise ValueError("成交记录未能保存。")
        conn.commit()
    return trade_id, created


def list_live_trades() -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        result = pd.read_sql_query(
            """
            SELECT id, trade_date, symbol, name, side, price, quantity,
                   fee_rate_pct, strategy, notes, created_at
            FROM live_trades
            ORDER BY trade_date DESC, id DESC
            """,
            conn,
        )
    if result.empty:
        return pd.DataFrame(columns=LIVE_TRADE_COLUMNS)
    return result


def delete_live_trade(trade_id: int) -> bool:
    init_db()
    with closing(get_conn()) as conn:
        cursor = conn.execute("DELETE FROM live_trades WHERE id=?", (int(trade_id),))
        if cursor.rowcount <= 0:
            return False
        remaining = pd.read_sql_query(
            """
            SELECT id, trade_date, symbol, name, side, price, quantity,
                   fee_rate_pct, strategy, notes, created_at
            FROM live_trades
            ORDER BY trade_date DESC, id DESC
            """,
            conn,
        )
        try:
            build_live_positions(remaining)
        except ValueError:
            conn.rollback()
            raise ValueError("删除后会造成历史卖出数量超过当时持仓，不能删除该记录。")
        conn.commit()
        return True


def enrich_live_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame(
            columns=LIVE_TRADE_COLUMNS + ["gross_amount", "fee_amount", "cash_amount"]
        )
    result = trades.copy()
    result["price"] = pd.to_numeric(result["price"], errors="coerce")
    result["quantity"] = pd.to_numeric(result["quantity"], errors="coerce")
    result["fee_rate_pct"] = pd.to_numeric(result["fee_rate_pct"], errors="coerce")
    result["gross_amount"] = result["price"] * result["quantity"]
    result["fee_amount"] = result["gross_amount"] * result["fee_rate_pct"] / 100
    result["cash_amount"] = result["gross_amount"] + result["fee_amount"]
    sell_mask = result["side"].eq("卖出")
    result.loc[sell_mask, "cash_amount"] = (
        result.loc[sell_mask, "gross_amount"] - result.loc[sell_mask, "fee_amount"]
    )
    return result


def summarize_live_trades(trades: pd.DataFrame) -> dict[str, float | int]:
    enriched = enrich_live_trades(trades)
    if enriched.empty:
        return {
            "record_count": 0,
            "position_count": 0,
            "buy_amount": 0.0,
            "fee_amount": 0.0,
            "net_investment": 0.0,
        }
    buy_mask = enriched["side"].eq("买入")
    sell_mask = enriched["side"].eq("卖出")
    signed_quantity = enriched["quantity"].where(buy_mask, -enriched["quantity"])
    open_quantities = signed_quantity.groupby(enriched["symbol"]).sum()
    net_investment = (
        enriched.loc[buy_mask, "cash_amount"].sum()
        - enriched.loc[sell_mask, "cash_amount"].sum()
    )
    return {
        "record_count": int(len(enriched)),
        "position_count": int(open_quantities.gt(0).sum()),
        "buy_amount": float(enriched.loc[buy_mask, "gross_amount"].sum()),
        "fee_amount": float(enriched["fee_amount"].sum()),
        "net_investment": float(net_investment),
    }


def build_live_positions(trades: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "name",
        "symbol",
        "quantity",
        "average_cost",
        "cost_basis",
        "realized_pnl",
        "fee_amount",
    ]
    enriched = enrich_live_trades(trades)
    if enriched.empty:
        return pd.DataFrame(columns=columns)

    states: dict[str, dict[str, object]] = {}
    ordered = enriched.sort_values(["trade_date", "id"])
    for row in ordered.itertuples(index=False):
        state = states.setdefault(
            str(row.symbol),
            {
                "name": str(row.name),
                "symbol": str(row.symbol),
                "quantity": 0,
                "cost_basis": 0.0,
                "realized_pnl": 0.0,
                "fee_amount": 0.0,
            },
        )
        state["name"] = str(row.name)
        state["fee_amount"] = float(state["fee_amount"]) + float(row.fee_amount)
        if row.side == "买入":
            state["quantity"] = int(state["quantity"]) + int(row.quantity)
            state["cost_basis"] = float(state["cost_basis"]) + float(row.cash_amount)
            continue

        held_quantity = int(state["quantity"])
        if held_quantity < int(row.quantity):
            raise ValueError(f"{row.symbol} 的历史卖出数量超过当时持仓。")
        average_cost = float(state["cost_basis"]) / held_quantity if held_quantity else 0.0
        removed_cost = average_cost * int(row.quantity)
        state["quantity"] = held_quantity - int(row.quantity)
        state["cost_basis"] = max(0.0, float(state["cost_basis"]) - removed_cost)
        state["realized_pnl"] = (
            float(state["realized_pnl"]) + float(row.cash_amount) - removed_cost
        )

    rows = []
    for state in states.values():
        quantity = int(state["quantity"])
        if quantity <= 0:
            continue
        cost_basis = float(state["cost_basis"])
        rows.append(
            {
                **state,
                "average_cost": cost_basis / quantity,
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values("symbol").reset_index(drop=True)
