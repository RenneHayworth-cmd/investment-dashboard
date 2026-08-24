from __future__ import annotations

import pandas as pd

from services.portfolio_audit_models import (
    AuditAllocation,
    AuditSettings,
    EXECUTION_MODES,
    MISSED_ORDER_SIDES,
)


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
    allowed_signal_rules = {"percent", "atr", "sigma", "hybrid_sigma"}
    for item in allocations:
        if item.signal_rule not in allowed_signal_rules:
            raise ValueError(f"{item.symbol} 不支持的信号规则：{item.signal_rule}")
        if item.ma_period < 1:
            raise ValueError(f"{item.symbol} 的均线周期必须大于0。")
        if item.threshold_pct < 0 or item.atr_k < 0:
            raise ValueError(f"{item.symbol} 的固定阈值或ATR系数不能为负数。")
        if item.signal_rule in {"sigma", "hybrid_sigma"}:
            if item.sigma_period < 2:
                raise ValueError(f"{item.symbol} 的σ周期不能小于2。")
            if min(item.buy_k, item.sell_k, item.buy_alpha_pct, item.sell_alpha_pct) < 0:
                raise ValueError(f"{item.symbol} 的σ系数和alpha不能为负数。")
    required = {"trade_date", "signal_close", "raw_open", "raw_close", "dividend_per_share"}
    for symbol in symbols:
        absent = required - set(market_data[symbol].columns)
        if absent:
            raise ValueError(f"{symbol} 缺少审计字段：{'、'.join(sorted(absent))}")
