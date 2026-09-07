from __future__ import annotations

import numpy as np
import pandas as pd

from core.metrics import annual_volatility, annualized_return
from services.fund_rotation_data import _normalize_date_range
from services.fund_rotation_metrics import (
    _calculate_drawdown,
    _calculate_nav_returns,
    _calculate_sharpe_ratio,
    _calculate_yearly_stats,
    _round_lot_shares,
)
from services.ma_timing_core import ma_threshold_states, threshold_desired_position
from services.fund_rotation_models import (
    BUY_SLIPPAGE,
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_NEXT_OPEN,
    PORTFOLIO_EMPTY_ACTIVATION_IMMEDIATE,
    PORTFOLIO_EMPTY_ACTIVATIONS,
    PORTFOLIO_INITIAL_ENTRY_FOLLOW_STATE,
    PORTFOLIO_INITIAL_ENTRY_FRESH_BUY,
    PORTFOLIO_INITIAL_ENTRY_POLICIES,
    PORTFOLIO_STRATEGIES,
    PORTFOLIO_STRATEGY_CASH,
    PORTFOLIO_STRATEGY_HALF_TIMING,
    PORTFOLIO_STRATEGY_HOLD,
    PORTFOLIO_STRATEGY_TIMING,
    PortfolioTimingAllocation,
    PortfolioTimingResult,
    RotationInput,
    TimingBacktestResult,
)
from services.fund_rotation_summary import _build_timing_summary


def _build_timing_signal_frame(
    data: pd.DataFrame,
    common_dates: pd.DatetimeIndex,
    *,
    ma_period: int,
    threshold_pct: float,
) -> pd.DataFrame:
    # 信号状态机统一定义见 services.ma_timing_core.ma_threshold_states
    close = pd.to_numeric(data["close"], errors="coerce")
    close.index = pd.DatetimeIndex(pd.to_datetime(data["trade_date"]))
    states = ma_threshold_states(close, ma_period, threshold_pct)
    ma_values = close.rolling(window=ma_period).mean()

    previous_state = 0
    raw_actions: list[str] = []
    for trade_date, state_value in states.items():
        ma_value = ma_values.loc[trade_date]
        if pd.isna(ma_value):
            raw_actions.append("等待均线")
            continue
        state = int(state_value)
        if state != previous_state:
            raw_actions.append("买入" if state == 1 else "卖出")
        else:
            raw_actions.append("持有" if state == 1 else "空仓")
        previous_state = state

    frame = pd.DataFrame(
        {
            "信号仓位": states.astype(int),
            "原始信号": raw_actions,
        },
        index=close.index,
    )
    return frame.reindex(common_dates)


