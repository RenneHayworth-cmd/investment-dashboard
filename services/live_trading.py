from __future__ import annotations

import calendar
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
LIVE_DAILY_PNL_COLUMNS = [
    "date",
    "market_value",
    "cost_basis",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "cumulative_buy_cost",
    "net_investment",
    "return_pct",
]
LIVE_DAILY_RETURN_COLUMNS = [
    "date",
    "pnl_amount",
    "return_base",
    "return_pct",
    "daily_buy_cost",
    "daily_sell_proceeds",
]
LIVE_PERIOD_RETURN_COLUMNS = [
    "period_start",
    "period_end",
    "label",
    "pnl_amount",
    "return_pct",
]
LIVE_RETURN_PERIODS = ("day", "week", "month", "year")
LIVE_POSITION_PERFORMANCE_COLUMNS = [
    "name",
    "symbol",
    "quantity",
    "average_cost",
    "cost_basis",
    "latest_price",
    "market_value",
    "realized_pnl",
    "fee_amount",
    "valuation_date",
    "daily_pnl",
    "daily_return_pct",
    "daily_return_base",
    "cumulative_pnl",
    "cumulative_return_pct",
    "cumulative_buy_cost",
]
LIVE_SYMBOL_PNL_COLUMNS = [
    "name",
    "symbol",
    "status",
    "first_trade_date",
    "last_trade_date",
    "quantity",
    "cumulative_buy_cost",
    "cumulative_sell_proceeds",
    "market_value",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "return_pct",
    "fee_amount",
    "valuation_date",
]


def normalize_live_trade_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if match:
        return match.group(1)
    if not text:
        raise ValueError("请输入标的代码。")
    return text


def live_close_refresh_due(
    *,
    target_date: date | datetime | str | pd.Timestamp,
    market_now: datetime,
    last_attempt: object = None,
    last_target_date: object = None,
    retry_seconds: int = 600,
) -> bool:
    """Allow backfilling any completed session, including weekends and mornings."""
    target = pd.to_datetime(target_date, errors="coerce")
    if pd.isna(target) or pd.Timestamp(target).date() > market_now.date():
        return False

    previous_target = pd.to_datetime(last_target_date, errors="coerce")
    if pd.isna(previous_target) or pd.Timestamp(previous_target).date() != pd.Timestamp(target).date():
        return True

    previous_attempt = pd.to_datetime(last_attempt, errors="coerce")
    if pd.isna(previous_attempt):
        return True
    now_timestamp = pd.Timestamp(market_now)
    if now_timestamp.tzinfo is not None:
        now_timestamp = now_timestamp.tz_localize(None)
    previous_attempt = pd.Timestamp(previous_attempt)
    if previous_attempt.tzinfo is not None:
        previous_attempt = previous_attempt.tz_localize(None)
    return (now_timestamp - previous_attempt).total_seconds() >= int(retry_seconds)


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
    normalized_strategy = str(strategy or "").strip()
    normalized_notes = str(notes or "").strip()
    normalized_record_key = str(record_key).strip() if record_key else None

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
                normalized_record_key,
                normalized_trade_date,
                normalized_symbol,
                normalized_name,
                normalized_side,
                price,
                quantity,
                fee_rate_pct,
                normalized_strategy,
                normalized_notes,
                created_at,
            ),
        )
        created = cursor.rowcount > 0
        if created:
            trade_id = int(cursor.lastrowid)
        elif normalized_record_key:
            row = conn.execute(
                """
                SELECT id, trade_date, symbol, name, side, price, quantity,
                       fee_rate_pct, strategy, notes
                FROM live_trades WHERE record_key=?
                """,
                (normalized_record_key,),
            ).fetchone()
            if row is None:
                raise ValueError("成交记录未能保存。")
            existing = tuple(row[1:])
            requested = (
                normalized_trade_date,
                normalized_symbol,
                normalized_name,
                normalized_side,
                price,
                quantity,
                fee_rate_pct,
                normalized_strategy,
                normalized_notes,
            )
            if existing != requested:
                raise ValueError("record_key 已被另一笔不同的成交记录使用。")
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


