from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd


DATE_COLUMNS = ("trade_date", "日期", "date", "datetime", "time", "净值日期")
PRICE_COLUMNS = ("close", "收盘价", "收盘", "累计净值", "复权净值", "单位净值", "nav", "price")
OPEN_COLUMNS = ("open", "开盘价", "开盘")
BUY_SLIPPAGE = 0.0005
SELL_SLIPPAGE = 0.0005
LOT_SIZE = 100
STANDARD_BACKTEST_PERIODS = ("近一年", "今年来", "近三年", "近五年", "成立来")
EXECUTION_AFTER_CLOSE = "after_close"
EXECUTION_NEXT_OPEN = "next_open"
EXECUTION_MODES = (EXECUTION_AFTER_CLOSE, EXECUTION_NEXT_OPEN)
PORTFOLIO_STRATEGY_HOLD = "hold"
PORTFOLIO_STRATEGY_TIMING = "timing"
PORTFOLIO_STRATEGY_HALF_TIMING = "half_timing"
PORTFOLIO_STRATEGY_CASH = "cash"
PORTFOLIO_STRATEGIES = (
    PORTFOLIO_STRATEGY_HOLD,
    PORTFOLIO_STRATEGY_TIMING,
    PORTFOLIO_STRATEGY_HALF_TIMING,
    PORTFOLIO_STRATEGY_CASH,
)


@dataclass
class RotationInput:
    symbol: str
    name: str
    dataframe: pd.DataFrame
    trade_lot_size: int = 100
    apply_slippage: bool = True


@dataclass
class RotationResult:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    nav_data: pd.DataFrame
    trades: pd.DataFrame
    summary: dict[str, object]
    individual_results: pd.DataFrame = field(default_factory=pd.DataFrame)
    individual_nav_data: pd.DataFrame = field(default_factory=pd.DataFrame)
    drawdown: pd.DataFrame = field(default_factory=pd.DataFrame)
    yearly_stats: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class TimingBacktestResult:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    data: pd.DataFrame
    trades: pd.DataFrame
    drawdown: pd.DataFrame
    yearly_stats: pd.DataFrame
    summary: dict[str, object]


@dataclass(frozen=True)
class PortfolioTimingAllocation:
    symbol: str
    name: str
    weight_pct: float
    strategy: str
    ma_period: int = 20
    threshold_pct: float = 1.0


@dataclass
class PortfolioTimingResult:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    nav_data: pd.DataFrame
    trades: pd.DataFrame
    drawdown: pd.DataFrame
    yearly_stats: pd.DataFrame
    summary: dict[str, object]
    component_results: pd.DataFrame = field(default_factory=pd.DataFrame)


def normalize_rotation_dataframe(df: pd.DataFrame, fallback_name: str) -> RotationInput:
    if df is None or df.empty:
        raise ValueError("文件中没有可回测的数据。")

    data = df.copy()
    data.columns = [str(col).strip().lstrip("\ufeff") for col in data.columns]
    date_col = _find_column(data.columns, DATE_COLUMNS)
    price_col = _find_column(data.columns, PRICE_COLUMNS)
    open_col = _find_column(data.columns, OPEN_COLUMNS)
    if not date_col or not price_col:
        raise ValueError(f"无法识别日期列或价格列。当前列名：{list(data.columns)}")

    symbol = _first_text(data, ("symbol", "代码", "基金代码")) or fallback_name
    name = _first_text(data, ("name", "基金名称", "名称", "简称")) or symbol

    selected_columns = [date_col]
    if open_col:
        selected_columns.append(open_col)
    selected_columns.append(price_col)
    normalized = data[selected_columns].copy()
    normalized.columns = ["trade_date", "open", "close"] if open_col else ["trade_date", "close"]
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
    if "open" not in normalized.columns:
        normalized["open"] = normalized["close"]
    normalized["open"] = pd.to_numeric(normalized["open"], errors="coerce")
    normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
    normalized = normalized.dropna(subset=["trade_date", "open", "close"])
    normalized = normalized.sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)
    if normalized.empty:
        raise ValueError("日期和价格列解析后没有有效数据。")

    return RotationInput(
        symbol=str(symbol),
        name=str(name),
        dataframe=normalized,
        apply_slippage=open_col is not None,
    )


