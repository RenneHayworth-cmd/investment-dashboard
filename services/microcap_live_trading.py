"""Independent A-share live-trading ledger for the microcap account."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO, StringIO
import csv
import json
import math
import re
import uuid

import pandas as pd

from core import db

FLOW_TYPES = ("期初资金", "资金转入", "资金转出", "现金分红", "其他收入", "其他支出")
ADJUSTMENT_TYPES = ("期初持仓", "送转")
TRADE_COLUMNS = [
    "id", "record_key", "trade_date", "trade_time", "symbol", "name", "side",
    "price", "quantity", "commission_amount", "stamp_tax_amount", "strategy",
    "notes", "source", "import_batch_id", "source_row", "created_at",
]
CASH_FLOW_COLUMNS = [
    "id", "record_key", "flow_date", "flow_time", "entry_type", "amount",
    "symbol", "notes", "created_at",
]
ADJUSTMENT_COLUMNS = [
    "id", "record_key", "event_date", "event_time", "symbol", "name",
    "adjustment_type", "quantity_delta", "cost_basis_delta", "notes", "created_at",
]
DAILY_COLUMNS = [
    "date", "market_value", "cost_basis", "cash", "total_assets", "realized_pnl",
    "unrealized_pnl", "total_pnl", "pnl_amount", "return_pct", "nav",
    "cumulative_return_pct", "external_flow", "missing_symbols",
]


def _conn(write: bool = False):
    db.init_db()
    connection = db.get_conn()
    connection.row_factory = __import__("sqlite3").Row
    if write:
        connection.execute("BEGIN IMMEDIATE")
    return connection


def _money(value, label="金额", *, allow_zero=True) -> float:
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError(f"{label}必须是有效数字。") from None
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        qualifier = "大于0" if not allow_zero else "不小于0"
        raise ValueError(f"{label}必须{qualifier}。")
    return float(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round_money(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _trade_gross(price, quantity) -> Decimal:
    return (Decimal(str(price)) * int(quantity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _date(value, label="日期") -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label}无效。")
    return parsed.date().isoformat()


def _time(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            pass
    parsed = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(parsed):
        raise ValueError("时间无效。")
    return pd.Timestamp(parsed).strftime("%H:%M:%S")


def normalize_microcap_symbol(value) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or "").strip())
    if not match:
        raise ValueError("A股代码必须包含六位数字。")
    return match.group(1)


def _settings_row(conn=None) -> dict:
    close = conn is None
    connection = conn or _conn()
    try:
        row = connection.execute(
            "SELECT buy_commission,sell_commission,stamp_tax_rate_pct,updated_at "
            "FROM microcap_live_settings WHERE id=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("微盘实盘费用设置未初始化。")
        return dict(row)
    finally:
        if close:
            connection.close()


def get_microcap_fee_settings() -> dict:
    return _settings_row()


def update_microcap_fee_settings(*, buy_commission: float, sell_commission: float) -> dict:
    buy = _money(buy_commission, "买入佣金")
    sell = _money(sell_commission, "卖出佣金")
    with closing(_conn(write=True)) as conn:
        conn.execute(
            "UPDATE microcap_live_settings SET buy_commission=?,sell_commission=?,updated_at=? WHERE id=1",
            (buy, sell, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    return get_microcap_fee_settings()


def _load_frames(conn=None):
    close = conn is None
    connection = conn or _conn()
    try:
        trades = pd.read_sql_query("SELECT * FROM microcap_live_trades ORDER BY trade_date,trade_time,id", connection)
        flows = pd.read_sql_query("SELECT * FROM microcap_live_cash_flows ORDER BY flow_date,flow_time,id", connection)
        adjustments = pd.read_sql_query(
            "SELECT * FROM microcap_live_position_adjustments ORDER BY event_date,event_time,id", connection
        )
        return trades, flows, adjustments
    finally:
        if close:
            connection.close()


def list_microcap_trades() -> pd.DataFrame:
    trades, _, _ = _load_frames()
    return trades if not trades.empty else pd.DataFrame(columns=TRADE_COLUMNS)


def list_microcap_cash_flows() -> pd.DataFrame:
    _, flows, _ = _load_frames()
    return flows if not flows.empty else pd.DataFrame(columns=CASH_FLOW_COLUMNS)


def list_microcap_position_adjustments() -> pd.DataFrame:
    _, _, adjustments = _load_frames()
    return adjustments if not adjustments.empty else pd.DataFrame(columns=ADJUSTMENT_COLUMNS)


def list_microcap_import_batches() -> pd.DataFrame:
    with closing(_conn()) as conn:
        frame = pd.read_sql_query(
            "SELECT id,file_hash,file_name,file_size,imported_at,mapping_json,row_count "
            "FROM microcap_live_import_batches ORDER BY id DESC", conn,
        )
    return frame


def _initial_cash_flow(conn):
    rows = conn.execute(
        "SELECT id,flow_date,amount FROM microcap_live_cash_flows WHERE entry_type='期初资金' ORDER BY id"
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("账户只能有一笔期初资金。")
    return rows[0] if rows else None


def _event_time(row, date_col, time_col):
    event_date = str(row[date_col])
    explicit = row[time_col] if time_col in row.keys() else None
    if explicit:
        return f"{event_date} {explicit}"
    created = str(row["created_at"] or "") if "created_at" in row.keys() else ""
    if len(created) >= 19:
        return f"{event_date} {created[11:19]}"
    return f"{event_date} 00:00:00"


def _flow_moment(row):
    if row["entry_type"] == "期初资金":
        return f"{row['flow_date']} 00:00:00"
    return _event_time(row, "flow_date", "flow_time")


def _adjustment_moment(row):
    if row["adjustment_type"] == "期初持仓":
        return f"{row['event_date']} 00:00:00"
    return _event_time(row, "event_date", "event_time")


def _ordered_events(conn):
    trades, flows, adjustments = _load_frames(conn)
    events = []
    for row in flows.to_dict("records"):
        priority = 0 if row["entry_type"] == "期初资金" else 1
        moment = _flow_moment(row)
        events.append((moment, priority, int(row["id"]), "flow", row))
    for row in adjustments.to_dict("records"):
        priority = 1 if row["adjustment_type"] == "期初持仓" else 2
        events.append((_adjustment_moment(row), priority, int(row["id"]), "adjustment", row))
    for row in trades.to_dict("records"):
        events.append((_event_time(row, "trade_date", "trade_time"), 3, int(row["id"]), "trade", row))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    return events


def _validate_ledger(conn):
    initial = _initial_cash_flow(conn)
    events = _ordered_events(conn)
    non_initial = [e for e in events if not (e[2] == (int(initial["id"]) if initial else -1) and e[3] == "flow")]
    if non_initial and initial is None:
        raise ValueError("请先录入期初资金。")
    if initial:
        first_date = min((e[0][:10] for e in non_initial), default=initial["flow_date"])
        if str(initial["flow_date"]) > first_date:
            raise ValueError(f"期初资金日期不能晚于首笔账户事件 {first_date}。")

    cash = Decimal("0")
    positions: dict[str, dict[str, Decimal | int | str]] = {}
    for _, _, event_id, kind, row in events:
        if kind == "flow":
            amount = Decimal(str(row["amount"]))
            entry = row["entry_type"]
            cash += -amount if entry in {"资金转出", "其他支出"} else amount
        elif kind == "trade":
            symbol = row["symbol"]
            state = positions.setdefault(symbol, {"quantity": 0, "basis": Decimal("0"), "name": row["name"]})
            quantity = int(row["quantity"])
            gross = _trade_gross(row["price"], quantity)
            commission = Decimal(str(row["commission_amount"]))
            tax = Decimal(str(row["stamp_tax_amount"]))
            if row["side"] == "买入":
                if tax != 0:
                    raise ValueError(f"第{event_id}笔买入不应收取印花税。")
                cash -= gross + commission
                state["quantity"] = int(state["quantity"]) + quantity
                state["basis"] = Decimal(state["basis"]) + gross + commission
            else:
                held = int(state["quantity"])
                if held < quantity:
                    raise ValueError(f"{symbol} 卖出数量超过当时持仓（持有{held}股，卖出{quantity}股）。")
                basis = Decimal(state["basis"])
                removed = basis * quantity / held if held else Decimal("0")
                state["quantity"] = held - quantity
                state["basis"] = max(Decimal("0"), basis - removed)
                cash += gross - commission - tax
        else:
            symbol = row["symbol"]
            state = positions.setdefault(symbol, {"quantity": 0, "basis": Decimal("0"), "name": row["name"]})
            state["quantity"] = int(state["quantity"]) + int(row["quantity_delta"])
            state["basis"] = Decimal(state["basis"]) + Decimal(str(row["cost_basis_delta"]))
            state["name"] = row["name"]
        if cash < Decimal("-0.005"):
            raise ValueError(f"{row.get('symbol', '') or row.get('entry_type', '')} 记账后账户现金为负（{cash:.2f}元）。")
    return {"cash": float(cash), "positions": positions}


def _has_initial_capital(conn) -> bool:
    return _initial_cash_flow(conn) is not None


def add_microcap_cash_flow(
    *, flow_date, entry_type: str, amount: float, flow_time=None,
    symbol: str = "", notes: str = "",
) -> int:
    day = _date(flow_date, "流水日期")
    time_text = _time(flow_time)
    kind = str(entry_type or "").strip()
    if kind not in FLOW_TYPES:
        raise ValueError("不支持的资金流水类型。")
    value = _money(amount, "流水金额", allow_zero=(kind == "期初资金"))
    code = normalize_microcap_symbol(symbol) if str(symbol or "").strip() else None
    if code and kind not in {"现金分红", "其他收入", "其他支出"}:
        raise ValueError("只有分红或其他收支可以关联股票代码。")
    connection = _conn(write=True)
    try:
        if kind == "期初资金":
            if _initial_cash_flow(connection):
                raise ValueError("账户已经录入期初资金；请删除原记录后再更正。")
        elif not _has_initial_capital(connection):
            raise ValueError("请先录入期初资金，再记录后续流水。")
        record_key = uuid.uuid4().hex
        cursor = connection.execute(
            "INSERT INTO microcap_live_cash_flows(record_key,flow_date,flow_time,entry_type,amount,symbol,notes,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (record_key, day, time_text, kind, value, code, str(notes or "").strip(), datetime.now().isoformat(timespec="seconds")),
        )
        _validate_ledger(connection)
        connection.commit()
        return int(cursor.lastrowid)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_microcap_cash_flow(flow_id: int) -> bool:
    connection = _conn(write=True)
    try:
        row = connection.execute("SELECT entry_type FROM microcap_live_cash_flows WHERE id=?", (int(flow_id),)).fetchone()
        if not row:
            connection.rollback()
            return False
        connection.execute("DELETE FROM microcap_live_cash_flows WHERE id=?", (int(flow_id),))
        _validate_ledger(connection)
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _trade_fees(side: str, gross: float, settings: dict, commission=None, stamp_tax=None):
    default_commission = settings["buy_commission"] if side == "买入" else settings["sell_commission"]
    commission_amount = _money(default_commission if commission is None else commission, "佣金")
    default_tax = _round_money(Decimal(str(gross)) * Decimal(str(settings["stamp_tax_rate_pct"])) / Decimal("100")) if side == "卖出" else 0.0
    tax_amount = default_tax if stamp_tax is None else _money(stamp_tax, "印花税")
    if side == "买入" and tax_amount != 0:
        raise ValueError("买入记录的印花税必须为0。")
    return commission_amount, tax_amount


def add_microcap_trade(
    *, trade_date, symbol, name, side, price, quantity, trade_time=None,
    commission_amount=None, stamp_tax_amount=None, strategy="", notes="",
    source="手工", record_key=None,
) -> int:
    day = _date(trade_date, "成交日期")
    time_text = _time(trade_time)
    code = normalize_microcap_symbol(symbol)
    display_name = str(name or "").strip() or code
    direction = str(side or "").strip()
    if direction not in {"买入", "卖出"}:
        raise ValueError("成交方向必须为买入或卖出。")
    trade_price = float(price)
    if not pd.notna(trade_price) or not math.isfinite(trade_price) or trade_price <= 0:
        raise ValueError("成交价格必须是有限的正数。")
    qty = int(quantity)
    if qty <= 0 or float(quantity) != qty:
        raise ValueError("成交数量必须为正整数。")
    gross = _trade_gross(trade_price, qty)
    settings = get_microcap_fee_settings()
    commission, tax = _trade_fees(direction, gross, settings, commission_amount, stamp_tax_amount)
    connection = _conn(write=True)
    try:
        if not _has_initial_capital(connection):
            raise ValueError("请先录入期初资金，再记录成交。")
        cursor = connection.execute(
            """INSERT INTO microcap_live_trades
            (record_key,trade_date,trade_time,symbol,name,side,price,quantity,commission_amount,
             stamp_tax_amount,strategy,notes,source,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record_key or uuid.uuid4().hex, day, time_text, code, display_name, direction,
             trade_price, qty, commission, tax, str(strategy or "").strip(), str(notes or "").strip(),
             str(source or "手工"), datetime.now().isoformat(timespec="seconds")),
        )
        _validate_ledger(connection)
        connection.commit()
        return int(cursor.lastrowid)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_microcap_trade(trade_id: int) -> bool:
    connection = _conn(write=True)
    try:
        cursor = connection.execute("DELETE FROM microcap_live_trades WHERE id=?", (int(trade_id),))
        if not cursor.rowcount:
            connection.rollback()
            return False
        _validate_ledger(connection)
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def add_microcap_position_adjustment(
    *, event_date, symbol, name, adjustment_type, quantity_delta, cost_basis_delta=0,
    event_time=None, notes="",
) -> int:
    day = _date(event_date, "调整日期")
    time_text = _time(event_time)
    code = normalize_microcap_symbol(symbol)
    kind = str(adjustment_type or "").strip()
    if kind not in ADJUSTMENT_TYPES:
        raise ValueError("调整类型必须为期初持仓或送转。")
    quantity = int(quantity_delta)
    if quantity <= 0 or float(quantity_delta) != quantity:
        raise ValueError("调整股数必须为正整数。")
    basis = _money(cost_basis_delta, "持仓总成本")
    if kind == "送转" and basis != 0:
        raise ValueError("送转只调整股数，不改变持仓总成本。")
    connection = _conn(write=True)
    try:
        if not _has_initial_capital(connection):
            raise ValueError("请先录入期初资金，再建立期初持仓。")
        if kind == "期初持仓":
            exists = connection.execute(
                "SELECT 1 FROM microcap_live_position_adjustments WHERE symbol=? AND adjustment_type='期初持仓'",
                (code,),
            ).fetchone()
            if exists:
                raise ValueError(f"{code} 已有期初持仓记录。")
        cursor = connection.execute(
            """INSERT INTO microcap_live_position_adjustments
            (record_key,event_date,event_time,symbol,name,adjustment_type,quantity_delta,cost_basis_delta,notes,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, day, time_text, code, str(name or "").strip() or code,
             kind, quantity, basis, str(notes or "").strip(), datetime.now().isoformat(timespec="seconds")),
        )
        _validate_ledger(connection)
        connection.commit()
        return int(cursor.lastrowid)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_microcap_position_adjustment(adjustment_id: int) -> bool:
    connection = _conn(write=True)
    try:
        cursor = connection.execute(
            "DELETE FROM microcap_live_position_adjustments WHERE id=?", (int(adjustment_id),)
        )
        if not cursor.rowcount:
            connection.rollback()
            return False
        _validate_ledger(connection)
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _states_after(events):
    cash = 0.0
    positions: dict[str, dict] = {}
    realized = 0.0
    fees_by_symbol: dict[str, float] = {}
    cumulative_dividends = 0.0
    external_flows = 0.0
    for event in events:
        _, _, _, kind, row = event
        if kind == "flow":
            value = float(row["amount"])
            entry = row["entry_type"]
            cash += -value if entry in {"资金转出", "其他支出"} else value
            if entry in {"期初资金", "资金转入"}:
                external_flows += value
            elif entry == "资金转出":
                external_flows -= value
            elif entry == "现金分红":
                cumulative_dividends += value
                realized += value
                if row.get("symbol"):
                    state = positions.setdefault(row["symbol"], {
                        "symbol": row["symbol"], "name": row.get("name") or row["symbol"],
                        "quantity": 0, "cost_basis": 0.0, "realized_pnl": 0.0,
                        "invested_basis": 0.0,
                    })
                    state["realized_pnl"] += value
            elif entry == "其他收入":
                realized += value
                if row.get("symbol"):
                    state = positions.setdefault(row["symbol"], {
                        "symbol": row["symbol"], "name": row.get("name") or row["symbol"],
                        "quantity": 0, "cost_basis": 0.0, "realized_pnl": 0.0,
                        "invested_basis": 0.0,
                    })
                    state["realized_pnl"] += value
            elif entry == "其他支出":
                realized -= value
                if row.get("symbol"):
                    state = positions.setdefault(row["symbol"], {
                        "symbol": row["symbol"], "name": row.get("name") or row["symbol"],
                        "quantity": 0, "cost_basis": 0.0, "realized_pnl": 0.0,
                        "invested_basis": 0.0,
                    })
                    state["realized_pnl"] -= value
        elif kind == "adjustment":
            code = row["symbol"]
            state = positions.setdefault(code, {"symbol": code, "name": row["name"], "quantity": 0, "cost_basis": 0.0, "realized_pnl": 0.0, "invested_basis": 0.0})
            state["name"] = row["name"]
            state["quantity"] += int(row["quantity_delta"])
            state["cost_basis"] += float(row["cost_basis_delta"])
            state["invested_basis"] += float(row["cost_basis_delta"])
        else:
            code = row["symbol"]
            state = positions.setdefault(code, {"symbol": code, "name": row["name"], "quantity": 0, "cost_basis": 0.0, "realized_pnl": 0.0, "invested_basis": 0.0})
            state["name"] = row["name"]
            qty = int(row["quantity"])
            gross = float(_trade_gross(row["price"], qty))
            commission = float(row["commission_amount"])
            tax = float(row["stamp_tax_amount"])
            fees_by_symbol[code] = fees_by_symbol.get(code, 0.0) + commission + tax
            if row["side"] == "买入":
                cash -= gross + commission + tax
                state["quantity"] += qty
                state["cost_basis"] += gross + commission + tax
                state["invested_basis"] += gross + commission + tax
            else:
                held = state["quantity"]
                removed_basis = state["cost_basis"] * qty / held if held else 0.0
                state["quantity"] -= qty
                state["cost_basis"] = max(0.0, state["cost_basis"] - removed_basis)
                proceeds = gross - commission - tax
                cash += proceeds
                realized += proceeds - removed_basis
                state["realized_pnl"] += proceeds - removed_basis
    for state in positions.values():
        state["fee_amount"] = fees_by_symbol.get(state["symbol"], 0.0)
        state["average_cost"] = state["cost_basis"] / state["quantity"] if state["quantity"] else 0.0
    return cash, positions, realized, external_flows, cumulative_dividends


def build_microcap_positions(trades=None, adjustments=None) -> pd.DataFrame:
    if trades is None or adjustments is None:
        loaded_trades, _, loaded_adjustments = _load_frames()
        trades = loaded_trades if trades is None else trades
        adjustments = loaded_adjustments if adjustments is None else adjustments
    events = []
    for row in trades.to_dict("records") if trades is not None else []:
        events.append((_event_time(row, "trade_date", "trade_time"), 3, int(row.get("id", 0)), "trade", row))
    for row in adjustments.to_dict("records") if adjustments is not None else []:
        priority = 1 if row["adjustment_type"] == "期初持仓" else 2
        events.append((_adjustment_moment(row), priority, int(row.get("id", 0)), "adjustment", row))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    _, states, _, _, _ = _states_after(events)
    rows = [state for state in states.values() if int(state["quantity"]) > 0]
    columns = ["name", "symbol", "quantity", "average_cost", "cost_basis", "invested_basis", "realized_pnl", "fee_amount"]
    return pd.DataFrame(rows, columns=columns).sort_values("symbol").reset_index(drop=True) if rows else pd.DataFrame(columns=columns)


def build_microcap_symbol_history(trades=None, adjustments=None, price_histories=None, cash_flows=None) -> pd.DataFrame:
    """Keep realized results visible for closed symbols as well as open holdings."""
    if trades is None or adjustments is None or cash_flows is None:
        loaded_trades, loaded_flows, loaded_adjustments = _load_frames()
        trades = loaded_trades if trades is None else trades
        cash_flows = loaded_flows if cash_flows is None else cash_flows
        adjustments = loaded_adjustments if adjustments is None else adjustments
    trade_rows = trades.to_dict("records") if trades is not None else []
    flow_rows = cash_flows.to_dict("records") if cash_flows is not None else []
    adjustment_rows = adjustments.to_dict("records") if adjustments is not None else []
    events = []
    for row in flow_rows:
        priority = 0 if row["entry_type"] == "期初资金" else 1
        events.append((_flow_moment(row), priority, int(row.get("id", 0)), "flow", row))
    for row in trade_rows:
        events.append((_event_time(row, "trade_date", "trade_time"), 3, int(row.get("id", 0)), "trade", row))
    for row in adjustment_rows:
        priority = 1 if row["adjustment_type"] == "期初持仓" else 2
        events.append((_adjustment_moment(row), priority, int(row.get("id", 0)), "adjustment", row))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    _, states, _, _, _ = _states_after(events)
    histories = _history_map(price_histories)
    totals = {}
    for row in trade_rows:
        code = row["symbol"]
        stat = totals.setdefault(code, {"buy_cost": 0.0, "sell_proceeds": 0.0, "fees": 0.0, "first_date": row["trade_date"], "last_date": row["trade_date"]})
        gross = float(_trade_gross(row["price"], row["quantity"]))
        fees = float(row["commission_amount"]) + float(row["stamp_tax_amount"])
        stat["fees"] += fees
        stat["last_date"] = row["trade_date"]
        if row["side"] == "买入":
            stat["buy_cost"] += gross + fees
        else:
            stat["sell_proceeds"] += gross - fees
    for row in adjustment_rows:
        code = row["symbol"]
        stat = totals.setdefault(code, {"buy_cost": 0.0, "sell_proceeds": 0.0, "fees": 0.0, "first_date": row["event_date"], "last_date": row["event_date"]})
        stat["buy_cost"] += float(row["cost_basis_delta"])
        if str(row["event_date"]) < str(stat["first_date"]):
            stat["first_date"] = row["event_date"]
        stat["last_date"] = max(str(stat["last_date"]), str(row["event_date"]))
    for row in flow_rows:
        code = row.get("symbol")
        if not code:
            continue
        day = row["flow_date"]
        stat = totals.setdefault(code, {"buy_cost": 0.0, "sell_proceeds": 0.0, "fees": 0.0, "first_date": day, "last_date": day})
        stat["first_date"] = min(str(stat["first_date"]), str(day))
        stat["last_date"] = max(str(stat["last_date"]), str(day))
    rows = []
    for code, state in states.items():
        stat = totals.get(code, {})
        qty = int(state["quantity"])
        close, close_day = _latest_close((price_histories or {}).get(code), code) if qty > 0 else (None, None)
        market_value = _round_money(qty * close) if close is not None else (0.0 if qty <= 0 else pd.NA)
        basis = float(state["cost_basis"])
        unrealized = float(market_value) - basis if qty > 0 and pd.notna(market_value) else 0.0 if qty <= 0 else pd.NA
        realized = float(state["realized_pnl"])
        total_pnl = realized + float(unrealized) if pd.notna(unrealized) else pd.NA
        capital_basis = max(float(stat.get("buy_cost", 0.0)), 0.0)
        rows.append({
            "symbol": code, "name": state["name"], "status": "持仓" if qty > 0 else "已清仓",
            "first_date": stat.get("first_date"), "last_date": stat.get("last_date"),
            "quantity": qty, "cumulative_buy_cost": capital_basis,
            "cumulative_sell_proceeds": float(stat.get("sell_proceeds", 0.0)),
            "market_value": market_value, "realized_pnl": realized,
            "unrealized_pnl": unrealized, "total_pnl": total_pnl,
            "return_pct": float(total_pnl) / capital_basis * 100 if pd.notna(total_pnl) and capital_basis > 0 else pd.NA,
            "fee_amount": float(stat.get("fees", 0.0)), "valuation_date": close_day,
        })
    columns = ["symbol", "name", "status", "first_date", "last_date", "quantity", "cumulative_buy_cost", "cumulative_sell_proceeds", "market_value", "realized_pnl", "unrealized_pnl", "total_pnl", "return_pct", "fee_amount", "valuation_date"]
    return pd.DataFrame(rows, columns=columns).sort_values("symbol").reset_index(drop=True) if rows else pd.DataFrame(columns=columns)


def _history_map(price_histories: dict[str, pd.DataFrame] | None):
    normalized = {}
    for raw_symbol, frame in (price_histories or {}).items():
        try:
            symbol = normalize_microcap_symbol(raw_symbol)
        except ValueError:
            continue
        if frame is None or frame.empty:
            normalized[symbol] = {}
            continue
        date_col = next((c for c in ("date", "日期", "trade_date") if c in frame.columns), None)
        price_col = next((c for c in ("close", "收盘价", "price") if c in frame.columns), None)
        if date_col is None or price_col is None:
            normalized[symbol] = {}
            continue
        dates = pd.to_datetime(frame[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        prices = pd.to_numeric(frame[price_col], errors="coerce")
        normalized[symbol] = {str(d): float(p) for d, p in zip(dates, prices) if pd.notna(d) and pd.notna(p) and float(p) > 0}
    return normalized


def _realized_through(events, day):
    partial = [e for e in events if e[0][:10] <= day]
    _, positions, realized, external, dividends = _states_after(partial)
    basis = sum(float(p["cost_basis"]) for p in positions.values() if p["quantity"] > 0)
    return positions, realized, external, dividends, basis


def build_microcap_daily_returns(
    trades=None, cash_flows=None, adjustments=None, price_histories: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    if trades is None or cash_flows is None or adjustments is None:
        loaded_trades, loaded_flows, loaded_adjustments = _load_frames()
        trades = loaded_trades if trades is None else trades
        cash_flows = loaded_flows if cash_flows is None else cash_flows
        adjustments = loaded_adjustments if adjustments is None else adjustments
    histories = _history_map(price_histories)
    events = []
    for row in cash_flows.to_dict("records") if cash_flows is not None else []:
        priority = 0 if row["entry_type"] == "期初资金" else 1
        events.append((_flow_moment(row), priority, int(row.get("id", 0)), "flow", row))
    for row in adjustments.to_dict("records") if adjustments is not None else []:
        priority = 1 if row["adjustment_type"] == "期初持仓" else 2
        events.append((_adjustment_moment(row), priority, int(row.get("id", 0)), "adjustment", row))
    for row in trades.to_dict("records") if trades is not None else []:
        events.append((_event_time(row, "trade_date", "trade_time"), 3, int(row.get("id", 0)), "trade", row))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    event_dates = [e[0][:10] for e in events]
    all_dates = sorted({day for values in histories.values() for day in values} | set(event_dates))
    if not all_dates:
        empty = pd.DataFrame(columns=DAILY_COLUMNS)
        empty.attrs["missing_days"] = []
        return empty
    if event_dates:
        all_dates = [day for day in all_dates if day >= min(event_dates)]
    rows = []
    previous_assets = None
    previous_external = 0.0
    nav = 1.0
    cumulative_pnl = 0.0
    baseline_equity = None
    event_index = 0
    state_cash = 0.0
    state_positions: dict[str, dict] = {}
    opening_quantities: dict[str, int] = {}
    cumulative_external = 0.0
    cumulative_realized = 0.0
    total_events = len(events)
    missing_days = []
    for day in all_dates:
        while event_index < total_events and events[event_index][0][:10] <= day:
            event = events[event_index]
            _, _, _, kind, row = event
            if kind == "flow":
                amount = float(row["amount"])
                entry = row["entry_type"]
                state_cash += -amount if entry in {"资金转出", "其他支出"} else amount
                if entry in {"期初资金", "资金转入"}:
                    cumulative_external += amount
                elif entry == "资金转出":
                    cumulative_external -= amount
                elif entry in {"现金分红", "其他收入"}:
                    cumulative_realized += amount
                elif entry == "其他支出":
                    cumulative_realized -= amount
            elif kind == "adjustment":
                code = row["symbol"]
                state = state_positions.setdefault(code, {"quantity": 0, "cost_basis": 0.0, "name": row["name"], "realized_pnl": 0.0})
                state["quantity"] += int(row["quantity_delta"])
                state["cost_basis"] += float(row["cost_basis_delta"])
                opening_quantities[code] = opening_quantities.get(code, 0) + int(row["quantity_delta"])
                state["name"] = row["name"]
            else:
                code = row["symbol"]
                state = state_positions.setdefault(code, {"quantity": 0, "cost_basis": 0.0, "name": row["name"], "realized_pnl": 0.0})
                quantity = int(row["quantity"])
                gross = float(_trade_gross(row["price"], quantity))
                fees = float(row["commission_amount"]) + float(row["stamp_tax_amount"])
                if row["side"] == "买入":
                    state_cash -= gross + fees
                    state["quantity"] += quantity
                    state["cost_basis"] += gross + fees
                else:
                    held = state["quantity"]
                    removed = state["cost_basis"] * quantity / held if held else 0.0
                    state["quantity"] -= quantity
                    state["cost_basis"] = max(0.0, state["cost_basis"] - removed)
                    net = gross - fees
                    state_cash += net
                    state["realized_pnl"] += net - removed
                    cumulative_realized += net - removed
            event_index += 1
        active = {code: p for code, p in state_positions.items() if p["quantity"] > 0}
        missing = sorted(code for code in active if day not in histories.get(code, {}))
        if missing:
            missing_days.append({"date": day, "symbols": missing})
            continue
        market_value = sum(_round_money(p["quantity"] * histories[code][day]) for code, p in active.items())
        basis = sum(float(p["cost_basis"]) for p in active.values())
        assets = state_cash + market_value
        if baseline_equity is None:
            opening_market_value = sum(
                _round_money(quantity * histories.get(code, {}).get(day, 0.0))
                for code, quantity in opening_quantities.items()
            )
            baseline_equity = cumulative_external + opening_market_value
            daily_pnl = assets - baseline_equity
            daily_return = daily_pnl / baseline_equity if baseline_equity > 0 else float("nan")
            cumulative_pnl = daily_pnl
            nav = 1.0 + daily_return if pd.notna(daily_return) else 1.0
            external_flow = 0.0
        else:
            external_flow = cumulative_external - previous_external
            daily_pnl = assets - previous_assets - external_flow
            denominator = previous_assets + external_flow
            daily_return = daily_pnl / denominator if denominator > 0 else float("nan")
            if pd.notna(daily_return):
                nav *= 1.0 + daily_return
                cumulative_pnl += daily_pnl
        rows.append({
            "date": pd.Timestamp(day), "market_value": market_value, "cost_basis": basis,
            "cash": state_cash, "total_assets": assets, "realized_pnl": cumulative_realized,
            "unrealized_pnl": market_value - basis, "total_pnl": cumulative_pnl,
            "pnl_amount": daily_pnl, "return_pct": daily_return * 100 if pd.notna(daily_return) else pd.NA,
            "nav": nav, "cumulative_return_pct": (nav - 1) * 100,
            "external_flow": external_flow, "missing_symbols": [],
        })
        previous_assets = assets
        previous_external = cumulative_external
    result = pd.DataFrame(rows, columns=DAILY_COLUMNS)
    result.attrs["missing_days"] = missing_days
    return result


def build_microcap_daily_pnl(*args, **kwargs) -> pd.DataFrame:
    return build_microcap_daily_returns(*args, **kwargs)


def _latest_close(history, symbol):
    prices = _history_map({symbol: history}).get(symbol, {})
    if not prices:
        return None, None
    day = max(prices)
    return prices[day], day


def _symbol_daily_pnl(symbol, valuation_day, mark_price, previous_close, events):
    prior_events = [event for event in events if event[0][:10] < valuation_day]
    _, prior_positions, _, _, _ = _states_after(prior_events)
    opening_quantity = int(prior_positions.get(symbol, {}).get("quantity", 0))
    if opening_quantity and previous_close is None:
        return pd.NA, pd.NA
    pnl = _round_money(opening_quantity * (mark_price - previous_close)) if opening_quantity else 0.0
    return_base = _round_money(opening_quantity * previous_close) if opening_quantity else 0.0
    for event in events:
        if event[0][:10] != valuation_day:
            continue
        _, _, _, kind, row = event
        if kind == "trade" and row["symbol"] == symbol:
            quantity = int(row["quantity"])
            mark_value = _round_money(quantity * mark_price)
            gross = float(_trade_gross(row["price"], quantity))
            fees = float(row["commission_amount"]) + float(row["stamp_tax_amount"])
            if row["side"] == "买入":
                pnl += mark_value - gross - fees
                return_base += gross
            else:
                pnl += gross - fees - mark_value
        elif kind == "adjustment" and row["symbol"] == symbol and row["adjustment_type"] == "送转":
            pnl += _round_money(int(row["quantity_delta"]) * mark_price)
        elif kind == "flow" and row.get("symbol") == symbol:
            if row["entry_type"] in {"现金分红", "其他收入"}:
                pnl += float(row["amount"])
            elif row["entry_type"] == "其他支出":
                pnl -= float(row["amount"])
    return _round_money(pnl), return_base if return_base > 0 else pd.NA


def build_microcap_account_snapshot(
    trades=None, cash_flows=None, adjustments=None, price_histories=None,
    quotes: dict[str, dict] | None = None, market_now: datetime | None = None,
) -> dict:
    if trades is None or cash_flows is None or adjustments is None:
        loaded_trades, loaded_flows, loaded_adjustments = _load_frames()
        trades = loaded_trades if trades is None else trades
        cash_flows = loaded_flows if cash_flows is None else cash_flows
        adjustments = loaded_adjustments if adjustments is None else adjustments
    trades = trades if trades is not None else pd.DataFrame(columns=TRADE_COLUMNS)
    cash_flows = cash_flows if cash_flows is not None else pd.DataFrame(columns=CASH_FLOW_COLUMNS)
    adjustments = adjustments if adjustments is not None else pd.DataFrame(columns=ADJUSTMENT_COLUMNS)
    histories = _history_map(price_histories)
    formal_daily = build_microcap_daily_returns(trades, cash_flows, adjustments, price_histories)
    base_positions = build_microcap_positions(trades, adjustments)
    # Current ledger state includes funds and fills even if no formal price is available.
    events = []
    for row in cash_flows.to_dict("records"):
        priority = 0 if row["entry_type"] == "期初资金" else 1
        events.append((_flow_moment(row), priority, int(row.get("id", 0)), "flow", row))
    for row in adjustments.to_dict("records"):
        priority = 1 if row["adjustment_type"] == "期初持仓" else 2
        events.append((_adjustment_moment(row), priority, int(row.get("id", 0)), "adjustment", row))
    for row in trades.to_dict("records"):
        events.append((_event_time(row, "trade_date", "trade_time"), 3, int(row.get("id", 0)), "trade", row))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    cash, states, realized, external, dividends = _states_after(events)
    current_positions = []
    quote_map = {normalize_microcap_symbol(k): v for k, v in (quotes or {}).items()}
    now = market_now or datetime.now()
    for state in states.values():
        qty = int(state["quantity"])
        if qty <= 0:
            continue
        code = state["symbol"]
        quote = quote_map.get(code, {})
        quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
        real_price = pd.to_numeric(quote.get("price"), errors="coerce")
        if not pd.isna(real_price) and float(real_price) > 0 and not pd.isna(quote_time) and quote_time.date() == now.date():
            price = float(real_price)
            valuation_date = quote_time
            status = "实时"
            source = str(quote.get("source") or "盘中报价")
        else:
            price, close_date = _latest_close((price_histories or {}).get(code), code)
            valuation_date = pd.to_datetime(close_date, errors="coerce") if close_date else pd.NaT
            status = "正式收盘" if close_date else "缺少行情"
            source = str(quote.get("source") or "本地正式日线")
        market_value = _round_money(qty * price) if price is not None else float("nan")
        previous_close = histories.get(code, {})
        dates = sorted(previous_close)
        valuation_day = quote_time.strftime("%Y-%m-%d") if status == "实时" else (dates[-1] if dates else "")
        prior_days = [day for day in dates if day < valuation_day]
        prev_close = previous_close[prior_days[-1]] if prior_days else None
        position_day_pnl, position_return_base = (
            _symbol_daily_pnl(code, valuation_day, price, prev_close, events)
            if price is not None and valuation_day
            else (pd.NA, pd.NA)
        )
        daily_return_pct = (
            position_day_pnl / position_return_base * 100
            if pd.notna(position_day_pnl) and pd.notna(position_return_base) and position_return_base > 0
            else pd.NA
        )
        current_positions.append({
            "name": state["name"], "symbol": code, "quantity": qty,
            "average_cost": float(state["average_cost"]), "cost_basis": float(state["cost_basis"]),
            "cumulative_buy_cost": float(state["invested_basis"]),
            "latest_price": price if price is not None else pd.NA, "market_value": market_value,
            "realized_pnl": float(state["realized_pnl"]), "fee_amount": float(state["fee_amount"]),
            "daily_pnl": position_day_pnl,
            "daily_return_base": position_return_base,
            "daily_return_pct": daily_return_pct,
            "cumulative_pnl": float(state["realized_pnl"]) + (market_value - float(state["cost_basis"]) if price is not None else 0),
            "cumulative_return_pct": (
                (float(state["realized_pnl"]) + (market_value - float(state["cost_basis"])))
                / float(state["invested_basis"]) * 100
                if price is not None and float(state["invested_basis"]) > 0 else pd.NA
            ),
            "price_status": status, "price_time": str(quote_time) if status == "实时" else str(close_date or ""),
            "price_source": source, "valuation_date": valuation_date,
        })
    positions = pd.DataFrame(current_positions)
    if positions.empty:
        positions = pd.DataFrame(columns=[
            "name", "symbol", "quantity", "average_cost", "cost_basis", "latest_price", "market_value",
            "realized_pnl", "fee_amount", "daily_pnl", "daily_return_pct", "cumulative_pnl",
            "cumulative_return_pct", "daily_return_base", "price_status", "price_time", "price_source", "valuation_date",
        ])
    priced = positions[pd.to_numeric(positions.get("market_value"), errors="coerce").notna()] if not positions.empty else positions
    market_value = float(priced["market_value"].sum()) if len(priced) else 0.0
    complete = len(priced) == len(positions)
    total_assets = cash + market_value if complete else pd.NA
    cost_basis = float(positions["cost_basis"].sum()) if not positions.empty else 0.0
    latest_formal = formal_daily.iloc[-1] if not formal_daily.empty else None
    baseline = None
    if latest_formal is not None:
        baseline = float(latest_formal["total_assets"] - latest_formal["total_pnl"])
    if baseline is None and total_assets is not pd.NA:
        baseline = float(total_assets)
    if complete and latest_formal is not None:
        formal_day = pd.Timestamp(latest_formal["date"]).date().isoformat()
        new_external = 0.0
        for flow in cash_flows.to_dict("records"):
            flow_day = str(flow["flow_date"])
            if flow_day > formal_day and flow["entry_type"] in {"期初资金", "资金转入"}:
                new_external += float(flow["amount"])
            elif flow_day > formal_day and flow["entry_type"] == "资金转出":
                new_external -= float(flow["amount"])
        current_pnl = float(latest_formal["total_pnl"]) + float(total_assets) - float(latest_formal["total_assets"]) - new_external
        baseline = float(latest_formal["total_assets"] - latest_formal["total_pnl"])
    elif complete and not formal_daily.empty:
        current_pnl = float(formal_daily.iloc[-1]["total_pnl"])
    else:
        current_pnl = pd.NA
    return_pct = current_pnl / baseline * 100 if pd.notna(current_pnl) and baseline and baseline > 0 else pd.NA
    warnings = []
    missing_days = formal_daily.attrs.get("missing_days", [])
    if missing_days:
        recent_gaps = missing_days[-5:]
        gap_text = "；".join(f"{item['date']}缺{','.join(item['symbols'])}" for item in recent_gaps)
        suffix = f"；另有{len(missing_days) - len(recent_gaps)}个日期" if len(missing_days) > len(recent_gaps) else ""
        warnings.append(f"历史正式行情缺口（这些日期未计入收益曲线）：{gap_text}{suffix}")
    if not complete:
        missing = positions.loc[pd.to_numeric(positions["market_value"], errors="coerce").isna(), "symbol"].astype(str).tolist()
        warnings.append("持仓行情缺失，账户总资产暂不可完整计算：" + ", ".join(missing))
    initialized = not cash_flows.empty and cash_flows["entry_type"].astype(str).eq("期初资金").any()
    realtime_used = any(p.get("price_status") == "实时" for p in current_positions)
    if complete and latest_formal is not None and realtime_used:
        formal_day = pd.Timestamp(latest_formal["date"]).date().isoformat()
        today_external = 0.0
        for flow in cash_flows.to_dict("records"):
            if str(flow["flow_date"]) > formal_day:
                if flow["entry_type"] in {"期初资金", "资金转入"}:
                    today_external += float(flow["amount"])
                elif flow["entry_type"] == "资金转出":
                    today_external -= float(flow["amount"])
        daily_pnl = float(total_assets) - float(latest_formal["total_assets"]) - today_external
        daily_base = float(latest_formal["total_assets"]) + today_external
        daily_return_pct = daily_pnl / daily_base * 100 if daily_base > 0 else pd.NA
    elif latest_formal is not None:
        daily_pnl = float(latest_formal["pnl_amount"])
        daily_return_pct = float(latest_formal["return_pct"]) if pd.notna(latest_formal["return_pct"]) else pd.NA
    else:
        daily_pnl = pd.NA
        daily_return_pct = pd.NA
    summary = {
        "initialized": initialized,
        "valuation_date": now.date().isoformat() if realtime_used else (None if latest_formal is None else latest_formal["date"]),
        "cash": cash, "market_value": market_value, "total_assets": total_assets,
        "cost_basis": cost_basis, "realized_pnl": realized, "unrealized_pnl": market_value - cost_basis if complete else pd.NA,
        "account_pnl": current_pnl, "cumulative_return_pct": return_pct,
        "daily_pnl": daily_pnl, "daily_return_pct": daily_return_pct,
        "fee_amount": float(trades[["commission_amount", "stamp_tax_amount"]].sum().sum()) if not trades.empty else 0.0,
        "dividends": dividends, "position_ratio_pct": market_value / float(total_assets) * 100 if complete and float(total_assets) > 0 else pd.NA,
        "nav": 1 + return_pct / 100 if pd.notna(return_pct) else pd.NA,
    }
    return {"initialized": initialized, "summary": summary, "positions": positions,
            "formal_daily": formal_daily, "warnings": warnings}


def _read_import_file(raw: bytes, file_name: str) -> pd.DataFrame:
    suffix = str(file_name).lower().rsplit(".", 1)[-1]
    if suffix not in {"csv", "xls", "xlsx"}:
        raise ValueError("仅支持CSV、XLS或XLSX文件。")

    # Broker statements are sometimes plain GB18030 TSV/CSV files with an
    # .xls suffix. Select an Excel engine from the file signature, not the
    # extension, and route text exports through the delimited-text reader.
    if raw.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return pd.read_excel(BytesIO(raw), engine="xlrd", dtype=object)
    if raw.startswith(b"PK\x03\x04"):
        return pd.read_excel(BytesIO(raw), engine="openpyxl", dtype=object)

    decode_errors = []
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            decode_errors.append(exc)
            continue

        sample = "\n".join(line for line in text.splitlines() if line.strip())[:8192]
        if not sample:
            raise ValueError("文件内容为空。")
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters="\t,;|").delimiter
        except csv.Error:
            # Broker exports with numeric rows can confuse Sniffer. Prefer
            # the delimiter used consistently across the sampled lines.
            lines = sample.splitlines()
            scores = []
            for candidate in ("\t", ",", ";", "|"):
                counts = [line.count(candidate) for line in lines]
                nonzero = [count for count in counts if count]
                if nonzero:
                    common = max(set(nonzero), key=nonzero.count)
                    scores.append((nonzero.count(common) / len(lines), common, candidate))
            delimiter = max(scores, default=(0, 0, ","))[2]
        try:
            return pd.read_csv(StringIO(text), sep=delimiter, dtype=object, engine="python")
        except (pd.errors.ParserError, UnicodeError) as exc:
            raise ValueError(f"文本型交割单无法解析：{exc}") from exc

    if decode_errors:
        raise ValueError(f"文件不是可识别的Excel或文本交割单，文本编码无法识别：{decode_errors[-1]}")
    raise ValueError("仅支持CSV、XLS或XLSX文件。")


def _numeric(value, label, *, optional=False):
    if value is None or pd.isna(value) or str(value).strip() == "":
        if optional:
            return None
        raise ValueError(f"{label}缺失。")
    text = str(value).strip().replace(",", "").replace("元", "")
    try:
        result = float(text)
    except ValueError:
        raise ValueError(f"{label}不是有效数字：{value}") from None
    if not pd.notna(result) or not math.isfinite(result):
        raise ValueError(f"{label}不是有效数字：{value}")
    return result


def _import_side(value):
    text = str(value or "").strip().upper()
    if any(token in text for token in ("买入", "买", "BUY", "B")):
        return "买入"
    if any(token in text for token in ("卖出", "卖", "SELL", "S")):
        return "卖出"
    raise ValueError(f"无法识别成交方向：{value}")


def _is_cash_dividend_import_row(value) -> bool:
    text = str(value or "").strip()
    return any(label in text for label in ("红利入账", "现金分红", "股息入账", "派息入账"))


def _mapped_value(row, mapping, field):
    column = mapping.get(field)
    if not column or column not in row:
        return None
    value = row[column]
    return None if pd.isna(value) or str(value).strip() == "" else value


def inspect_microcap_import_columns(file_bytes: bytes, file_name: str) -> dict:
    """Return input headers for the page's user-controlled column mapper."""
    frame = _read_import_file(bytes(file_bytes), file_name)
    return {"columns": [str(column) for column in frame.columns], "row_count": len(frame)}


def preview_microcap_trade_import(file_bytes: bytes, file_name: str, mapping: dict[str, str]) -> dict:
    raw = bytes(file_bytes)
    file_hash = sha256(raw).hexdigest()
    if not isinstance(mapping, dict):
        raise ValueError("请先配置导入列映射。")
    try:
        frame = _read_import_file(raw, file_name)
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "rows": [], "file_hash": file_hash,
                "file_name": file_name, "file_size": len(raw), "mapping": mapping, "duplicate": False}
    required = ("trade_date", "symbol", "side", "price", "quantity")
    missing_mappings = [field for field in required if not mapping.get(field) or mapping[field] not in frame.columns]
    if missing_mappings:
        return {"ok": False, "errors": ["缺少必需列映射：" + ", ".join(missing_mappings)], "rows": [],
                "file_hash": file_hash, "file_name": file_name, "file_size": len(raw), "mapping": mapping, "duplicate": False}
    with closing(_conn()) as conn:
        duplicate = conn.execute("SELECT id,row_count FROM microcap_live_import_batches WHERE file_hash=?", (file_hash,)).fetchone()
    if duplicate:
        return {"ok": False, "duplicate": True, "errors": [f"该文件已导入（批次#{duplicate['id']}，{duplicate['row_count']}笔）。"],
                "rows": [], "file_hash": file_hash, "file_name": file_name, "file_size": len(raw), "mapping": mapping}
    settings = get_microcap_fee_settings()
    rows, errors, ignored_rows = [], [], []
    for offset, source in frame.iterrows():
        source_row = int(offset) + 2
        operation_value = _mapped_value(source, mapping, "side")
        if _is_cash_dividend_import_row(operation_value):
            ignored_rows.append({
                "source_row": source_row, "kind": "cash_dividend",
                "description": str(operation_value).strip(),
            })
            continue
        try:
            trade_day = _date(_mapped_value(source, mapping, "trade_date"), "成交日期")
            symbol = normalize_microcap_symbol(_mapped_value(source, mapping, "symbol"))
            side = _import_side(operation_value)
            price = _numeric(_mapped_value(source, mapping, "price"), "成交价格")
            quantity_value = _numeric(_mapped_value(source, mapping, "quantity"), "成交数量")
            if int(quantity_value) != quantity_value or quantity_value <= 0:
                raise ValueError("成交数量必须为正整数。")
            if price <= 0:
                raise ValueError("成交价格必须大于0。")
            gross = _trade_gross(price, int(quantity_value))
            commission_input = _numeric(_mapped_value(source, mapping, "commission_amount"), "佣金", optional=True)
            tax_input = _numeric(_mapped_value(source, mapping, "stamp_tax_amount"), "印花税", optional=True)
            total_fee = _numeric(_mapped_value(source, mapping, "total_fee"), "总费用", optional=True)
            if total_fee is not None:
                if commission_input is not None and tax_input is not None:
                    if abs(commission_input + tax_input - total_fee) > 0.02:
                        raise ValueError("佣金与印花税合计和总费用不一致。")
                elif commission_input is not None:
                    tax_input = total_fee - commission_input
                else:
                    tax_input = tax_input if tax_input is not None else (
                        _round_money(gross * Decimal(str(settings["stamp_tax_rate_pct"])) / Decimal("100")) if side == "卖出" else 0.0
                    )
                    commission_input = total_fee - tax_input
                if commission_input < -0.005 or (tax_input is not None and tax_input < -0.005):
                    raise ValueError("拆分后的佣金或印花税为负，请核对文件字段映射。")
            if commission_input is None:
                commission_input = settings["buy_commission"] if side == "买入" else settings["sell_commission"]
            if tax_input is None:
                tax_input = _round_money(gross * Decimal(str(settings["stamp_tax_rate_pct"])) / Decimal("100")) if side == "卖出" else 0.0
            commission = _money(commission_input, "佣金")
            tax = _money(tax_input, "印花税")
            if side == "买入" and tax != 0:
                raise ValueError("买入印花税必须为0。")
            name_value = _mapped_value(source, mapping, "name")
            time_value = _mapped_value(source, mapping, "trade_time")
            strategy_value = _mapped_value(source, mapping, "strategy")
            notes_value = _mapped_value(source, mapping, "notes")
            rows.append({
                "trade_date": trade_day, "trade_time": _time(time_value), "symbol": symbol,
                "name": str(name_value or symbol).strip(), "side": side,
                "price": float(price), "quantity": int(quantity_value),
                "commission_amount": commission, "stamp_tax_amount": tax,
                "strategy": str(strategy_value or "").strip(), "notes": str(notes_value or "").strip(),
                "source_row": source_row,
            })
        except Exception as exc:
            errors.append(f"第{source_row}行：{exc}")
    if not rows and not errors:
        errors.append("文件没有可导入的成交行。")
    return {"ok": bool(rows) and not errors, "duplicate": False, "errors": errors, "rows": rows,
            "ignored_rows": ignored_rows, "file_hash": file_hash, "file_name": str(file_name),
            "file_size": len(raw), "mapping": mapping}


