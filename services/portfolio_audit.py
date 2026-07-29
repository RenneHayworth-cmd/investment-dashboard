from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import floor
from typing import Iterable

import numpy as np
import pandas as pd


EXECUTION_AFTER_CLOSE = "after_close"
EXECUTION_NEXT_OPEN = "next_open"
EXECUTION_NEXT_CLOSE = "next_close"
EXECUTION_T2_OPEN = "t2_open"
EXECUTION_MODES = (
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_OPEN,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_T2_OPEN,
)
MISSED_ORDER_BOTH = "both"
MISSED_ORDER_BUY = "buy"
MISSED_ORDER_SELL = "sell"
MISSED_ORDER_NONE = "none"
MISSED_ORDER_SIDES = (
    MISSED_ORDER_BOTH,
    MISSED_ORDER_BUY,
    MISSED_ORDER_SELL,
    MISSED_ORDER_NONE,
)


@dataclass(frozen=True)
class AuditAllocation:
    symbol: str
    name: str
    weight_pct: float
    strategy: str
    ma_period: int = 20
    threshold_pct: float = 1.0
    signal_rule: str = "percent"
    atr_k: float = 0.0


@dataclass(frozen=True)
class AuditSettings:
    initial_capital: float = 100000.0
    commission_rate: float = 0.00006
    lot_size: int = 100
    execution_mode: str = EXECUTION_AFTER_CLOSE
    after_hours_fill_rate: float = 1.0
    slippage_bp: float = 0.0
    cash_annual_rate: float = 0.0
    random_seed: int = 20260727
    missed_signal_rate: float = 0.0
    missed_order_side: str = MISSED_ORDER_BOTH
    start_date: str | pd.Timestamp | None = None
    end_date: str | pd.Timestamp | None = None


@dataclass
class AuditRunResult:
    summary: dict[str, object]
    daily: pd.DataFrame
    trades: pd.DataFrame
    contribution: pd.DataFrame
    component_daily: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class _SleeveResult:
    daily: pd.DataFrame
    trades: pd.DataFrame
    summary: dict[str, float]