def _normalize_date_range(
    start_date: str | pd.Timestamp | None,
    end_date: str | pd.Timestamp | None,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    start = pd.Timestamp(start_date).normalize() if start_date is not None else None
    end = pd.Timestamp(end_date).normalize() if end_date is not None else None
    if start is not None and pd.isna(start):
        raise ValueError("开始日期无效。")
    if end is not None and pd.isna(end):
        raise ValueError("结束日期无效。")
    if start is not None and end is not None and start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    return start, end


def build_standard_backtest_periods(end_date: str | pd.Timestamp) -> list[tuple[str, pd.Timestamp | None]]:
    end = pd.Timestamp(end_date).normalize()
    starts = {
        "近一年": end - pd.DateOffset(years=1),
        "今年来": pd.Timestamp(end.year, 1, 1),
        "近三年": end - pd.DateOffset(years=3),
        "近五年": end - pd.DateOffset(years=5),
        "成立来": None,
    }
    return [(label, starts[label]) for label in STANDARD_BACKTEST_PERIODS]


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
) -> PortfolioTimingResult:
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
                timing_result = run_ma20_timing_backtest(
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
                timing_result = run_ma20_timing_backtest(
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


def _find_column(columns, keywords: tuple[str, ...]) -> str | None:
    normalized = [str(column).strip().lstrip("\ufeff") for column in columns]
    normalized_lower = {column.lower(): column for column in normalized}
    for keyword in keywords:
        exact_match = normalized_lower.get(keyword.lower())
        if exact_match is not None:
            return exact_match
    for keyword in keywords:
        keyword_lower = keyword.lower()
        for column in normalized:
            if keyword_lower in column.lower():
                return column
    return None


def _first_text(df: pd.DataFrame, columns: tuple[str, ...]) -> str | None:
    for column in columns:
        if column in df.columns and df[column].notna().any():
            value = str(df[column].dropna().iloc[0]).strip()
            if value:
                return value
    return None


def _prepare_merged_data(source_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    merged = None
    for symbol, df in source_data.items():
        current = df[["trade_date", "open", "close"]].copy()
        current.columns = ["trade_date", f"{symbol}__open", symbol]
        current[f"{symbol}__raw_close"] = current[symbol]
        merged = current if merged is None else pd.merge(merged, current, on="trade_date", how="outer")
    if merged is None or merged.empty:
        raise ValueError("没有可合并的基金数据。")
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    for symbol in source_data:
        merged[f"{symbol}__raw_close"] = pd.to_numeric(merged[f"{symbol}__raw_close"], errors="coerce")
        merged[symbol] = pd.to_numeric(merged[symbol], errors="coerce").ffill()
        merged[f"{symbol}__open"] = pd.to_numeric(merged[f"{symbol}__open"], errors="coerce")
    return merged


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


def _execution_mode_label(execution_mode: str) -> str:
    if execution_mode == EXECUTION_AFTER_CLOSE:
        return "盘后固定价（当日收盘信号/收盘成交）"
    return "次日开盘（前收盘信号/开盘成交）"


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


def _round_lot_shares(shares: float, lot_size: int = LOT_SIZE) -> float:
    if shares <= 0:
        return 0.0
    if lot_size <= 0:
        return float(shares)
    return float(int(shares // lot_size) * lot_size)


def _portfolio_value(shares: dict[str, float], row: pd.Series) -> float:
    return float(sum(amount * _row_price(row, symbol) for symbol, amount in shares.items()))


def _holding_amount_detail(shares: dict[str, float], row: pd.Series, names: dict[str, str]) -> str:
    parts = []
    for symbol, amount in shares.items():
        value = amount * _row_price(row, symbol)
        parts.append(f"{names.get(symbol, symbol)}:{value:.2f}")
    return " | ".join(parts)


def _calculate_drawdown(
    nav_df: pd.DataFrame,
    initial_capital: float | None = None,
) -> pd.DataFrame:
    result = nav_df[["日期", "账户净值"]].copy()
    account_values = pd.to_numeric(result["账户净值"], errors="coerce")
    running_peak = account_values.cummax()
    if initial_capital is not None:
        running_peak = running_peak.clip(lower=float(initial_capital))
    result["running_peak"] = running_peak
    result["回撤(%)"] = (result["账户净值"] / result["running_peak"] - 1) * 100
    return result.round({"回撤(%)": 2})


def _calculate_individual_results(
    source_data: dict[str, pd.DataFrame],
    names: dict[str, str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    initial_capital: float,
) -> pd.DataFrame:
    rows = []
    for symbol, df in source_data.items():
        data = df[
            (df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)
        ].copy()
        if len(data) < 2:
            continue
        first = float(data["close"].iloc[0])
        last = float(data["close"].iloc[-1])
        total_return = last / first - 1 if first > 0 else 0
        days = (pd.Timestamp(data["trade_date"].max()) - pd.Timestamp(data["trade_date"].min())).days
        annual_return = (1 + total_return) ** (365 / days) - 1 if days > 0 and total_return > -1 else 0
        nav = data["close"] / first * initial_capital if first > 0 else pd.Series(dtype=float)
        drawdown = nav / nav.cummax() - 1 if not nav.empty else pd.Series(dtype=float)
        rows.append(
            {
                "标的": names.get(symbol, symbol),
                "代码": symbol,
                "总收益率(%)": round(total_return * 100, 2),
                "年化收益率(%)": round(annual_return * 100, 2),
                "一直持有最大回撤(%)": round(float(drawdown.min() * 100), 2) if not drawdown.empty else 0,
                "期末资金": round(initial_capital * (1 + total_return), 2),
            }
        )
    return pd.DataFrame(rows)


def _calculate_individual_nav_data(
    source_data: dict[str, pd.DataFrame],
    names: dict[str, str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    initial_capital: float,
) -> pd.DataFrame:
    rows = []
    for symbol, df in source_data.items():
        data = df[
            (df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)
        ].copy()
        if data.empty:
            continue
        first = pd.to_numeric(data["close"].iloc[0], errors="coerce")
        if pd.isna(first) or float(first) <= 0:
            continue
        data["close"] = pd.to_numeric(data["close"], errors="coerce")
        data = data.dropna(subset=["trade_date", "close"])
        for _, row in data.iterrows():
            value = float(row["close"]) / float(first) * initial_capital
            rows.append(
                {
                    "日期": row["trade_date"],
                    "标的": names.get(symbol, symbol),
                    "代码": symbol,
                    "一直持有净值": round(value, 2),
                    "累计收益率(%)": round((value / initial_capital - 1) * 100, 2),
                }
            )
    return pd.DataFrame(rows)


def _calculate_yearly_stats(
    nav_df: pd.DataFrame,
    initial_capital: float | None = None,
) -> pd.DataFrame:
    data = nav_df[["日期", "账户净值"]].copy()
    data = data.sort_values("日期").reset_index(drop=True)
    data["year"] = pd.to_datetime(data["日期"]).dt.year
    rows = []
    previous_year_end = float(initial_capital) if initial_capital is not None else None
    for year, group in data.groupby("year"):
        group = group.sort_values("日期")
        values = pd.to_numeric(group["账户净值"], errors="coerce").dropna().reset_index(drop=True)
        if values.empty:
            continue
        baseline = previous_year_end if previous_year_end is not None else float(values.iloc[0])
        year_return = float(values.iloc[-1]) / baseline - 1 if baseline > 0 else 0.0
        seeded_values = pd.concat([pd.Series([baseline]), values], ignore_index=True)
        running_peak = seeded_values.cummax().iloc[1:].reset_index(drop=True)
        drawdown = values / running_peak - 1
        rows.append(
            {
                "年份": int(year),
                "年收益率(%)": round(year_return * 100, 2),
                "年最大回撤(%)": round(float(drawdown.min() * 100), 2),
            }
        )
        previous_year_end = float(values.iloc[-1])
    return pd.DataFrame(rows)


def _calculate_sharpe_ratio(daily_returns: pd.Series) -> float:
    clean_returns = pd.to_numeric(daily_returns, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean_returns) < 2:
        return 0.0
    daily_volatility = float(clean_returns.std())
    if daily_volatility <= 0:
        return 0.0
    return float(clean_returns.mean() / daily_volatility * np.sqrt(252))


def _calculate_nav_returns(nav_df: pd.DataFrame, initial_capital: float) -> pd.Series:
    account_values = pd.to_numeric(nav_df["账户净值"], errors="coerce").dropna().reset_index(drop=True)
    if account_values.empty:
        return pd.Series(dtype=float)
    seeded_values = pd.concat(
        [pd.Series([float(initial_capital)]), account_values],
        ignore_index=True,
    )
    return seeded_values.pct_change().replace([np.inf, -np.inf], np.nan).dropna()


def _calculate_trade_win_stats(realized_trade_pnls: list[float]) -> tuple[int, int, float]:
    closed_count = len(realized_trade_pnls)
    winning_count = sum(1 for pnl in realized_trade_pnls if pnl > 0)
    win_rate = winning_count / closed_count * 100 if closed_count else 0.0
    return closed_count, winning_count, win_rate


def _build_summary(
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    drawdown_df: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    initial_capital: float,
    total_buy_cost: float,
    total_sell_cost: float,
    realized_trade_pnls: list[float],
    execution_mode: str,
) -> dict[str, object]:
    final_value = float(nav_df["账户净值"].iloc[-1])
    total_return = final_value / initial_capital - 1
    days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days
    annual_return = (1 + total_return) ** (365 / days) - 1 if days > 0 and total_return > -1 else 0
    daily_returns = _calculate_nav_returns(nav_df, initial_capital)
    annual_vol = float(daily_returns.std() * np.sqrt(252)) if not daily_returns.empty else 0.0
    sharpe = _calculate_sharpe_ratio(daily_returns)
    max_drawdown = float(drawdown_df["回撤(%)"].min()) if not drawdown_df.empty else 0
    switch_count = int((trades_df["操作"] == "调仓").sum()) if not trades_df.empty else 0
    closed_trade_count, winning_trade_count, trade_win_rate = _calculate_trade_win_stats(realized_trade_pnls)
    return {
        "开始日期": pd.Timestamp(start_date).strftime("%Y-%m-%d"),
        "结束日期": pd.Timestamp(end_date).strftime("%Y-%m-%d"),
        "成交方式": _execution_mode_label(execution_mode),
        "成交假设": (
            "按收盘价全部成交，未模拟盘后排队未成交"
            if execution_mode == EXECUTION_AFTER_CLOSE
            else "按开盘价成交，场内标的计入双边滑点"
        ),
        "期末资金": round(final_value, 2),
        "总收益率(%)": round(total_return * 100, 2),
        "年化收益率(%)": round(annual_return * 100, 2),
        "策略最大回撤(%)": round(max_drawdown, 2),
        "年化波动率(%)": round(annual_vol * 100, 2),
        "夏普比率": round(sharpe, 2),
        "调仓次数": switch_count,
        "已平仓交易次数": closed_trade_count,
        "盈利交易次数": winning_trade_count,
        "交易胜率(%)": round(trade_win_rate, 2),
        "累计买入手续费": round(total_buy_cost, 2),
        "累计卖出手续费": round(total_sell_cost, 2),
        "累计总成本": round(total_buy_cost + total_sell_cost, 2),
    }


def _build_timing_summary(
    result_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    drawdown_df: pd.DataFrame,
    fund: RotationInput,
    ma_period: int,
    threshold_pct: float,
    initial_capital: float,
    total_buy_cost: float,
    total_sell_cost: float,
    realized_trade_pnls: list[float],
) -> dict[str, object]:
    start_date = pd.Timestamp(result_df["日期"].iloc[0])
    end_date = pd.Timestamp(result_df["日期"].iloc[-1])
    final_value = float(result_df["账户净值"].iloc[-1])
    benchmark_final = float(result_df["一直持有净值"].iloc[-1])
    total_return = final_value / initial_capital - 1
    benchmark_return = benchmark_final / initial_capital - 1
    benchmark_values = pd.to_numeric(result_df["一直持有净值"], errors="coerce").dropna()
    benchmark_drawdown = benchmark_values / benchmark_values.cummax() - 1
    benchmark_max_drawdown = float(benchmark_drawdown.min() * 100) if not benchmark_drawdown.empty else 0.0
    days = (end_date - start_date).days
    annual_return = (1 + total_return) ** (365 / days) - 1 if days > 0 and total_return > -1 else 0
    benchmark_annual_return = (
        (1 + benchmark_return) ** (365 / days) - 1 if days > 0 and benchmark_return > -1 else 0
    )
    daily_returns = _calculate_nav_returns(result_df, initial_capital)
    annual_vol = float(daily_returns.std() * np.sqrt(252)) if not daily_returns.empty else 0.0
    sharpe = _calculate_sharpe_ratio(daily_returns)
    latest = result_df.iloc[-1]
    trade_count = len(trades_df)
    buy_count = int((trades_df["操作"] == "买入").sum()) if not trades_df.empty else 0
    sell_count = int((trades_df["操作"] == "卖出").sum()) if not trades_df.empty else 0
    holding_days = int((result_df["持仓份额"] > 0).sum())
    holding_ratio = holding_days / len(result_df) if len(result_df) else 0
    closed_trade_count, winning_trade_count, trade_win_rate = _calculate_trade_win_stats(realized_trade_pnls)

    return {
        "标的": fund.name,
        "代码": fund.symbol,
        "策略": (
            f"收盘价 > MA{ma_period} 上方 {threshold_pct:.2f}% 买入，"
            f"收盘价 < MA{ma_period} 下方 {threshold_pct:.2f}% 卖出"
        ),
        "触发阈值(%)": round(float(threshold_pct), 2),
        "开始日期": start_date.strftime("%Y-%m-%d"),
        "结束日期": end_date.strftime("%Y-%m-%d"),
        "期末资金": round(final_value, 2),
        "总收益率(%)": round(total_return * 100, 2),
        "年化收益率(%)": round(annual_return * 100, 2),
        "一直持有收益率(%)": round(benchmark_return * 100, 2),
        "一直持有年化(%)": round(benchmark_annual_return * 100, 2),
        "超额收益(%)": round((total_return - benchmark_return) * 100, 2),
        "策略最大回撤(%)": round(float(drawdown_df["回撤(%)"].min()), 2) if not drawdown_df.empty else 0,
        "一直持有最大回撤(%)": round(benchmark_max_drawdown, 2),
        "年化波动率(%)": round(annual_vol * 100, 2),
        "夏普比率": round(sharpe, 2),
        "交易次数": trade_count,
        "买入次数": buy_count,
        "卖出次数": sell_count,
        "已平仓交易次数": closed_trade_count,
        "盈利交易次数": winning_trade_count,
        "交易胜率(%)": round(trade_win_rate, 2),
        "持仓天数": holding_days,
        "持仓占比(%)": round(holding_ratio * 100, 2),
        "最新信号": latest["信号"],
        "最新收盘价": round(float(latest["收盘价"]), 4),
        f"最新MA{ma_period}": round(float(latest[f"MA{ma_period}"]), 4),
        "最新买入线": round(float(latest["买入线"]), 4),
        "最新卖出线": round(float(latest["卖出线"]), 4),
        "累计买入手续费": round(total_buy_cost, 2),
        "累计卖出手续费": round(total_sell_cost, 2),
        "累计总成本": round(total_buy_cost + total_sell_cost, 2),
    }
