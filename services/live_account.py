"""ETF实盘账户资金流水、账户估值和临时实时快照。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time as datetime_time

import pandas as pd

from core import db
from services.live_trading import (
    build_live_daily_pnl,
    build_live_position_performance,
    build_live_positions,
    enrich_live_trades,
    normalize_live_trade_symbol,
    summarize_live_position_performance,
)


LIVE_CASH_FLOW_TYPES = (
    "期初资金",
    "资金转入",
    "资金转出",
    "现金分红",
    "利息",
    "其他收入",
    "其他支出",
)
LIVE_EXTERNAL_CASH_FLOW_TYPES = {"期初资金", "资金转入", "资金转出"}
LIVE_CASH_FLOW_COLUMNS = [
    "id",
    "flow_date",
    "flow_time",
    "entry_type",
    "amount",
    "symbol",
    "notes",
    "created_at",
]
LIVE_ACCOUNT_DAILY_COLUMNS = [
    "date",
    "market_value",
    "cash",
    "total_assets",
    "external_flow",
    "positive_external_flow",
    "cumulative_external_capital",
    "account_pnl",
    "daily_pnl",
    "return_base",
    "daily_return_pct",
    "nav",
    "cumulative_return_pct",
    "holding_pnl",
    "holding_return_pct",
]


def _normalize_date(value: date | datetime | str | pd.Timestamp, *, label: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label}无效。")
    return pd.Timestamp(parsed).strftime("%Y-%m-%d")


def _normalize_optional_time(value: object, *, label: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%H:%M:%S")
        except (TypeError, ValueError):
            pass
    parsed = pd.to_datetime(str(value).strip(), errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label}无效。")
    return pd.Timestamp(parsed).strftime("%H:%M:%S")


def add_live_cash_flow(
    *,
    flow_date: date | datetime | str | pd.Timestamp,
    flow_time: object = None,
    entry_type: str,
    amount: float,
    symbol: str = "",
    notes: str = "",
) -> int:
    """Save one manually maintained ETF-account cash event."""
    db.init_db()
    normalized_date = _normalize_date(flow_date, label="流水日期")
    normalized_time = _normalize_optional_time(flow_time, label="流水时间")
    normalized_type = str(entry_type or "").strip()
    if normalized_type not in LIVE_CASH_FLOW_TYPES:
        raise ValueError("不支持的资金流水类型。")
    normalized_amount = float(amount)
    if normalized_amount <= 0:
        raise ValueError("流水金额必须大于0。")
    normalized_symbol = (
        normalize_live_trade_symbol(symbol) if str(symbol or "").strip() else None
    )
    if normalized_symbol and (len(normalized_symbol) != 6 or not normalized_symbol.isdigit()):
        raise ValueError("关联ETF代码必须为六位数字。")
    if normalized_symbol and normalized_type not in {"现金分红", "其他收入", "其他支出"}:
        raise ValueError("只有分红或其他收支可以关联ETF代码。")

    with closing(db.get_conn()) as conn:
        opening = conn.execute(
            "SELECT id, flow_date FROM live_cash_flows WHERE entry_type='期初资金' LIMIT 1"
        ).fetchone()
        if normalized_type == "期初资金":
            if opening is not None:
                raise ValueError("账户已经录入期初资金；如需更正，请先删除原记录。")
            first_trade = conn.execute("SELECT MIN(trade_date) FROM live_trades").fetchone()[0]
            first_flow = conn.execute("SELECT MIN(flow_date) FROM live_cash_flows").fetchone()[0]
            first_event = min(
                [value for value in (first_trade, first_flow) if value is not None],
                default=None,
            )
            if first_event is not None and normalized_date > str(first_event):
                raise ValueError(f"期初资金日期不能晚于首笔账户事件 {first_event}。")
        elif opening is None:
            raise ValueError("请先录入期初资金，再记录后续资金流水。")

        cursor = conn.execute(
            """
            INSERT INTO live_cash_flows (
                flow_date, flow_time, entry_type, amount, symbol, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_date,
                normalized_time,
                normalized_type,
                normalized_amount,
                normalized_symbol,
                str(notes or "").strip(),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def list_live_cash_flows() -> pd.DataFrame:
    db.init_db()
    with closing(db.get_conn()) as conn:
        result = pd.read_sql_query(
            """
            SELECT id, flow_date, flow_time, entry_type, amount, symbol, notes, created_at
            FROM live_cash_flows
            ORDER BY flow_date DESC, COALESCE(flow_time, '') DESC, id DESC
            """,
            conn,
        )
    if result.empty:
        return pd.DataFrame(columns=LIVE_CASH_FLOW_COLUMNS)
    return result


def delete_live_cash_flow(flow_id: int) -> bool:
    db.init_db()
    with closing(db.get_conn()) as conn:
        cursor = conn.execute("DELETE FROM live_cash_flows WHERE id=?", (int(flow_id),))
        conn.commit()
        return cursor.rowcount > 0


def live_account_is_initialized(cash_flows: pd.DataFrame | None) -> bool:
    return bool(
        cash_flows is not None
        and not cash_flows.empty
        and "entry_type" in cash_flows.columns
        and cash_flows["entry_type"].astype(str).eq("期初资金").any()
    )


def _cash_flow_effect(entry_type: str, amount: float) -> tuple[float, float, float]:
    cash_sign = -1.0 if entry_type in {"资金转出", "其他支出"} else 1.0
    external_sign = (
        -1.0
        if entry_type == "资金转出"
        else 1.0
        if entry_type in {"期初资金", "资金转入"}
        else 0.0
    )
    positive_external = amount if entry_type in {"期初资金", "资金转入"} else 0.0
    return cash_sign * amount, external_sign * amount, positive_external


def _normalize_cash_flows(cash_flows: pd.DataFrame | None) -> pd.DataFrame:
    if cash_flows is None or cash_flows.empty:
        return pd.DataFrame(
            columns=LIVE_CASH_FLOW_COLUMNS
            + ["cash_delta", "external_delta", "positive_external"]
        )
    result = cash_flows.copy()
    for column in LIVE_CASH_FLOW_COLUMNS:
        if column not in result.columns:
            result[column] = None
    result["flow_date"] = pd.to_datetime(result["flow_date"], errors="coerce").dt.normalize()
    result["amount"] = pd.to_numeric(result["amount"], errors="coerce")
    result = result.dropna(subset=["flow_date", "amount"])
    effects = [
        _cash_flow_effect(str(row.entry_type), float(row.amount))
        for row in result.itertuples(index=False)
    ]
    result["cash_delta"] = [item[0] for item in effects]
    result["external_delta"] = [item[1] for item in effects]
    result["positive_external"] = [item[2] for item in effects]
    return result


def build_live_account_daily(
    trades: pd.DataFrame,
    cash_flows: pd.DataFrame,
    price_histories: dict[str, pd.DataFrame],
    *,
    holding_daily: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build account equity after neutralizing deposits and withdrawals."""
    if not live_account_is_initialized(cash_flows):
        return pd.DataFrame(columns=LIVE_ACCOUNT_DAILY_COLUMNS)
    flows = _normalize_cash_flows(cash_flows)
    holdings = (
        build_live_daily_pnl(trades, price_histories)
        if holding_daily is None
        else holding_daily.copy()
    )
    if not holdings.empty:
        holdings["date"] = pd.to_datetime(holdings["date"], errors="coerce").dt.normalize()
        holdings = holdings.dropna(subset=["date"]).sort_values("date")

    enriched_trades = enrich_live_trades(trades)
    if not enriched_trades.empty:
        enriched_trades["trade_date"] = pd.to_datetime(
            enriched_trades["trade_date"], errors="coerce"
        ).dt.normalize()
        enriched_trades["trade_cash_delta"] = enriched_trades["cash_amount"]
        enriched_trades.loc[
            enriched_trades["side"].eq("买入"), "trade_cash_delta"
        ] *= -1

    valuation_dates = (
        set(pd.to_datetime(holdings["date"], errors="coerce").dropna())
        if not holdings.empty
        else set()
    )
    flow_dates = set(pd.to_datetime(flows["flow_date"], errors="coerce").dropna())
    if trades is None or trades.empty:
        valuation_dates.update(flow_dates)
    else:
        first_trade = pd.to_datetime(trades["trade_date"], errors="coerce").min()
        valuation_dates.update(date_value for date_value in flow_dates if date_value < first_trade)
    if not valuation_dates:
        return pd.DataFrame(columns=LIVE_ACCOUNT_DAILY_COLUMNS)

    holding_by_date = (
        holdings.set_index("date").to_dict("index") if not holdings.empty else {}
    )
    rows: list[dict[str, object]] = []
    previous_assets = 0.0
    previous_account_pnl = 0.0
    previous_external_capital = 0.0
    nav = 1.0
    previous_date: pd.Timestamp | None = None
    for valuation_date in sorted(pd.Timestamp(value) for value in valuation_dates):
        holding_row = holding_by_date.get(valuation_date)
        if holding_row is None and not trades.empty:
            continue
        market_value = float(holding_row["market_value"]) if holding_row else 0.0
        through_date_flows = flows[flows["flow_date"].le(valuation_date)]
        cash = float(through_date_flows["cash_delta"].sum())
        cumulative_external = float(through_date_flows["external_delta"].sum())
        if not enriched_trades.empty:
            cash += float(
                enriched_trades.loc[
                    enriched_trades["trade_date"].le(valuation_date), "trade_cash_delta"
                ].sum()
            )
        period_flows = flows[flows["flow_date"].le(valuation_date)]
        if previous_date is not None:
            period_flows = period_flows[period_flows["flow_date"].gt(previous_date)]
        external_flow = cumulative_external - previous_external_capital
        positive_external_flow = float(period_flows["positive_external"].sum())
        total_assets = market_value + cash
        account_pnl = total_assets - cumulative_external
        daily_pnl = account_pnl - previous_account_pnl
        return_base = previous_assets + positive_external_flow
        daily_return_pct = daily_pnl / return_base * 100 if return_base > 0 else 0.0
        if return_base > 0:
            nav *= 1.0 + daily_return_pct / 100.0
        rows.append(
            {
                "date": valuation_date,
                "market_value": market_value,
                "cash": cash,
                "total_assets": total_assets,
                "external_flow": external_flow,
                "positive_external_flow": positive_external_flow,
                "cumulative_external_capital": cumulative_external,
                "account_pnl": account_pnl,
                "daily_pnl": daily_pnl,
                "return_base": return_base,
                "daily_return_pct": daily_return_pct,
                "nav": nav,
                "cumulative_return_pct": (nav - 1.0) * 100.0,
                "holding_pnl": float(holding_row["total_pnl"]) if holding_row else 0.0,
                "holding_return_pct": float(holding_row["return_pct"]) if holding_row else 0.0,
            }
        )
        previous_assets = total_assets
        previous_account_pnl = account_pnl
        previous_external_capital = cumulative_external
        previous_date = valuation_date
    return pd.DataFrame(rows, columns=LIVE_ACCOUNT_DAILY_COLUMNS)


def append_live_realtime_quotes(
    price_histories: dict[str, pd.DataFrame],
    quotes: dict[str, dict[str, object]] | None,
    *,
    market_now: datetime,
) -> dict[str, pd.DataFrame]:
    """Overlay same-day quotes in memory; never persist these rows."""
    result = {
        normalize_live_trade_symbol(symbol): history.copy()
        for symbol, history in price_histories.items()
        if history is not None
    }
    for symbol, quote in (quotes or {}).items():
        normalized_symbol = normalize_live_trade_symbol(symbol)
        quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
        price = pd.to_numeric(quote.get("price"), errors="coerce")
        if (
            pd.isna(quote_time)
            or quote_time.date() != market_now.date()
            or pd.isna(price)
            or float(price) <= 0
        ):
            continue
        history = result.get(normalized_symbol, pd.DataFrame(columns=["date", "price"]))
        date_column = next(
            (column for column in ("date", "日期", "trade_date") if column in history.columns),
            "date",
        )
        price_column = next(
            (column for column in ("price", "收盘价", "close") if column in history.columns),
            "price",
        )
        overlay = history.copy()
        if date_column not in overlay.columns:
            overlay[date_column] = pd.Series(dtype="datetime64[ns]")
        if price_column not in overlay.columns:
            overlay[price_column] = pd.Series(dtype="float64")
        dates = pd.to_datetime(overlay[date_column], errors="coerce")
        overlay = overlay.loc[dates.dt.date != market_now.date()].copy()
        overlay = pd.concat(
            [
                overlay,
                pd.DataFrame(
                    {date_column: [pd.Timestamp(market_now.date())], price_column: [float(price)]}
                ),
            ],
            ignore_index=True,
        )
        result[normalized_symbol] = overlay
    return result


def _quote_status(
    *,
    quote: dict[str, object] | None,
    valuation_date: object,
    formal_target_date: object,
    market_now: datetime,
) -> tuple[str, str]:
    quote_time = pd.to_datetime((quote or {}).get("quote_time"), errors="coerce")
    valuation = pd.to_datetime(valuation_date, errors="coerce")
    target = pd.to_datetime(formal_target_date, errors="coerce")
    if not pd.isna(quote_time) and quote_time.date() == market_now.date():
        time_text = quote_time.strftime("%Y-%m-%d %H:%M:%S")
        if datetime_time(11, 30) <= market_now.time() < datetime_time(13, 0):
            return "午间", time_text
        if market_now.time().hour < 15 or (
            market_now.time().hour == 15 and market_now.time().minute < 5
        ):
            return "实时", time_text
        return "缓存", time_text
    if pd.isna(valuation):
        return "已过期", "-"
    date_text = pd.Timestamp(valuation).strftime("%Y-%m-%d")
    if pd.isna(target) or pd.Timestamp(valuation).date() >= pd.Timestamp(target).date():
        return "正式收盘", date_text
    return "已过期", date_text


def build_live_account_snapshot(
    trades: pd.DataFrame,
    cash_flows: pd.DataFrame,
    price_histories: dict[str, pd.DataFrame],
    *,
    quotes: dict[str, dict[str, object]] | None = None,
    market_now: datetime,
    formal_target_date: object = None,
) -> dict[str, object]:
    """Return one reconciled model for both holdings and live-record pages."""
    initialized = live_account_is_initialized(cash_flows)
    formal_holding_daily = build_live_daily_pnl(trades, price_histories)
    formal_account_daily = build_live_account_daily(
        trades,
        cash_flows,
        price_histories,
        holding_daily=formal_holding_daily,
    )
    view_histories = append_live_realtime_quotes(
        price_histories,
        quotes,
        market_now=market_now,
    )
    view_holding_daily = build_live_daily_pnl(trades, view_histories)
    positions = build_live_position_performance(trades, view_histories)
    open_positions = build_live_positions(trades)
    quote_map = {
        normalize_live_trade_symbol(symbol): quote for symbol, quote in (quotes or {}).items()
    }
    if not positions.empty:
        first_dates = (
            trades.assign(
                _symbol=trades["symbol"].astype(str).map(normalize_live_trade_symbol),
                _date=pd.to_datetime(trades["trade_date"], errors="coerce"),
            )
            .groupby("_symbol")["_date"]
            .min()
        )
        positions["first_trade_date"] = positions["symbol"].map(first_dates)
        positions["unrealized_pnl"] = (
            pd.to_numeric(positions["market_value"], errors="coerce")
            - pd.to_numeric(positions["cost_basis"], errors="coerce")
        )
        status_values = [
            _quote_status(
                quote=quote_map.get(str(row.symbol)),
                valuation_date=row.valuation_date,
                formal_target_date=formal_target_date,
                market_now=market_now,
            )
            for row in positions.itertuples(index=False)
        ]
        positions["price_status"] = [item[0] for item in status_values]
        positions["price_time"] = [item[1] for item in status_values]
        total_market_value = pd.to_numeric(positions["market_value"], errors="coerce").sum()
        positions["weight_pct"] = (
            pd.to_numeric(positions["market_value"], errors="coerce")
            / total_market_value
            * 100
            if total_market_value > 0
            else pd.NA
        )

    current_symbols = set(open_positions["symbol"].astype(str)) if not open_positions.empty else set()
    current_quote_date = {
        symbol
        for symbol in current_symbols
        if symbol in quote_map
        and not pd.isna(pd.to_datetime(quote_map[symbol].get("quote_time"), errors="coerce"))
        and pd.to_datetime(quote_map[symbol].get("quote_time"), errors="coerce").date()
        == market_now.date()
    }
    # A transient account estimate may combine today's quotes with explicitly
    # labelled latest formal closes. It remains separate from formal history.
    if current_quote_date and not positions.empty:
        market_values = pd.to_numeric(positions["market_value"], errors="coerce")
        latest_view_date = (
            pd.to_datetime(view_holding_daily["date"], errors="coerce").max()
            if not view_holding_daily.empty
            else pd.NaT
        )
        if market_values.notna().all() and (
            pd.isna(latest_view_date) or pd.Timestamp(latest_view_date).date() < market_now.date()
        ):
            position_summary = summarize_live_position_performance(positions)
            enriched = enrich_live_trades(trades)
            buy_cost = float(
                enriched.loc[enriched["side"].eq("买入"), "cash_amount"].sum()
            )
            sell_proceeds = float(
                enriched.loc[enriched["side"].eq("卖出"), "cash_amount"].sum()
            )
            cost_basis = float(pd.to_numeric(positions["cost_basis"], errors="coerce").sum())
            realized_pnl = float(
                pd.to_numeric(positions["realized_pnl"], errors="coerce").sum()
            )
            market_value = float(market_values.sum())
            total_pnl = position_summary["cumulative_pnl"]
            mixed_row = {
                "date": pd.Timestamp(market_now.date()),
                "market_value": market_value,
                "cost_basis": cost_basis,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": market_value - cost_basis,
                "total_pnl": total_pnl,
                "cumulative_buy_cost": buy_cost,
                "net_investment": buy_cost - sell_proceeds,
                "return_pct": (
                    float(total_pnl) / buy_cost * 100
                    if not pd.isna(total_pnl) and buy_cost > 0
                    else pd.NA
                ),
            }
            view_holding_daily = pd.concat(
                [view_holding_daily, pd.DataFrame([mixed_row])], ignore_index=True
            )
    view_account_daily = build_live_account_daily(
        trades,
        cash_flows,
        view_histories,
        holding_daily=view_holding_daily,
    )

    latest_holding = view_holding_daily.iloc[-1] if not view_holding_daily.empty else None
    latest_account = view_account_daily.iloc[-1] if not view_account_daily.empty else None
    market_value = (
        float(latest_holding["market_value"])
        if latest_holding is not None
        else pd.NA
        if not open_positions.empty
        else 0.0
    )
    summary: dict[str, object] = {
        "initialized": initialized,
        "valuation_date": pd.NaT if latest_holding is None else latest_holding["date"],
        "market_value": market_value,
        "cash": pd.NA,
        "total_assets": pd.NA,
        "account_pnl": pd.NA,
        "daily_pnl": pd.NA,
        "daily_return_pct": pd.NA,
        "cumulative_return_pct": pd.NA,
        "nav": pd.NA,
        "position_ratio_pct": pd.NA,
        "holding_pnl": (
            pd.NA if latest_holding is None else float(latest_holding["total_pnl"])
        ),
        "holding_return_pct": (
            pd.NA if latest_holding is None else float(latest_holding["return_pct"])
        ),
        "cash_warning": "",
    }
    if initialized and latest_account is not None:
        total_assets = float(latest_account["total_assets"])
        cash = float(latest_account["cash"])
        summary.update(
            {
                "valuation_date": latest_account["date"],
                "market_value": float(latest_account["market_value"]),
                "cash": cash,
                "total_assets": total_assets,
                "account_pnl": float(latest_account["account_pnl"]),
                "daily_pnl": float(latest_account["daily_pnl"]),
                "daily_return_pct": float(latest_account["daily_return_pct"]),
                "cumulative_return_pct": float(latest_account["cumulative_return_pct"]),
                "nav": float(latest_account["nav"]),
                "position_ratio_pct": (
                    float(latest_account["market_value"]) / total_assets * 100
                    if total_assets > 0
                    else pd.NA
                ),
                "cash_warning": (
                    "账户现金为负，请检查期初资金、资金流水或成交记录是否完整。"
                    if cash < -0.005
                    else ""
                ),
            }
        )

    return {
        "initialized": initialized,
        "summary": summary,
        "positions": positions,
        "formal_holding_daily": formal_holding_daily,
        "formal_account_daily": formal_account_daily,
        "view_holding_daily": view_holding_daily,
        "view_account_daily": view_account_daily,
        "incomplete_realtime_symbols": sorted(current_symbols - current_quote_date)
        if current_quote_date
        else [],
    }


__all__ = [
    "LIVE_ACCOUNT_DAILY_COLUMNS",
    "LIVE_CASH_FLOW_COLUMNS",
    "LIVE_CASH_FLOW_TYPES",
    "LIVE_EXTERNAL_CASH_FLOW_TYPES",
    "add_live_cash_flow",
    "append_live_realtime_quotes",
    "build_live_account_daily",
    "build_live_account_snapshot",
    "delete_live_cash_flow",
    "list_live_cash_flows",
    "live_account_is_initialized",
]
