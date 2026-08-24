from __future__ import annotations

from contextlib import closing
from datetime import datetime

import pandas as pd

from core.db import get_conn
from services.futures_spread import completed_futures_daily_cutoff
from services.futures_live_calendar import _futures_trading_dates
from services.futures_live_models import (
    CASH_FLOW_TYPES,
    DAILY_PNL_RESOLUTIONS,
    RECONCILIATION_TOLERANCE,
)
from services.futures_live_positions import (
    _known_multiplier,
    _option_contract_parts,
    iron_ore_option_expiry_date,
    list_option_expiry_events,
)
from services.futures_live_repository import (
    _effective_cash_flows,
    list_futures_daily_pnl_overrides,
    list_futures_live_trades,
    list_monthly_accounts,
    load_daily_closes,
)
from services.futures_live_statement_parser import _date_text, _number, _text

def _daily_fee_adjustments(
    accounts: pd.DataFrame,
    trades: pd.DataFrame,
    cash_flows: pd.DataFrame,
    trading_dates: list[str],
) -> dict[str, float]:
    adjustments: dict[str, float] = {}
    for account in accounts.to_dict("records"):
        month = str(account["statement_month"])
        official_trades = trades[
            trades["source"].eq("月结单") & trades["statement_month"].eq(month)
        ]
        detail_fee = float(
            pd.to_numeric(official_trades.get("fee"), errors="coerce").fillna(0).sum()
        ) if not official_trades.empty else 0.0
        month_account_fees = cash_flows[
            cash_flows["source"].eq("月结单")
            & cash_flows["statement_month"].eq(month)
            & cash_flows["entry_type"].isin(["申报费", "账户费用"])
        ]
        explicit_account_fee = float(
            pd.to_numeric(month_account_fees.get("amount"), errors="coerce").fillna(0).sum()
        ) if not month_account_fees.empty else 0.0
        residual = float(account.get("monthly_fee") or 0) - detail_fee - explicit_account_fee
        if abs(residual) <= RECONCILIATION_TOLERANCE:
            continue
        eligible = [day for day in trading_dates if day.startswith(month)]
        if eligible:
            adjustments[max(eligible)] = adjustments.get(max(eligible), 0.0) + residual
    return adjustments


def _load_daily_account_pnl_inputs(
    *,
    as_of: object = None,
    valuation_mode: str = "close",
) -> dict[str, object] | None:
    if valuation_mode not in {"close", "settlement"}:
        raise ValueError("估值口径必须是 close 或 settlement。")
    accounts = list_monthly_accounts()
    trades = list_futures_live_trades(include_taken_over=False)
    if accounts.empty or trades.empty:
        return None
    trades = trades.copy()
    trades["trade_date"] = pd.to_datetime(trades["trade_date"], errors="coerce")
    trades = trades.dropna(subset=["trade_date"]).sort_values(
        ["trade_date", "trade_time", "id"], na_position="last"
    )
    cash_flows = _effective_cash_flows(as_of=as_of)
    start_candidates = [trades["trade_date"].min().date()]
    if not cash_flows.empty:
        first_flow = pd.to_datetime(cash_flows["flow_date"], errors="coerce").min()
        if pd.notna(first_flow):
            start_candidates.append(first_flow.date())
    target = (
        pd.Timestamp(_date_text(as_of)).date()
        if as_of is not None and _date_text(as_of)
        else completed_futures_daily_cutoff().date()
    )
    start = min(start_candidates)
    if target < start:
        return None
    trading_dates = _futures_trading_dates(start, target)
    if not trading_dates:
        return None

    cached = load_daily_closes()
    close_lookup = {
        (str(row.asset_type), str(row.contract), str(row.trade_date)): float(row.close_price)
        for row in cached.itertuples(index=False)
        if pd.notna(row.close_price)
    }
    settlement_lookup = {
        (str(row.asset_type), str(row.contract), str(row.trade_date)): float(row.settlement_price)
        for row in cached.itertuples(index=False)
        if pd.notna(row.settlement_price)
    }
    valuation_lookup = close_lookup if valuation_mode == "close" else settlement_lookup
    trade_groups = {
        day.strftime("%Y-%m-%d"): group
        for day, group in trades.groupby(trades["trade_date"].dt.normalize())
    }
    flow_groups: dict[str, pd.DataFrame] = {}
    if not cash_flows.empty:
        dated_flows = cash_flows.copy()
        dated_flows["_valuation_date"] = dated_flows["flow_date"].astype(str).map(
            lambda flow_date: next(
                (day for day in trading_dates if day >= flow_date),
                None,
            )
        )
        dated_flows = dated_flows.dropna(subset=["_valuation_date"])
        flow_groups = {
            str(day): group.drop(columns=["_valuation_date"])
            for day, group in dated_flows.groupby("_valuation_date")
        }
    event_rows = list_option_expiry_events()
    event_rows = event_rows[
        event_rows["reconciliation_status"].isin(["已确认", "已接管"])
    ] if not event_rows.empty else event_rows
    event_groups = {
        str(day): group
        for day, group in event_rows.groupby(event_rows["event_date"].astype(str))
    } if not event_rows.empty else {}
    fee_adjustments = _daily_fee_adjustments(
        accounts, trades, cash_flows, trading_dates
    )
    latest_statement_end = str(accounts.iloc[-1]["statement_end_date"])
    return {
        "trading_dates": trading_dates,
        "close_lookup": close_lookup,
        "settlement_lookup": settlement_lookup,
        "valuation_lookup": valuation_lookup,
        "trade_groups": trade_groups,
        "flow_groups": flow_groups,
        "event_groups": event_groups,
        "fee_adjustments": fee_adjustments,
        "latest_statement_end": latest_statement_end,
    }


