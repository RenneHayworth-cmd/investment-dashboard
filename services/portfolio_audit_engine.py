from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from services.portfolio_audit_data import validate_audit_inputs
from services.portfolio_audit_execution import _run_sleeve
from services.portfolio_audit_metrics import (
    _max_consecutive_losses,
    calculate_performance_summary,
)
from services.portfolio_audit_models import (
    AuditAllocation,
    AuditRunResult,
    AuditSettings,
    _SleeveResult,
)


def run_portfolio_audit_engine(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings | None = None,
    *,
    blocked_entries: set[tuple[str, pd.Timestamp]] | None = None,
    _validate_inputs: Callable[
        [dict[str, pd.DataFrame], list[AuditAllocation], AuditSettings], None
    ] = validate_audit_inputs,
    _performance_summary: Callable[
        [pd.DataFrame, float], dict[str, object]
    ] = calculate_performance_summary,
) -> AuditRunResult:
    settings = settings or AuditSettings()
    _validate_inputs(market_data, allocations, settings)
    dates = _common_dates(market_data, allocations, settings.start_date, settings.end_date)
    if len(dates) < 2:
        raise ValueError("共同交易日不足，无法回测。")

    component_frames: list[pd.DataFrame] = []
    trade_frames: list[pd.DataFrame] = []
    contribution_rows: list[dict[str, object]] = []
    allocated_weight = sum(item.weight_pct for item in allocations)
    residual_cash = settings.initial_capital * max(0.0, 100.0 - allocated_weight) / 100
    residual_values = _cash_series(residual_cash, dates, settings.cash_annual_rate)

    for allocation_index, item in enumerate(allocations):
        capital = settings.initial_capital * item.weight_pct / 100
        hold_fraction = 1.0 if item.strategy == "hold" else 0.5 if item.strategy == "half_timing" else 0.0
        timing_fraction = 1.0 - hold_fraction
        parts: list[_SleeveResult] = []
        if hold_fraction:
            parts.append(
                _run_sleeve(
                    item,
                    market_data[item.symbol],
                    dates,
                    capital * hold_fraction,
                    replace(settings, random_seed=settings.random_seed + allocation_index * 17),
                    always_hold=True,
                    blocked_entries=blocked_entries or set(),
                )
            )
        if timing_fraction:
            parts.append(
                _run_sleeve(
                    item,
                    market_data[item.symbol],
                    dates,
                    capital * timing_fraction,
                    replace(settings, random_seed=settings.random_seed + allocation_index * 17 + 1),
                    always_hold=False,
                    blocked_entries=blocked_entries or set(),
                )
            )

        component = _combine_sleeves(item, parts, dates, settings.initial_capital)
        component_frames.append(component)
        trades = [part.trades for part in parts if not part.trades.empty]
        if trades:
            trade_frames.append(pd.concat(trades, ignore_index=True))
        summaries = [part.summary for part in parts]
        final_value = float(component["component_value"].iloc[-1])
        realized = sum(summary["realized_pnl"] for summary in summaries)
        dividends = sum(summary["dividend_income"] for summary in summaries)
        cash_income = sum(summary["cash_income"] for summary in summaries)
        commission = sum(summary["commission_cost"] for summary in summaries)
        slippage = sum(summary["slippage_cost"] for summary in summaries)
        signal_count = sum(int(summary["signal_generated_count"]) for summary in summaries)
        submitted_count = sum(int(summary["order_submitted_count"]) for summary in summaries)
        missed_buy_count = sum(int(summary["missed_buy_count"]) for summary in summaries)
        missed_sell_count = sum(int(summary["missed_sell_count"]) for summary in summaries)
        retried_count = sum(int(summary["order_retried_count"]) for summary in summaries)
        filled_count = sum(int(summary["order_filled_count"]) for summary in summaries)
        delay_total = sum(
            float(summary["average_execution_delay_days"]) * int(summary["order_filled_count"])
            for summary in summaries
        )
        symbol_trades = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
        closed = symbol_trades[symbol_trades["action"] == "sell"] if not symbol_trades.empty else pd.DataFrame()
        pnls = pd.to_numeric(closed.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").dropna()
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        contribution_rows.append(
            {
                "symbol": item.symbol,
                "name": item.name,
                "initial_capital": capital,
                "final_value": final_value,
                "cumulative_contribution": final_value - capital,
                "realized_pnl": realized,
                "unrealized_pnl": final_value - capital - realized - dividends - cash_income,
                "dividend_income": dividends,
                "cash_income": cash_income,
                "commission_cost": commission,
                "slippage_cost": slippage,
                "trade_count": len(symbol_trades),
                "closed_trade_count": len(pnls),
                "win_rate_pct": float((pnls > 0).mean() * 100) if len(pnls) else 0.0,
                "average_win": float(wins.mean()) if len(wins) else 0.0,
                "average_loss": float(losses.mean()) if len(losses) else 0.0,
                "profit_loss_ratio": abs(float(wins.mean() / losses.mean())) if len(wins) and len(losses) else np.nan,
                "largest_win": float(pnls.max()) if len(pnls) else 0.0,
                "largest_loss": float(pnls.min()) if len(pnls) else 0.0,
                "max_consecutive_losses": _max_consecutive_losses(pnls),
                "signal_generated_count": signal_count,
                "order_submitted_count": submitted_count,
                "missed_order_count": missed_buy_count + missed_sell_count,
                "missed_buy_count": missed_buy_count,
                "missed_sell_count": missed_sell_count,
                "order_retried_count": retried_count,
                "order_filled_count": filled_count,
                "average_execution_delay_days": delay_total / filled_count if filled_count else 0.0,
            }
        )

    component_daily = pd.concat(component_frames, ignore_index=True)
    wide_value = component_daily.pivot(index="trade_date", columns="symbol", values="component_value").reindex(dates)
    total_value = wide_value.sum(axis=1) + residual_values
    wide_market = component_daily.pivot(index="trade_date", columns="symbol", values="market_value").reindex(dates)
    total_market = wide_market.sum(axis=1)
    daily = pd.DataFrame(
        {
            "trade_date": dates,
            "portfolio_value": total_value.values,
            "etf_market_value": total_market.values,
            "cash_value": (total_value - total_market).values,
            "etf_weight_pct": np.where(total_value > 0, total_market / total_value * 100, 0),
            "cash_weight_pct": np.where(total_value > 0, (total_value - total_market) / total_value * 100, 0),
        }
    )
    symbol_daily_columns: dict[str, object] = {}
    for symbol in wide_value.columns:
        symbol_rows = component_daily[component_daily["symbol"] == symbol].set_index("trade_date").reindex(dates)
        symbol_daily_columns[f"{symbol}_shares"] = symbol_rows["shares"].values
        symbol_daily_columns[f"{symbol}_market_value"] = symbol_rows["market_value"].values
        symbol_daily_columns[f"{symbol}_weight_pct"] = np.where(
            total_value > 0, symbol_rows["market_value"] / total_value * 100, 0
        )
        symbol_daily_columns[f"{symbol}_cash"] = symbol_rows["cash"].values
        symbol_daily_columns[f"{symbol}_signal"] = symbol_rows["signal"].values
        for diagnostic_column in (
            "ma",
            "deviation",
            "sigma_prev",
            "buy_threshold_pct",
            "sell_threshold_pct",
            "upper_trigger_line",
            "lower_trigger_line",
        ):
            symbol_daily_columns[f"{symbol}_{diagnostic_column}"] = symbol_rows[
                diagnostic_column
            ].values
        for event_column in (
            "signal_generated",
            "target_position",
            "actual_position",
            "order_submitted",
            "order_missed",
            "order_retried",
            "order_filled",
            "execution_delay_days",
        ):
            symbol_daily_columns[f"{symbol}_{event_column}"] = symbol_rows[event_column].values
        symbol_daily_columns[f"{symbol}_execution_status"] = symbol_rows["execution_status"].values
    daily = pd.concat([daily, pd.DataFrame(symbol_daily_columns, index=daily.index)], axis=1)

    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    if not trades.empty:
        trades = trades.sort_values(["execution_date", "symbol", "sleeve"]).reset_index(drop=True)
    contribution = pd.DataFrame(contribution_rows)
    if residual_cash:
        contribution = pd.concat(
            [
                contribution,
                pd.DataFrame(
                    [
                        {
                            "symbol": "CASH",
                            "name": "未配置现金",
                            "initial_capital": residual_cash,
                            "final_value": float(residual_values.iloc[-1]),
                            "cumulative_contribution": float(residual_values.iloc[-1] - residual_cash),
                            "cash_income": float(residual_values.iloc[-1] - residual_cash),
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
    summary = _performance_summary(daily, settings.initial_capital)
    summary.update(
        {
            "execution_mode": settings.execution_mode,
            "after_hours_fill_rate": settings.after_hours_fill_rate,
            "slippage_bp": settings.slippage_bp,
            "cash_annual_rate": settings.cash_annual_rate,
            "commission_cost": float(contribution["commission_cost"].fillna(0).sum()),
            "slippage_cost": float(contribution["slippage_cost"].fillna(0).sum()),
            "dividend_income": float(contribution["dividend_income"].fillna(0).sum()),
            "cash_income": float(contribution["cash_income"].fillna(0).sum()),
            "trade_count": len(trades),
            "closed_trade_count": int((trades.get("action") == "sell").sum()) if not trades.empty else 0,
            "average_etf_weight_pct": float(daily["etf_weight_pct"].mean()),
            "missed_signal_rate": settings.missed_signal_rate,
            "missed_order_side": settings.missed_order_side,
            "signal_generated_count": int(component_daily["signal_generated_count"].sum()),
            "order_submitted_count": int(component_daily["order_submitted_count"].sum()),
            "missed_order_count": int(component_daily["order_missed_count"].sum()),
            "missed_buy_count": int(component_daily["missed_buy_count"].sum()),
            "missed_sell_count": int(component_daily["missed_sell_count"].sum()),
            "order_retried_count": int(component_daily["order_retried_count"].sum()),
            "order_filled_count": int(component_daily["order_filled_count"].sum()),
            "average_execution_delay_days": float(
                pd.to_numeric(trades.get("execution_delay_days"), errors="coerce").mean()
            )
            if not trades.empty
            else 0.0,
        }
    )
    return AuditRunResult(summary, daily, trades, contribution, component_daily)


def _common_dates(
    market_data: dict[str, pd.DataFrame],
    allocations: Iterable[AuditAllocation],
    start_date: str | pd.Timestamp | None,
    end_date: str | pd.Timestamp | None,
) -> pd.DatetimeIndex:
    symbols = [item.symbol for item in allocations]
    starts = [pd.to_datetime(market_data[symbol]["trade_date"]).min() for symbol in symbols]
    ends = [pd.to_datetime(market_data[symbol]["trade_date"]).max() for symbol in symbols]
    start = max(starts)
    end = min(ends)
    if start_date is not None:
        start = max(start, pd.Timestamp(start_date).normalize())
    if end_date is not None:
        end = min(end, pd.Timestamp(end_date).normalize())
    dates: pd.DatetimeIndex | None = None
    for symbol in symbols:
        frame_dates = pd.DatetimeIndex(
            pd.to_datetime(market_data[symbol]["trade_date"])
            .loc[lambda value: (value >= start) & (value <= end)]
        )
        dates = frame_dates if dates is None else dates.intersection(frame_dates)
    return (dates if dates is not None else pd.DatetimeIndex([])).sort_values()

def _combine_sleeves(
    item: AuditAllocation,
    parts: list[_SleeveResult],
    dates: pd.DatetimeIndex,
    portfolio_initial_capital: float,
) -> pd.DataFrame:
    if not parts:
        raise ValueError(f"{item.symbol} 没有可执行资金单元。")
    combined = pd.DataFrame(index=dates)
    indexed_parts = [part.daily.set_index("trade_date").reindex(dates) for part in parts]
    for column in ("shares", "cash", "market_value", "sleeve_value", "dividend_income_today"):
        combined[column] = sum(frame[column] for frame in indexed_parts)

    signals = pd.concat(
        [frame[["signal", "execution_status"]].add_suffix(f"_{index}") for index, frame in enumerate(indexed_parts)],
        axis=1,
    )
    combined["signal"] = indexed_parts[-1]["signal"]
    for diagnostic_column in (
        "ma",
        "deviation",
        "sigma_prev",
        "buy_threshold_pct",
        "sell_threshold_pct",
        "upper_trigger_line",
        "lower_trigger_line",
    ):
        combined[diagnostic_column] = indexed_parts[-1][diagnostic_column]
    combined["execution_status"] = signals.filter(like="execution_status_").astype(str).agg("/".join, axis=1)
    for event_column in (
        "signal_generated",
        "order_submitted",
        "order_missed",
        "order_retried",
        "order_filled",
        "missed_buy",
        "missed_sell",
    ):
        event_count = sum(frame[event_column].fillna(False).astype(int) for frame in indexed_parts)
        combined[f"{event_column}_count"] = event_count
        combined[event_column] = event_count > 0
    combined["target_position"] = sum(frame["target_position"] for frame in indexed_parts) / len(indexed_parts)
    combined["actual_position"] = sum(frame["actual_position"] for frame in indexed_parts) / len(indexed_parts)
    delay_values = pd.concat(
        [frame["execution_delay_days"].rename(index) for index, frame in enumerate(indexed_parts)],
        axis=1,
    )
    combined["execution_delay_days"] = delay_values.max(axis=1, skipna=True)
    combined["order_action"] = indexed_parts[-1]["order_action"]
    combined["symbol"] = item.symbol
    combined["name"] = item.name
    combined["component_value"] = combined["sleeve_value"]
    combined["portfolio_weight_pct"] = combined["market_value"] / portfolio_initial_capital * 100
    return combined.reset_index(names="trade_date")


def _cash_series(capital: float, dates: pd.DatetimeIndex, annual_rate: float) -> pd.Series:
    if not len(dates):
        return pd.Series(dtype=float)
    elapsed = (dates - dates[0]).days
    return pd.Series(capital * np.power(1 + annual_rate, elapsed / 365), index=dates, dtype=float)
