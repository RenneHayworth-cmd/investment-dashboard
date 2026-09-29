from __future__ import annotations

import numpy as np
import pandas as pd

from services.fund_rotation_metrics import (
    _calculate_nav_returns,
    _calculate_sharpe_ratio,
    _calculate_trade_win_stats,
)
from services.fund_rotation_models import EXECUTION_AFTER_CLOSE, RotationInput

def _execution_mode_label(execution_mode: str) -> str:
    if execution_mode == EXECUTION_AFTER_CLOSE:
        return "盘后固定价（当日收盘信号/收盘成交）"
    return "次日开盘（前收盘信号/开盘成交）"

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
    base_position_pct: float = 0.0,
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
    # Holding days describe the timing sleeve; a permanent base would make them trivially 100%.
    timing_shares = result_df["择时份额"] if "择时份额" in result_df.columns else result_df["持仓份额"]
    holding_days = int((timing_shares > 0).sum())
    holding_ratio = holding_days / len(result_df) if len(result_df) else 0
    closed_trade_count, winning_trade_count, trade_win_rate = _calculate_trade_win_stats(realized_trade_pnls)
    rule = (
        f"收盘价 > MA{ma_period} 上方 {threshold_pct:.2f}% 买入，"
        f"收盘价 < MA{ma_period} 下方 {threshold_pct:.2f}% 卖出"
    )
    base_pct = float(base_position_pct)
    if base_pct > 0:
        rule = f"{base_pct:g}%底仓首日买入一直持有；其余{100 - base_pct:g}%资金：{rule}"

    return {
        "标的": fund.name,
        "代码": fund.symbol,
        "策略": rule,
        "底仓比例(%)": round(base_pct, 2),
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


def _seeded_max_drawdown_pct(values: pd.Series, baseline: float) -> float:
    seeded = pd.concat([pd.Series([float(baseline)]), values], ignore_index=True)
    return float((seeded / seeded.cummax() - 1).min() * 100)


def build_timing_calendar_year_table(
    result_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    initial_capital: float,
) -> pd.DataFrame:
    """Split one continuous MA-timing run into calendar years.

    Each year starts from the previous year-end value (initial capital for the
    first year), so positions carried across 12-31 are not reset and no
    artificial year-start trades appear.
    """
    data = result_df.copy()
    data["日期"] = pd.to_datetime(data["日期"])
    trades = trades_df.copy()
    if not trades.empty:
        trades["日期"] = pd.to_datetime(trades["日期"])
    timing_column = "择时份额" if "择时份额" in data.columns else "持仓份额"
    rows = []
    strategy_base = benchmark_base = float(initial_capital)
    for year, group in data.groupby(data["日期"].dt.year, sort=True):
        strategy = pd.to_numeric(group["账户净值"], errors="coerce").reset_index(drop=True)
        benchmark = pd.to_numeric(group["一直持有净值"], errors="coerce").reset_index(drop=True)
        strategy_return = float(strategy.iloc[-1]) / strategy_base - 1
        benchmark_return = float(benchmark.iloc[-1]) / benchmark_base - 1
        daily_returns = _calculate_nav_returns(group, strategy_base)
        year_trades = trades.loc[trades["日期"].dt.year == year] if not trades.empty else trades
        closed_pnls = (
            pd.to_numeric(year_trades.loc[year_trades["操作"] == "卖出", "本次交易盈亏金额"], errors="coerce").dropna().tolist()
            if not year_trades.empty else []
        )
        closed_count, winning_count, win_rate = _calculate_trade_win_stats(closed_pnls)
        rows.append(
            {
                "年份": int(year),
                "实际开始": group["日期"].iloc[0].strftime("%Y-%m-%d"),
                "实际结束": group["日期"].iloc[-1].strftime("%Y-%m-%d"),
                "策略收益率(%)": round(strategy_return * 100, 2),
                "一直持有收益率(%)": round(benchmark_return * 100, 2),
                "超额收益(%)": round((strategy_return - benchmark_return) * 100, 2),
                "策略最大回撤(%)": round(_seeded_max_drawdown_pct(strategy, strategy_base), 2),
                "一直持有最大回撤(%)": round(_seeded_max_drawdown_pct(benchmark, benchmark_base), 2),
                "夏普比率": round(_calculate_sharpe_ratio(daily_returns), 2),
                "交易次数": int(len(year_trades)),
                "已平仓交易次数": closed_count,
                "交易胜率(%)": round(win_rate, 2) if closed_count else None,
                "择时持仓占比(%)": round(float((group[timing_column] > 0).mean() * 100), 2),
                "年末信号": str(group["信号"].iloc[-1]),
            }
        )
        strategy_base = float(strategy.iloc[-1])
        benchmark_base = float(benchmark.iloc[-1])
    return pd.DataFrame(rows)


__all__ = [
    "_execution_mode_label",
    "_build_summary",
    "_build_timing_summary",
    "build_timing_calendar_year_table",
]
