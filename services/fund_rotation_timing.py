from __future__ import annotations

import numpy as np
import pandas as pd

from services.fund_rotation_data import _normalize_date_range
from services.fund_rotation_metrics import (
    _calculate_drawdown,
    _calculate_nav_returns,
    _calculate_sharpe_ratio,
    _calculate_yearly_stats,
    _round_lot_shares,
)
from services.fund_rotation_models import (
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
    data[ma_col] = data["close"].rolling(window=ma_period).mean()
    requested_start, requested_end = _normalize_date_range(start_date, end_date)
    if requested_end is not None:
        data = data[data["trade_date"] <= requested_end]
    if requested_start is not None:
        data = data[data["trade_date"] >= requested_start]
    data = data.reset_index(drop=True)
    if data.empty:
        raise ValueError("所选时间区间内没有可回测的数据。")
    if not data[ma_col].notna().any():
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
        ma_raw = pd.to_numeric(row[ma_col], errors="coerce")
        if pd.isna(ma_raw):
            ma_value = np.nan
            buy_line = np.nan
            sell_line = np.nan
            desired_position = int(shares > 0)
            signal = "等待均线"
            action = "等待"
        else:
            ma_value = float(ma_raw)
            threshold = float(threshold_pct) / 100
            buy_line = ma_value * (1 + threshold)
            sell_line = ma_value * (1 - threshold)
            desired_position = (
                1
                if close_price > buy_line
                else 0
                if close_price < sell_line
                else int(shares > 0)
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
    _timing_runner=None,
) -> PortfolioTimingResult:
    timing_runner = _timing_runner or run_ma20_timing_backtest
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
            )
    if any(item.strategy not in PORTFOLIO_STRATEGIES for item in allocations):
        raise ValueError("存在不支持的组合策略类型。")

    fund_by_symbol = {fund.symbol: fund for fund in funds}
    required_symbols = [
        item.symbol
        for item in allocations
        if item.strategy != PORTFOLIO_STRATEGY_CASH
    ]
    if len(required_symbols) != len(set(required_symbols)):
        raise ValueError("同一标的只能配置一次。")
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

    def buy_and_hold_nav(capital: float, prices: pd.Series) -> tuple[pd.Series, float]:
        first_price = float(prices.iloc[0])
        affordable = capital / (first_price * (1 + transaction_cost))
        shares = _round_lot_shares(affordable, lot_size=lot_size)
        gross_value = shares * first_price
        fee = gross_value * transaction_cost
        cash = capital - gross_value - fee
        return cash + shares * prices, fee

    strategy_parts: list[pd.Series] = []
    benchmark_parts: list[pd.Series] = []
    trade_frames: list[pd.DataFrame] = []
    component_rows: list[dict[str, object]] = []
    total_cost = 0.0

    for item in allocations:
        capital = initial_capital * float(item.weight_pct) / 100
        if item.strategy == PORTFOLIO_STRATEGY_CASH:
            strategy_nav = pd.Series(capital, index=common_dates, dtype=float)
            benchmark_nav = strategy_nav.copy()
            current_weight = float(item.weight_pct)
            latest_signal = "现金"
        else:
            prices = source_data[item.symbol].set_index("trade_date")["close"].reindex(common_dates)
            benchmark_nav = prices / float(prices.iloc[0]) * capital
            timing_result = None
            if item.strategy == PORTFOLIO_STRATEGY_HOLD:
                strategy_nav, hold_fee = buy_and_hold_nav(capital, prices)
                total_cost += hold_fee
                current_weight = float(item.weight_pct)
                latest_signal = "一直持有"
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
                strategy_nav = timing_result.data.set_index("日期")["账户净值"].reindex(common_dates)
                total_cost += float(timing_result.summary.get("累计总成本", 0))
                timing_held = float(timing_result.data.iloc[-1]["持仓份额"]) > 0
                current_weight = float(item.weight_pct) if timing_held else 0.0
                latest_signal = "持仓" if timing_held else "空仓"
            else:
                hold_capital = capital / 2
                timing_capital = capital - hold_capital
                hold_nav, hold_fee = buy_and_hold_nav(hold_capital, prices)
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
                timing_nav = timing_result.data.set_index("日期")["账户净值"].reindex(common_dates)
                strategy_nav = hold_nav + timing_nav
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
    benchmark_frame = pd.concat(benchmark_parts, axis=1)
    account_values = strategy_frame.sum(axis=1)
    benchmark_values = benchmark_frame.sum(axis=1)
    nav_data = pd.DataFrame(
        {
            "日期": common_dates,
            "账户净值": account_values.values,
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
    annual_return = (1 + total_return) ** (365 / days) - 1 if days > 0 and total_return > -1 else 0.0
    benchmark_annual_return = (
        (1 + benchmark_return) ** (365 / days) - 1
        if days > 0 and benchmark_return > -1
        else 0.0
    )
    daily_returns = _calculate_nav_returns(nav_data, initial_capital)
    annual_vol = float(daily_returns.std() * np.sqrt(252)) if not daily_returns.empty else 0.0
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
