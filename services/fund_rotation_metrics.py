from __future__ import annotations

import numpy as np
import pandas as pd

from services.fund_rotation_models import LOT_SIZE

def _round_lot_shares(shares: float, lot_size: int = LOT_SIZE) -> float:
    if shares <= 0:
        return 0.0
    if lot_size <= 0:
        return float(shares)
    return float(int(shares // lot_size) * lot_size)

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

__all__ = [
    "_round_lot_shares",
    "_calculate_drawdown",
    "_calculate_individual_results",
    "_calculate_individual_nav_data",
    "_calculate_yearly_stats",
    "_calculate_sharpe_ratio",
    "_calculate_nav_returns",
    "_calculate_trade_win_stats",
]
