from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from services.fund_rotation_data import _normalize_date_range, _prepare_merged_data
from services.fund_rotation_metrics import (
    _calculate_drawdown,
    _calculate_individual_nav_data,
    _calculate_individual_results,
    _calculate_yearly_stats,
    _round_lot_shares,
)
from services.fund_rotation_models import (
    BUY_SLIPPAGE,
    EXECUTION_AFTER_CLOSE,
    EXECUTION_MODES,
    EXECUTION_NEXT_OPEN,
    LOT_SIZE,
    SELL_SLIPPAGE,
    RotationInput,
    RotationResult,
)
from services.fund_rotation_summary import _build_summary, _execution_mode_label

def run_fund_rotation_backtest(
    funds: list[RotationInput],
    frequency: str = "week",
    lookback_period: int = 22,
    num_positions: int = 1,
    initial_capital: float = 100000.0,
    transaction_cost: float = 0.00006,
    start_date: str | pd.Timestamp | None = None,
    end_date: str | pd.Timestamp | None = None,
    execution_mode: str = EXECUTION_AFTER_CLOSE,
) -> RotationResult:
    if len(funds) < 2:
        raise ValueError("至少需要导入 2 只基金进行轮动回测。")
    if num_positions < 1 or num_positions > len(funds):
        raise ValueError(f"持仓数量必须在 1 到 {len(funds)} 之间。")
    if initial_capital <= 0:
        raise ValueError("初始资金必须大于 0。")
    if lookback_period < 1:
        raise ValueError("动量周期必须大于 0。")
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"不支持的轮动成交方式：{execution_mode}")

    symbol_names = {fund.symbol: fund.name for fund in funds}
    source_data = {fund.symbol: fund.dataframe.copy() for fund in funds}
    merged = _prepare_merged_data(source_data)
    requested_start, requested_end = _normalize_date_range(start_date, end_date)
    if requested_end is not None:
        merged_until_end = merged[merged["trade_date"] <= requested_end]
        if merged_until_end.empty:
            raise ValueError("所选结束日期早于可用行情。")
        actual_end_date = pd.Timestamp(merged_until_end["trade_date"].max())
    else:
        actual_end_date = pd.Timestamp(merged["trade_date"].max())
    signal_lag = 0 if execution_mode == EXECUTION_AFTER_CLOSE else 1
    earliest_start_date = _get_start_date(source_data, lookback_period, signal_lag=signal_lag)
    desired_start_date = (
        max(earliest_start_date, requested_start)
        if requested_start is not None
        else earliest_start_date
    )
    if desired_start_date > actual_end_date:
        raise ValueError("所选时间区间不足以完成动量预热和首次调仓。")
    market_data = merged[merged["trade_date"] <= actual_end_date].reset_index(drop=True)
    scheduled_start_date = _align_rebalance_start(
        desired_start_date,
        frequency,
        actual_end_date,
        market_data,
    )
    calendar_backtest_df = market_data[
        market_data["trade_date"] >= scheduled_start_date
    ].reset_index(drop=True)
    all_dates = list(pd.to_datetime(market_data["trade_date"]).dropna().sort_values().unique())
    scheduled_dates = _build_rebalance_dates(
        scheduled_start_date,
        actual_end_date,
        frequency,
        all_dates,
        calendar_backtest_df,
    )
    momentum_cache = _calculate_momentum(
        merged,
        list(source_data.keys()),
        lookback_period,
        signal_lag=signal_lag,
    )
    rotation_plan = _build_rotation_plan(
        scheduled_dates,
        market_data,
        momentum_cache,
        num_positions,
        actual_end_date,
        execution_mode,
    )
    if not rotation_plan:
        raise ValueError("所选时间区间内没有生成可执行的调仓计划。")
    actual_start_date = rotation_plan[0][0]
    backtest_df = merged[
        (merged["trade_date"] >= actual_start_date)
        & (merged["trade_date"] <= actual_end_date)
    ].reset_index(drop=True)
    if backtest_df.empty:
        raise ValueError("所选时间区间内没有可回测行情。")

    current_shares: dict[str, float] = {}
    current_cost_basis: dict[str, float] = {}
    cash_value = float(initial_capital)
    total_buy_cost = 0.0
    total_sell_cost = 0.0
    realized_trade_pnls: list[float] = []
    nav_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []

    for index, (rebal_date, selected, momentum) in enumerate(rotation_plan):
        date_rows = backtest_df[backtest_df["trade_date"] == rebal_date]
        if date_rows.empty:
            continue
        row = date_rows.iloc[0]
        holdings_changed = set(selected) != set(current_shares.keys())

        value_before = _portfolio_value(current_shares, row) + cash_value
        if not current_shares:
            value_before = cash_value

        sell_cost = 0.0
        buy_cost = 0.0
        sold_cost_basis = 0.0
        realized_pnl_total = 0.0
        sell_details: list[str] = []
        buy_details: list[str] = []
        retained_symbols = [symbol for symbol in selected if symbol in current_shares]
        exiting_symbols = [symbol for symbol in current_shares if symbol not in selected]
        entering_symbols = [symbol for symbol in selected if symbol not in current_shares]
        new_shares = {symbol: current_shares[symbol] for symbol in retained_symbols}
        new_cost_basis = {symbol: current_cost_basis.get(symbol, 0.0) for symbol in retained_symbols}
        execution_prices = {symbol: _row_price(row, symbol) for symbol in retained_symbols}

        if holdings_changed:
            for symbol in exiting_symbols:
                shares = current_shares[symbol]
                price = _trade_price(
                    row,
                    symbol,
                    side="sell",
                    apply_slippage=_trade_uses_slippage(funds, symbol),
                    execution_mode=execution_mode,
                )
                gross_value = shares * price
                cost = gross_value * transaction_cost
                net_value = gross_value - cost
                cost_basis = current_cost_basis.get(symbol, gross_value)
                realized_pnl = net_value - cost_basis
                realized_return = realized_pnl / cost_basis * 100 if cost_basis > 0 else 0.0
                cash_value += net_value
                sell_cost += cost
                sold_cost_basis += cost_basis
                realized_pnl_total += realized_pnl
                if shares > 0 and cost_basis > 0:
                    realized_trade_pnls.append(realized_pnl)
                sell_details.append(
                    f"{symbol_names.get(symbol, symbol)} 卖出份额:{shares:.2f} 卖出价:{price:.4f} "
                    f"卖出金额:{gross_value:.2f} 手续费:{cost:.2f} 到账:{net_value:.2f} "
                    f"盈亏:{realized_pnl:.2f} 盈亏率:{realized_return:.2f}%"
                )
        allocation = cash_value / len(entering_symbols) if entering_symbols else 0.0
        for symbol in entering_symbols:
            price = _trade_price(
                row,
                symbol,
                side="buy",
                apply_slippage=_trade_uses_slippage(funds, symbol),
                execution_mode=execution_mode,
            )
            cost = allocation * transaction_cost
            net_allocation = allocation - cost
            shares = _round_lot_shares(
                net_allocation / price if price > 0 else 0.0,
                lot_size=_trade_lot_size(funds, symbol),
            )
            actual_buy_value = shares * price
            cost = actual_buy_value * transaction_cost
            cash_value -= actual_buy_value + cost
            buy_cost += cost
            buy_details.append(
                f"{symbol_names.get(symbol, symbol)} 计划金额:{allocation:.2f} 买入价:{price:.4f} "
                f"买入金额:{actual_buy_value:.2f} "
                f"买入份额:{shares:.2f} 手续费:{cost:.2f}"
            )
            new_shares[symbol] = shares
            new_cost_basis[symbol] = actual_buy_value + cost
            execution_prices[symbol] = price

        current_shares = new_shares
        current_cost_basis = new_cost_basis
        execution_value_after = float(
            sum(shares * execution_prices.get(symbol, _row_price(row, symbol)) for symbol, shares in current_shares.items())
        ) + cash_value
        close_value_after = _portfolio_value(current_shares, row) + cash_value
        total_buy_cost += buy_cost
        total_sell_cost += sell_cost

        trade_rows.append(
            {
                "日期": rebal_date,
                "操作": "调仓" if holdings_changed else "持有",
                "成交方式": _execution_mode_label(execution_mode),
                "选中标的": "; ".join(symbol_names.get(symbol, symbol) for symbol in selected),
                "标的代码": "; ".join(selected),
                "动量": "; ".join(
                    f"{symbol_names.get(symbol, symbol)}:{momentum.get(symbol, 0) * 100:.2f}%"
                    for symbol in source_data.keys()
                ),
                "调仓前金额": round(value_before, 2),
                "成交后金额": round(execution_value_after, 2),
                "调仓日收盘金额": round(close_value_after, 2),
                "买入手续费": round(buy_cost, 2),
                "卖出手续费": round(sell_cost, 2),
                "本次总成本": round(buy_cost + sell_cost, 2),
                "本次交易盈亏金额": round(realized_pnl_total, 2) if sold_cost_basis > 0 else None,
                "本次交易盈亏率(%)": (
                    round(realized_pnl_total / sold_cost_basis * 100, 2)
                    if sold_cost_basis > 0
                    else None
                ),
                "现金余额": round(cash_value, 2),
                "卖出明细": " | ".join(sell_details),
                "买入明细": " | ".join(buy_details),
                "调仓后持仓金额": _holding_amount_detail(current_shares, row, symbol_names),
            }
        )

        next_date = (
            rotation_plan[index + 1][0]
            if index + 1 < len(rotation_plan)
            else actual_end_date + timedelta(days=1)
        )
        period_data = backtest_df[(backtest_df["trade_date"] >= rebal_date) & (backtest_df["trade_date"] < next_date)]
        for _, period_row in period_data.iterrows():
            total_value = _portfolio_value(current_shares, period_row) + cash_value
            nav_rows.append(
                {
                    "日期": period_row["trade_date"],
                    "账户净值": round(total_value, 2),
                    "累计收益率(%)": round((total_value / initial_capital - 1) * 100, 2),
                    "当前持仓": ", ".join(symbol_names.get(symbol, symbol) for symbol in current_shares),
                    "现金余额": round(cash_value, 2),
                    "持仓金额明细": _holding_amount_detail(current_shares, period_row, symbol_names),
                }
            )

    nav_df = pd.DataFrame(nav_rows).drop_duplicates("日期", keep="last").reset_index(drop=True)
    trades_df = pd.DataFrame(trade_rows)
    if nav_df.empty:
        raise ValueError("没有生成有效回测净值。")

    drawdown_df = _calculate_drawdown(nav_df, initial_capital=initial_capital)
    individual_df = _calculate_individual_results(
        source_data,
        symbol_names,
        actual_start_date,
        actual_end_date,
        initial_capital,
    )
    individual_nav_df = _calculate_individual_nav_data(
        source_data,
        symbol_names,
        actual_start_date,
        actual_end_date,
        initial_capital,
    )
    yearly_stats = _calculate_yearly_stats(nav_df, initial_capital=initial_capital)
    summary = _build_summary(
        nav_df=nav_df,
        trades_df=trades_df,
        drawdown_df=drawdown_df,
        start_date=actual_start_date,
        end_date=actual_end_date,
        initial_capital=initial_capital,
        total_buy_cost=total_buy_cost,
        total_sell_cost=total_sell_cost,
        realized_trade_pnls=realized_trade_pnls,
        execution_mode=execution_mode,
    )

    return RotationResult(
        start_date=actual_start_date,
        end_date=actual_end_date,
        nav_data=nav_df,
        trades=trades_df,
        summary=summary,
        individual_results=individual_df,
        individual_nav_data=individual_nav_df,
        drawdown=drawdown_df,
        yearly_stats=yearly_stats,
    )