def _calculate_daily_account_state(
    inputs: dict[str, object],
    *,
    valuation_mode: str,
) -> pd.DataFrame:
    trading_dates = inputs["trading_dates"]
    close_lookup = inputs["close_lookup"]
    settlement_lookup = inputs["settlement_lookup"]
    valuation_lookup = inputs["valuation_lookup"]
    trade_groups = inputs["trade_groups"]
    flow_groups = inputs["flow_groups"]
    event_groups = inputs["event_groups"]
    fee_adjustments = inputs["fee_adjustments"]
    latest_statement_end = str(inputs["latest_statement_end"])
    futures_states: dict[tuple[str, str], dict[str, float | int]] = {}
    option_states: dict[str, dict[str, float | int]] = {}
    futures_realized = 0.0
    option_realized = 0.0
    cumulative_fee = 0.0
    cumulative_net_flow = 0.0
    rows: list[dict[str, object]] = []

    def add_futures_position(
        contract: str,
        side: str,
        quantity: int,
        price: float,
        multiplier: float,
    ) -> None:
        key = (contract, side)
        state = futures_states.setdefault(
            key, {"quantity": 0, "average": 0.0, "multiplier": multiplier}
        )
        current_quantity = int(state["quantity"])
        new_quantity = current_quantity + quantity
        state["average"] = (
            float(state["average"]) * current_quantity + price * quantity
        ) / new_quantity
        state["quantity"] = new_quantity
        state["multiplier"] = multiplier

    def process_expiry(
        contract: str,
        quantity: int,
        outcome: str,
        underlying: str | None,
        futures_side: str | None,
        strike: float,
    ) -> None:
        nonlocal option_realized
        state = option_states.get(contract)
        if state is None or int(state["quantity"]) == 0:
            return
        signed_quantity = int(state["quantity"])
        closed_quantity = min(abs(signed_quantity), quantity)
        multiplier = float(state["multiplier"])
        option_realized += (
            -float(state["average"]) * closed_quantity * multiplier
            if signed_quantity > 0
            else float(state["average"]) * closed_quantity * multiplier
        )
        state["quantity"] = (
            signed_quantity - closed_quantity
            if signed_quantity > 0
            else signed_quantity + closed_quantity
        )
        if outcome == "履约" and underlying and futures_side:
            add_futures_position(
                underlying, futures_side, closed_quantity, strike, multiplier
            )

    for day in trading_dates:
        day_trades = trade_groups.get(day, pd.DataFrame())
        day_trade_fee = 0.0
        for trade in day_trades.to_dict("records"):
            asset_type = str(trade["asset_type"])
            contract = str(trade["contract"])
            quantity = int(trade["quantity"])
            price = float(trade["price"])
            multiplier = _number(trade.get("multiplier")) or _known_multiplier(
                asset_type, contract
            )
            if multiplier is None:
                continue
            day_trade_fee += float(trade.get("fee") or 0)
            if asset_type == "期货":
                if trade["open_close"] == "开":
                    side = "多" if trade["buy_sell"] == "买" else "空"
                    add_futures_position(contract, side, quantity, price, multiplier)
                    continue
                side = "多" if trade["buy_sell"] == "卖" else "空"
                state = futures_states.get((contract, side))
                available = int(state["quantity"]) if state else 0
                closed_quantity = min(quantity, available)
                supplied_pnl = _number(trade.get("close_pnl"))
                if supplied_pnl is not None:
                    futures_realized += supplied_pnl
                elif state is not None and closed_quantity > 0:
                    futures_realized += (
                        (price - float(state["average"])) * closed_quantity * multiplier
                        if side == "多"
                        else (float(state["average"]) - price) * closed_quantity * multiplier
                    )
                if state is not None:
                    state["quantity"] = max(0, available - quantity)
                continue

            state = option_states.setdefault(
                contract, {"quantity": 0, "average": 0.0, "multiplier": multiplier}
            )
            signed_quantity = int(state["quantity"])
            delta = quantity if trade["buy_sell"] == "买" else -quantity
            if signed_quantity == 0 or signed_quantity * delta > 0:
                new_quantity = signed_quantity + delta
                state["average"] = (
                    float(state["average"]) * abs(signed_quantity) + price * abs(delta)
                ) / abs(new_quantity)
                state["quantity"] = new_quantity
                state["multiplier"] = multiplier
                continue
            closed_quantity = min(abs(signed_quantity), abs(delta))
            option_realized += (
                (price - float(state["average"])) * closed_quantity * multiplier
                if signed_quantity > 0
                else (float(state["average"]) - price) * closed_quantity * multiplier
            )
            new_quantity = signed_quantity + delta
            if signed_quantity * new_quantity < 0:
                state["average"] = price
            elif new_quantity == 0:
                state["average"] = 0.0
            state["quantity"] = new_quantity
            state["multiplier"] = multiplier

        pending_expiry: list[str] = []
        explicit_events = event_groups.get(day, pd.DataFrame())
        explicit_contracts: set[str] = set()
        for event in explicit_events.to_dict("records"):
            explicit_contracts.add(str(event["option_contract"]))
            process_expiry(
                str(event["option_contract"]),
                int(event["quantity"]),
                str(event["outcome"]),
                _text(event.get("underlying_contract")) or None,
                _text(event.get("futures_side")) or None,
                float(event.get("strike") or 0),
            )
        for contract, state in option_states.items():
            if int(state["quantity"]) == 0:
                continue
            expiry_date = iron_ore_option_expiry_date(contract)
            if not expiry_date or expiry_date > day or contract in explicit_contracts:
                continue
            parts = _option_contract_parts(contract) or {}
            underlying = str(parts.get("underlying_contract") or "")
            settlement = settlement_lookup.get(("期货", underlying, expiry_date))
            settlement = settlement or close_lookup.get(("期货", underlying, expiry_date))
            if expiry_date > latest_statement_end or settlement is None:
                pending_expiry.append(contract)
                continue
            strike = float(parts.get("strike") or 0)
            is_put = parts.get("option_type") == "P"
            in_the_money = (
                (is_put and settlement < strike)
                or (not is_put and settlement > strike)
            )
            signed_quantity = int(state["quantity"])
            option_side = "多" if signed_quantity > 0 else "空"
            futures_side = (
                "空" if is_put and option_side == "多" else
                "多" if is_put else
                "多" if option_side == "多" else "空"
            )
            process_expiry(
                contract,
                abs(signed_quantity),
                "履约" if in_the_money else "作废",
                underlying if in_the_money else None,
                futures_side if in_the_money else None,
                strike,
            )

        day_flows = flow_groups.get(day, pd.DataFrame())
        external = day_flows[day_flows["entry_type"].isin(CASH_FLOW_TYPES)] if not day_flows.empty else day_flows
        net_flow = 0.0
        if not external.empty:
            net_flow = float(
                pd.to_numeric(external.loc[external["entry_type"].eq("入金"), "amount"], errors="coerce").fillna(0).sum()
                - pd.to_numeric(external.loc[external["entry_type"].eq("出金"), "amount"], errors="coerce").fillna(0).sum()
            )
        account_fee = 0.0
        if not day_flows.empty:
            account_fee = float(
                pd.to_numeric(
                    day_flows.loc[
                        day_flows["entry_type"].isin(["申报费", "账户费用"]),
                        "amount",
                    ],
                    errors="coerce",
                ).fillna(0).sum()
            )
        cumulative_net_flow += net_flow
        if valuation_mode == "settlement":
            cumulative_fee += day_trade_fee
        else:
            cumulative_fee += day_trade_fee + account_fee + fee_adjustments.get(day, 0.0)

        floating = 0.0
        missing: list[str] = [f"{contract}到期处理待确认" for contract in pending_expiry]
        for (contract, side), state in futures_states.items():
            quantity = int(state["quantity"])
            if quantity <= 0:
                continue
            valuation_price = valuation_lookup.get(("期货", contract, day))
            if valuation_price is None:
                missing.append(contract)
                continue
            direction = 1 if side == "多" else -1
            floating += (
                (valuation_price - float(state["average"]))
                * quantity
                * float(state["multiplier"])
                * direction
            )
        for contract, state in option_states.items():
            signed_quantity = int(state["quantity"])
            if signed_quantity == 0:
                continue
            valuation_price = valuation_lookup.get(("期权", contract, day))
            if valuation_price is None:
                missing.append(contract)
                continue
            direction = 1 if signed_quantity > 0 else -1
            floating += (
                (valuation_price - float(state["average"]))
                * abs(signed_quantity)
                * float(state["multiplier"])
                * direction
            )
        complete = not missing
        realized = futures_realized + option_realized
        net_pnl = realized + floating - cumulative_fee if complete else pd.NA
        economic_equity = cumulative_net_flow + float(net_pnl) if complete else pd.NA
        rows.append(
            {
                "date": day,
                "realized_pnl": realized,
                "floating_pnl": floating if complete else pd.NA,
                "fee": cumulative_fee,
                "net_pnl": net_pnl,
                "net_cash_flow": net_flow,
                "cumulative_net_cash_flow": cumulative_net_flow,
                "economic_equity": economic_equity,
                "source": (
                    "数据不完整"
                    if not complete
                    else (
                        "正式收盘估值"
                        if valuation_mode == "close"
                        else "正式结算估值"
                    )
                ),
                "status": "完整" if complete else "数据不完整",
                "confirmation_status": (
                    "正式" if day <= latest_statement_end else "待月结单确认"
                ),
                "missing_contracts": "、".join(sorted(set(missing))),
            }
        )

    result = pd.DataFrame(rows)
    return pd.DataFrame(rows)


