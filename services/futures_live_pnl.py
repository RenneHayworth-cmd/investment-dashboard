from __future__ import annotations

import pandas as pd

from services.futures_live_models import exercise_fee_mask
from services.futures_live_positions import (
    _apply_manual_to_positions,
    _known_multiplier,
    build_estimated_positions,
)
from services.futures_live_repository import (
    _effective_cash_flows,
    _effective_manual_trades,
    latest_monthly_account,
    list_futures_live_trades,
    list_month_end_positions,
    list_monthly_accounts,
    load_daily_closes,
)
from services.futures_live_statement_parser import _date_text, _number


def _contract_account_pnl_snapshot(
    *,
    as_of: object = None,
    valuation_mode: str = "close",
    valuation_date: str | None = None,
) -> tuple[dict[tuple[str, str], dict[str, object]], str | None]:
    """Return the per-contract slice produced by the account daily ledger."""
    # Local import keeps the stable P&L modules independent at import time.
    from services.futures_live_daily_pnl import build_daily_account_pnl

    daily = build_daily_account_pnl(as_of=as_of, valuation_mode=valuation_mode)
    if daily.empty or "contract_pnl" not in daily.columns:
        return {}, None
    eligible = daily[
        daily["status"].isin(["完整", "手工估算"])
        & daily["net_pnl"].notna()
    ]
    if valuation_date:
        eligible = eligible[eligible["date"].astype(str).eq(valuation_date)]
    if eligible.empty:
        return {}, None
    row = eligible.iloc[-1]
    selected_date = str(row["date"])
    details = row.get("contract_pnl")
    if not isinstance(details, list):
        return {}, selected_date

    previous_rows = daily[
        daily["date"].astype(str).lt(selected_date)
        & daily["status"].isin(["完整", "手工估算"])
        & daily["net_pnl"].notna()
    ]
    previous_lookup: dict[tuple[str, str], dict[str, object]] = {}
    if not previous_rows.empty:
        previous_details = previous_rows.iloc[-1].get("contract_pnl")
        if isinstance(previous_details, list):
            previous_lookup = {
                (str(item["asset_type"]), str(item["contract"])): item
                for item in previous_details
            }

    lookup: dict[tuple[str, str], dict[str, object]] = {}
    for item in details:
        key = (str(item["asset_type"]), str(item["contract"]))
        detail = dict(item)
        current_net = _number(detail.get("net_pnl"))
        previous_net = _number(previous_lookup.get(key, {}).get("net_pnl"))
        detail["daily_pnl"] = (
            None
            if current_net is None
            else current_net - previous_net
            if previous_net is not None
            else current_net
        )
        lookup[key] = detail
    return lookup, selected_date

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
    ledger_lookup, ledger_date = _contract_account_pnl_snapshot(
        as_of=valuation_date,
        valuation_mode=valuation_mode,
        valuation_date=valuation_date,
    )
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
        ledger_detail = ledger_lookup.get((asset_type, contract))
        if ledger_detail is not None and ledger_date == valuation_date:
            realized = float(ledger_detail["realized_pnl"])
            floating = _number(ledger_detail.get("floating_pnl"))
            fee = float(ledger_detail["fee"])
            daily = _number(ledger_detail.get("daily_pnl"))
            net_pnl = _number(ledger_detail.get("net_pnl"))
        else:
            net_pnl = None if floating is None else realized + floating - fee
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
                "net_pnl": net_pnl,
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
    ledger_lookup, ledger_date = _contract_account_pnl_snapshot(
        as_of=as_of,
        valuation_mode=valuation_mode,
    )
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
        ledger_detail = ledger_lookup.get((str(asset_type), str(contract)))
        if ledger_detail is not None:
            realized = float(ledger_detail["realized_pnl"])
            floating = _number(ledger_detail.get("floating_pnl"))
            fee = float(ledger_detail["fee"])
            net_pnl = _number(ledger_detail.get("net_pnl"))
        else:
            net_pnl = None if floating is None else realized + floating - fee
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
                "net_pnl": net_pnl,
                "valuation_date": ledger_date or (max(valuation_dates) if valuation_dates else ""),
            }
        )
    return pd.DataFrame(rows).sort_values(["asset_type", "status", "contract"]).reset_index(drop=True)