def _get_start_date(
    source_data: dict[str, pd.DataFrame],
    lookback_period: int,
    signal_lag: int = 1,
) -> pd.Timestamp:
    eligible_dates = []
    for df in source_data.values():
        data = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
        first_trade_position = lookback_period + signal_lag
        if len(data) <= first_trade_position:
            raise ValueError("有基金数据长度不足，无法计算完整动量窗口。")
        eligible_dates.append(pd.Timestamp(data.loc[first_trade_position, "trade_date"]))
    return max(eligible_dates)


def _align_rebalance_start(
    start_date: pd.Timestamp,
    frequency: str,
    end_date: pd.Timestamp,
    merged: pd.DataFrame,
) -> pd.Timestamp:
    target = pd.Timestamp(start_date)
    if frequency == "week":
        while target.weekday() != 0:
            target += timedelta(days=1)
    else:
        if target.day != 1:
            if target.month == 12:
                target = pd.Timestamp(target.year + 1, 1, 1)
            else:
                target = pd.Timestamp(target.year, target.month + 1, 1)
    aligned = _find_valid_date(merged, target, direction="next")
    if aligned is None or aligned > end_date:
        raise ValueError("没有找到满足动量窗口要求的调仓日期。")
    return aligned


def _find_valid_date(df: pd.DataFrame, target_date: pd.Timestamp, direction: str) -> pd.Timestamp | None:
    dates = pd.to_datetime(df["trade_date"])
    if direction == "next":
        valid = dates[dates >= target_date]
        return pd.Timestamp(valid.iloc[0]) if not valid.empty else None
    valid = dates[dates <= target_date]
    return pd.Timestamp(valid.iloc[-1]) if not valid.empty else None