def _normalize_live_price_history(df: pd.DataFrame | None) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    date_column = next(
        (column for column in ("date", "日期", "trade_date") if column in df.columns),
        None,
    )
    price_column = next(
        (column for column in ("price", "收盘价", "close") if column in df.columns),
        None,
    )
    if date_column is None or price_column is None:
        return pd.Series(dtype="float64")
    result = df[[date_column, price_column]].copy()
    result[date_column] = pd.to_datetime(result[date_column], errors="coerce").dt.normalize()
    result[price_column] = pd.to_numeric(result[price_column], errors="coerce")
    result = (
        result.dropna(subset=[date_column, price_column])
        .sort_values(date_column)
        .drop_duplicates(subset=[date_column], keep="last")
    )
    if result.empty:
        return pd.Series(dtype="float64")
    return result.set_index(date_column)[price_column].astype(float)


def build_live_daily_pnl(
    trades: pd.DataFrame,
    price_histories: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Rebuild close-based account P&L without persisting derived snapshots."""
    enriched = enrich_live_trades(trades)
    if enriched.empty:
        return pd.DataFrame(columns=LIVE_DAILY_PNL_COLUMNS)
    enriched["trade_date"] = pd.to_datetime(
        enriched["trade_date"], errors="coerce"
    ).dt.normalize()
    enriched = enriched.dropna(subset=["trade_date"]).sort_values(["trade_date", "id"])
    if enriched.empty:
        return pd.DataFrame(columns=LIVE_DAILY_PNL_COLUMNS)

    histories = {
        normalize_live_trade_symbol(symbol): _normalize_live_price_history(history)
        for symbol, history in price_histories.items()
    }
    histories = {symbol: history for symbol, history in histories.items() if not history.empty}
    if not histories:
        return pd.DataFrame(columns=LIVE_DAILY_PNL_COLUMNS)

    valuation_dates = set(enriched["trade_date"].tolist())
    for history in histories.values():
        valuation_dates.update(history.index.tolist())
    first_trade_date = pd.Timestamp(enriched["trade_date"].min())
    valuation_dates = sorted(
        pd.Timestamp(value) for value in valuation_dates if pd.Timestamp(value) >= first_trade_date
    )

    trades_by_date = {
        pd.Timestamp(trade_date): group.sort_values("id")
        for trade_date, group in enriched.groupby("trade_date")
    }
    price_by_date: dict[pd.Timestamp, dict[str, float]] = {}
    history_end_dates: dict[str, pd.Timestamp] = {}
    for symbol, history in histories.items():
        history_end_dates[symbol] = pd.Timestamp(history.index.max())
        for price_date, price in history.items():
            price_by_date.setdefault(pd.Timestamp(price_date), {})[symbol] = float(price)

    states: dict[str, dict[str, float | int]] = {}
    latest_prices: dict[str, float] = {}
    cumulative_buy_cost = 0.0
    cumulative_sell_proceeds = 0.0
    rows: list[dict[str, float | pd.Timestamp]] = []
    for valuation_date in valuation_dates:
        latest_prices.update(price_by_date.get(valuation_date, {}))
        day_trades = trades_by_date.get(valuation_date)
        if day_trades is not None:
            for trade in day_trades.itertuples(index=False):
                symbol = normalize_live_trade_symbol(trade.symbol)
                state = states.setdefault(
                    symbol,
                    {"quantity": 0, "cost_basis": 0.0, "realized_pnl": 0.0},
                )
                if trade.side == "买入":
                    state["quantity"] = int(state["quantity"]) + int(trade.quantity)
                    state["cost_basis"] = float(state["cost_basis"]) + float(trade.cash_amount)
                    cumulative_buy_cost += float(trade.cash_amount)
                    continue

                held_quantity = int(state["quantity"])
                if held_quantity < int(trade.quantity):
                    raise ValueError(f"{symbol} 的历史卖出数量超过当时持仓。")
                average_cost = float(state["cost_basis"]) / held_quantity if held_quantity else 0.0
                removed_cost = average_cost * int(trade.quantity)
                state["quantity"] = held_quantity - int(trade.quantity)
                state["cost_basis"] = max(0.0, float(state["cost_basis"]) - removed_cost)
                state["realized_pnl"] = (
                    float(state["realized_pnl"]) + float(trade.cash_amount) - removed_cost
                )
                cumulative_sell_proceeds += float(trade.cash_amount)

        held_symbols = [
            symbol for symbol, state in states.items() if int(state["quantity"]) > 0
        ]
        complete_close = all(
            symbol in latest_prices
            and symbol in history_end_dates
            and history_end_dates[symbol] >= valuation_date
            for symbol in held_symbols
        )
        if not complete_close:
            continue

        market_value = sum(
            int(states[symbol]["quantity"]) * latest_prices[symbol]
            for symbol in held_symbols
        )
        cost_basis = sum(float(state["cost_basis"]) for state in states.values())
        realized_pnl = sum(float(state["realized_pnl"]) for state in states.values())
        unrealized_pnl = market_value - cost_basis
        total_pnl = realized_pnl + unrealized_pnl
        rows.append(
            {
                "date": valuation_date,
                "market_value": market_value,
                "cost_basis": cost_basis,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": total_pnl,
                "cumulative_buy_cost": cumulative_buy_cost,
                "net_investment": cumulative_buy_cost - cumulative_sell_proceeds,
                "return_pct": (
                    total_pnl / cumulative_buy_cost * 100
                    if cumulative_buy_cost > 0
                    else 0.0
                ),
            }
        )
    return pd.DataFrame(rows, columns=LIVE_DAILY_PNL_COLUMNS)


def build_live_daily_returns(daily_pnl: pd.DataFrame) -> pd.DataFrame:
    """Derive close-to-close holding returns without treating trades as profit."""
    required_columns = {
        "date",
        "market_value",
        "total_pnl",
        "cumulative_buy_cost",
        "net_investment",
    }
    if daily_pnl is None or daily_pnl.empty or not required_columns.issubset(daily_pnl.columns):
        return pd.DataFrame(columns=LIVE_DAILY_RETURN_COLUMNS)

    data = daily_pnl[list(required_columns)].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    numeric_columns = required_columns - {"date"}
    for column in numeric_columns:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = (
        data.dropna(subset=list(required_columns))
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    if data.empty:
        return pd.DataFrame(columns=LIVE_DAILY_RETURN_COLUMNS)

    previous_total_pnl = data["total_pnl"].shift(1, fill_value=0.0)
    pnl_amount = data["total_pnl"] - previous_total_pnl
    daily_buy_cost = data["cumulative_buy_cost"].diff()
    daily_buy_cost.iloc[0] = data.iloc[0]["cumulative_buy_cost"]
    daily_buy_cost = daily_buy_cost.clip(lower=0.0)

    cumulative_sell_proceeds = data["cumulative_buy_cost"] - data["net_investment"]
    daily_sell_proceeds = cumulative_sell_proceeds.diff()
    daily_sell_proceeds.iloc[0] = cumulative_sell_proceeds.iloc[0]
    daily_sell_proceeds = daily_sell_proceeds.clip(lower=0.0)

    previous_market_value = data["market_value"].shift(1, fill_value=0.0)
    net_new_investment = (daily_buy_cost - daily_sell_proceeds).clip(lower=0.0)
    return_base = previous_market_value + net_new_investment
    starts_from_empty = previous_market_value.le(0.0) & daily_buy_cost.gt(0.0)
    return_base = return_base.where(~starts_from_empty, daily_buy_cost)

    return_pct = pd.Series(pd.NA, index=data.index, dtype="Float64")
    valid_base = return_base.gt(0.0)
    return_pct.loc[valid_base] = (
        pnl_amount.loc[valid_base] / return_base.loc[valid_base] * 100
    )
    no_exposure_change = ~valid_base & pnl_amount.abs().le(1e-12)
    return_pct.loc[no_exposure_change] = 0.0

    return pd.DataFrame(
        {
            "date": data["date"],
            "pnl_amount": pnl_amount.astype(float),
            "return_base": return_base.astype(float),
            "return_pct": return_pct,
            "daily_buy_cost": daily_buy_cost.astype(float),
            "daily_sell_proceeds": daily_sell_proceeds.astype(float),
        },
        columns=LIVE_DAILY_RETURN_COLUMNS,
    )


def _compound_return_pct(values: pd.Series) -> float | object:
    rates = pd.to_numeric(values, errors="coerce")
    if rates.isna().any():
        return pd.NA
    return float(((1.0 + rates / 100.0).prod() - 1.0) * 100.0)


def build_live_period_returns(
    daily_returns: pd.DataFrame,
    *,
    period: str,
    excluded_dates: set[date] | None = None,
) -> pd.DataFrame:
    """Aggregate daily holding returns into calendar day, week, month, or year."""
    normalized_period = str(period or "").strip().lower()
    if normalized_period not in LIVE_RETURN_PERIODS:
        raise ValueError(f"不支持的收益周期：{period}。")
    required_columns = {"date", "pnl_amount", "return_pct"}
    if (
        daily_returns is None
        or daily_returns.empty
        or not required_columns.issubset(daily_returns.columns)
    ):
        return pd.DataFrame(columns=LIVE_PERIOD_RETURN_COLUMNS)

    data = daily_returns[list(required_columns)].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data["pnl_amount"] = pd.to_numeric(data["pnl_amount"], errors="coerce")
    data["return_pct"] = pd.to_numeric(data["return_pct"], errors="coerce")
    data = (
        data.dropna(subset=["date", "pnl_amount"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    data = data[data["date"].dt.weekday.lt(5)].copy()
    if excluded_dates:
        normalized_excluded_dates = {
            pd.Timestamp(value).date()
            for value in excluded_dates
            if not pd.isna(pd.Timestamp(value))
        }
        data = data[~data["date"].dt.date.isin(normalized_excluded_dates)].copy()
    if data.empty:
        return pd.DataFrame(columns=LIVE_PERIOD_RETURN_COLUMNS)

    if normalized_period == "day":
        return pd.DataFrame(
            {
                "period_start": data["date"],
                "period_end": data["date"],
                "label": data["date"].dt.strftime("%d"),
                "pnl_amount": data["pnl_amount"].astype(float),
                "return_pct": data["return_pct"].astype("Float64"),
            },
            columns=LIVE_PERIOD_RETURN_COLUMNS,
        )

    frequency = {
        "week": "W-SUN",
        "month": "M",
        "year": "Y",
    }[normalized_period]
    data["_period"] = data["date"].dt.to_period(frequency)
    rows: list[dict[str, object]] = []
    for period_key, group in data.groupby("_period", sort=True):
        period_start = pd.Timestamp(period_key.start_time).normalize()
        period_end = (
            period_start + pd.Timedelta(days=4)
            if normalized_period == "week"
            else pd.Timestamp(period_key.end_time).normalize()
        )
        if normalized_period == "week":
            iso_year, iso_week, _weekday = period_start.isocalendar()
            label = f"{iso_year}年第{iso_week:02d}周"
        elif normalized_period == "month":
            label = f"{period_start.month}月"
        else:
            label = f"{period_start.year}年"
        rows.append(
            {
                "period_start": period_start,
                "period_end": period_end,
                "label": label,
                "pnl_amount": float(group["pnl_amount"].sum()),
                "return_pct": _compound_return_pct(group["return_pct"]),
            }
        )
    return pd.DataFrame(rows, columns=LIVE_PERIOD_RETURN_COLUMNS)


def build_live_return_month_grid(year: int, month: int) -> list[list[date]]:
    """Build Monday-Friday rows for a monthly return calendar."""
    rows: list[list[date]] = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        weekdays = week[:5]
        if any(day.year == year and day.month == month for day in weekdays):
            rows.append(weekdays)
    return rows


def build_live_position_performance(
    trades: pd.DataFrame,
    price_histories: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    positions = build_live_positions(trades)
    if positions.empty:
        return pd.DataFrame(columns=LIVE_POSITION_PERFORMANCE_COLUMNS)

    normalized_histories = {
        normalize_live_trade_symbol(symbol): history
        for symbol, history in price_histories.items()
    }
    rows: list[dict[str, object]] = []
    for position in positions.itertuples(index=False):
        symbol = normalize_live_trade_symbol(position.symbol)
        symbol_trades = trades[
            trades["symbol"].astype(str).map(normalize_live_trade_symbol).eq(symbol)
        ].copy()
        symbol_daily = build_live_daily_pnl(
            symbol_trades,
            {symbol: normalized_histories.get(symbol)},
        )
        row = {
            "name": position.name,
            "symbol": symbol,
            "quantity": int(position.quantity),
            "average_cost": float(position.average_cost),
            "cost_basis": float(position.cost_basis),
            "latest_price": pd.NA,
            "market_value": pd.NA,
            "realized_pnl": float(position.realized_pnl),
            "fee_amount": float(position.fee_amount),
            "valuation_date": pd.NaT,
            "daily_pnl": pd.NA,
            "daily_return_pct": pd.NA,
            "daily_return_base": pd.NA,
            "cumulative_pnl": pd.NA,
            "cumulative_return_pct": pd.NA,
            "cumulative_buy_cost": pd.NA,
        }
        latest_trade_date = pd.to_datetime(
            symbol_trades["trade_date"], errors="coerce"
        ).max()
        if not symbol_daily.empty:
            latest = symbol_daily.iloc[-1]
            valuation_date = pd.Timestamp(latest["date"])
            if pd.isna(latest_trade_date) or valuation_date >= pd.Timestamp(latest_trade_date):
                previous = symbol_daily.iloc[-2] if len(symbol_daily) >= 2 else None
                previous_total_pnl = float(previous["total_pnl"]) if previous is not None else 0.0
                previous_market_value = (
                    float(previous["market_value"]) if previous is not None else 0.0
                )
                previous_buy_cost = (
                    float(previous["cumulative_buy_cost"]) if previous is not None else 0.0
                )
                current_buy_cost = float(latest["cumulative_buy_cost"])
                new_buy_cost = max(0.0, current_buy_cost - previous_buy_cost)
                daily_base = previous_market_value + new_buy_cost
                daily_pnl = float(latest["total_pnl"]) - previous_total_pnl
                row.update(
                    {
                        "valuation_date": valuation_date,
                        "latest_price": (
                            float(latest["market_value"]) / int(position.quantity)
                        ),
                        "market_value": float(latest["market_value"]),
                        "daily_pnl": daily_pnl,
                        "daily_return_pct": (
                            daily_pnl / daily_base * 100 if daily_base > 0 else 0.0
                        ),
                        "daily_return_base": daily_base,
                        "cumulative_pnl": float(latest["total_pnl"]),
                        "cumulative_return_pct": float(latest["return_pct"]),
                        "cumulative_buy_cost": current_buy_cost,
                    }
                )
        rows.append(row)
    return pd.DataFrame(rows, columns=LIVE_POSITION_PERFORMANCE_COLUMNS).sort_values(
        "symbol"
    ).reset_index(drop=True)


def summarize_live_position_performance(
    positions: pd.DataFrame,
) -> dict[str, float | object]:
    """Build a portfolio total without adding individual percentage returns."""
    if positions is None or positions.empty:
        return {
            "market_value": 0.0,
            "daily_pnl": 0.0,
            "daily_return_pct": 0.0,
            "cumulative_pnl": 0.0,
            "cumulative_return_pct": 0.0,
            "realized_pnl": 0.0,
            "fee_amount": 0.0,
        }

    def complete_sum(column: str) -> float | object:
        values = pd.to_numeric(positions[column], errors="coerce")
        return float(values.sum()) if values.notna().all() else pd.NA

    market_value = complete_sum("market_value")
    daily_pnl = complete_sum("daily_pnl")
    daily_return_base = complete_sum("daily_return_base")
    cumulative_pnl = complete_sum("cumulative_pnl")
    cumulative_buy_cost = complete_sum("cumulative_buy_cost")
    return {
        "market_value": market_value,
        "daily_pnl": daily_pnl,
        "daily_return_pct": (
            float(daily_pnl) / float(daily_return_base) * 100
            if not pd.isna(daily_pnl)
            and not pd.isna(daily_return_base)
            and float(daily_return_base) > 0
            else pd.NA
        ),
        "cumulative_pnl": cumulative_pnl,
        "cumulative_return_pct": (
            float(cumulative_pnl) / float(cumulative_buy_cost) * 100
            if not pd.isna(cumulative_pnl)
            and not pd.isna(cumulative_buy_cost)
            and float(cumulative_buy_cost) > 0
            else pd.NA
        ),
        "realized_pnl": float(
            pd.to_numeric(positions["realized_pnl"], errors="coerce").sum()
        ),
        "fee_amount": float(
            pd.to_numeric(positions["fee_amount"], errors="coerce").sum()
        ),
    }


def build_live_symbol_pnl_history(
    trades: pd.DataFrame,
    price_histories: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Summarize open and fully closed symbols from the complete live ledger."""
    enriched = enrich_live_trades(trades)
    if enriched.empty:
        return pd.DataFrame(columns=LIVE_SYMBOL_PNL_COLUMNS)
    enriched["trade_date"] = pd.to_datetime(
        enriched["trade_date"], errors="coerce"
    ).dt.normalize()
    enriched = enriched.dropna(subset=["trade_date"])
    sort_columns = ["trade_date"] + (["id"] if "id" in enriched.columns else [])
    enriched = enriched.sort_values(sort_columns)
    if enriched.empty:
        return pd.DataFrame(columns=LIVE_SYMBOL_PNL_COLUMNS)

    histories = {
        normalize_live_trade_symbol(symbol): _normalize_live_price_history(history)
        for symbol, history in price_histories.items()
    }
    states: dict[str, dict[str, object]] = {}
    for trade in enriched.itertuples(index=False):
        symbol = normalize_live_trade_symbol(trade.symbol)
        state = states.setdefault(
            symbol,
            {
                "name": str(trade.name),
                "symbol": symbol,
                "first_trade_date": pd.Timestamp(trade.trade_date),
                "last_trade_date": pd.Timestamp(trade.trade_date),
                "quantity": 0,
                "cost_basis": 0.0,
                "realized_pnl": 0.0,
                "cumulative_buy_cost": 0.0,
                "cumulative_sell_proceeds": 0.0,
                "fee_amount": 0.0,
            },
        )
        state["name"] = str(trade.name)
        state["last_trade_date"] = pd.Timestamp(trade.trade_date)
        state["fee_amount"] = float(state["fee_amount"]) + float(trade.fee_amount)
        if trade.side == "买入":
            state["quantity"] = int(state["quantity"]) + int(trade.quantity)
            state["cost_basis"] = float(state["cost_basis"]) + float(trade.cash_amount)
            state["cumulative_buy_cost"] = (
                float(state["cumulative_buy_cost"]) + float(trade.cash_amount)
            )
            continue

        held_quantity = int(state["quantity"])
        if held_quantity < int(trade.quantity):
            raise ValueError(f"{symbol} 的历史卖出数量超过当时持仓。")
        average_cost = float(state["cost_basis"]) / held_quantity if held_quantity else 0.0
        removed_cost = average_cost * int(trade.quantity)
        state["quantity"] = held_quantity - int(trade.quantity)
        state["cost_basis"] = max(0.0, float(state["cost_basis"]) - removed_cost)
        state["realized_pnl"] = (
            float(state["realized_pnl"]) + float(trade.cash_amount) - removed_cost
        )
        state["cumulative_sell_proceeds"] = (
            float(state["cumulative_sell_proceeds"]) + float(trade.cash_amount)
        )

    rows: list[dict[str, object]] = []
    for symbol, state in states.items():
        quantity = int(state["quantity"])
        realized_pnl = float(state["realized_pnl"])
        buy_cost = float(state["cumulative_buy_cost"])
        market_value: float | object = 0.0 if quantity == 0 else pd.NA
        unrealized_pnl: float | object = 0.0 if quantity == 0 else pd.NA
        total_pnl: float | object = realized_pnl if quantity == 0 else pd.NA
        valuation_date: pd.Timestamp | object = (
            pd.Timestamp(state["last_trade_date"]) if quantity == 0 else pd.NaT
        )
        if quantity > 0:
            history = histories.get(symbol, pd.Series(dtype="float64"))
            last_trade_date = pd.Timestamp(state["last_trade_date"])
            if not history.empty and pd.Timestamp(history.index.max()) >= last_trade_date:
                valuation_date = pd.Timestamp(history.index.max())
                latest_price = float(history.iloc[-1])
                market_value = quantity * latest_price
                unrealized_pnl = market_value - float(state["cost_basis"])
                total_pnl = realized_pnl + unrealized_pnl
        rows.append(
            {
                "name": state["name"],
                "symbol": symbol,
                "status": "持仓" if quantity > 0 else "已清仓",
                "first_trade_date": state["first_trade_date"],
                "last_trade_date": state["last_trade_date"],
                "quantity": quantity,
                "cumulative_buy_cost": buy_cost,
                "cumulative_sell_proceeds": float(state["cumulative_sell_proceeds"]),
                "market_value": market_value,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": total_pnl,
                "return_pct": (
                    float(total_pnl) / buy_cost * 100
                    if not pd.isna(total_pnl) and buy_cost > 0
                    else pd.NA
                ),
                "fee_amount": float(state["fee_amount"]),
                "valuation_date": valuation_date,
            }
        )
    return pd.DataFrame(rows, columns=LIVE_SYMBOL_PNL_COLUMNS).sort_values(
        ["status", "symbol"],
        ascending=[False, True],
    ).reset_index(drop=True)


def append_live_symbol_pnl_total(history: pd.DataFrame) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame(columns=LIVE_SYMBOL_PNL_COLUMNS)

    def complete_sum(column: str) -> float | object:
        values = pd.to_numeric(history[column], errors="coerce")
        return float(values.sum()) if values.notna().all() else pd.NA

    cumulative_buy_cost = float(
        pd.to_numeric(history["cumulative_buy_cost"], errors="coerce").sum()
    )
    total_pnl = complete_sum("total_pnl")
    total_row = {
        "name": "合计",
        "symbol": "-",
        "status": "-",
        "first_trade_date": pd.NaT,
        "last_trade_date": pd.NaT,
        "quantity": pd.NA,
        "cumulative_buy_cost": cumulative_buy_cost,
        "cumulative_sell_proceeds": float(
            pd.to_numeric(history["cumulative_sell_proceeds"], errors="coerce").sum()
        ),
        "market_value": complete_sum("market_value"),
        "realized_pnl": float(
            pd.to_numeric(history["realized_pnl"], errors="coerce").sum()
        ),
        "unrealized_pnl": complete_sum("unrealized_pnl"),
        "total_pnl": total_pnl,
        "return_pct": (
            float(total_pnl) / cumulative_buy_cost * 100
            if not pd.isna(total_pnl) and cumulative_buy_cost > 0
            else pd.NA
        ),
        "fee_amount": float(
            pd.to_numeric(history["fee_amount"], errors="coerce").sum()
        ),
        "valuation_date": pd.NaT,
    }
    return pd.concat(
        [history, pd.DataFrame([total_row], columns=LIVE_SYMBOL_PNL_COLUMNS)],
        ignore_index=True,
    )
