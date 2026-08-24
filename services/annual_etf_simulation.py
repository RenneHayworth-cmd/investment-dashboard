from __future__ import annotations

from math import floor
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from services.annual_etf_models import (
    ALL_SLOTS,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_SAME_CLOSE,
    NON_US_SLOTS,
    PARKING_LISTING_DATE,
    PARKING_SLOTS,
    PARKING_SYMBOL,
    US_SLOTS,
    AnnualBacktestSettings,
    AnnualSelection,
    DirectionSleeveState,
)
from services.annual_etf_report import calculate_direction_contribution
from services.annual_etf_selection import (
    _apply_split,
    _desired_states,
    _selection_map,
)


def _initial_weights(start_selections: list[AnnualSelection]) -> dict[str, float]:
    if {item.slot for item in start_selections} != set(ALL_SLOTS):
        missing = sorted(set(ALL_SLOTS) - {item.slot for item in start_selections})
        raise ValueError(f"起投年度缺少方向：{'、'.join(missing)}")
    us = sorted(
        [item for item in start_selections if item.slot in US_SLOTS],
        key=lambda item: (-item.validation_score, item.slot),
    )
    weights = {slot: 10.0 for slot in NON_US_SLOTS}
    weights[us[0].slot] = 25.0
    weights[us[1].slot] = 15.0
    if not np.isclose(sum(weights.values()), 100.0):
        raise AssertionError("起投方向权重合计必须为100%。")
    return weights


def _price_row(frame: pd.DataFrame | None, trade_date: pd.Timestamp) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    rows = frame[pd.to_datetime(frame["trade_date"]) == trade_date]
    return rows.iloc[-1] if not rows.empty else None


def _last_price(frame: pd.DataFrame | None, trade_date: pd.Timestamp) -> float:
    if frame is None or frame.empty:
        return np.nan
    dates = pd.to_datetime(frame["trade_date"])
    values = pd.to_numeric(frame.loc[dates <= trade_date, "raw_close"], errors="coerce").dropna()
    return float(values.iloc[-1]) if not values.empty else np.nan


def _signal_state(
    frame: pd.DataFrame | None,
    signal_date: pd.Timestamp,
    ma_period: int,
    threshold_pct: float,
) -> int | None:
    if frame is None or frame.empty:
        return None
    dates = pd.to_datetime(frame["trade_date"])
    history = frame.loc[dates <= signal_date].copy()
    if len(history) < ma_period:
        return None
    states, _ma = _desired_states(history["signal_close"], ma_period, threshold_pct)
    return int(states.iloc[-1])


def _trade(
    state: DirectionSleeveState,
    *,
    symbol: str,
    name: str,
    leg: str,
    action: str,
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    price: float,
    settings: AnnualBacktestSettings,
    reason: str,
) -> dict[str, object] | None:
    shares_field = {"long": "long_shares", "timing": "timing_shares", "parking": "parking_shares"}[leg]
    cost_field = {"long": "long_cost", "timing": "timing_cost", "parking": "parking_cost"}[leg]
    shares = float(getattr(state, shares_field))
    if not np.isfinite(price) or price <= 0:
        return None
    if action == "buy":
        affordable = state.cash / (price * (1 + settings.commission_rate))
        quantity = floor(affordable / settings.lot_size) * settings.lot_size
        if quantity <= 0:
            return None
        gross = quantity * price
        commission = gross * settings.commission_rate
        state.cash -= gross + commission
        setattr(state, shares_field, shares + float(quantity))
        setattr(state, cost_field, float(getattr(state, cost_field)) + gross + commission)
    else:
        quantity = shares
        if quantity <= 0:
            return None
        gross = quantity * price
        commission = gross * settings.commission_rate
        state.cash += gross - commission
        setattr(state, shares_field, 0.0)
        setattr(state, cost_field, 0.0)
    return {
        "slot": state.slot,
        "symbol": symbol,
        "name": name,
        "leg": leg,
        "signal_date": signal_date,
        "execution_date": execution_date,
        "action": action,
        "price": price,
        "shares": float(quantity),
        "gross_amount": gross,
        "commission": commission,
        "reason": reason,
        "cash_after": state.cash,
    }