def _next_rebalance_date(current_date: pd.Timestamp, frequency: str, all_dates: list[pd.Timestamp]) -> pd.Timestamp:
    if frequency == "week":
        next_date = current_date + timedelta(days=1)
        while next_date.weekday() != 0:
            next_date += timedelta(days=1)
    else:
        if current_date.month == 12:
            next_date = pd.Timestamp(current_date.year + 1, 1, 1)
        else:
            next_date = pd.Timestamp(current_date.year, current_date.month + 1, 1)
    valid = [pd.Timestamp(date) for date in all_dates if pd.Timestamp(date) >= next_date]
    return valid[0] if valid else next_date


def _build_rebalance_dates(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    frequency: str,
    all_dates: list[pd.Timestamp],
    backtest_df: pd.DataFrame,
) -> list[pd.Timestamp]:
    dates: list[pd.Timestamp] = []
    current = start_date
    while current <= end_date:
        valid = _find_valid_date(backtest_df, current, direction="next")
        if valid is not None and valid not in dates:
            dates.append(valid)
        current = _next_rebalance_date(current, frequency, all_dates)
    return dates


def _build_rotation_plan(
    scheduled_dates: list[pd.Timestamp],
    market_data: pd.DataFrame,
    momentum_cache: pd.DataFrame,
    num_positions: int,
    end_date: pd.Timestamp,
    execution_mode: str,
) -> list[tuple[pd.Timestamp, list[str], dict[str, float]]]:
    plan: list[tuple[pd.Timestamp, list[str], dict[str, float]]] = []
    current_symbols: set[str] = set()
    for index, scheduled_date in enumerate(scheduled_dates):
        momentum = momentum_cache.loc[scheduled_date].to_dict() if scheduled_date in momentum_cache.index else {}
        momentum = {
            key: float(value)
            for key, value in momentum.items()
            if not pd.isna(value) and not np.isinf(value)
        }
        selected = _select_top_symbols(momentum, num_positions)
        if len(selected) < num_positions:
            continue

        selected_symbols = set(selected)
        holdings_changed = selected_symbols != current_symbols
        if holdings_changed:
            symbols_to_trade = current_symbols.symmetric_difference(selected_symbols)
            next_scheduled_date = (
                scheduled_dates[index + 1]
                if index + 1 < len(scheduled_dates)
                else pd.Timestamp(end_date) + timedelta(days=1)
            )
            execution_date = _find_tradeable_date(
                market_data,
                scheduled_date,
                next_scheduled_date,
                symbols_to_trade,
                execution_mode,
            )
            if execution_date is None:
                continue
        else:
            execution_date = pd.Timestamp(scheduled_date)

        plan.append((execution_date, selected, momentum))
        current_symbols = selected_symbols
    return plan