def normalize_audit_market_data(
    raw_df: pd.DataFrame,
    adjusted_df: pd.DataFrame,
    dividend_df: pd.DataFrame | None = None,
    share_split_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align raw OHLC with qfq signals and an explicit distribution ledger."""
    raw = _normalize_price_frame(raw_df, "raw")
    adjusted = _normalize_price_frame(adjusted_df, "adjusted")
    data = raw.merge(
        adjusted[["trade_date", "open", "close", "high", "low"]],
        on="trade_date",
        how="inner",
        suffixes=("_raw", "_adjusted"),
    )
    if data.empty:
        raise ValueError("未复权与前复权行情没有共同交易日。")
    data = data.sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)
    data = data.rename(
        columns={
            "open_raw": "raw_open",
            "close_raw": "raw_close",
            "high_raw": "raw_high",
            "low_raw": "raw_low",
            "open_adjusted": "signal_open",
            "close_adjusted": "signal_close",
            "high_adjusted": "signal_high",
            "low_adjusted": "signal_low",
        }
    )
    data["adjustment_factor"] = data["signal_close"] / data["raw_close"]
    data["dividend_per_share"] = 0.0
    data["share_split_ratio"] = 1.0
    data["share_split_rounding"] = ""
    data["share_split_source"] = ""
    data["corporate_action_status"] = "无调整事件"
    if dividend_df is not None and not dividend_df.empty:
        dividends = dividend_df.copy()
        dividends = dividends.rename(
            columns={
                "除息日期": "trade_date",
                "date": "trade_date",
                "分红": "dividend_per_share",
                "dividend": "dividend_per_share",
            }
        )
        if {"trade_date", "dividend_per_share"}.issubset(dividends.columns):
            dividends["trade_date"] = pd.to_datetime(dividends["trade_date"], errors="coerce").dt.normalize()
            dividends["dividend_per_share"] = pd.to_numeric(dividends["dividend_per_share"], errors="coerce")
            dividends = dividends.dropna(subset=["trade_date", "dividend_per_share"])
            dividend_map = dividends.groupby("trade_date")["dividend_per_share"].sum()
            data["dividend_per_share"] = data["trade_date"].map(dividend_map).fillna(0.0)
            data.loc[data["dividend_per_share"] > 0, "corporate_action_status"] = "官方现金分红"
    if share_split_df is not None and not share_split_df.empty:
        splits = share_split_df.copy().rename(
            columns={"date": "effective_date", "split_ratio": "ratio"}
        )
        required = {"effective_date", "ratio"}
        if not required.issubset(splits.columns):
            raise ValueError("份额折算配置缺少 effective_date 或 ratio。")
        splits["effective_date"] = pd.to_datetime(
            splits["effective_date"], errors="coerce"
        ).dt.normalize()
        splits["ratio"] = pd.to_numeric(splits["ratio"], errors="coerce")
        splits = splits.dropna(subset=["effective_date", "ratio"])
        if (splits["ratio"] <= 0).any():
            raise ValueError("份额折算比例必须大于0。")
        for split in splits.itertuples(index=False):
            mask = data["trade_date"] == split.effective_date
            if not mask.any():
                continue
            rounding = str(getattr(split, "rounding", "floor") or "floor")
            if rounding not in {"floor", "ceil", "round"}:
                raise ValueError(f"不支持的份额折算取整方式：{rounding}")
            data.loc[mask, "share_split_ratio"] = float(split.ratio)
            data.loc[mask, "share_split_rounding"] = rounding
            data.loc[mask, "share_split_source"] = str(
                getattr(split, "source", "官方基金公告") or "官方基金公告"
            )
            dividend_mask = mask & (data["corporate_action_status"] == "官方现金分红")
            data.loc[mask, "corporate_action_status"] = "官方份额折算"
            data.loc[dividend_mask, "corporate_action_status"] = "官方现金分红+份额折算"
    audit = data.loc[
        data["corporate_action_status"] != "无调整事件",
        [
            "trade_date",
            "raw_close",
            "signal_close",
            "adjustment_factor",
            "dividend_per_share",
            "share_split_ratio",
            "share_split_rounding",
            "share_split_source",
            "corporate_action_status",
        ],
    ].copy()
    return data, audit


def _normalize_price_frame(df: pd.DataFrame, label: str) -> pd.DataFrame:
    if df is None or df.empty:
        raise ValueError(f"{label} 行情为空。")
    data = df.copy()
    aliases = {
        "日期": "trade_date",
        "date": "trade_date",
        "开盘价": "open",
        "开盘": "open",
        "收盘价": "close",
        "收盘": "close",
        "最高价": "high",
        "最高": "high",
        "最低价": "low",
        "最低": "low",
        "成交额": "amount",
    }
    data = data.rename(columns={column: aliases.get(str(column).strip(), str(column).strip()) for column in data.columns})
    required = {"trade_date", "open", "close"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"{label} 行情缺少字段：{'、'.join(missing)}")
    if "high" not in data:
        data["high"] = data[["open", "close"]].max(axis=1)
    if "low" not in data:
        data["low"] = data[["open", "close"]].min(axis=1)
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.normalize()
    for column in ("open", "close", "high", "low"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return (
        data.dropna(subset=["trade_date", "open", "close"])
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )


def validate_audit_inputs(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings,
) -> None:
    if settings.initial_capital <= 0:
        raise ValueError("初始资金必须大于0。")
    if settings.commission_rate < 0 or settings.slippage_bp < 0:
        raise ValueError("佣金和滑点不能为负数。")
    if settings.lot_size < 1:
        raise ValueError("交易单位必须大于0。")
    if settings.execution_mode not in EXECUTION_MODES:
        raise ValueError(f"不支持的成交模式：{settings.execution_mode}")
    if not 0 <= settings.after_hours_fill_rate <= 1:
        raise ValueError("盘后成交率必须在0到1之间。")
    if not 0 <= settings.missed_signal_rate <= 1:
        raise ValueError("漏单率必须在0到1之间。")
    total_weight = sum(item.weight_pct for item in allocations)
    if settings.missed_order_side not in MISSED_ORDER_SIDES:
        raise ValueError(f"不支持的漏单方向：{settings.missed_order_side}")
    if total_weight > 100 + 1e-8:
        raise ValueError("配置比例合计不能超过100%。")
    symbols = [item.symbol for item in allocations]
    if len(symbols) != len(set(symbols)):
        raise ValueError("同一ETF只能配置一次。")
    missing = [symbol for symbol in symbols if symbol not in market_data]
    if missing:
        raise ValueError(f"缺少行情：{'、'.join(missing)}")
    required = {"trade_date", "signal_close", "raw_open", "raw_close", "dividend_per_share"}
    for symbol in symbols:
        absent = required - set(market_data[symbol].columns)
        if absent:
            raise ValueError(f"{symbol} 缺少审计字段：{'、'.join(sorted(absent))}")


def run_portfolio_audit(
    market_data: dict[str, pd.DataFrame],
    allocations: list[AuditAllocation],
    settings: AuditSettings | None = None,
    *,
    blocked_entries: set[tuple[str, pd.Timestamp]] | None = None,
) -> AuditRunResult:
    settings = settings or AuditSettings()
    validate_audit_inputs(market_data, allocations, settings)
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
    summary = calculate_performance_summary(daily, settings.initial_capital)
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


def _run_sleeve(
    allocation: AuditAllocation,
    source: pd.DataFrame,
    dates: pd.DatetimeIndex,
    initial_capital: float,
    settings: AuditSettings,
    *,
    always_hold: bool,
    blocked_entries: set[tuple[str, pd.Timestamp]],
) -> _SleeveResult:
    data = source.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"]).dt.normalize()
    data = data.sort_values("trade_date").drop_duplicates("trade_date").set_index("trade_date")
    ma_period = allocation.ma_period
    data["ma"] = data["signal_close"].rolling(ma_period, min_periods=ma_period).mean()
    previous_close = data["signal_close"].shift(1)
    true_range = pd.concat(
        [
            data["signal_high"] - data["signal_low"],
            (data["signal_high"] - previous_close).abs(),
            (data["signal_low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr20"] = true_range.rolling(20, min_periods=20).mean()
    threshold = allocation.threshold_pct / 100
    rng = np.random.default_rng(settings.random_seed)
    cash = float(initial_capital)
    shares = 0.0
    cost_basis = 0.0
    trade_dividends = 0.0
    pending: dict[str, object] | None = None
    blocked_desired: int | None = None
    retry_action: str | None = None
    retry_signal_date: pd.Timestamp | None = None
    retry_origin_index: int | None = None
    retry_count = 0
    previous_desired = 0
    previous_date: pd.Timestamp | None = None
    commission_total = 0.0
    slippage_total = 0.0
    dividend_total = 0.0
    cash_income_total = 0.0
    realized_total = 0.0
    signal_generated_count = 0
    order_submitted_count = 0
    missed_buy_count = 0
    missed_sell_count = 0
    order_retried_count = 0
    order_filled_count = 0
    execution_delays: list[int] = []
    rows: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    sleeve_name = "长期" if always_hold else "择时"

    def record_trade(
        trade: dict[str, object] | None,
        costs: tuple[float, float],
        *,
        signal_price: float,
        submission_date: pd.Timestamp,
        was_retried: bool,
        delay_days: int,
    ) -> bool:
        nonlocal commission_total, slippage_total, realized_total, order_filled_count
        if trade is None:
            return False
        trade["signal_price"] = signal_price
        trade["submission_date"] = submission_date
        trade["order_retried"] = was_retried
        trade["execution_delay_days"] = delay_days
        trades.append(trade)
        commission_total += costs[0]
        slippage_total += costs[1]
        realized_value = trade.get("realized_pnl")
        realized_total += float(realized_value) if pd.notna(realized_value) else 0.0
        order_filled_count += 1
        execution_delays.append(delay_days)
        return True

    for date_index, trade_date in enumerate(dates):
        row = data.loc[trade_date]
        if previous_date is not None and cash > 0 and settings.cash_annual_rate:
            days = max(0, (trade_date - previous_date).days)
            income = cash * ((1 + settings.cash_annual_rate) ** (days / 365) - 1)
            cash += income
            cash_income_total += income
        shares_before_split = shares
        split_ratio = float(row.get("share_split_ratio", 1.0) or 1.0)
        if shares > 0 and not np.isclose(split_ratio, 1.0):
            split_shares = shares * split_ratio
            split_rounding = str(row.get("share_split_rounding", "floor") or "floor")
            if split_rounding == "ceil":
                shares = float(np.ceil(split_shares - 1e-12))
            elif split_rounding == "round":
                shares = float(np.rint(split_shares))
            else:
                shares = float(np.floor(split_shares + 1e-12))
        shares_after_split = shares
        dividend = float(row.get("dividend_per_share", 0) or 0) * shares
        if dividend > 0:
            cash += dividend
            dividend_total += dividend
            trade_dividends += dividend

        ma = float(row["ma"]) if pd.notna(row["ma"]) else np.nan
        signal_price = float(row["signal_close"])
        if always_hold:
            desired = 1
            signal = "持有"
        elif np.isnan(ma):
            desired = previous_desired
            signal = "等待均线"
        else:
            if allocation.signal_rule == "atr":
                atr = float(row["atr20"]) if pd.notna(row["atr20"]) else np.nan
                upper_line = ma + allocation.atr_k * atr
                lower_line = ma - allocation.atr_k * atr
            else:
                upper_line = ma * (1 + threshold)
                lower_line = ma * (1 - threshold)
            if np.isnan(upper_line) or np.isnan(lower_line):
                desired = previous_desired
                signal = "等待均线"
            elif signal_price > upper_line:
                desired = 1
                signal = "持有"
            elif signal_price < lower_line:
                desired = 0
                signal = "空仓"
            else:
                desired = previous_desired
                signal = "维持"

        signal_generated = desired != previous_desired
        signal_generated_count += int(signal_generated)
        target_position = float(desired)
        execution_status = "无成交"
        order_submitted = False
        order_missed = False
        order_retried = False
        order_filled = False
        execution_delay_days = np.nan

        if blocked_desired is not None and desired != blocked_desired:
            blocked_desired = None

        if pending is not None:
            pending_action = str(pending["action"])
            pending_matches_target = (pending_action == "buy" and desired == 1) or (
                pending_action == "sell" and desired == 0
            )
            if not pending_matches_target:
                execution_status = "目标仓位变化，取消旧方向待执行订单"
                pending = None
                retry_action = None
                retry_signal_date = None
                retry_origin_index = None
                retry_count = 0

        if pending is not None and _pending_due(pending, date_index, settings.execution_mode):
            price_kind = str(pending["price_kind"])
            raw_price = float(row["raw_open"] if price_kind == "open" else row["raw_close"])
            cash, shares, cost_basis, trade_dividends, trade, costs = _execute_order(
                allocation,
                sleeve_name,
                str(pending["action"]),
                pd.Timestamp(pending["signal_date"]),
                trade_date,
                raw_price,
                cash,
                shares,
                cost_basis,
                trade_dividends,
                settings,
                f"顺延至{price_kind}",
            )
            delay_days = date_index - int(pending["origin_index"])
            order_retried = bool(pending.get("order_retried", False))
            order_filled = record_trade(
                trade,
                costs,
                signal_price=float(data.loc[pd.Timestamp(pending["signal_date"]), "signal_close"]),
                submission_date=pd.Timestamp(pending["submission_date"]),
                was_retried=order_retried,
                delay_days=delay_days,
            )
            if order_filled:
                execution_status = "顺延成交"
                execution_delay_days = delay_days
                retry_action = None
                retry_signal_date = None
                retry_origin_index = None
                retry_count = 0
            else:
                execution_status = "顺延未成交，继续纠偏"
                retry_action = str(pending["action"])
                retry_signal_date = pd.Timestamp(pending["signal_date"])
                retry_origin_index = int(pending["origin_index"])
                retry_count = max(1, int(pending.get("retry_count", 0)))
            pending = None

        actual_state = int(shares > 0)
        needed_action = "buy" if desired == 1 and actual_state == 0 else "sell" if desired == 0 and actual_state == 1 else None
        if retry_action is not None and retry_action != needed_action:
            retry_action = None
            retry_signal_date = None
            retry_origin_index = None
            retry_count = 0

        if needed_action is not None and pending is None:
            block_key = (allocation.symbol, pd.Timestamp(trade_date).normalize())
            blocked = needed_action == "buy" and block_key in blocked_entries
            if blocked_desired == desired or blocked:
                blocked_desired = desired
                execution_status = "整笔交易已剔除" if blocked else "剔除交易信号已抑制"
            else:
                order_submitted = True
                order_submitted_count += 1
                order_retried = retry_action == needed_action and retry_count > 0
                order_retried_count += int(order_retried)
                origin_date = retry_signal_date if order_retried else trade_date
                origin_index = retry_origin_index if order_retried else date_index
                can_miss = settings.missed_order_side in (MISSED_ORDER_BOTH, needed_action)
                missed = (
                    settings.missed_signal_rate > 0
                    and can_miss
                    and rng.random() < settings.missed_signal_rate
                )
                if missed:
                    order_missed = True
                    if needed_action == "buy":
                        missed_buy_count += 1
                    else:
                        missed_sell_count += 1
                    retry_action = needed_action
                    retry_signal_date = pd.Timestamp(origin_date)
                    retry_origin_index = int(origin_index)
                    retry_count += 1
                    execution_status = "买入漏单，下一交易日继续纠偏" if needed_action == "buy" else "卖出漏单，下一交易日继续纠偏"
                elif settings.execution_mode == EXECUTION_AFTER_CLOSE:
                    filled = settings.after_hours_fill_rate >= 1 or rng.random() < settings.after_hours_fill_rate
                    if filled:
                        cash, shares, cost_basis, trade_dividends, trade, costs = _execute_order(
                            allocation,
                            sleeve_name,
                            needed_action,
                            pd.Timestamp(origin_date),
                            trade_date,
                            float(row["raw_close"]),
                            cash,
                            shares,
                            cost_basis,
                            trade_dividends,
                            settings,
                            "盘后收盘价成交",
                        )
                        delay_days = date_index - int(origin_index)
                        order_filled = record_trade(
                            trade,
                            costs,
                            signal_price=float(data.loc[pd.Timestamp(origin_date), "signal_close"]),
                            submission_date=trade_date,
                            was_retried=order_retried,
                            delay_days=delay_days,
                        )
                        if order_filled:
                            execution_status = "盘后成交"
                            execution_delay_days = delay_days
                            retry_action = None
                            retry_signal_date = None
                            retry_origin_index = None
                            retry_count = 0
                        else:
                            execution_status = "当日未成交，下一交易日继续纠偏"
                            retry_action = needed_action
                            retry_signal_date = pd.Timestamp(origin_date)
                            retry_origin_index = int(origin_index)
                            retry_count = max(1, retry_count)
                    else:
                        pending = {
                            "action": needed_action,
                            "signal_date": pd.Timestamp(origin_date),
                            "submission_date": trade_date,
                            "origin_index": int(origin_index),
                            "due_index": date_index + 1,
                            "price_kind": "open",
                            "order_retried": order_retried,
                            "retry_count": retry_count,
                        }
                        retry_action = None
                        retry_signal_date = None
                        retry_origin_index = None
                        retry_count = 0
                        execution_status = "盘后未成交，顺延开盘"
                else:
                    delay = 1 if settings.execution_mode in (EXECUTION_NEXT_OPEN, EXECUTION_NEXT_CLOSE) else 2
                    pending = {
                        "action": needed_action,
                        "signal_date": pd.Timestamp(origin_date),
                        "submission_date": trade_date,
                        "origin_index": int(origin_index),
                        "due_index": date_index + delay,
                        "price_kind": "close" if settings.execution_mode == EXECUTION_NEXT_CLOSE else "open",
                        "order_retried": order_retried,
                        "retry_count": retry_count,
                    }
                    retry_action = None
                    retry_signal_date = None
                    retry_origin_index = None
                    retry_count = 0
                    execution_status = "待顺延成交"

        actual_position = float(shares > 0)
        value = cash + shares * float(row["raw_close"])
        rows.append(
            {
                "trade_date": trade_date,
                "symbol": allocation.symbol,
                "sleeve": sleeve_name,
                "signal_price": signal_price,
                "raw_close": float(row["raw_close"]),
                "valuation_price": float(row["raw_close"]),
                "ma": ma,
                "signal": signal,
                "signal_generated": bool(signal_generated),
                "target_position": target_position,
                "actual_position": actual_position,
                "order_submitted": bool(order_submitted),
                "order_missed": bool(order_missed),
                "order_retried": bool(order_retried),
                "order_filled": bool(order_filled),
                "order_action": needed_action if order_submitted else "",
                "missed_buy": bool(order_missed and needed_action == "buy"),
                "missed_sell": bool(order_missed and needed_action == "sell"),
                "execution_delay_days": execution_delay_days,
                "execution_status": execution_status,
                "share_split_ratio": split_ratio,
                "shares_before_split": shares_before_split,
                "shares_after_split": shares_after_split,
                "shares": shares,
                "cash": cash,
                "market_value": shares * float(row["raw_close"]),
                "sleeve_value": value,
                "dividend_income_today": dividend,
            }
        )
        previous_desired = desired
        previous_date = trade_date

    return _SleeveResult(
        daily=pd.DataFrame(rows),
        trades=pd.DataFrame(trades),
        summary={
            "commission_cost": commission_total,
            "slippage_cost": slippage_total,
            "dividend_income": dividend_total,
            "cash_income": cash_income_total,
            "realized_pnl": realized_total,
            "signal_generated_count": signal_generated_count,
            "order_submitted_count": order_submitted_count,
            "missed_order_count": missed_buy_count + missed_sell_count,
            "missed_buy_count": missed_buy_count,
            "missed_sell_count": missed_sell_count,
            "order_retried_count": order_retried_count,
            "order_filled_count": order_filled_count,
            "average_execution_delay_days": float(np.mean(execution_delays)) if execution_delays else 0.0,
        },
    )


def _pending_due(pending: dict[str, object], date_index: int, execution_mode: str) -> bool:
    del execution_mode
    return date_index >= int(pending["due_index"])


def _execute_order(
    allocation: AuditAllocation,
    sleeve_name: str,
    action: str,
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    raw_price: float,
    cash: float,
    shares: float,
    cost_basis: float,
    trade_dividends: float,
    settings: AuditSettings,
    status: str,
) -> tuple[float, float, float, float, dict[str, object] | None, tuple[float, float]]:
    if not np.isfinite(raw_price) or raw_price <= 0:
        return cash, shares, cost_basis, trade_dividends, None, (0.0, 0.0)
    slip = settings.slippage_bp / 10000
    execution_price = raw_price * (1 + slip if action == "buy" else 1 - slip)
    if action == "buy":
        affordable = cash / (execution_price * (1 + settings.commission_rate))
        buy_shares = floor(affordable / settings.lot_size) * settings.lot_size
        if buy_shares <= 0:
            return cash, shares, cost_basis, trade_dividends, None, (0.0, 0.0)
        gross = buy_shares * execution_price
        commission = gross * settings.commission_rate
        slippage_cost = buy_shares * max(0.0, execution_price - raw_price)
        cash -= gross + commission
        shares = float(buy_shares)
        cost_basis = gross + commission
        trade_dividends = 0.0
        realized_pnl = np.nan
        realized_return = np.nan
    else:
        if shares <= 0:
            return cash, shares, cost_basis, trade_dividends, None, (0.0, 0.0)
        gross = shares * execution_price
        commission = gross * settings.commission_rate
        slippage_cost = shares * max(0.0, raw_price - execution_price)
        proceeds = gross - commission
        cash += proceeds
        realized_pnl = proceeds + trade_dividends - cost_basis
        realized_return = realized_pnl / cost_basis * 100 if cost_basis else 0.0
        sold_shares = shares
        shares = 0.0
        cost_basis = 0.0
    trade = {
        "symbol": allocation.symbol,
        "name": allocation.name,
        "sleeve": sleeve_name,
        "signal_date": signal_date,
        "execution_date": execution_date,
        "action": action,
        "signal_price": np.nan,
        "raw_reference_price": raw_price,
        "execution_price": execution_price,
        "shares": float(buy_shares if action == "buy" else sold_shares),
        "gross_amount": gross,
        "commission": commission,
        "slippage_cost": slippage_cost,
        "dividend_during_trade": trade_dividends if action == "sell" else 0.0,
        "realized_pnl": realized_pnl,
        "realized_return_pct": realized_return,
        "execution_status": status,
        "cash_after": cash,
    }
    if action == "sell":
        trade_dividends = 0.0
    return cash, shares, cost_basis, trade_dividends, trade, (commission, slippage_cost)


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