def _delay_signal_frame(
    signal_frame: pd.DataFrame,
    common_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """把信号帧整体延迟一个交易日：t 日的行携带 t-1 日的信号（首个交易日为 NaN）。"""
    delayed = signal_frame.shift(1)
    delayed.index = common_dates
    return delayed


def _align_timing_data(
    timing_result,
    common_dates: pd.DatetimeIndex,
    *,
    execution_mode: str,
    slippage: float,
    open_source: RotationInput | None = None,
) -> pd.DataFrame:
    """把单标的择时结果对齐到共同日期；非默认口径下按延迟执行重估。

    next_close/next_open：信号仓位收益贡献延迟一日（t 日信号赚 t+1→t+2 的收益），
    统一实现为净值收益序列 shift(1)；next_open 额外把换手日的滑点差
    （新口径成交在开盘价 vs 旧口径收盘价）近似为延迟一日换手 × 滑点扣减。
    估值仍用收盘价，现金按新净值与持仓重推。
    """
    timing_data = timing_result.data.set_index("日期").reindex(common_dates)
    if execution_mode == EXECUTION_AFTER_CLOSE:
        return timing_data
    nav = pd.to_numeric(timing_data["账户净值"], errors="coerce")
    benchmark = pd.to_numeric(timing_data["一直持有净值"], errors="coerce")
    strategy_return = nav.pct_change().fillna(0.0)
    delayed_return = strategy_return.shift(1).fillna(0.0)
    if slippage > 0:
        holdings = pd.to_numeric(timing_data["持仓份额"], errors="coerce").ffill().fillna(0.0)
        position = (holdings > 0).astype(float)
        turnover = position.diff().abs().fillna(0.0)
        delayed_turnover = turnover.shift(1).fillna(0.0)
        extra_cost = (delayed_turnover - turnover).clip(lower=0) * slippage
        delayed_return = delayed_return - extra_cost
    delayed_nav = float(nav.iloc[0]) * (1 + delayed_return).cumprod()
    timing_data["账户净值"] = delayed_nav
    timing_data["策略累计收益率(%)"] = (delayed_nav / float(nav.iloc[0]) - 1) * 100
    close_price = pd.to_numeric(timing_data["收盘价"], errors="coerce").ffill().fillna(0)
    timing_data["现金余额"] = timing_data["账户净值"] - pd.to_numeric(
        timing_data["持仓份额"], errors="coerce"
    ).fillna(0) * close_price
    timing_data["一直持有净值"] = benchmark
    return timing_data


def _apply_sleeve_slippage(sleeve_result: dict[str, object], slippage: float) -> dict[str, object]:
    """对延迟口径（next_open）袖套的每笔交易按滑点调整成交价并重估净值。

    买价 ×(1+s)、卖价 ×(1-s)：从交易明细重放每日现金与份额
    （初始现金 = 首日净值 - 首日市值），估值价用最近一次成交价近似
    （成交当日估值价=成交价，非成交日沿用最后一次成交价）。
    滑点仅作用于 next_open 口径的袖套；研究组合口径用。
    """
    trades = sleeve_result.get("trades")
    nav = sleeve_result["nav"].astype(float)
    market = sleeve_result["market"].astype(float)
    if slippage <= 0 or trades is None or trades.empty:
        return sleeve_result

    initial_cash = float(nav.iloc[0]) - float(market.iloc[0])
    trades_by_date: dict[pd.Timestamp, list[dict[str, object]]] = {}
    for _, row in trades.iterrows():
        trades_by_date.setdefault(pd.Timestamp(row["日期"]), []).append(row)

    cash = initial_cash
    primary_shares = 0.0
    primary_cost = 0.0
    fallback_shares = 0.0
    fallback_cost = 0.0
    total_cost = 0.0
    realized_pnls: list[float] = []
    nav_values: list[float] = []
    cash_values: list[float] = []
    market_values: list[float] = []

    last_primary_price = float("nan")
    last_fallback_price = float("nan")
    for trade_date in nav.index:
        for row in trades_by_date.get(pd.Timestamp(trade_date), []):
            price = float(row["成交价"])
            shares = float(row["份额"])
            reason = str(row.get("原因", ""))
            is_fallback = "承接标的" in reason
            fee_rate = (
                float(row["手续费"]) / float(row["成交金额"]) if float(row["成交金额"]) else 0.0
            )
            if row["操作"] == "买入":
                fill_price = price * (1 + slippage)
                gross = shares * fill_price
                fee = gross * fee_rate
                cash -= gross + fee
                total_cost += fee
                if is_fallback:
                    fallback_shares += shares
                    fallback_cost += gross + fee
                    last_fallback_price = fill_price
                else:
                    if "长期半仓" in reason:
                        primary_cost += 0.0  # 长期半仓与择时半仓共用主标的均价跟踪
                    primary_cost += gross + fee
                    primary_shares += shares
                    last_primary_price = fill_price
            else:
                fill_price = price * (1 - slippage)
                gross = shares * fill_price
                fee = gross * fee_rate
                net = gross - fee
                if is_fallback:
                    realized = net - fallback_cost * (shares / fallback_shares) if fallback_shares > 0 else net
                    fallback_shares = max(0.0, fallback_shares - shares)
                    if fallback_shares <= 0:
                        fallback_cost = 0.0
                    else:
                        fallback_cost *= (fallback_shares) / (fallback_shares + shares)
                    last_fallback_price = fill_price
                else:
                    avg_cost = primary_cost / primary_shares if primary_shares > 0 else 0.0
                    realized = net - avg_cost * shares
                    primary_shares = max(0.0, primary_shares - shares)
                    primary_cost = avg_cost * primary_shares
                    last_primary_price = fill_price
                cash += net
                total_cost += fee
                realized_pnls.append(realized)
        market_value = 0.0
        if np.isfinite(last_primary_price) and primary_shares > 0:
            market_value += primary_shares * last_primary_price
        if np.isfinite(last_fallback_price) and fallback_shares > 0:
            market_value += fallback_shares * last_fallback_price
        market_values.append(market_value)
        nav_values.append(cash + market_value)
        cash_values.append(cash)

    sleeve_result["nav"] = pd.Series(nav_values, index=nav.index)
    sleeve_result["cash"] = pd.Series(cash_values, index=nav.index)
    sleeve_result["market"] = pd.Series(market_values, index=nav.index)
    sleeve_result["total_cost"] = total_cost
    sleeve_result["realized_trade_pnls"] = realized_pnls
    return sleeve_result


def _run_delayed_timing_sleeve(
    *,
    item: PortfolioTimingAllocation,
    capital: float,
    primary_prices: pd.Series,
    fallback_prices: pd.Series | None,
    signal_frame: pd.DataFrame,
    transaction_cost: float,
    lot_size: int,
) -> dict[str, object]:
    is_half_timing = item.strategy == PORTFOLIO_STRATEGY_HALF_TIMING
    fresh_entry = item.initial_entry_policy == PORTFOLIO_INITIAL_ENTRY_FRESH_BUY
    cash = float(capital)
    primary_shares = 0.0
    primary_cost_basis = 0.0
    fallback_shares = 0.0
    fallback_cost_basis = 0.0
    long_shares = 0.0
    has_primary_entry = False
    initial_wait_completed = False
    activated = False
    total_cost = 0.0
    rows: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []

    def record_buy(
        *,
        trade_date: pd.Timestamp,
        symbol: str,
        name: str,
        price: float,
        available_cash: float,
        reason: str,
    ) -> tuple[float, float, float]:
        nonlocal cash, total_cost
        affordable = available_cash / (price * (1 + transaction_cost)) if price > 0 else 0.0
        shares = _round_lot_shares(affordable, lot_size=lot_size)
        gross_value = shares * price
        fee = gross_value * transaction_cost
        if shares <= 0:
            return 0.0, 0.0, 0.0
        cash -= gross_value + fee
        total_cost += fee
        cost_basis = gross_value + fee
        trades.append(
            {
                "日期": trade_date,
                "交易标的": symbol,
                "交易标的名称": name,
                "操作": "买入",
                "成交价": round(price, 4),
                "份额": round(shares, 2),
                "成交金额": round(gross_value, 2),
                "手续费": round(fee, 2),
                "本次交易盈亏金额": None,
                "本次交易盈亏率(%)": None,
                "现金余额": round(cash, 2),
                "原因": reason,
            }
        )
        return shares, cost_basis, fee

    def record_sell(
        *,
        trade_date: pd.Timestamp,
        symbol: str,
        name: str,
        price: float,
        shares: float,
        cost_basis: float,
        reason: str,
    ) -> None:
        nonlocal cash, total_cost
        gross_value = shares * price
        fee = gross_value * transaction_cost
        net_value = gross_value - fee
        realized_pnl = net_value - cost_basis
        realized_return = realized_pnl / cost_basis * 100 if cost_basis > 0 else 0.0
        cash += net_value
        total_cost += fee
        trades.append(
            {
                "日期": trade_date,
                "交易标的": symbol,
                "交易标的名称": name,
                "操作": "卖出",
                "成交价": round(price, 4),
                "份额": round(shares, 2),
                "成交金额": round(gross_value, 2),
                "手续费": round(fee, 2),
                "本次交易盈亏金额": round(realized_pnl, 2),
                "本次交易盈亏率(%)": round(realized_return, 2),
                "现金余额": round(cash, 2),
                "原因": reason,
            }
        )

    for row_index, trade_date in enumerate(primary_prices.index):
        trade_date = pd.Timestamp(trade_date)
        primary_price = float(primary_prices.loc[trade_date])
        fallback_price = (
            float(fallback_prices.loc[trade_date])
            if fallback_prices is not None
            else np.nan
        )
        raw_action = str(signal_frame.loc[trade_date, "原始信号"])
        signal_position = int(signal_frame.loc[trade_date, "信号仓位"])

        if not activated:
            if row_index == 0:
                if fresh_entry:
                    initial_wait_completed = raw_action in {"空仓", "等待均线"}
                    should_enter = raw_action == "买入"
                else:
                    initial_wait_completed = signal_position == 0
                    should_enter = signal_position == 1
            else:
                if signal_position == 0:
                    initial_wait_completed = True
                should_enter = raw_action == "买入" and initial_wait_completed

            if should_enter:
                if fallback_shares > 0:
                    record_sell(
                        trade_date=trade_date,
                        symbol=item.empty_position_symbol,
                        name=item.empty_position_symbol,
                        price=fallback_price,
                        shares=fallback_shares,
                        cost_basis=fallback_cost_basis,
                        reason=f"{item.symbol} 首次买入，退出空仓承接标的",
                    )
                    fallback_shares = 0.0
                    fallback_cost_basis = 0.0
                if is_half_timing:
                    long_budget = capital / 2
                    timing_budget = capital - long_budget
                    long_shares, _, _ = record_buy(
                        trade_date=trade_date,
                        symbol=item.symbol,
                        name=item.name or item.symbol,
                        price=primary_price,
                        available_cash=long_budget,
                        reason="首次有效买入，建立长期半仓",
                    )
                    primary_shares, primary_cost_basis, _ = record_buy(
                        trade_date=trade_date,
                        symbol=item.symbol,
                        name=item.name or item.symbol,
                        price=primary_price,
                        available_cash=timing_budget,
                        reason="首次有效买入，建立择时半仓",
                    )
                    activated = long_shares > 0 or primary_shares > 0
                else:
                    primary_shares, primary_cost_basis, _ = record_buy(
                        trade_date=trade_date,
                        symbol=item.symbol,
                        name=item.name or item.symbol,
                        price=primary_price,
                        available_cash=cash,
                        reason="首次有效买入，建立来源ETF仓位",
                    )
                    activated = primary_shares > 0
                has_primary_entry = activated
            elif (
                signal_position == 0
                and fallback_prices is not None
                and item.empty_position_activation == PORTFOLIO_EMPTY_ACTIVATION_IMMEDIATE
                and fallback_shares <= 0
            ):
                fallback_shares, fallback_cost_basis, _ = record_buy(
                    trade_date=trade_date,
                    symbol=item.empty_position_symbol,
                    name=item.empty_position_symbol,
                    price=fallback_price,
                    available_cash=cash,
                    reason=f"{item.symbol} 空仓，转入承接标的",
                )
        else:
            if signal_position == 0 and primary_shares > 0:
                record_sell(
                    trade_date=trade_date,
                    symbol=item.symbol,
                    name=item.name or item.symbol,
                    price=primary_price,
                    shares=primary_shares,
                    cost_basis=primary_cost_basis,
                    reason="收盘信号转为空仓",
                )
                primary_shares = 0.0
                primary_cost_basis = 0.0
                fallback_enabled = (
                    fallback_prices is not None
                    and (
                        item.empty_position_activation == PORTFOLIO_EMPTY_ACTIVATION_IMMEDIATE
                        or has_primary_entry
                    )
                )
                if fallback_enabled and not is_half_timing:
                    fallback_shares, fallback_cost_basis, _ = record_buy(
                        trade_date=trade_date,
                        symbol=item.empty_position_symbol,
                        name=item.empty_position_symbol,
                        price=fallback_price,
                        available_cash=cash,
                        reason=f"{item.symbol} 卖出后转入空仓承接标的",
                    )
            elif signal_position == 1 and primary_shares <= 0:
                if fallback_shares > 0:
                    record_sell(
                        trade_date=trade_date,
                        symbol=item.empty_position_symbol,
                        name=item.empty_position_symbol,
                        price=fallback_price,
                        shares=fallback_shares,
                        cost_basis=fallback_cost_basis,
                        reason=f"{item.symbol} 重新买入，退出空仓承接标的",
                    )
                    fallback_shares = 0.0
                    fallback_cost_basis = 0.0
                timing_budget = cash if not is_half_timing else min(cash, capital / 2)
                primary_shares, primary_cost_basis, _ = record_buy(
                    trade_date=trade_date,
                    symbol=item.symbol,
                    name=item.name or item.symbol,
                    price=primary_price,
                    available_cash=timing_budget,
                    reason="收盘信号重新买入",
                )
                has_primary_entry = has_primary_entry or primary_shares > 0

        primary_value = (primary_shares + long_shares) * primary_price
        fallback_value = fallback_shares * fallback_price if fallback_shares > 0 else 0.0
        market_value = primary_value + fallback_value
        rows.append(
            {
                "日期": trade_date,
                "账户净值": cash + market_value,
                "现金余额": cash,
                "持仓市值": market_value,
                "来源ETF份额": primary_shares + long_shares,
                "承接ETF份额": fallback_shares,
            }
        )

    result = pd.DataFrame(rows).set_index("日期")
    if fallback_shares > 0:
        latest_signal = "空仓承接"
        current_weight = float(item.weight_pct)
    elif not activated:
        latest_signal = "等待建仓"
        current_weight = 0.0
    elif is_half_timing and primary_shares <= 0:
        latest_signal = "半仓"
        current_weight = float(item.weight_pct) / 2
    elif is_half_timing:
        latest_signal = "满仓"
        current_weight = float(item.weight_pct)
    elif primary_shares > 0:
        latest_signal = "持仓"
        current_weight = float(item.weight_pct)
    else:
        latest_signal = "现金"
        current_weight = 0.0
    return {
        "nav": result["账户净值"],
        "cash": result["现金余额"],
        "market": result["持仓市值"],
        "trades": pd.DataFrame(trades),
        "total_cost": total_cost,
        "latest_signal": latest_signal,
        "current_weight": current_weight,
    }

def run_ma20_timing_backtest(
    fund: RotationInput,
    ma_period: int = 20,
    threshold_pct: float = 0.0,
    initial_capital: float = 100000.0,
    transaction_cost: float = 0.00006,
    lot_size: int = 100,
    start_date: str | pd.Timestamp | None = None,
    end_date: str | pd.Timestamp | None = None,
) -> TimingBacktestResult:
    if ma_period < 1:
        raise ValueError("均线周期必须大于 0。")
    if threshold_pct < 0:
        raise ValueError("触发阈值不能为负数。")
    if initial_capital <= 0:
        raise ValueError("初始资金必须大于 0。")

    data = fund.dataframe[["trade_date", "close"]].copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce")
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data = data.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    if len(data) < ma_period:
        raise ValueError("数据长度不足，无法计算 MA20 策略。")

    ma_col = f"MA{ma_period}"
    # 触发线与阈值决策统一定义见 services.ma_timing_core（账户自愈语义）
    close_indexed = pd.Series(
        pd.to_numeric(data["close"], errors="coerce").to_numpy(),
        index=pd.DatetimeIndex(pd.to_datetime(data["trade_date"])),
    )
    threshold = float(threshold_pct) / 100
    ma_series = close_indexed.rolling(window=ma_period).mean()
    buy_line_series = ma_series * (1 + threshold)
    sell_line_series = ma_series * (1 - threshold)

    requested_start, requested_end = _normalize_date_range(start_date, end_date)
    if requested_end is not None:
        data = data[data["trade_date"] <= requested_end]
    if requested_start is not None:
        data = data[data["trade_date"] >= requested_start]
    data = data.reset_index(drop=True)
    if data.empty:
        raise ValueError("所选时间区间内没有可回测的数据。")
    if not ma_series.reindex(pd.DatetimeIndex(pd.to_datetime(data["trade_date"]))).notna().any():
        raise ValueError("所选时间区间内均线尚未形成，请扩大区间或缩短均线周期。")

    cash = float(initial_capital)
    shares = 0.0
    position_cost_basis = 0.0
    total_buy_cost = 0.0
    total_sell_cost = 0.0
    realized_trade_pnls: list[float] = []
    rows: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    benchmark_first_close = float(data["close"].iloc[0])

    for _, row in data.iterrows():
        trade_date = pd.Timestamp(row["trade_date"])
        close_price = float(row["close"])
        ma_raw = ma_series.get(trade_date, np.nan)
        if pd.isna(ma_raw):
            ma_value = np.nan
            buy_line = np.nan
            sell_line = np.nan
            desired_position = int(shares > 0)
            signal = "等待均线"
            action = "等待"
        else:
            ma_value = float(ma_raw)
            buy_line = float(buy_line_series.get(trade_date, np.nan))
            sell_line = float(sell_line_series.get(trade_date, np.nan))
            desired_position = threshold_desired_position(
                close_price, buy_line, sell_line, int(shares > 0)
            )
            signal = "持仓" if desired_position == 1 else "空仓"
            action = "持有"

        if desired_position == 1 and shares <= 0:
            affordable_shares = cash / (close_price * (1 + transaction_cost)) if close_price > 0 else 0.0
            buy_shares = _round_lot_shares(affordable_shares, lot_size=lot_size)
            if buy_shares > 0:
                gross_value = buy_shares * close_price
                cost = gross_value * transaction_cost
                cash -= gross_value + cost
                shares = buy_shares
                position_cost_basis = gross_value + cost
                total_buy_cost += cost
                action = "买入"
                trades.append(
                    {
                        "日期": trade_date,
                        "操作": "买入",
                        "成交价": round(close_price, 4),
                        "份额": round(buy_shares, 2),
                        "成交金额": round(gross_value, 2),
                        "手续费": round(cost, 2),
                        "本次交易盈亏金额": None,
                        "本次交易盈亏率(%)": None,
                        "现金余额": round(cash, 2),
                        "原因": f"收盘价 {close_price:.4f} > 买入线 {buy_line:.4f}",
                    }
                )
        elif desired_position == 0 and shares > 0:
            gross_value = shares * close_price
            cost = gross_value * transaction_cost
            net_value = gross_value - cost
            realized_pnl = net_value - position_cost_basis
            realized_return = realized_pnl / position_cost_basis * 100 if position_cost_basis > 0 else 0.0
            cash += net_value
            total_sell_cost += cost
            realized_trade_pnls.append(realized_pnl)
            action = "卖出"
            trades.append(
                {
                    "日期": trade_date,
                    "操作": "卖出",
                    "成交价": round(close_price, 4),
                    "份额": round(shares, 2),
                    "成交金额": round(gross_value, 2),
                    "手续费": round(cost, 2),
                    "本次交易盈亏金额": round(realized_pnl, 2),
                    "本次交易盈亏率(%)": round(realized_return, 2),
                    "现金余额": round(cash, 2),
                    "原因": f"收盘价 {close_price:.4f} < 卖出线 {sell_line:.4f}",
                }
            )
            shares = 0.0
            position_cost_basis = 0.0

        account_value = cash + shares * close_price
        benchmark_value = close_price / benchmark_first_close * initial_capital if benchmark_first_close > 0 else initial_capital
        rows.append(
            {
                "日期": trade_date,
                "收盘价": round(close_price, 4),
                ma_col: round(ma_value, 4),
                "买入线": round(buy_line, 4),
                "卖出线": round(sell_line, 4),
                "信号": signal,
                "操作": action,
                "持仓份额": round(shares, 2),
                "现金余额": round(cash, 2),
                "账户净值": round(account_value, 2),
                "策略累计收益率(%)": round((account_value / initial_capital - 1) * 100, 2),
                "一直持有净值": round(benchmark_value, 2),
                "一直持有收益率(%)": round((benchmark_value / initial_capital - 1) * 100, 2),
            }
        )

    result_df = pd.DataFrame(rows)
    trades_df = pd.DataFrame(trades)
    drawdown_df = _calculate_drawdown(result_df, initial_capital=initial_capital)
    yearly_stats = _calculate_yearly_stats(result_df, initial_capital=initial_capital)
    summary = _build_timing_summary(
        result_df=result_df,
        trades_df=trades_df,
        drawdown_df=drawdown_df,
        fund=fund,
        ma_period=ma_period,
        threshold_pct=threshold_pct,
        initial_capital=initial_capital,
        total_buy_cost=total_buy_cost,
        total_sell_cost=total_sell_cost,
        realized_trade_pnls=realized_trade_pnls,
    )
    return TimingBacktestResult(
        start_date=pd.Timestamp(result_df["日期"].iloc[0]),
        end_date=pd.Timestamp(result_df["日期"].iloc[-1]),
        data=result_df,
        trades=trades_df,
        drawdown=drawdown_df,
        yearly_stats=yearly_stats,
        summary=summary,
    )


def run_portfolio_timing_backtest(
    funds: list[RotationInput],
    allocations: list[PortfolioTimingAllocation],
    initial_capital: float = 100000.0,
    transaction_cost: float = 0.00006,
    lot_size: int = 100,
    start_date: str | pd.Timestamp | None = None,
    end_date: str | pd.Timestamp | None = None,
    *,
    execution_mode: str = EXECUTION_AFTER_CLOSE,
    slippage: float = 0.0,
    _timing_runner=None,
) -> PortfolioTimingResult:
    """组合择时回测。

    执行口径（audit P1 扩展，默认保持既有行为）：
    - ``after_close``：t 日收盘信号、t 日收盘价成交（盘后固定价机制假设）；
    - ``next_open``：t 日收盘信号、t+1 日开盘价成交（可选滑点）；
    - ``next_close``：t 日收盘信号、t+1 日收盘价成交。

    非默认口径下，袖套信号延迟一日执行：延迟期间的净值按原持仓估值，
    到执行日才发生交易；半仓长期腿同样延迟建仓。
    """
    timing_runner = _timing_runner or run_ma20_timing_backtest
    if execution_mode not in (EXECUTION_AFTER_CLOSE, EXECUTION_NEXT_OPEN, EXECUTION_NEXT_CLOSE):
        raise ValueError(f"不支持的执行口径：{execution_mode}")
    if slippage < 0:
        raise ValueError("滑点不能为负数。")
    if execution_mode == EXECUTION_NEXT_OPEN and slippage == 0:
        # 次日开盘成交默认计入项目口径的双边 0.05% 滑点
        slippage = BUY_SLIPPAGE
    if initial_capital <= 0:
        raise ValueError("初始资金必须大于 0。")
    if transaction_cost < 0:
        raise ValueError("交易成本不能为负数。")
    if lot_size < 1:
        raise ValueError("交易单位必须大于 0。")
    if not allocations:
        raise ValueError("至少需要配置一个标的或现金仓位。")

    total_weight = sum(float(item.weight_pct) for item in allocations)
    if any(float(item.weight_pct) < 0 for item in allocations):
        raise ValueError("配置比例不能为负数。")
    if total_weight > 100.0 and not np.isclose(total_weight, 100.0, atol=1e-6):
        raise ValueError(f"配置比例合计不能超过 100%，当前为 {total_weight:.2f}%。")
    allocations = [item for item in allocations if float(item.weight_pct) > 0]
    remaining_cash = max(0.0, 100.0 - total_weight)
    if remaining_cash > 1e-6:
        cash_index = next(
            (
                index
                for index, item in enumerate(allocations)
                if item.strategy == PORTFOLIO_STRATEGY_CASH
            ),
            None,
        )
        if cash_index is None:
            allocations.append(
                PortfolioTimingAllocation(
                    symbol="",
                    name="剩余现金",
                    weight_pct=remaining_cash,
                    strategy=PORTFOLIO_STRATEGY_CASH,
                )
            )
        else:
            cash_item = allocations[cash_index]
            allocations[cash_index] = PortfolioTimingAllocation(
                symbol=cash_item.symbol,
                name=cash_item.name,
                weight_pct=float(cash_item.weight_pct) + remaining_cash,
                strategy=cash_item.strategy,
                ma_period=cash_item.ma_period,
                threshold_pct=cash_item.threshold_pct,
                initial_entry_policy=cash_item.initial_entry_policy,
                empty_position_symbol=cash_item.empty_position_symbol,
                empty_position_activation=cash_item.empty_position_activation,
            )
    if any(item.strategy not in PORTFOLIO_STRATEGIES for item in allocations):
        raise ValueError("存在不支持的组合策略类型。")
    if any(item.initial_entry_policy not in PORTFOLIO_INITIAL_ENTRY_POLICIES for item in allocations):
        raise ValueError("存在不支持的初始建仓规则。")
    if any(item.empty_position_activation not in PORTFOLIO_EMPTY_ACTIVATIONS for item in allocations):
        raise ValueError("存在不支持的空仓承接启用规则。")
    if any(
        item.empty_position_symbol
        and item.strategy not in (PORTFOLIO_STRATEGY_TIMING, PORTFOLIO_STRATEGY_HALF_TIMING)
        for item in allocations
    ):
        raise ValueError("空仓承接标的只能用于均线择时策略。")
    if any(item.empty_position_symbol == item.symbol for item in allocations if item.symbol):
        raise ValueError("空仓承接标的不能与来源标的相同。")

    fund_by_symbol = {fund.symbol: fund for fund in funds}
    primary_symbols = [
        item.symbol
        for item in allocations
        if item.strategy != PORTFOLIO_STRATEGY_CASH
    ]
    if len(primary_symbols) != len(set(primary_symbols)):
        raise ValueError("同一标的只能配置一次。")
    required_symbols = list(
        dict.fromkeys(
            primary_symbols
            + [item.empty_position_symbol for item in allocations if item.empty_position_symbol]
        )
    )
    missing_symbols = [symbol for symbol in required_symbols if symbol not in fund_by_symbol]
    if missing_symbols:
        raise ValueError(f"缺少以下标的数据：{'、'.join(missing_symbols)}")

    requested_start, requested_end = _normalize_date_range(start_date, end_date)
    source_data: dict[str, pd.DataFrame] = {}
    for symbol in required_symbols:
        data = fund_by_symbol[symbol].dataframe[["trade_date", "close"]].copy()
        data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce")
        data["close"] = pd.to_numeric(data["close"], errors="coerce")
        data = data.dropna(subset=["trade_date", "close"])
        data = data.sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)
        if data.empty:
            raise ValueError(f"{symbol} 没有可回测的数据。")
        source_data[symbol] = data

    if source_data:
        actual_start = max(data["trade_date"].min() for data in source_data.values())
        actual_end = min(data["trade_date"].max() for data in source_data.values())
        if requested_start is not None:
            actual_start = max(actual_start, requested_start)
        if requested_end is not None:
            actual_end = min(actual_end, requested_end)
        if actual_start > actual_end:
            raise ValueError("所选区间内没有所有标的共同可用的数据。")

        common_dates: pd.DatetimeIndex | None = None
        for data in source_data.values():
            dates = pd.DatetimeIndex(
                data.loc[
                    (data["trade_date"] >= actual_start) & (data["trade_date"] <= actual_end),
                    "trade_date",
                ]
            )
            common_dates = dates if common_dates is None else common_dates.intersection(dates)
        if common_dates is None or len(common_dates) < 2:
            raise ValueError("共同交易日不足，无法执行组合回测。")
        common_dates = common_dates.sort_values()
        actual_start = pd.Timestamp(common_dates[0])
        actual_end = pd.Timestamp(common_dates[-1])
    else:
        actual_start = requested_start or pd.Timestamp.today().normalize()
        actual_end = requested_end or actual_start
        if actual_start > actual_end:
            raise ValueError("开始日期不能晚于结束日期。")
        common_dates = pd.DatetimeIndex([actual_start, actual_end]).unique().sort_values()

    def buy_and_hold_nav(capital: float, prices: pd.Series) -> tuple[pd.Series, float, pd.Series]:
        first_price = float(prices.iloc[0])
        affordable = capital / (first_price * (1 + transaction_cost))
        shares = _round_lot_shares(affordable, lot_size=lot_size)
        gross_value = shares * first_price
        fee = gross_value * transaction_cost
        cash = capital - gross_value - fee
        return cash + shares * prices, fee, pd.Series(cash, index=prices.index, dtype=float)

    strategy_parts: list[pd.Series] = []
    cash_parts: list[pd.Series] = []
    market_parts: list[pd.Series] = []
    benchmark_parts: list[pd.Series] = []
    trade_frames: list[pd.DataFrame] = []
    component_rows: list[dict[str, object]] = []
    total_cost = 0.0

    for item in allocations:
        capital = initial_capital * float(item.weight_pct) / 100
        if item.strategy == PORTFOLIO_STRATEGY_CASH:
            strategy_nav = pd.Series(capital, index=common_dates, dtype=float)
            strategy_cash = strategy_nav.copy()
            strategy_market = pd.Series(0.0, index=common_dates, dtype=float)
            benchmark_nav = strategy_nav.copy()
            current_weight = float(item.weight_pct)
            latest_signal = "现金"
        else:
            prices = source_data[item.symbol].set_index("trade_date")["close"].reindex(common_dates)
            benchmark_nav = prices / float(prices.iloc[0]) * capital
            timing_result = None
            if item.strategy == PORTFOLIO_STRATEGY_HOLD:
                strategy_nav, hold_fee, strategy_cash = buy_and_hold_nav(capital, prices)
                strategy_market = strategy_nav - strategy_cash
                total_cost += hold_fee
                current_weight = float(item.weight_pct)
                latest_signal = "一直持有"
            elif (
                item.initial_entry_policy != PORTFOLIO_INITIAL_ENTRY_FOLLOW_STATE
                or bool(item.empty_position_symbol)
            ):
                signal_frame = _build_timing_signal_frame(
                    source_data[item.symbol],
                    common_dates,
                    ma_period=int(item.ma_period),
                    threshold_pct=float(item.threshold_pct),
                )
                fallback_prices = (
                    source_data[item.empty_position_symbol]
                    .set_index("trade_date")["close"]
                    .reindex(common_dates)
                    if item.empty_position_symbol
                    else None
                )
                if execution_mode != EXECUTION_AFTER_CLOSE:
                    # 非默认口径：袖套信号整体延迟一日执行，成交价切换到次日开盘/收盘
                    signal_frame = _delay_signal_frame(signal_frame, common_dates)
                    if execution_mode == EXECUTION_NEXT_OPEN:
                        prices = _open_prices_for(fund_by_symbol[item.symbol], common_dates)
                        if fallback_prices is not None:
                            fallback_prices = _open_prices_for(
                                fund_by_symbol[item.empty_position_symbol], common_dates
                            )
                    sleeve_result = _run_delayed_timing_sleeve(
                        item=item,
                        capital=capital,
                        primary_prices=prices,
                        fallback_prices=fallback_prices,
                        signal_frame=signal_frame,
                        transaction_cost=transaction_cost,
                        lot_size=lot_size,
                    )
                    if execution_mode == EXECUTION_NEXT_OPEN and slippage > 0:
                        sleeve_result = _apply_sleeve_slippage(sleeve_result, slippage)
                else:
                    sleeve_result = _run_delayed_timing_sleeve(
                        item=item,
                        capital=capital,
                        primary_prices=prices,
                        fallback_prices=fallback_prices,
                        signal_frame=signal_frame,
                        transaction_cost=transaction_cost,
                        lot_size=lot_size,
                    )
                strategy_nav = sleeve_result["nav"]
                strategy_cash = sleeve_result["cash"]
                strategy_market = sleeve_result["market"]
                total_cost += float(sleeve_result["total_cost"])
                current_weight = float(sleeve_result["current_weight"])
                latest_signal = str(sleeve_result["latest_signal"])
                sleeve_trades = sleeve_result["trades"]
                if not sleeve_trades.empty:
                    trades = sleeve_trades.copy()
                    trades.insert(0, "标的名称", item.name or item.symbol)
                    trades.insert(1, "代码", item.symbol)
                    trades.insert(2, "配置比例(%)", float(item.weight_pct))
                    trade_frames.append(trades)
            elif item.strategy == PORTFOLIO_STRATEGY_TIMING:
                timing_result = timing_runner(
                    fund=fund_by_symbol[item.symbol],
                    ma_period=int(item.ma_period),
                    threshold_pct=float(item.threshold_pct),
                    initial_capital=capital,
                    transaction_cost=transaction_cost,
                    lot_size=lot_size,
                    start_date=actual_start,
                    end_date=actual_end,
                )
                timing_data = _align_timing_data(
                    timing_result,
                    common_dates,
                    execution_mode=execution_mode,
                    slippage=slippage,
                    open_source=fund_by_symbol[item.symbol],
                )
                strategy_nav = timing_data["账户净值"]
                strategy_cash = timing_data["现金余额"]
                strategy_market = strategy_nav - strategy_cash
                total_cost += float(timing_result.summary.get("累计总成本", 0))
                timing_held = float(timing_result.data.iloc[-1]["持仓份额"]) > 0
                current_weight = float(item.weight_pct) if timing_held else 0.0
                latest_signal = "持仓" if timing_held else "空仓"
            else:
                hold_capital = capital / 2
                timing_capital = capital - hold_capital
                hold_nav, hold_fee, hold_cash = buy_and_hold_nav(hold_capital, prices)
                timing_result = timing_runner(
                    fund=fund_by_symbol[item.symbol],
                    ma_period=int(item.ma_period),
                    threshold_pct=float(item.threshold_pct),
                    initial_capital=timing_capital,
                    transaction_cost=transaction_cost,
                    lot_size=lot_size,
                    start_date=actual_start,
                    end_date=actual_end,
                )
                timing_data = _align_timing_data(
                    timing_result,
                    common_dates,
                    execution_mode=execution_mode,
                    slippage=slippage,
                    open_source=fund_by_symbol[item.symbol],
                )
                timing_nav = timing_data["账户净值"]
                strategy_nav = hold_nav + timing_nav
                strategy_cash = hold_cash + timing_data["现金余额"]
                strategy_market = strategy_nav - strategy_cash
                total_cost += hold_fee + float(timing_result.summary.get("累计总成本", 0))
                timing_held = float(timing_result.data.iloc[-1]["持仓份额"]) > 0
                current_weight = float(item.weight_pct) if timing_held else float(item.weight_pct) / 2
                latest_signal = "满仓" if timing_held else "半仓"

            if timing_result is not None and not timing_result.trades.empty:
                trades = timing_result.trades.copy()
                trades.insert(0, "标的名称", item.name or item.symbol)
                trades.insert(1, "代码", item.symbol)
                trades.insert(2, "配置比例(%)", float(item.weight_pct))
                trade_frames.append(trades)

        strategy_parts.append(strategy_nav.rename(item.symbol or "现金"))
        cash_parts.append(strategy_cash.rename(item.symbol or "现金"))
        market_parts.append(strategy_market.rename(item.symbol or "现金"))
        benchmark_parts.append(benchmark_nav.rename(item.symbol or "现金"))
        component_rows.append(
            {
                "标的名称": item.name or item.symbol or "现金",
                "代码": item.symbol,
                "配置比例(%)": float(item.weight_pct),
                "策略类型": item.strategy,
                "均线周期": int(item.ma_period) if item.strategy in (PORTFOLIO_STRATEGY_TIMING, PORTFOLIO_STRATEGY_HALF_TIMING) else pd.NA,
                "触发阈值(%)": float(item.threshold_pct) if item.strategy in (PORTFOLIO_STRATEGY_TIMING, PORTFOLIO_STRATEGY_HALF_TIMING) else pd.NA,
                "最新状态": latest_signal,
                "当前理论仓位(%)": current_weight,
            }
        )

    strategy_frame = pd.concat(strategy_parts, axis=1)
    cash_frame = pd.concat(cash_parts, axis=1)
    market_frame = pd.concat(market_parts, axis=1)
    benchmark_frame = pd.concat(benchmark_parts, axis=1)
    account_values = strategy_frame.sum(axis=1)
    benchmark_values = benchmark_frame.sum(axis=1)
    nav_data = pd.DataFrame(
        {
            "日期": common_dates,
            "账户净值": account_values.values,
            "策略持仓市值": market_frame.sum(axis=1).values,
            "策略现金": cash_frame.sum(axis=1).values,
            "策略累计收益率(%)": (account_values.values / initial_capital - 1) * 100,
            "一直持有净值": benchmark_values.values,
            "一直持有收益率(%)": (benchmark_values.values / initial_capital - 1) * 100,
        }
    )
    for column in strategy_frame.columns:
        nav_data[f"策略持仓：{column}"] = strategy_frame[column].values
    numeric_columns = nav_data.select_dtypes(include=[np.number]).columns
    nav_data[numeric_columns] = nav_data[numeric_columns].round(2)

    trades_df = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    if not trades_df.empty:
        trades_df = trades_df.sort_values(["日期", "代码"]).reset_index(drop=True)
    drawdown_df = _calculate_drawdown(nav_data, initial_capital=initial_capital)
    yearly_stats = _calculate_yearly_stats(nav_data, initial_capital=initial_capital)

    final_value = float(nav_data["账户净值"].iloc[-1])
    benchmark_final = float(nav_data["一直持有净值"].iloc[-1])
    total_return = final_value / initial_capital - 1
    benchmark_return = benchmark_final / initial_capital - 1
    days = (actual_end - actual_start).days
    annual_return = annualized_return(total_return, days)
    benchmark_annual_return = annualized_return(benchmark_return, days)
    daily_returns = _calculate_nav_returns(nav_data, initial_capital)
    annual_vol = annual_volatility(daily_returns)
    sharpe = _calculate_sharpe_ratio(daily_returns)
    benchmark_seeded = pd.concat(
        [pd.Series([initial_capital]), nav_data["一直持有净值"].reset_index(drop=True)],
        ignore_index=True,
    )
    benchmark_drawdown = benchmark_seeded / benchmark_seeded.cummax() - 1
    sell_trades = trades_df[trades_df["操作"] == "卖出"] if not trades_df.empty else pd.DataFrame()
    winning_trades = (
        pd.to_numeric(sell_trades["本次交易盈亏金额"], errors="coerce").gt(0).sum()
        if not sell_trades.empty
        else 0
    )
    closed_count = len(sell_trades)
    summary = {
        "开始日期": actual_start.strftime("%Y-%m-%d"),
        "结束日期": actual_end.strftime("%Y-%m-%d"),
        "期末资金": round(final_value, 2),
        "总收益率(%)": round(total_return * 100, 2),
        "年化收益率(%)": round(annual_return * 100, 2),
        "策略最大回撤(%)": round(float(drawdown_df["回撤(%)"].min()), 2),
        "年化波动率(%)": round(annual_vol * 100, 2),
        "夏普比率": round(sharpe, 2),
        "一直持有期末资金": round(benchmark_final, 2),
        "一直持有收益率(%)": round(benchmark_return * 100, 2),
        "一直持有年化(%)": round(benchmark_annual_return * 100, 2),
        "一直持有最大回撤(%)": round(float(benchmark_drawdown.min() * 100), 2),
        "年化超额收益(百分点)": round((annual_return - benchmark_annual_return) * 100, 2),
        "交易次数": len(trades_df),
        "已平仓交易次数": closed_count,
        "盈利交易次数": int(winning_trades),
        "交易胜率(%)": round(int(winning_trades) / closed_count * 100, 2) if closed_count else 0.0,
        "累计总成本": round(total_cost, 2),
        "当前ETF仓位(%)": round(
            sum(row["当前理论仓位(%)"] for row in component_rows if row["代码"]),
            2,
        ),
        "当前现金仓位(%)": round(
            100 - sum(row["当前理论仓位(%)"] for row in component_rows if row["代码"]),
            2,
        ),
    }
    return PortfolioTimingResult(
        start_date=actual_start,
        end_date=actual_end,
        nav_data=nav_data,
        trades=trades_df,
        drawdown=drawdown_df,
        yearly_stats=yearly_stats,
        summary=summary,
        component_results=pd.DataFrame(component_rows),
    )

__all__ = [
    "run_ma20_timing_backtest",
    "run_portfolio_timing_backtest",
]