def _finalize_close_daily_returns(result: pd.DataFrame) -> pd.DataFrame:
    result["daily_pnl"] = pd.NA
    result["return_base"] = pd.NA
    result["daily_return_pct"] = pd.NA
    previous_complete = False
    previous_net = 0.0
    previous_equity = 0.0
    for index, row in result.iterrows():
        if row["status"] != "完整" or pd.isna(row["net_pnl"]):
            previous_complete = False
            continue
        if index == 0:
            daily_pnl = float(row["net_pnl"])
            return_base = max(float(row["net_cash_flow"]), 0.0)
        elif previous_complete:
            daily_pnl = float(row["net_pnl"]) - previous_net
            return_base = previous_equity + max(float(row["net_cash_flow"]), 0.0)
        else:
            previous_net = float(row["net_pnl"])
            previous_equity = float(row["economic_equity"])
            previous_complete = True
            continue
        result.at[index, "daily_pnl"] = daily_pnl
        result.at[index, "return_base"] = return_base
        result.at[index, "daily_return_pct"] = (
            daily_pnl / return_base * 100 if return_base > 0 else pd.NA
        )
        previous_net = float(row["net_pnl"])
        previous_equity = float(row["economic_equity"])
        previous_complete = True
    return result.reset_index(drop=True)