def _find_tradeable_date(
    market_data: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date_exclusive: pd.Timestamp,
    symbols: set[str],
    execution_mode: str,
) -> pd.Timestamp | None:
    candidates = market_data[
        (market_data["trade_date"] >= pd.Timestamp(start_date))
        & (market_data["trade_date"] < pd.Timestamp(end_date_exclusive))
    ]
    for _, row in candidates.iterrows():
        if all(
            _has_execution_price(row, symbol, execution_mode)
            for symbol in symbols
        ):
            return pd.Timestamp(row["trade_date"])
    return None


def _calculate_momentum(
    merged: pd.DataFrame,
    symbols: list[str],
    lookback_period: int,
    signal_lag: int = 1,
) -> pd.DataFrame:
    momentum = {}
    indexed = merged.set_index("trade_date")
    for symbol in symbols:
        raw_column = f"{symbol}__raw_close"
        prices = pd.to_numeric(indexed[raw_column] if raw_column in indexed.columns else indexed[symbol], errors="coerce")
        actual_prices = prices.dropna()
        actual_momentum = actual_prices.pct_change(periods=lookback_period, fill_method=None)
        aligned_momentum = actual_momentum.reindex(indexed.index).ffill()
        momentum[symbol] = aligned_momentum.shift(signal_lag) if signal_lag else aligned_momentum
    return pd.DataFrame(momentum, index=indexed.index)


