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


@dataclass
class RotationInput:
    symbol: str
    name: str
    dataframe: pd.DataFrame
    trade_lot_size: int = 100


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

    return RotationInput(symbol=str(symbol), name=str(name), dataframe=normalized)


def run_fund_rotation_backtest(
    funds: list[RotationInput],
    frequency: str = "week",
    lookback_period: int = 22,
    num_positions: int = 1,
    initial_capital: float = 100000.0,
    transaction_cost: float = 0.00006,
) -> RotationResult:
    if len(funds) < 2:
        raise ValueError("至少需要导入 2 只基金进行轮动回测。")
    if num_positions < 1 or num_positions > len(funds):
        raise ValueError(f"持仓数量必须在 1 到 {len(funds)} 之间。")
    if initial_capital <= 0:
        raise ValueError("初始资金必须大于 0。")
    if lookback_period < 1:
        raise ValueError("动量周期必须大于 0。")

    symbol_names = {fund.symbol: fund.name for fund in funds}
    source_data = {fund.symbol: fund.dataframe.copy() for fund in funds}
    merged = _prepare_merged_data(source_data)
    end_date = pd.Timestamp(merged["trade_date"].max())
    start_date = _get_start_date(source_data, lookback_period)
    start_date = _align_rebalance_start(start_date, frequency, end_date, merged)
    backtest_df = merged[merged["trade_date"] >= start_date].reset_index(drop=True)
    if len(backtest_df) <= lookback_period:
        raise ValueError("回测区间数据不足，请缩短动量周期或导入更长历史数据。")

    all_dates = list(pd.to_datetime(merged["trade_date"]).dropna().sort_values().unique())
    rebalance_dates = _build_rebalance_dates(start_date, end_date, frequency, all_dates, backtest_df)
    momentum_cache = _calculate_momentum(merged, list(source_data.keys()), lookback_period)

    current_shares: dict[str, float] = {}
    cash_value = float(initial_capital)
    total_buy_cost = 0.0
    total_sell_cost = 0.0
    nav_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []

    for index, rebal_date in enumerate(rebalance_dates):
        date_rows = backtest_df[backtest_df["trade_date"] == rebal_date]
        if date_rows.empty:
            continue
        row = date_rows.iloc[0]
        momentum = momentum_cache.loc[rebal_date].to_dict() if rebal_date in momentum_cache.index else {}
        momentum = {key: 0.0 if pd.isna(value) or np.isinf(value) else float(value) for key, value in momentum.items()}
        selected = _select_top_symbols(momentum, num_positions) or list(source_data.keys())[:num_positions]
        holdings_changed = set(selected) != set(current_shares.keys())

        value_before = _portfolio_value(current_shares, row) + cash_value
        if not current_shares:
            value_before = cash_value

        sell_cost = 0.0
        buy_cost = 0.0
        sell_details: list[str] = []
        buy_details: list[str] = []
        new_shares: dict[str, float] = {}
        execution_prices: dict[str, float] = {}

        if holdings_changed and current_shares:
            for symbol, shares in current_shares.items():
                price = _trade_price(row, symbol, side="sell")
                gross_value = shares * price
                cost = gross_value * transaction_cost
                net_value = gross_value - cost
                cash_value += net_value
                sell_cost += cost
                sell_details.append(
                    f"{symbol_names.get(symbol, symbol)} 卖出份额:{shares:.2f} 卖出价:{price:.4f} "
                    f"卖出金额:{gross_value:.2f} 手续费:{cost:.2f} 到账:{net_value:.2f}"
                )
        elif current_shares:
            cash_value = cash_value

        allocation = cash_value / len(selected)
        for symbol in selected:
            price = _trade_price(row, symbol, side="buy")
            if holdings_changed or symbol not in current_shares:
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
                execution_prices[symbol] = price
            else:
                new_shares[symbol] = current_shares.get(symbol, 0.0)
                execution_prices[symbol] = _row_price(row, symbol)

        current_shares = new_shares
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
                "现金余额": round(cash_value, 2),
                "卖出明细": " | ".join(sell_details),
                "买入明细": " | ".join(buy_details),
                "调仓后持仓金额": _holding_amount_detail(current_shares, row, symbol_names),
            }
        )

        next_date = rebalance_dates[index + 1] if index + 1 < len(rebalance_dates) else end_date + timedelta(days=1)
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

    drawdown_df = _calculate_drawdown(nav_df)
    individual_df = _calculate_individual_results(source_data, symbol_names, start_date, initial_capital)
    individual_nav_df = _calculate_individual_nav_data(source_data, symbol_names, start_date, initial_capital)
    yearly_stats = _calculate_yearly_stats(nav_df)
    summary = _build_summary(
        nav_df=nav_df,
        trades_df=trades_df,
        drawdown_df=drawdown_df,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        total_buy_cost=total_buy_cost,
        total_sell_cost=total_sell_cost,
    )

    return RotationResult(
        start_date=start_date,
        end_date=end_date,
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
        merged = current if merged is None else pd.merge(merged, current, on="trade_date", how="outer")
    if merged is None or merged.empty:
        raise ValueError("没有可合并的基金数据。")
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    for symbol in source_data:
        merged[symbol] = pd.to_numeric(merged[symbol], errors="coerce").ffill()
        merged[f"{symbol}__open"] = pd.to_numeric(merged[f"{symbol}__open"], errors="coerce").ffill()
    return merged


def _get_start_date(source_data: dict[str, pd.DataFrame], lookback_period: int) -> pd.Timestamp:
    eligible_dates = []
    for df in source_data.values():
        data = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
        if len(data) <= lookback_period:
            raise ValueError("有基金数据长度不足，无法计算完整动量窗口。")
        eligible_dates.append(pd.Timestamp(data.loc[lookback_period, "trade_date"]))
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


def _calculate_momentum(merged: pd.DataFrame, symbols: list[str], lookback_period: int) -> pd.DataFrame:
    momentum = {}
    indexed = merged.set_index("trade_date")
    for symbol in symbols:
        prices = pd.to_numeric(indexed[symbol], errors="coerce")
        previous_close = prices.shift(1)
        rows = []
        for position in range(len(previous_close)):
            end_price = previous_close.iloc[position]
            if pd.isna(end_price) or end_price <= 0:
                rows.append(0.0)
                continue
            valid_history = prices.iloc[:position].dropna()
            if valid_history.empty:
                rows.append(0.0)
                continue
            start_position = max(0, len(valid_history) - lookback_period)
            start_price = valid_history.iloc[start_position]
            rows.append(float(end_price / start_price - 1) if start_price > 0 else 0.0)
        momentum[symbol] = rows
    return pd.DataFrame(momentum, index=indexed.index).fillna(0)


def _select_top_symbols(momentum: dict[str, float], num_positions: int) -> list[str]:
    return [symbol for symbol, _ in sorted(momentum.items(), key=lambda item: item[1], reverse=True)[:num_positions]]


def _row_price(row: pd.Series, symbol: str) -> float:
    value = pd.to_numeric(row.get(symbol), errors="coerce")
    return 0.0 if pd.isna(value) else float(value)


def _trade_price(row: pd.Series, symbol: str, side: str) -> float:
    open_value = pd.to_numeric(row.get(f"{symbol}__open"), errors="coerce")
    base_price = _row_price(row, symbol) if pd.isna(open_value) else float(open_value)
    if side == "buy":
        return base_price * (1 + BUY_SLIPPAGE)
    if side == "sell":
        return base_price * (1 - SELL_SLIPPAGE)
    return base_price


def _trade_lot_size(funds: list[RotationInput], symbol: str) -> int:
    for fund in funds:
        if fund.symbol == symbol:
            return int(fund.trade_lot_size)
    return LOT_SIZE


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


def _calculate_drawdown(nav_df: pd.DataFrame) -> pd.DataFrame:
    result = nav_df[["日期", "账户净值"]].copy()
    result["running_peak"] = result["账户净值"].cummax()
    result["回撤(%)"] = (result["账户净值"] / result["running_peak"] - 1) * 100
    return result.round({"回撤(%)": 2})


def _calculate_individual_results(
    source_data: dict[str, pd.DataFrame],
    names: dict[str, str],
    start_date: pd.Timestamp,
    initial_capital: float,
) -> pd.DataFrame:
    rows = []
    for symbol, df in source_data.items():
        data = df[df["trade_date"] >= start_date].copy()
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
                "最大回撤(%)": round(float(drawdown.min() * 100), 2) if not drawdown.empty else 0,
                "期末资金": round(initial_capital * (1 + total_return), 2),
            }
        )
    return pd.DataFrame(rows)


