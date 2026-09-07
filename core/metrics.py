"""统一绩效指标计算。

本项目此前在 fund_rotation_metrics、fund_rotation_summary、fund_rotation_timing、
portfolio_audit_metrics、dynamic_threshold_research、annual_etf_selection 等
至少 8 处各自实现年化收益、Sharpe、回撤等指标，风险利率与极端情形处理不一致。
本模块是这些指标的唯一定义；各子系统按需包装。

约定：
- 收益率序列指"逐期简单收益率"，首期应包含以初始资金为基数的起点收益。
- 年化收益使用日历日复利口径 (1+r)^(365/days)-1；总亏损超过 -100% 时按 -100% 计。
- 最大回撤返回负的百分比数值（例如 -23.45 表示 -23.45%）。
- 最长水下期返回自然日；峰值相等不重置峰值日期，与年度动态组合口径一致。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365


def seeded_values(values: pd.Series | np.ndarray, initial_capital: float) -> pd.Series:
    """在序列头部加入初始资金，使首期收益/回撤包含期初交易成本。"""
    clean = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    seeded = pd.concat(
        [pd.Series([float(initial_capital)]), clean.reset_index(drop=True)],
        ignore_index=True,
    )
    return seeded.astype(float)


def seeded_returns(
    values: pd.Series | np.ndarray,
    initial_capital: float,
) -> pd.Series:
    """以初始资金为起点的逐期收益率，剔除 inf 与缺口。"""
    seeded = seeded_values(values, initial_capital)
    returns = seeded.pct_change()
    return returns.replace([np.inf, -np.inf], np.nan).dropna()


def total_return(values: pd.Series | np.ndarray, initial_capital: float) -> float:
    clean = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if clean.empty or initial_capital <= 0:
        return 0.0
    return float(clean.iloc[-1]) / float(initial_capital) - 1.0


def annualized_return(total_return: float, calendar_days: int) -> float:
    """日历日复利年化；总亏损达到或超过 -100% 时返回 -1。"""
    if calendar_days <= 0:
        return 0.0
    if total_return <= -1.0:
        return -1.0
    return float((1.0 + total_return) ** (CALENDAR_DAYS_PER_YEAR / calendar_days) - 1.0)


def annual_volatility(daily_returns: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    clean = pd.to_numeric(pd.Series(daily_returns), errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    if len(clean) < 2:
        return 0.0
    return float(clean.std(ddof=1) * np.sqrt(periods_per_year))


def sharpe_ratio(
    daily_returns: pd.Series,
    risk_free_annual: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """夏普比率；无风险利率按年复利折算到期。risk_free_annual=0 时退化为均值/波动。"""
    clean = pd.to_numeric(pd.Series(daily_returns), errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    if len(clean) < 2:
        return 0.0
    volatility = float(clean.std(ddof=1))
    if volatility <= 1e-12:
        return 0.0
    risk_free_daily = float((1.0 + risk_free_annual) ** (1.0 / periods_per_year) - 1.0)
    excess = float(clean.mean()) - risk_free_daily
    return float(excess / volatility * np.sqrt(periods_per_year))


def drawdown_series(
    values: pd.Series | np.ndarray,
    initial_capital: float | None = None,
) -> pd.Series:
    """回撤序列（百分比数值，例如 -5.0 表示 -5%）。

    initial_capital 作为起点峰值计入（等价于以初始资金播种序列），
    首日交易成本造成的初始亏损也会被计入回撤。
    """
    clean = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if clean.empty:
        return pd.Series(dtype=float)
    running_peak = clean.cummax()
    if initial_capital is not None:
        running_peak = running_peak.clip(lower=float(initial_capital))
    return (clean / running_peak - 1.0) * 100.0


def max_drawdown(
    values: pd.Series | np.ndarray,
    initial_capital: float | None = None,
) -> float:
    """最大回撤（百分比数值，例如 -23.45 表示 -23.45%）。"""
    drawdown = drawdown_series(values, initial_capital=initial_capital)
    if drawdown.empty:
        return 0.0
    return float(drawdown.min())


def longest_underwater_days(
    values: pd.Series | np.ndarray,
    dates: pd.Series | pd.DatetimeIndex | None = None,
    initial_capital: float | None = None,
    initial_date: pd.Timestamp | None = None,
) -> int:
    """最长水下期（自然日；未提供 dates 时按数据点数）。

    语义与年度动态组合一致：净值回到峰值（含恰好相等，容差 1e-10）即视为出水；
    initial_capital 作为起点峰值。种子峰值日期默认为首个数据点前一天，
    使期初跌破本金也计入水下期；也可用 initial_date 显式指定（如动态初值场景）。
    """
    raw_values = pd.to_numeric(pd.Series(np.asarray(values, dtype=float)), errors="coerce")
    mask = raw_values.notna().to_numpy()
    clean_values = raw_values.to_numpy()[mask]
    if clean_values.size == 0:
        return 0
    if dates is None:
        return _longest_underwater_steps(clean_values, initial_capital)
    date_values = pd.to_datetime(pd.Series(np.asarray(dates))).to_numpy()[mask]
    if date_values.size != clean_values.size:
        raise ValueError("dates 与 values 长度不一致。")
    return _longest_underwater_calendar_days(
        clean_values, date_values, initial_capital, initial_date
    )


def _longest_underwater_steps(values: np.ndarray, initial_capital: float | None) -> int:
    peak = float(initial_capital) if initial_capital is not None else float("-inf")
    peak_step = -1
    longest = 0
    for step, value in enumerate(values.astype(float)):
        if value >= peak - 1e-10:
            if value > peak:
                peak = float(value)
                peak_step = step
        else:
            longest = max(longest, step - peak_step)
    return longest


def _longest_underwater_calendar_days(
    values: np.ndarray,
    dates: np.ndarray,
    initial_capital: float | None,
    initial_date: pd.Timestamp | None,
) -> int:
    if initial_capital is not None:
        peak = float(initial_capital)
    elif len(values) > 0:
        peak = float("-inf")
    else:
        peak = float("-inf")
    if initial_date is not None:
        peak_date = pd.Timestamp(initial_date)
    elif initial_capital is not None:
        peak_date = pd.Timestamp(dates[0]) - pd.Timedelta(days=1)
    else:
        peak_date = None
    longest = 0
    for value, raw_date in zip(values.astype(float), dates):
        date = pd.Timestamp(raw_date)
        if value >= peak - 1e-10:
            if value > peak or peak_date is None:
                peak = float(value)
                peak_date = date
        elif peak_date is not None:
            longest = max(longest, (date - peak_date).days)
    return longest


def calmar_ratio(annual_return_pct: float, max_drawdown_pct: float) -> float:
    """卡玛比率；年化收益与最大回撤均以百分比传入，回撤为 0 时返回 NaN。"""
    if max_drawdown_pct >= 0:
        return float("nan")
    return float(annual_return_pct / abs(max_drawdown_pct))


def trade_win_stats(realized_trade_pnls: list[float] | pd.Series) -> tuple[int, int, float]:
    """平仓交易统计：返回 (平仓次数, 盈利次数, 胜率%)。"""
    pnls = [pnl for pnl in pd.to_numeric(pd.Series(list(realized_trade_pnls)), errors="coerce") if pd.notna(pnl)]
    closed_count = len(pnls)
    winning_count = sum(1 for pnl in pnls if pnl > 0)
    win_rate = winning_count / closed_count * 100 if closed_count else 0.0
    return closed_count, winning_count, win_rate


def interval_return_pct(start_value: float, end_value: float) -> float:
    """区间收益率（百分比），起点非正时返回 NaN。"""
    if start_value is None or start_value <= 0:
        return float("nan")
    return float(end_value) / float(start_value) * 100 - 100.0


__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "CALENDAR_DAYS_PER_YEAR",
    "seeded_values",
    "seeded_returns",
    "total_return",
    "annualized_return",
    "annual_volatility",
    "sharpe_ratio",
    "drawdown_series",
    "max_drawdown",
    "longest_underwater_days",
    "calmar_ratio",
    "trade_win_stats",
    "interval_return_pct",
]