def build_daily_account_pnl(
    *,
    as_of: object = None,
    valuation_mode: str = "close",
) -> pd.DataFrame:
    inputs = _load_daily_account_pnl_inputs(
        as_of=as_of,
        valuation_mode=valuation_mode,
    )
    if inputs is None:
        return pd.DataFrame()
    result = _calculate_daily_account_state(inputs, valuation_mode=valuation_mode)
    if valuation_mode == "settlement":
        return _apply_manual_daily_pnl_overrides(
            result,
            latest_statement_end=str(inputs["latest_statement_end"]),
        )
    return _finalize_close_daily_returns(result)


def _apply_manual_daily_pnl_overrides(
    result: pd.DataFrame,
    *,
    latest_statement_end: str,
) -> pd.DataFrame:
    if result.empty:
        return result
    overrides = list_futures_daily_pnl_overrides()
    override_by_date = {
        str(row["trade_date"]): row
        for row in overrides.to_dict("records")
    } if not overrides.empty else {}
    result = result.copy()
    result["formal_net_pnl"] = result["net_pnl"]
    result["formal_economic_equity"] = result["economic_equity"]
    result["formal_daily_pnl"] = pd.NA
    result["manual_daily_pnl"] = pd.NA
    result["difference"] = pd.NA
    result["reconciliation_status"] = ""
    result["daily_pnl"] = pd.NA
    result["return_base"] = pd.NA
    result["daily_return_pct"] = pd.NA

    previous_effective_net: float | None = None
    previous_effective_equity: float | None = None
    previous_day_has_cumulative = False
    previous_formal_net: float | None = None
    previous_day_formal_complete = False
    confirmation_paused = False
    reconciliation_updates: list[tuple[float, float, str, str | None, str, int]] = []

    for index, row in result.iterrows():
        day = str(row["date"])
        formal_complete = row["status"] == "完整" and pd.notna(row["formal_net_pnl"])
        formal_daily: float | None = None
        if formal_complete:
            formal_net = float(row["formal_net_pnl"])
            if index == 0:
                formal_daily = formal_net
            elif previous_day_formal_complete and previous_formal_net is not None:
                formal_daily = formal_net - previous_formal_net
            elif previous_day_has_cumulative and previous_effective_net is not None:
                formal_daily = formal_net - previous_effective_net
            if formal_daily is not None:
                result.at[index, "formal_daily_pnl"] = formal_daily

        override = override_by_date.get(day)
        manual_pnl = float(override["pnl_amount"]) if override is not None else None
        resolution = (
            str(override.get("resolution"))
            if override is not None and str(override.get("resolution")) in DAILY_PNL_RESOLUTIONS
            else None
        )
        reconciliation_status = ""
        difference: float | None = None
        if manual_pnl is not None:
            result.at[index, "manual_daily_pnl"] = manual_pnl
        if manual_pnl is not None and formal_daily is not None:
            difference = formal_daily - manual_pnl
            if abs(difference) <= RECONCILIATION_TOLERANCE:
                reconciliation_status = "已一致"
                resolution = "采用正式"
            elif resolution in DAILY_PNL_RESOLUTIONS:
                reconciliation_status = resolution
            else:
                reconciliation_status = "待核对"
            reconciliation_updates.append(
                (
                    formal_daily,
                    difference,
                    reconciliation_status,
                    resolution,
                    datetime.now().isoformat(timespec="seconds"),
                    int(override["id"]),
                )
            )
            result.at[index, "difference"] = difference
            result.at[index, "reconciliation_status"] = reconciliation_status
        elif manual_pnl is not None:
            reconciliation_status = "待确认"
            result.at[index, "reconciliation_status"] = reconciliation_status

        chosen_daily: float | None = None
        use_manual = False
        if manual_pnl is not None:
            use_manual = formal_daily is None or resolution != "采用正式"
            chosen_daily = manual_pnl if use_manual else formal_daily
        elif formal_daily is not None:
            chosen_daily = formal_daily

        can_extend = index == 0 or (
            previous_day_has_cumulative and previous_effective_net is not None
        )
        if chosen_daily is not None and can_extend:
            if index == 0:
                effective_net = chosen_daily
                return_base = max(float(row["net_cash_flow"]), 0.0)
            else:
                effective_net = float(previous_effective_net) + chosen_daily
                return_base = float(previous_effective_equity) + max(
                    float(row["net_cash_flow"]), 0.0
                )
            effective_equity = float(row["cumulative_net_cash_flow"]) + effective_net
            result.at[index, "net_pnl"] = effective_net
            result.at[index, "economic_equity"] = effective_equity
            result.at[index, "daily_pnl"] = chosen_daily
            result.at[index, "return_base"] = return_base
            result.at[index, "daily_return_pct"] = (
                chosen_daily / return_base * 100 if return_base > 0 else pd.NA
            )
            if use_manual:
                result.at[index, "source"] = "同花顺手工"
                result.at[index, "status"] = "手工估算"
            previous_effective_net = effective_net
            previous_effective_equity = effective_equity
            previous_day_has_cumulative = True
        elif chosen_daily is not None and use_manual:
            result.at[index, "daily_pnl"] = chosen_daily
            result.at[index, "status"] = "手工估算"
            if formal_complete:
                result.at[index, "net_pnl"] = float(row["formal_net_pnl"])
                result.at[index, "economic_equity"] = float(
                    row["formal_economic_equity"]
                )
                result.at[index, "source"] = "同花顺手工日收益/正式结算累计"
                previous_effective_net = float(row["formal_net_pnl"])
                previous_effective_equity = float(row["formal_economic_equity"])
                previous_day_has_cumulative = True
            else:
                result.at[index, "net_pnl"] = pd.NA
                result.at[index, "economic_equity"] = pd.NA
                result.at[index, "source"] = "同花顺手工"
                previous_day_has_cumulative = False
        elif formal_complete:
            previous_effective_net = float(row["formal_net_pnl"])
            previous_effective_equity = float(row["formal_economic_equity"])
            previous_day_has_cumulative = True
        else:
            result.at[index, "net_pnl"] = pd.NA
            result.at[index, "economic_equity"] = pd.NA
            previous_day_has_cumulative = False

        previous_formal_net = (
            float(row["formal_net_pnl"]) if formal_complete else None
        )
        previous_day_formal_complete = formal_complete

        unresolved = manual_pnl is not None and (
            formal_daily is None or reconciliation_status == "待核对"
        )
        confirmation_paused = confirmation_paused or unresolved
        if confirmation_paused:
            result.at[index, "confirmation_status"] = (
                "待核对" if unresolved else "待前序核对"
            )
        elif day <= latest_statement_end:
            result.at[index, "confirmation_status"] = "正式"
        else:
            result.at[index, "confirmation_status"] = "待月结单确认"

    if reconciliation_updates:
        with closing(get_conn()) as conn:
            conn.executemany(
                """
                UPDATE futures_daily_pnl_overrides
                SET formal_pnl=?, difference=?, reconciliation_status=?,
                    resolution=?, updated_at=?
                WHERE id=?
                """,
                reconciliation_updates,
            )
            conn.commit()
    return result.reset_index(drop=True)


def build_futures_daily_returns(daily_pnl: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "daily_pnl", "return_base", "daily_return_pct", "status"}
    if daily_pnl is None or daily_pnl.empty or not required.issubset(daily_pnl.columns):
        return pd.DataFrame()
    result = daily_pnl[
        daily_pnl["status"].isin(["完整", "手工估算"])
        & pd.to_numeric(daily_pnl["daily_pnl"], errors="coerce").notna()
    ].copy()
    if result.empty:
        return pd.DataFrame()
    result = result.rename(
        columns={"daily_pnl": "pnl_amount", "daily_return_pct": "return_pct"}
    )
    columns = ["date", "pnl_amount", "return_base", "return_pct", "source"]
    if "confirmation_status" in result.columns:
        columns.append("confirmation_status")
    return result[columns].reset_index(drop=True)