def _calculate_individual_nav_data(
    source_data: dict[str, pd.DataFrame],
    names: dict[str, str],
    start_date: pd.Timestamp,
    initial_capital: float,
) -> pd.DataFrame:
    rows = []
    for symbol, df in source_data.items():
        data = df[df["trade_date"] >= start_date].copy()
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
                    "单独持有净值": round(value, 2),
                    "累计收益率(%)": round((value / initial_capital - 1) * 100, 2),
                }
            )
    return pd.DataFrame(rows)


def _calculate_yearly_stats(nav_df: pd.DataFrame) -> pd.DataFrame:
    data = nav_df[["日期", "账户净值"]].copy()
    data["year"] = pd.to_datetime(data["日期"]).dt.year
    rows = []
    for year, group in data.groupby("year"):
        group = group.sort_values("日期")
        year_return = group["账户净值"].iloc[-1] / group["账户净值"].iloc[0] - 1
        drawdown = group["账户净值"] / group["账户净值"].cummax() - 1
        rows.append(
            {
                "年份": int(year),
                "年收益率(%)": round(year_return * 100, 2),
                "年最大回撤(%)": round(float(drawdown.min() * 100), 2),
            }
        )
    return pd.DataFrame(rows)


def _build_summary(
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    drawdown_df: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    initial_capital: float,
    total_buy_cost: float,
    total_sell_cost: float,
) -> dict[str, object]:
    final_value = float(nav_df["账户净值"].iloc[-1])
    total_return = final_value / initial_capital - 1
    days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days
    annual_return = (1 + total_return) ** (365 / days) - 1 if days > 0 and total_return > -1 else 0
    daily_returns = nav_df["账户净值"].pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    annual_vol = float(daily_returns.std() * np.sqrt(252)) if not daily_returns.empty else 0.0
    sharpe = float(annual_return / annual_vol) if annual_vol > 0 else 0.0
    max_drawdown = float(drawdown_df["回撤(%)"].min()) if not drawdown_df.empty else 0
    switch_count = int((trades_df["操作"] == "调仓").sum()) if not trades_df.empty else 0
    return {
        "开始日期": pd.Timestamp(start_date).strftime("%Y-%m-%d"),
        "结束日期": pd.Timestamp(end_date).strftime("%Y-%m-%d"),
        "期末资金": round(final_value, 2),
        "总收益率(%)": round(total_return * 100, 2),
        "年化收益率(%)": round(annual_return * 100, 2),
        "最大回撤(%)": round(max_drawdown, 2),
        "年化波动率(%)": round(annual_vol * 100, 2),
        "夏普比率": round(sharpe, 2),
        "调仓次数": switch_count,
        "累计买入手续费": round(total_buy_cost, 2),
        "累计卖出手续费": round(total_sell_cost, 2),
        "累计总成本": round(total_buy_cost + total_sell_cost, 2),
    }