def commit_microcap_trade_import(preview: dict) -> dict:
    if not preview or not preview.get("ok") or not preview.get("rows"):
        raise ValueError("导入预览未通过，不能写入。")
    connection = _conn(write=True)
    try:
        duplicate = connection.execute("SELECT id,row_count FROM microcap_live_import_batches WHERE file_hash=?", (preview["file_hash"],)).fetchone()
        if duplicate:
            connection.rollback()
            return {"duplicate": True, "batch_id": int(duplicate["id"]), "row_count": int(duplicate["row_count"])}
        if not _has_initial_capital(connection):
            raise ValueError("请先录入期初资金，再导入成交。")
        cursor = connection.execute(
            "INSERT INTO microcap_live_import_batches(file_hash,file_name,file_size,imported_at,mapping_json,row_count) VALUES(?,?,?,?,?,?)",
            (preview["file_hash"], preview["file_name"], int(preview["file_size"]),
             datetime.now().isoformat(timespec="seconds"), json.dumps(preview["mapping"], ensure_ascii=False, sort_keys=True), len(preview["rows"])),
        )
        batch_id = int(cursor.lastrowid)
        for row in preview["rows"]:
            source_row = int(row["source_row"])
            connection.execute(
                """INSERT INTO microcap_live_trades
                (record_key,trade_date,trade_time,symbol,name,side,price,quantity,commission_amount,
                 stamp_tax_amount,strategy,notes,source,import_batch_id,source_row,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"{preview['file_hash']}:{source_row}", row["trade_date"], row.get("trade_time"),
                 row["symbol"], row["name"], row["side"], row["price"], row["quantity"],
                 row["commission_amount"], row["stamp_tax_amount"], row.get("strategy", ""),
                 row.get("notes", ""), preview["file_name"], batch_id, source_row,
                 datetime.now().isoformat(timespec="seconds")),
            )
        _validate_ledger(connection)
        connection.commit()
        return {"duplicate": False, "batch_id": batch_id, "row_count": len(preview["rows"])}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_microcap_import_batch(batch_id: int) -> bool:
    connection = _conn(write=True)
    try:
        batch = connection.execute("SELECT id FROM microcap_live_import_batches WHERE id=?", (int(batch_id),)).fetchone()
        if not batch:
            connection.rollback()
            return False
        connection.execute("DELETE FROM microcap_live_trades WHERE import_batch_id=?", (int(batch_id),))
        connection.execute("DELETE FROM microcap_live_import_batches WHERE id=?", (int(batch_id),))
        _validate_ledger(connection)
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "FLOW_TYPES", "ADJUSTMENT_TYPES", "TRADE_COLUMNS", "CASH_FLOW_COLUMNS", "ADJUSTMENT_COLUMNS",
    "DAILY_COLUMNS", "normalize_microcap_symbol", "get_microcap_fee_settings", "update_microcap_fee_settings",
    "list_microcap_trades", "list_microcap_cash_flows", "list_microcap_position_adjustments", "list_microcap_import_batches",
    "add_microcap_trade", "delete_microcap_trade", "add_microcap_cash_flow", "delete_microcap_cash_flow",
    "add_microcap_position_adjustment", "delete_microcap_position_adjustment", "build_microcap_positions", "build_microcap_symbol_history",
    "build_microcap_daily_pnl", "build_microcap_daily_returns", "build_microcap_account_snapshot",
    "inspect_microcap_import_columns", "preview_microcap_trade_import", "commit_microcap_trade_import", "delete_microcap_import_batch",
]
