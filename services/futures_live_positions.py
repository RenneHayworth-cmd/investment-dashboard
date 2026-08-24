from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
import re

import pandas as pd

from core.db import get_conn, init_db
from services.futures_spread import completed_futures_daily_cutoff
from services.market_calendar import get_market_window, is_market_holiday
from services.futures_live_models import (
    ASSET_TYPES,
    BUY_SELL_VALUES,
    OPEN_CLOSE_VALUES,
    OPTION_EXPIRY_OUTCOMES,
    normalize_contract,
)
from services.futures_live_statement_parser import (
    _date_text,
    _number,
    _time_text,
    _trade_multiplier,
)
from services.futures_live_repository import (
    _effective_manual_trades,
    latest_monthly_account,
    list_futures_live_trades,
    list_month_end_positions,
    load_daily_closes,
)

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