def summarize_futures_live_pnl(
    *,
    as_of: object = None,
    valuation_mode: str = "close",
    include_declaration_fee: bool = True,
) -> dict[str, object]:
    if valuation_mode not in {"close", "settlement"}:
        raise ValueError("估值口径必须是 close 或 settlement。")
    accounts = list_monthly_accounts()
    cutoff = pd.to_datetime(as_of, errors="coerce") if as_of is not None else pd.NaT
    if pd.notna(cutoff) and not accounts.empty:
        accounts = accounts[
            pd.to_datetime(accounts["statement_end_date"], errors="coerce") <= pd.Timestamp(cutoff)
        ].copy()
    official_fee = float(pd.to_numeric(accounts.get("monthly_fee"), errors="coerce").fillna(0).sum()) if not accounts.empty else 0.0
    declaration_fee = float(pd.to_numeric(accounts.get("declaration_fee"), errors="coerce").fillna(0).sum()) if not accounts.empty else 0.0
    cash_flows = _effective_cash_flows(as_of=as_of)
    exercise_fee = float(
        pd.to_numeric(
            cash_flows.loc[exercise_fee_mask(cash_flows), "amount"],
            errors="coerce",
        ).fillna(0).sum()
    ) if not cash_flows.empty else 0.0
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
    expected_total_fee = official_fee + manual_fee

    # 汇总直接取账户日度账本，避免再次把月结单昨结算平仓盈亏与
    # 开仓成本浮盈混算。该账本也是趋势图和收益日历的唯一来源。
    from services.futures_live_daily_pnl import build_daily_account_pnl

    daily = build_daily_account_pnl(as_of=as_of, valuation_mode=valuation_mode)
    if not daily.empty:
        eligible = daily[
            daily["status"].isin(["完整", "手工估算"])
            & daily["net_pnl"].notna()
        ]
        if not accounts.empty:
            statement_end = str(accounts.iloc[-1]["statement_end_date"])
            eligible = eligible[eligible["date"].astype(str).ge(statement_end)]
        if not eligible.empty:
            row = eligible.iloc[-1]
            total_fee = float(row["fee"])
            net_pnl = float(row["net_pnl"])
            if valuation_mode == "close" and not include_declaration_fee:
                total_fee -= declaration_fee
                net_pnl += declaration_fee
            details = row.get("contract_pnl")
            contract_fee = (
                sum(float(item.get("fee") or 0) for item in details)
                if isinstance(details, list)
                else 0.0
            )
            unallocated_fee = total_fee - contract_fee
            unallocated_fee -= exercise_fee
            if valuation_mode == "close" and include_declaration_fee:
                unallocated_fee -= declaration_fee
            return {
                "daily_pnl": _number(row.get("daily_pnl")),
                "realized_pnl": _number(row.get("realized_pnl")),
                "floating_pnl": _number(row.get("floating_pnl")),
                "fee": total_fee,
                "declaration_fee": declaration_fee,
                "exercise_fee": exercise_fee,
                "unallocated_fee": unallocated_fee,
                "net_pnl": net_pnl,
                "valuation_date": str(row["date"]),
                "valuation_mode": valuation_mode,
            }

    # 缺少完整正式价格时继续显示月结单费用及旧的可用估值，不凭空补价。
    current = build_current_position_pnl(
        as_of=as_of,
        valuation_mode=valuation_mode,
    )
    history = build_contract_pnl_history(
        as_of=as_of,
        valuation_mode=valuation_mode,
    )
    if history.empty:
        total_fee = expected_total_fee
        if not include_declaration_fee:
            total_fee -= declaration_fee
        return {
            "daily_pnl": None,
            "realized_pnl": 0.0,
            "floating_pnl": 0.0,
            "fee": total_fee,
            "declaration_fee": declaration_fee,
            "exercise_fee": exercise_fee,
            "unallocated_fee": total_fee - (
                declaration_fee if include_declaration_fee else 0.0
            ) - exercise_fee,
            "net_pnl": -total_fee,
            "valuation_date": None,
            "valuation_mode": valuation_mode,
        }
    total_fee = expected_total_fee
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
        "exercise_fee": exercise_fee,
        "unallocated_fee": total_fee
        - float(pd.to_numeric(history["fee"], errors="coerce").fillna(0).sum())
        - (declaration_fee if include_declaration_fee else 0.0)
        - exercise_fee,
        "net_pnl": realized + floating - total_fee,
        "valuation_date": current["valuation_date"].max() if not current.empty else None,
        "valuation_mode": valuation_mode,
    }
