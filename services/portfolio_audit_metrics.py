from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_performance_summary(daily: pd.DataFrame, initial_capital: float) -> dict[str, object]:
    values = pd.to_numeric(daily["portfolio_value"], errors="coerce")
    dates = pd.to_datetime(daily["trade_date"])
    seeded = pd.concat([pd.Series([initial_capital]), values.reset_index(drop=True)], ignore_index=True)
    returns = seeded.pct_change().dropna()
    drawdown = seeded / seeded.cummax() - 1
    total_return = float(values.iloc[-1] / initial_capital - 1)
    days = max(1, int((dates.iloc[-1] - dates.iloc[0]).days))
    annual_return = (1 + total_return) ** (365 / days) - 1 if total_return > -1 else -1.0
    volatility = float(returns.std() * np.sqrt(252)) if len(returns) > 1 else 0.0
    sharpe = float(returns.mean() / returns.std() * np.sqrt(252)) if len(returns) > 1 and returns.std() > 0 else 0.0
    max_drawdown = float(drawdown.min())
    return {
        "start_date": dates.iloc[0].strftime("%Y-%m-%d"),
        "end_date": dates.iloc[-1].strftime("%Y-%m-%d"),
        "trading_days": len(daily),
        "final_value": float(values.iloc[-1]),
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "annual_volatility_pct": volatility * 100,
        "sharpe_ratio": sharpe,
        "calmar_ratio": annual_return / abs(max_drawdown) if max_drawdown < 0 else np.nan,
    }


def position_statistics(daily: pd.DataFrame) -> pd.DataFrame:
    exposure = pd.to_numeric(daily["etf_weight_pct"], errors="coerce").fillna(0)
    bins = [0, 20, 40, 60, 80, 100.000001]
    labels = ["0%-20%", "20%-40%", "40%-60%", "60%-80%", "80%-100%"]
    bucket = pd.cut(exposure.clip(0, 100), bins=bins, labels=labels, include_lowest=True, right=False)
    rows = [
        {"metric": "平均ETF仓位", "value": float(exposure.mean())},
        {"metric": "仓位中位数", "value": float(exposure.median())},
        {"metric": "最低仓位", "value": float(exposure.min())},
        {"metric": "最高仓位", "value": float(exposure.max())},
        {"metric": "仓位标准差", "value": float(exposure.std())},
    ]
    rows.extend(
        {"metric": f"{label}交易日占比", "value": float((bucket == label).mean() * 100)}
        for label in labels
    )
    return pd.DataFrame(rows)


def _max_consecutive_losses(pnls: pd.Series) -> int:
    longest = current = 0
    for value in pnls:
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    return longest
