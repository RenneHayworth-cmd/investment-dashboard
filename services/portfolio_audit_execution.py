from __future__ import annotations

from math import floor

import numpy as np
import pandas as pd

from services.portfolio_audit_models import (
    AuditAllocation,
    AuditSettings,
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_NEXT_OPEN,
    MISSED_ORDER_BOTH,
    _SleeveResult,
)


def _run_sleeve(
    allocation: AuditAllocation,
    source: pd.DataFrame,
    dates: pd.DatetimeIndex,
    initial_capital: float,
    settings: AuditSettings,
    *,
    always_hold: bool,
    blocked_entries: set[tuple[str, pd.Timestamp]],
) -> _SleeveResult:
    data = source.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"]).dt.normalize()
    data = data.sort_values("trade_date").drop_duplicates("trade_date").set_index("trade_date")
    ma_period = allocation.ma_period
    data["ma"] = data["signal_close"].rolling(ma_period, min_periods=ma_period).mean()
    data["deviation"] = data["signal_close"] / data["ma"] - 1
    data["sigma_prev"] = (
        data["deviation"]
        .rolling(allocation.sigma_period, min_periods=allocation.sigma_period)
        .std(ddof=1)
        .shift(1)
    )
    previous_close = data["signal_close"].shift(1)
    true_range = pd.concat(
        [
            data["signal_high"] - data["signal_low"],
            (data["signal_high"] - previous_close).abs(),
            (data["signal_low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr20"] = true_range.rolling(20, min_periods=20).mean()
    threshold = allocation.threshold_pct / 100
    rng = np.random.default_rng(settings.random_seed)
    cash = float(initial_capital)
    shares = 0.0
    cost_basis = 0.0
    trade_dividends = 0.0
    pending: dict[str, object] | None = None
    blocked_desired: int | None = None
    retry_action: str | None = None
    retry_signal_date: pd.Timestamp | None = None
    retry_origin_index: int | None = None
    retry_count = 0
    previous_desired = 0
    previous_date: pd.Timestamp | None = None
    commission_total = 0.0
    slippage_total = 0.0
    dividend_total = 0.0
    cash_income_total = 0.0
    realized_total = 0.0
    signal_generated_count = 0
    order_submitted_count = 0
    missed_buy_count = 0
    missed_sell_count = 0
    order_retried_count = 0
    order_filled_count = 0
    execution_delays: list[int] = []
    rows: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    sleeve_name = "长期" if always_hold else "择时"

    def record_trade(
        trade: dict[str, object] | None,
        costs: tuple[float, float],
        *,
        signal_price: float,
        submission_date: pd.Timestamp,
        was_retried: bool,
        delay_days: int,
    ) -> bool:
        nonlocal commission_total, slippage_total, realized_total, order_filled_count
        if trade is None:
            return False
        trade["signal_price"] = signal_price
        trade["submission_date"] = submission_date
        trade["order_retried"] = was_retried
        trade["execution_delay_days"] = delay_days
        trades.append(trade)
        commission_total += costs[0]
        slippage_total += costs[1]
        realized_value = trade.get("realized_pnl")
        realized_total += float(realized_value) if pd.notna(realized_value) else 0.0
        order_filled_count += 1
        execution_delays.append(delay_days)
        return True

    for date_index, trade_date in enumerate(dates):
        row = data.loc[trade_date]
        if previous_date is not None and cash > 0 and settings.cash_annual_rate:
            days = max(0, (trade_date - previous_date).days)
            income = cash * ((1 + settings.cash_annual_rate) ** (days / 365) - 1)
            cash += income
            cash_income_total += income
        shares_before_split = shares
        split_ratio = float(row.get("share_split_ratio", 1.0) or 1.0)
        if shares > 0 and not np.isclose(split_ratio, 1.0):
            split_shares = shares * split_ratio
            split_rounding = str(row.get("share_split_rounding", "floor") or "floor")
            if split_rounding == "ceil":
                shares = float(np.ceil(split_shares - 1e-12))
            elif split_rounding == "round":
                shares = float(np.rint(split_shares))
            else:
                shares = float(np.floor(split_shares + 1e-12))
        shares_after_split = shares
        dividend = float(row.get("dividend_per_share", 0) or 0) * shares
        if dividend > 0:
            cash += dividend
            dividend_total += dividend
            trade_dividends += dividend

        ma = float(row["ma"]) if pd.notna(row["ma"]) else np.nan
        deviation = float(row["deviation"]) if pd.notna(row["deviation"]) else np.nan
        sigma_prev = float(row["sigma_prev"]) if pd.notna(row["sigma_prev"]) else np.nan
        signal_price = float(row["signal_close"])
        buy_threshold = np.nan
        sell_threshold = np.nan
        upper_line = np.nan
        lower_line = np.nan
        if always_hold:
            desired = 1
            signal = "持有"
        elif np.isnan(ma):
            desired = previous_desired
            signal = "等待均线"
        else:
            if allocation.signal_rule == "atr":
                atr = float(row["atr20"]) if pd.notna(row["atr20"]) else np.nan
                upper_line = ma + allocation.atr_k * atr
                lower_line = ma - allocation.atr_k * atr
                buy_threshold = upper_line / ma - 1 if ma else np.nan
                sell_threshold = 1 - lower_line / ma if ma else np.nan
            elif allocation.signal_rule in {"sigma", "hybrid_sigma"}:
                if np.isfinite(sigma_prev) and sigma_prev > 0:
                    buy_alpha = allocation.buy_alpha_pct / 100 if allocation.signal_rule == "hybrid_sigma" else 0.0
                    sell_alpha = allocation.sell_alpha_pct / 100 if allocation.signal_rule == "hybrid_sigma" else 0.0
                    buy_threshold = buy_alpha + allocation.buy_k * sigma_prev
                    sell_threshold = sell_alpha + allocation.sell_k * sigma_prev
                    upper_line = ma * (1 + buy_threshold)
                    lower_line = ma * (1 - sell_threshold)
            else:
                buy_threshold = threshold
                sell_threshold = threshold
                upper_line = ma * (1 + threshold)
                lower_line = ma * (1 - threshold)
            if np.isnan(upper_line) or np.isnan(lower_line):
                desired = previous_desired
                signal = "等待均线"
            elif signal_price > upper_line:
                desired = 1
                signal = "持有"
            elif signal_price < lower_line:
                desired = 0
                signal = "空仓"
            else:
                desired = previous_desired
                signal = "维持"

        signal_generated = desired != previous_desired
        signal_generated_count += int(signal_generated)
        target_position = float(desired)
        execution_status = "无成交"
        order_submitted = False
        order_missed = False
        order_retried = False
        order_filled = False
        execution_delay_days = np.nan

        if blocked_desired is not None and desired != blocked_desired:
            blocked_desired = None

        if pending is not None:
            pending_action = str(pending["action"])
            pending_matches_target = (pending_action == "buy" and desired == 1) or (
                pending_action == "sell" and desired == 0
            )
            if not pending_matches_target:
                execution_status = "目标仓位变化，取消旧方向待执行订单"
                pending = None
                retry_action = None
                retry_signal_date = None
                retry_origin_index = None
                retry_count = 0

        if pending is not None and _pending_due(pending, date_index, settings.execution_mode):
            price_kind = str(pending["price_kind"])
            raw_price = float(row["raw_open"] if price_kind == "open" else row["raw_close"])
            cash, shares, cost_basis, trade_dividends, trade, costs = _execute_order(
                allocation,
                sleeve_name,
                str(pending["action"]),
                pd.Timestamp(pending["signal_date"]),
                trade_date,
                raw_price,
                cash,
                shares,
                cost_basis,
                trade_dividends,
                settings,
                f"顺延至{price_kind}",
            )
            delay_days = date_index - int(pending["origin_index"])
            order_retried = bool(pending.get("order_retried", False))
            order_filled = record_trade(
                trade,
                costs,
                signal_price=float(data.loc[pd.Timestamp(pending["signal_date"]), "signal_close"]),
                submission_date=pd.Timestamp(pending["submission_date"]),
                was_retried=order_retried,
                delay_days=delay_days,
            )
            if order_filled:
                execution_status = "顺延成交"
                execution_delay_days = delay_days
                retry_action = None
                retry_signal_date = None
                retry_origin_index = None
                retry_count = 0
            else:
                execution_status = "顺延未成交，继续纠偏"
                retry_action = str(pending["action"])
                retry_signal_date = pd.Timestamp(pending["signal_date"])
                retry_origin_index = int(pending["origin_index"])
                retry_count = max(1, int(pending.get("retry_count", 0)))
            pending = None

        actual_state = int(shares > 0)
        needed_action = "buy" if desired == 1 and actual_state == 0 else "sell" if desired == 0 and actual_state == 1 else None
        if retry_action is not None and retry_action != needed_action:
            retry_action = None
            retry_signal_date = None
            retry_origin_index = None
            retry_count = 0

        if needed_action is not None and pending is None:
            block_key = (allocation.symbol, pd.Timestamp(trade_date).normalize())
            blocked = needed_action == "buy" and block_key in blocked_entries
            if blocked_desired == desired or blocked:
                blocked_desired = desired
                execution_status = "整笔交易已剔除" if blocked else "剔除交易信号已抑制"
            else:
                order_submitted = True
                order_submitted_count += 1
                order_retried = retry_action == needed_action and retry_count > 0
                order_retried_count += int(order_retried)
                origin_date = retry_signal_date if order_retried else trade_date
                origin_index = retry_origin_index if order_retried else date_index
                can_miss = settings.missed_order_side in (MISSED_ORDER_BOTH, needed_action)
                missed = (
                    settings.missed_signal_rate > 0
                    and can_miss
                    and rng.random() < settings.missed_signal_rate
                )
                if missed:
                    order_missed = True
                    if needed_action == "buy":
                        missed_buy_count += 1
                    else:
                        missed_sell_count += 1
                    retry_action = needed_action
                    retry_signal_date = pd.Timestamp(origin_date)
                    retry_origin_index = int(origin_index)
                    retry_count += 1
                    execution_status = "买入漏单，下一交易日继续纠偏" if needed_action == "buy" else "卖出漏单，下一交易日继续纠偏"
                elif settings.execution_mode == EXECUTION_AFTER_CLOSE:
                    filled = settings.after_hours_fill_rate >= 1 or rng.random() < settings.after_hours_fill_rate
                    if filled:
                        cash, shares, cost_basis, trade_dividends, trade, costs = _execute_order(
                            allocation,
                            sleeve_name,
                            needed_action,
                            pd.Timestamp(origin_date),
                            trade_date,
                            float(row["raw_close"]),
                            cash,
                            shares,
                            cost_basis,
                            trade_dividends,
                            settings,
                            "盘后收盘价成交",
                        )
                        delay_days = date_index - int(origin_index)
                        order_filled = record_trade(
                            trade,
                            costs,
                            signal_price=float(data.loc[pd.Timestamp(origin_date), "signal_close"]),
                            submission_date=trade_date,
                            was_retried=order_retried,
                            delay_days=delay_days,
                        )
                        if order_filled:
                            execution_status = "盘后成交"
                            execution_delay_days = delay_days
                            retry_action = None
                            retry_signal_date = None
                            retry_origin_index = None
                            retry_count = 0
                        else:
                            execution_status = "当日未成交，下一交易日继续纠偏"
                            retry_action = needed_action
                            retry_signal_date = pd.Timestamp(origin_date)
                            retry_origin_index = int(origin_index)
                            retry_count = max(1, retry_count)
                    else:
                        pending = {
                            "action": needed_action,
                            "signal_date": pd.Timestamp(origin_date),
                            "submission_date": trade_date,
                            "origin_index": int(origin_index),
                            "due_index": date_index + 1,
                            "price_kind": "open",
                            "order_retried": order_retried,
                            "retry_count": retry_count,
                        }
                        retry_action = None
                        retry_signal_date = None
                        retry_origin_index = None
                        retry_count = 0
                        execution_status = "盘后未成交，顺延开盘"
                else:
                    delay = 1 if settings.execution_mode in (EXECUTION_NEXT_OPEN, EXECUTION_NEXT_CLOSE) else 2
                    pending = {
                        "action": needed_action,
                        "signal_date": pd.Timestamp(origin_date),
                        "submission_date": trade_date,
                        "origin_index": int(origin_index),
                        "due_index": date_index + delay,
                        "price_kind": "close" if settings.execution_mode == EXECUTION_NEXT_CLOSE else "open",
                        "order_retried": order_retried,
                        "retry_count": retry_count,
                    }
                    retry_action = None
                    retry_signal_date = None
                    retry_origin_index = None
                    retry_count = 0
                    execution_status = "待顺延成交"

        actual_position = float(shares > 0)
        value = cash + shares * float(row["raw_close"])
        rows.append(
            {
                "trade_date": trade_date,
                "symbol": allocation.symbol,
                "sleeve": sleeve_name,
                "signal_price": signal_price,
                "raw_close": float(row["raw_close"]),
                "valuation_price": float(row["raw_close"]),
                "ma": ma,
                "deviation": deviation,
                "sigma_prev": sigma_prev,
                "buy_threshold_pct": buy_threshold * 100 if np.isfinite(buy_threshold) else np.nan,
                "sell_threshold_pct": sell_threshold * 100 if np.isfinite(sell_threshold) else np.nan,
                "upper_trigger_line": upper_line,
                "lower_trigger_line": lower_line,
                "signal": signal,
                "signal_generated": bool(signal_generated),
                "target_position": target_position,
                "actual_position": actual_position,
                "order_submitted": bool(order_submitted),
                "order_missed": bool(order_missed),
                "order_retried": bool(order_retried),
                "order_filled": bool(order_filled),
                "order_action": needed_action if order_submitted else "",
                "missed_buy": bool(order_missed and needed_action == "buy"),
                "missed_sell": bool(order_missed and needed_action == "sell"),
                "execution_delay_days": execution_delay_days,
                "execution_status": execution_status,
                "share_split_ratio": split_ratio,
                "shares_before_split": shares_before_split,
                "shares_after_split": shares_after_split,
                "shares": shares,
                "cash": cash,
                "market_value": shares * float(row["raw_close"]),
                "sleeve_value": value,
                "dividend_income_today": dividend,
            }
        )
        previous_desired = desired
        previous_date = trade_date

    return _SleeveResult(
        daily=pd.DataFrame(rows),
        trades=pd.DataFrame(trades),
        summary={
            "commission_cost": commission_total,
            "slippage_cost": slippage_total,
            "dividend_income": dividend_total,
            "cash_income": cash_income_total,
            "realized_pnl": realized_total,
            "signal_generated_count": signal_generated_count,
            "order_submitted_count": order_submitted_count,
            "missed_order_count": missed_buy_count + missed_sell_count,
            "missed_buy_count": missed_buy_count,
            "missed_sell_count": missed_sell_count,
            "order_retried_count": order_retried_count,
            "order_filled_count": order_filled_count,
            "average_execution_delay_days": float(np.mean(execution_delays)) if execution_delays else 0.0,
        },
    )


def _pending_due(pending: dict[str, object], date_index: int, execution_mode: str) -> bool:
    del execution_mode
    return date_index >= int(pending["due_index"])


def _execute_order(
    allocation: AuditAllocation,
    sleeve_name: str,
    action: str,
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    raw_price: float,
    cash: float,
    shares: float,
    cost_basis: float,
    trade_dividends: float,
    settings: AuditSettings,
    status: str,
) -> tuple[float, float, float, float, dict[str, object] | None, tuple[float, float]]:
    if not np.isfinite(raw_price) or raw_price <= 0:
        return cash, shares, cost_basis, trade_dividends, None, (0.0, 0.0)
    slip = settings.slippage_bp / 10000
    execution_price = raw_price * (1 + slip if action == "buy" else 1 - slip)
    if action == "buy":
        affordable = cash / (execution_price * (1 + settings.commission_rate))
        buy_shares = floor(affordable / settings.lot_size) * settings.lot_size
        if buy_shares <= 0:
            return cash, shares, cost_basis, trade_dividends, None, (0.0, 0.0)
        gross = buy_shares * execution_price
        commission = gross * settings.commission_rate
        slippage_cost = buy_shares * max(0.0, execution_price - raw_price)
        cash -= gross + commission
        shares = float(buy_shares)
        cost_basis = gross + commission
        trade_dividends = 0.0
        realized_pnl = np.nan
        realized_return = np.nan
    else:
        if shares <= 0:
            return cash, shares, cost_basis, trade_dividends, None, (0.0, 0.0)
        gross = shares * execution_price
        commission = gross * settings.commission_rate
        slippage_cost = shares * max(0.0, raw_price - execution_price)
        proceeds = gross - commission
        cash += proceeds
        realized_pnl = proceeds + trade_dividends - cost_basis
        realized_return = realized_pnl / cost_basis * 100 if cost_basis else 0.0
        sold_shares = shares
        shares = 0.0
        cost_basis = 0.0
    trade = {
        "symbol": allocation.symbol,
        "name": allocation.name,
        "sleeve": sleeve_name,
        "signal_date": signal_date,
        "execution_date": execution_date,
        "action": action,
        "signal_price": np.nan,
        "raw_reference_price": raw_price,
        "execution_price": execution_price,
        "shares": float(buy_shares if action == "buy" else sold_shares),
        "gross_amount": gross,
        "commission": commission,
        "slippage_cost": slippage_cost,
        "dividend_during_trade": trade_dividends if action == "sell" else 0.0,
        "realized_pnl": realized_pnl,
        "realized_return_pct": realized_return,
        "execution_status": status,
        "cash_after": cash,
    }
    if action == "sell":
        trade_dividends = 0.0
    return cash, shares, cost_basis, trade_dividends, trade, (commission, slippage_cost)