def _sell_parking(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
) -> None:
    if state.parking_shares <= 0:
        return
    price = _last_price(market_data.get(PARKING_SYMBOL), execution_date)
    trade = _trade(
        state,
        symbol=PARKING_SYMBOL,
        name="红利低波ETF华泰柏瑞",
        leg="parking",
        action="sell",
        signal_date=signal_date,
        execution_date=execution_date,
        price=price,
        settings=settings,
        reason=reason,
    )
    if trade:
        trades.append(trade)


def _buy_parking(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
) -> None:
    if state.slot not in PARKING_SLOTS or execution_date < PARKING_LISTING_DATE or state.parking_shares > 0:
        return
    row = _price_row(market_data.get(PARKING_SYMBOL), execution_date)
    if row is None:
        return
    trade = _trade(
        state,
        symbol=PARKING_SYMBOL,
        name="红利低波ETF华泰柏瑞",
        leg="parking",
        action="buy",
        signal_date=signal_date,
        execution_date=execution_date,
        price=float(row["raw_close"]),
        settings=settings,
        reason=reason,
    )
    if trade:
        trades.append(trade)


def _sell_current(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
) -> None:
    price = _last_price(market_data.get(state.current_symbol), execution_date)
    for leg in ("timing", "long"):
        if float(getattr(state, f"{leg}_shares")) <= 0:
            continue
        trade = _trade(
            state,
            symbol=state.current_symbol,
            name=state.current_name,
            leg=leg,
            action="sell",
            signal_date=signal_date,
            execution_date=execution_date,
            price=price,
            settings=settings,
            reason=reason,
        )
        if trade:
            trades.append(trade)


def _activate_selection(state: DirectionSleeveState, selection: AnnualSelection, effective_date: pd.Timestamp) -> None:
    state.current_symbol = selection.symbol
    state.current_name = selection.name
    state.current_strategy = selection.strategy
    state.ma_period = selection.ma_period
    state.threshold_pct = selection.threshold_pct
    state.parameter_effective_date = effective_date
    state.pending = None


def _buy_active(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
    *,
    initial_half_hold: bool = False,
) -> None:
    row = _price_row(market_data.get(state.current_symbol), execution_date)
    if row is None:
        return
    price = float(row["raw_close"])
    _sell_parking(state, market_data, signal_date, execution_date, settings, trades, "目标ETF恢复持仓")
    if state.current_strategy == "half_timing" and state.long_shares <= 0:
        target_long_cash = state.cash / 2 if (initial_half_hold or state.timing_shares <= 0) else 0.0
        if target_long_cash > 0:
            saved_cash = state.cash
            state.cash = target_long_cash
            trade = _trade(
                state,
                symbol=state.current_symbol,
                name=state.current_name,
                leg="long",
                action="buy",
                signal_date=signal_date,
                execution_date=execution_date,
                price=price,
                settings=settings,
                reason=f"{reason}（长期半仓）",
            )
            unused = state.cash
            state.cash = saved_cash - target_long_cash + unused
            if trade:
                trade["cash_after"] = state.cash
                trades.append(trade)
    if state.timing_shares <= 0:
        trade = _trade(
            state,
            symbol=state.current_symbol,
            name=state.current_name,
            leg="timing",
            action="buy",
            signal_date=signal_date,
            execution_date=execution_date,
            price=price,
            settings=settings,
            reason=f"{reason}（择时仓）" if state.current_strategy == "half_timing" else reason,
        )
        if trade:
            trades.append(trade)