def _select_top_symbols(momentum: dict[str, float], num_positions: int) -> list[str]:
    return [symbol for symbol, _ in sorted(momentum.items(), key=lambda item: item[1], reverse=True)[:num_positions]]


def _row_price(row: pd.Series, symbol: str) -> float:
    value = pd.to_numeric(row.get(symbol), errors="coerce")
    return 0.0 if pd.isna(value) else float(value)


def _trade_price(
    row: pd.Series,
    symbol: str,
    side: str,
    apply_slippage: bool = True,
    execution_mode: str = EXECUTION_NEXT_OPEN,
) -> float:
    price_column = f"{symbol}__raw_close" if execution_mode == EXECUTION_AFTER_CLOSE else f"{symbol}__open"
    price_value = pd.to_numeric(row.get(price_column), errors="coerce")
    if pd.isna(price_value) or float(price_value) <= 0:
        price_name = "收盘价" if execution_mode == EXECUTION_AFTER_CLOSE else "开盘价"
        raise ValueError(f"{symbol} 在调仓日没有有效{price_name}，无法成交。")
    base_price = float(price_value)
    if execution_mode == EXECUTION_AFTER_CLOSE:
        return base_price
    if not apply_slippage:
        return base_price
    if side == "buy":
        return base_price * (1 + BUY_SLIPPAGE)
    if side == "sell":
        return base_price * (1 - SELL_SLIPPAGE)
    return base_price


def _has_execution_price(row: pd.Series, symbol: str, execution_mode: str) -> bool:
    price_column = f"{symbol}__raw_close" if execution_mode == EXECUTION_AFTER_CLOSE else f"{symbol}__open"
    value = pd.to_numeric(row.get(price_column), errors="coerce")
    return not pd.isna(value) and float(value) > 0

def _trade_lot_size(funds: list[RotationInput], symbol: str) -> int:
    for fund in funds:
        if fund.symbol == symbol:
            return int(fund.trade_lot_size)
    return LOT_SIZE


def _trade_uses_slippage(funds: list[RotationInput], symbol: str) -> bool:
    for fund in funds:
        if fund.symbol == symbol:
            return bool(fund.apply_slippage)
    return True

def _portfolio_value(shares: dict[str, float], row: pd.Series) -> float:
    return float(sum(amount * _row_price(row, symbol) for symbol, amount in shares.items()))


def _holding_amount_detail(shares: dict[str, float], row: pd.Series, names: dict[str, str]) -> str:
    parts = []
    for symbol, amount in shares.items():
        value = amount * _row_price(row, symbol)
        parts.append(f"{names.get(symbol, symbol)}:{value:.2f}")
    return " | ".join(parts)

__all__ = [
    "run_fund_rotation_backtest",
    "_get_start_date",
    "_align_rebalance_start",
    "_find_valid_date",
    "_next_rebalance_date",
    "_build_rebalance_dates",
    "_build_rotation_plan",
    "_find_tradeable_date",
    "_calculate_momentum",
    "_select_top_symbols",
    "_row_price",
    "_trade_price",
    "_has_execution_price",
    "_trade_lot_size",
    "_trade_uses_slippage",
    "_portfolio_value",
    "_holding_amount_detail",
]