def _buy_half_long(
    state: DirectionSleeveState,
    market_data: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
    trades: list[dict[str, object]],
    reason: str,
) -> None:
    """买入美股方向不参与择时的长期半仓，不触碰择时半仓。"""
    if state.current_strategy != "half_timing" or state.long_shares > 0 or state.pending is not None:
        return
    row = _price_row(market_data.get(state.current_symbol), execution_date)
    if row is None:
        return
    target_cash = state.cash / 2
    saved_cash = state.cash
    state.cash = target_cash
    trade = _trade(
        state,
        symbol=state.current_symbol,
        name=state.current_name,
        leg="long",
        action="buy",
        signal_date=signal_date,
        execution_date=execution_date,
        price=float(row["raw_close"]),
        settings=settings,
        reason=reason,
    )
    unused = state.cash
    state.cash = saved_cash - target_cash + unused
    if trade:
        trade["cash_after"] = state.cash
        trades.append(trade)


def _annual_effective_dates(master_dates: pd.DatetimeIndex) -> dict[int, pd.Timestamp]:
    result = {}
    for year, dates in pd.Series(master_dates, index=master_dates).groupby(master_dates.year):
        result[int(year)] = pd.Timestamp(dates.iloc[0])
    return result


def _apply_actions(state: DirectionSleeveState, row: pd.Series | None, *, parking: bool = False) -> None:
    if row is None:
        return
    ratio = float(row.get("share_split_ratio", 1.0) or 1.0)
    rounding = str(row.get("share_split_rounding", "") or "")
    dividend = float(row.get("dividend_per_share", 0.0) or 0.0)
    if parking:
        state.parking_shares = _apply_split(state.parking_shares, ratio, rounding)
        if state.parking_shares > 0 and dividend > 0:
            state.cash += state.parking_shares * dividend
        return
    state.long_shares = _apply_split(state.long_shares, ratio, rounding)
    state.timing_shares = _apply_split(state.timing_shares, ratio, rounding)
    if state.shares > 0 and dividend > 0:
        state.cash += state.shares * dividend


def _value_state(state: DirectionSleeveState, market_data: dict[str, pd.DataFrame], date: pd.Timestamp) -> float:
    price = _last_price(market_data.get(state.current_symbol), date)
    parking_price = _last_price(market_data.get(PARKING_SYMBOL), date)
    if np.isfinite(price):
        state.last_price = price
    if np.isfinite(parking_price):
        state.last_parking_price = parking_price
    market = state.shares * (state.last_price if np.isfinite(state.last_price) else 0.0)
    parking = state.parking_shares * (
        state.last_parking_price if np.isfinite(state.last_parking_price) else 0.0
    )
    return float(state.cash + market + parking)


def simulate_annual_portfolio(
    market_data: dict[str, pd.DataFrame],
    selections: list[AnnualSelection],
    settings: AnnualBacktestSettings,
    *,
    execution_mode: str = EXECUTION_SAME_CLOSE,
    progress_callback: Callable[[str, float], None] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if execution_mode not in {EXECUTION_SAME_CLOSE, EXECUTION_NEXT_CLOSE}:
        raise ValueError(f"不支持的成交模式：{execution_mode}")
    selection_map = _selection_map(selections)
    start_selections = [item for item in selections if item.year == settings.start_year]
    weights = _initial_weights(start_selections)
    end_date = pd.Timestamp(settings.end_date or pd.Timestamp.today()).normalize()
    selected_symbols = {item.symbol for item in selections} | {PARKING_SYMBOL}
    dates = sorted(
        {
            pd.Timestamp(date).normalize()
            for symbol in selected_symbols
            for date in pd.to_datetime(market_data.get(symbol, pd.DataFrame()).get("trade_date", []), errors="coerce")
            if pd.notna(date) and pd.Timestamp(date).year >= settings.start_year and pd.Timestamp(date) <= end_date
        }
    )
    master_dates = pd.DatetimeIndex(dates)
    if master_dates.empty:
        raise ValueError("起投年份至结束日期没有正式交易日。")
    effective_dates = _annual_effective_dates(master_dates)
    start_date = effective_dates.get(settings.start_year)
    if start_date is None:
        raise ValueError("起投年份没有正式交易日。")
    master_dates = master_dates[master_dates >= start_date]
    states = {
        slot: DirectionSleeveState(
            slot=slot,
            initial_capital=settings.initial_capital * weight / 100,
            cash=settings.initial_capital * weight / 100,
        )
        for slot, weight in weights.items()
    }
    trades: list[dict[str, object]] = []
    migrations: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    previous_date: pd.Timestamp | None = None
    previous_master_date: pd.Timestamp | None = None
    for date_index, trade_date in enumerate(master_dates):
        if previous_date is not None:
            for state in states.values():
                if state.cash > 0 and settings.cash_annual_rate:
                    state.cash *= (1 + settings.cash_annual_rate) ** (
                        max(0, (trade_date - previous_date).days) / 365
                    )
        changed_slots: set[str] = set()
        if effective_dates.get(trade_date.year) == trade_date:
            for slot, state in states.items():
                selection = selection_map.get((trade_date.year, slot))
                if selection is None:
                    continue
                changed_slots.add(slot)
                if not state.current_symbol:
                    _activate_selection(state, selection, trade_date)
                elif state.current_symbol == selection.symbol:
                    _activate_selection(state, selection, trade_date)
                elif state.shares > 0:
                    state.pending = selection
                else:
                    old_symbol = state.current_symbol
                    _sell_parking(
                        state, market_data, trade_date, trade_date, settings, trades, "年度优选变化"
                    )
                    _activate_selection(state, selection, trade_date)
                    migrations.append(
                        {
                            "slot": slot,
                            "decision_year": trade_date.year,
                            "old_symbol": old_symbol,
                            "new_symbol": selection.symbol,
                            "exit_date": trade_date,
                            "entry_date": pd.NaT,
                            "status": "旧仓已空，更新等待目标",
                        }
                    )

        for state in states.values():
            _apply_actions(state, _price_row(market_data.get(state.current_symbol), trade_date))
            _apply_actions(state, _price_row(market_data.get(PARKING_SYMBOL), trade_date), parking=True)

        signal_date = trade_date if execution_mode == EXECUTION_SAME_CLOSE else previous_master_date
        for slot, state in states.items():
            if signal_date is None:
                continue
            if execution_mode == EXECUTION_NEXT_CLOSE and slot in changed_slots:
                continue
            _buy_half_long(
                state,
                market_data,
                signal_date,
                trade_date,
                settings,
                trades,
                "美股方向长期半仓建仓",
            )
            signal = _signal_state(
                market_data.get(state.current_symbol),
                signal_date,
                state.ma_period,
                state.threshold_pct,
            )
            if state.pending is not None and state.shares > 0:
                should_exit = signal == 0
                if state.current_strategy == "half_timing" and state.timing_shares <= 0 and slot in changed_slots:
                    should_exit = True
                if should_exit:
                    old_symbol = state.current_symbol
                    target = state.pending
                    _sell_current(
                        state,
                        market_data,
                        signal_date,
                        trade_date,
                        settings,
                        trades,
                        "旧标的退出后整批迁移",
                    )
                    _activate_selection(state, target, trade_date)
                    target_signal = _signal_state(
                        market_data.get(state.current_symbol),
                        signal_date,
                        state.ma_period,
                        state.threshold_pct,
                    )
                    entered = False
                    if target_signal == 1:
                        _buy_active(
                            state,
                            market_data,
                            signal_date,
                            trade_date,
                            settings,
                            trades,
                            "迁入当年优选",
                        )
                        entered = state.shares > 0
                    else:
                        _buy_parking(
                            state,
                            market_data,
                            signal_date,
                            trade_date,
                            settings,
                            trades,
                            "等待新标合买入信号",
                        )
                    migrations.append(
                        {
                            "slot": slot,
                            "decision_year": target.year,
                            "old_symbol": old_symbol,
                            "new_symbol": target.symbol,
                            "exit_date": trade_date,
                            "entry_date": trade_date if entered else pd.NaT,
                            "status": "同日迁入" if entered else ("转入512890等待" if state.parking_shares > 0 else "转入现金等待"),
                        }
                    )
                continue

            if signal == 1:
                _buy_active(
                    state,
                    market_data,
                    signal_date,
                    trade_date,
                    settings,
                    trades,
                    "均线买入信号",
                    initial_half_hold=(trade_date == start_date),
                )
            elif signal == 0 and state.timing_shares > 0:
                price = _last_price(market_data.get(state.current_symbol), trade_date)
                trade = _trade(
                    state,
                    symbol=state.current_symbol,
                    name=state.current_name,
                    leg="timing",
                    action="sell",
                    signal_date=signal_date,
                    execution_date=trade_date,
                    price=price,
                    settings=settings,
                    reason="均线卖出信号",
                )
                if trade:
                    trades.append(trade)
                _buy_parking(
                    state,
                    market_data,
                    signal_date,
                    trade_date,
                    settings,
                    trades,
                    "方向空仓承接",
                )
            elif signal == 0 and state.current_strategy == "timing" and state.shares <= 0:
                _buy_parking(
                    state,
                    market_data,
                    signal_date,
                    trade_date,
                    settings,
                    trades,
                    "方向空仓承接",
                )

        values = {slot: _value_state(state, market_data, trade_date) for slot, state in states.items()}
        total = float(sum(values.values()))
        row = {
            "trade_date": trade_date,
            "portfolio_value": total,
            "cash_value": float(sum(state.cash for state in states.values())),
            "etf_market_value": total - float(sum(state.cash for state in states.values())),
        }
        for slot, value in values.items():
            state = states[slot]
            row[f"{slot}_value"] = value
            row[f"{slot}_symbol"] = state.current_symbol
            row[f"{slot}_shares"] = state.shares
            row[f"{slot}_parking_shares"] = state.parking_shares
        rows.append(row)
        previous_date = trade_date
        previous_master_date = trade_date
        if progress_callback and date_index % max(1, len(master_dates) // 100) == 0:
            progress_callback(f"逐日模拟：{trade_date.date()}", date_index / max(1, len(master_dates)))
    daily = pd.DataFrame(rows)
    trade_frame = pd.DataFrame(trades)
    migration_frame = pd.DataFrame(migrations)
    contribution = calculate_direction_contribution(daily, weights, settings.initial_capital)
    return daily, trade_frame, migration_frame, contribution


def _simulate_annual_hold_benchmark(
    market_data: dict[str, pd.DataFrame],
    selections: list[AnnualSelection],
    settings: AnnualBacktestSettings,
) -> pd.DataFrame:
    selection_map = _selection_map(selections)
    start_selections = [item for item in selections if item.year == settings.start_year]
    weights = _initial_weights(start_selections)
    end_date = pd.Timestamp(settings.end_date or pd.Timestamp.today()).normalize()
    dates = pd.DatetimeIndex(
        sorted(
            {
                pd.Timestamp(date).normalize()
                for item in selections
                for date in pd.to_datetime(market_data.get(item.symbol, pd.DataFrame()).get("trade_date", []), errors="coerce")
                if pd.notna(date) and pd.Timestamp(date).year >= settings.start_year and pd.Timestamp(date) <= end_date
            }
        )
    )
    effective = _annual_effective_dates(dates)
    dates = dates[dates >= effective[settings.start_year]]
    states = {
        slot: {"cash": settings.initial_capital * weight / 100, "shares": 0.0, "symbol": "", "last_price": np.nan}
        for slot, weight in weights.items()
    }
    rows = []
    commission_total = 0.0
    trade_count = 0
    previous_date = None
    for trade_date in dates:
        for slot, state in states.items():
            if previous_date is not None and state["cash"] > 0:
                state["cash"] *= (1 + settings.cash_annual_rate) ** ((trade_date - previous_date).days / 365)
            current_frame = market_data.get(state["symbol"])
            current_row = _price_row(current_frame, trade_date)
            if current_row is not None and state["shares"] > 0:
                state["shares"] = _apply_split(
                    state["shares"],
                    float(current_row.get("share_split_ratio", 1.0) or 1.0),
                    str(current_row.get("share_split_rounding", "") or ""),
                )
                state["cash"] += state["shares"] * float(current_row.get("dividend_per_share", 0.0) or 0.0)
            if effective.get(trade_date.year) == trade_date:
                target = selection_map.get((trade_date.year, slot))
                if target is not None and target.symbol != state["symbol"]:
                    old_price = _last_price(current_frame, trade_date)
                    if state["shares"] > 0 and np.isfinite(old_price):
                        gross = state["shares"] * old_price
                        commission = gross * settings.commission_rate
                        state["cash"] += gross - commission
                        commission_total += commission
                        trade_count += 1
                        state["shares"] = 0.0
                    state["symbol"] = target.symbol
            row = _price_row(market_data.get(state["symbol"]), trade_date)
            if state["shares"] <= 0 and row is not None:
                price = float(row["raw_close"])
                quantity = floor(state["cash"] / (price * (1 + settings.commission_rate)) / settings.lot_size) * settings.lot_size
                if quantity > 0:
                    gross = quantity * price
                    commission = gross * settings.commission_rate
                    state["cash"] -= gross + commission
                    commission_total += commission
                    trade_count += 1
                    state["shares"] = float(quantity)
            price = _last_price(market_data.get(state["symbol"]), trade_date)
            if np.isfinite(price):
                state["last_price"] = price
        value = sum(state["cash"] + state["shares"] * (state["last_price"] if np.isfinite(state["last_price"]) else 0) for state in states.values())
        rows.append(
            {
                "trade_date": trade_date,
                "portfolio_value": value,
                "trade_count": trade_count,
                "commission_cost": commission_total,
            }
        )
        previous_date = trade_date
    return pd.DataFrame(rows)
def _simulate_parking_benchmark(
    market_data: dict[str, pd.DataFrame],
    master_dates: Iterable[pd.Timestamp],
    settings: AnnualBacktestSettings,
) -> pd.DataFrame:
    frame = market_data.get(PARKING_SYMBOL)
    if frame is None or frame.empty:
        raise ValueError("缺少512890正式行情，无法生成承接ETF基准。")
    dates = (
        pd.DatetimeIndex(pd.to_datetime(list(master_dates), errors="coerce"))
        .dropna()
        .unique()
        .sort_values()
    )
    if dates.empty:
        raise ValueError("缺少共同回测日期，无法生成512890基准。")
    cash = float(settings.initial_capital)
    shares = 0.0
    last_price = np.nan
    previous_date = None
    commission_total = 0.0
    trade_count = 0
    rows = []
    for trade_date in dates:
        row = _price_row(frame, trade_date)
        if previous_date is not None and cash > 0:
            cash *= (1 + settings.cash_annual_rate) ** ((trade_date - previous_date).days / 365)
        if row is not None:
            shares = _apply_split(
                shares,
                float(row.get("share_split_ratio", 1.0) or 1.0),
                str(row.get("share_split_rounding", "") or ""),
            )
            if shares > 0:
                cash += shares * float(row.get("dividend_per_share", 0.0) or 0.0)
            last_price = float(row["raw_close"])
        if shares <= 0 and trade_date >= PARKING_LISTING_DATE and row is not None:
            price = float(row["raw_close"])
            quantity = (
                floor(cash / (price * (1 + settings.commission_rate)) / settings.lot_size)
                * settings.lot_size
            )
            if quantity > 0:
                gross = quantity * price
                commission = gross * settings.commission_rate
                cash -= gross + commission
                shares = float(quantity)
                commission_total += commission
                trade_count += 1
        rows.append(
            {
                "trade_date": trade_date,
                "portfolio_value": cash + shares * (last_price if np.isfinite(last_price) else 0.0),
                "trade_count": trade_count,
                "commission_cost": commission_total,
            }
        )
        previous_date = trade_date
    return pd.DataFrame(rows)
