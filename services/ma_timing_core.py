"""均线阈值择时统一框架。

本项目的"收盘价相对 MA 上/下带 ±阈值"滞回状态机此前有 4 份并行实现
（回测页单标的择时、组合择时信号帧、持仓页择时快照、年度动态组合选参），
指标计算另有 8 处重复。本模块把信号定义与账户模拟收敛为唯一定义：

- :func:`ma_threshold_states`：唯一的信号状态机定义（初始空仓，MA 不足时保持原状态）。
- :func:`ma_threshold_transitions`：从状态序列推导状态转换表。
- :func:`simulate_timing_account`：参考账户模拟器，显式区分
  ``after_close``（当日收盘信号/当日收盘成交）、``next_open``（前收盘信号/次日开盘成交）
  与 ``next_close``（前收盘信号/次日收盘成交）三种执行口径，并支持滑点与手续费。

口径说明（与 AGENTS.md 一致）：页面回测的 ``after_close`` 依赖盘后固定价机制
（2026-07-06 起可用），更早历史属于"当前规则模拟"；研究评估必须同时报告
``next_open``/``next_close`` 的保守口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from core.metrics import (
    annual_volatility,
    annualized_return,
    calmar_ratio,
    max_drawdown,
    longest_underwater_days,
    seeded_returns,
    sharpe_ratio,
    total_return,
    trade_win_stats,
)

EXECUTION_AFTER_CLOSE = "after_close"
EXECUTION_NEXT_OPEN = "next_open"
EXECUTION_NEXT_CLOSE = "next_close"
EXECUTION_MODES = (EXECUTION_AFTER_CLOSE, EXECUTION_NEXT_OPEN, EXECUTION_NEXT_CLOSE)
ExecutionMode = Literal["after_close", "next_open", "next_close"]

DEFAULT_LOT_SIZE = 100


def validate_timing_parameters(ma_period: int, threshold_pct: float) -> None:
    if ma_period < 1:
        raise ValueError("均线周期必须大于 0。")
    if threshold_pct < 0:
        raise ValueError("触发阈值不能为负数。")


def ma_threshold_series(
    close: pd.Series,
    ma_period: int,
    threshold_pct: float,
) -> pd.DataFrame:
    """计算均线与买/卖触发线。指数复行（不足窗口）为 NaN。"""
    validate_timing_parameters(ma_period, threshold_pct)
    prices = pd.to_numeric(pd.Series(close), errors="coerce")
    ma = prices.rolling(window=int(ma_period)).mean()
    threshold = float(threshold_pct) / 100
    return pd.DataFrame(
        {
            "close": prices,
            "ma": ma,
            "buy_line": ma * (1 + threshold),
            "sell_line": ma * (1 - threshold),
        }
    )


def ma_threshold_states(
    close: pd.Series,
    ma_period: int,
    threshold_pct: float,
    initial_state: int = 0,
) -> pd.Series:
    """唯一的状态机定义。

    close 高于 MA*(1+阈值) → 目标仓位 1；低于 MA*(1-阈值) → 目标仓位 0；
    带内或均线未形成 → 维持原状态。返回与 close 同索引的 0/1 序列。
    """
    validate_timing_parameters(ma_period, threshold_pct)
    frame = ma_threshold_series(close, ma_period, threshold_pct)
    states = np.empty(len(frame), dtype=np.int64)
    state = int(initial_state)
    close_values = frame["close"].to_numpy(dtype=float)
    buy_values = frame["buy_line"].to_numpy(dtype=float)
    sell_values = frame["sell_line"].to_numpy(dtype=float)
    for index in range(len(frame)):
        buy_line = buy_values[index]
        if np.isfinite(buy_line) and np.isfinite(close_values[index]):
            if close_values[index] > buy_line:
                state = 1
            elif np.isfinite(sell_values[index]) and close_values[index] < sell_values[index]:
                state = 0
        states[index] = state
    return pd.Series(states, index=pd.Series(close).index, name="state")


def threshold_desired_position(
    close_price: float,
    buy_line: float,
    sell_line: float,
    current_position: int,
) -> int:
    """账户自愈语义的阈值决策：带内维持*实际*仓位。

    与 :func:`ma_threshold_states`（纯信号状态机，带内维持信号状态）不同：
    本函数以上一日的实际持仓为带内锚点，用于账户模拟——信号日买入失败
    （现金不足以买一手）时，不会在带内持续重试，而是等下一次有效穿带再试。
    单标的回测 run_ma20_timing_backtest 使用本语义。
    """
    if close_price > buy_line:
        return 1
    if close_price < sell_line:
        return 0
    return int(current_position)


def ma_threshold_transitions(
    states: pd.Series,
    close: pd.Series | None = None,
    initial_state: int = 0,
) -> pd.DataFrame:
    """状态转换表：每次 0↔1 切换一行，含转换日收盘价。

    首个有效状态若与初始状态不同同样记为一次转换（用于从空仓起步的口径）。
    """
    state_values = pd.to_numeric(pd.Series(states), errors="coerce").to_numpy(dtype=float)
    price_values = (
        pd.to_numeric(pd.Series(close), errors="coerce").to_numpy(dtype=float)
        if close is not None
        else np.full(len(state_values), np.nan)
    )
    rows: list[dict[str, object]] = []
    previous = int(initial_state)
    for index, raw_state in enumerate(state_values):
        if not np.isfinite(raw_state):
            continue
        state = int(raw_state)
        if state != previous:
            rows.append(
                {
                    "转换日期": pd.Series(states).index[index],
                    "目标状态": state,
                    "转换价": float(price_values[index]) if np.isfinite(price_values[index]) else np.nan,
                }
            )
            previous = state
    return pd.DataFrame(rows, columns=["转换日期", "目标状态", "转换价"])


@dataclass(frozen=True)
class TimingAccountConfig:
    """账户模拟参数。transaction_cost 为单边比例费率（如 0.00006 = 万0.6）。"""

    initial_capital: float = 100_000.0
    transaction_cost: float = 0.00006
    lot_size: int = DEFAULT_LOT_SIZE
    slippage: float = 0.0
    execution_mode: ExecutionMode = EXECUTION_AFTER_CLOSE

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("初始资金必须大于 0。")
        if self.transaction_cost < 0:
            raise ValueError("交易成本不能为负数。")
        if self.lot_size < 1:
            raise ValueError("交易单位必须大于 0。")
        if self.slippage < 0:
            raise ValueError("滑点不能为负数。")
        if self.execution_mode not in EXECUTION_MODES:
            raise ValueError(f"不支持的执行口径：{self.execution_mode}")


@dataclass
class TimingAccountResult:
    nav: pd.Series
    cash: pd.Series
    shares: pd.Series
    initial_capital: float
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    total_buy_cost: float = 0.0
    total_sell_cost: float = 0.0
    realized_trade_pnls: list[float] = field(default_factory=list)

    def metrics(self, risk_free_annual: float = 0.0) -> dict[str, float]:
        """标准指标卡：总收益/年化/波动/Sharpe/最大回撤/最长水下期(自然日)。"""
        nav = self.nav.dropna()
        if nav.empty:
            return {}
        calendar_days = max(
            int((pd.Timestamp(nav.index[-1]) - pd.Timestamp(nav.index[0])).days), 1
        )
        total_ret = total_return(nav, self.initial_capital)
        returns = seeded_returns(nav, self.initial_capital)
        annual = annualized_return(total_ret, calendar_days)
        mdd = max_drawdown(nav, initial_capital=self.initial_capital)
        return {
            "total_return_pct": round(total_ret * 100, 4),
            "annual_return_pct": round(annual * 100, 4),
            "annual_volatility_pct": round(annual_volatility(returns) * 100, 4),
            "sharpe_ratio": round(sharpe_ratio(returns, risk_free_annual=risk_free_annual), 4),
            "max_drawdown_pct": round(mdd, 4),
            "longest_underwater_days": longest_underwater_days(
                nav, dates=nav.index, initial_capital=self.initial_capital
            ),
            "calmar_ratio": round(calmar_ratio(annual * 100, mdd), 4)
            if mdd < 0
            else float("nan"),
            "calendar_days": calendar_days,
            "trading_days": int(len(nav)),
        }


def simulate_timing_account(
    close: pd.Series,
    states: pd.Series,
    config: TimingAccountConfig,
    open_: pd.Series | None = None,
) -> TimingAccountResult:
    """参考账户模拟器（满仓/空仓二态，整手，单边费率，可选滑点）。

    执行口径：
    - ``after_close``：第 t 日收盘信号在当日收盘成交；
    - ``next_open``：第 t 日收盘信号在次日开盘成交（含滑点）；
    - ``next_close``：第 t 日收盘信号在次日收盘成交。

    停牌/缺失价（NaN）日不成交也不重估，净值沿用最近一次有效收盘估值。
    """
    validate_execution_inputs(close, states)
    prices = pd.to_numeric(pd.Series(close, dtype=float), errors="coerce")
    state_values = pd.to_numeric(pd.Series(states), errors="coerce").reindex(prices.index)
    open_values = (
        pd.to_numeric(pd.Series(open_, dtype=float), errors="coerce").reindex(prices.index)
        if open_ is not None
        else prices
    )

    mode = config.execution_mode
    cash = float(config.initial_capital)
    shares = 0.0
    cost_basis = 0.0
    total_buy_cost = 0.0
    total_sell_cost = 0.0
    realized_pnls: list[float] = []
    trade_rows: list[dict[str, object]] = []
    nav_values: list[float] = []
    cash_values: list[float] = []
    share_values: list[float] = []
    last_valid_close = float("nan")

    for index, trade_date in enumerate(prices.index):
        close_price = float(prices.loc[trade_date]) if pd.notna(prices.loc[trade_date]) else float("nan")
        previous_state_known = index > 0 and pd.notna(state_values.iloc[index - 1])
        current_state = state_values.iloc[index]

        execution_price = float("nan")
        target_state: float | None = None
        if mode == EXECUTION_AFTER_CLOSE:
            if pd.notna(current_state):
                target_state = float(current_state)
                execution_price = close_price
        elif mode == EXECUTION_NEXT_OPEN:
            if previous_state_known:
                target_state = float(state_values.iloc[index - 1])
                execution_price = float(open_values.loc[trade_date]) if pd.notna(open_values.loc[trade_date]) else float("nan")
        else:  # next_close
            if previous_state_known:
                target_state = float(state_values.iloc[index - 1])
                execution_price = close_price

        if target_state is not None and np.isfinite(execution_price) and execution_price > 0:
            if target_state >= 1 and shares <= 0:
                fill_price = execution_price * (1 + config.slippage)
                affordable = cash / (fill_price * (1 + config.transaction_cost)) if fill_price > 0 else 0.0
                buy_shares = _round_lot(affordable, config.lot_size)
                if buy_shares > 0:
                    gross = buy_shares * fill_price
                    fee = gross * config.transaction_cost
                    cash -= gross + fee
                    shares = buy_shares
                    cost_basis = gross + fee
                    total_buy_cost += fee
                    trade_rows.append(_trade_row(trade_date, "买入", fill_price, buy_shares, gross, fee, cash))
            elif target_state <= 0 and shares > 0:
                fill_price = execution_price * (1 - config.slippage)
                gross = shares * fill_price
                fee = gross * config.transaction_cost
                net = gross - fee
                realized = net - cost_basis
                cash += net
                total_sell_cost += fee
                realized_pnls.append(realized)
                trade_rows.append(_trade_row(trade_date, "卖出", fill_price, shares, gross, fee, cash, realized))
                shares = 0.0
                cost_basis = 0.0

        if np.isfinite(close_price) and close_price > 0:
            last_valid_close = close_price
        marked_close = last_valid_close if np.isfinite(last_valid_close) else float("nan")
        nav_values.append(cash + shares * marked_close if np.isfinite(marked_close) else np.nan)
        cash_values.append(cash)
        share_values.append(shares)

    result = TimingAccountResult(
        nav=pd.Series(nav_values, index=prices.index, name="账户净值"),
        cash=pd.Series(cash_values, index=prices.index, name="现金余额"),
        shares=pd.Series(share_values, index=prices.index, name="持仓份额"),
        initial_capital=float(config.initial_capital),
        trades=pd.DataFrame(trade_rows),
        total_buy_cost=total_buy_cost,
        total_sell_cost=total_sell_cost,
        realized_trade_pnls=realized_pnls,
    )
    return result


def validate_execution_inputs(close: pd.Series, states: pd.Series) -> None:
    if close is None or states is None:
        raise ValueError("close 与 states 不能为空。")
    if len(close) == 0:
        raise ValueError("价格序列为空。")


def _round_lot(shares: float, lot_size: int) -> float:
    if shares <= 0:
        return 0.0
    if lot_size <= 1:
        return float(shares)
    return float(int(shares // lot_size) * lot_size)


def _trade_row(
    trade_date: object,
    action: str,
    price: float,
    shares: float,
    gross: float,
    fee: float,
    cash: float,
    realized: float | None = None,
) -> dict[str, object]:
    return {
        "日期": trade_date,
        "操作": action,
        "成交价": round(price, 4),
        "份额": round(shares, 2),
        "成交金额": round(gross, 2),
        "手续费": round(fee, 2),
        "本次交易盈亏金额": None if realized is None else round(realized, 2),
        "现金余额": round(cash, 2),
    }


__all__ = [
    "EXECUTION_AFTER_CLOSE",
    "EXECUTION_NEXT_CLOSE",
    "EXECUTION_NEXT_OPEN",
    "EXECUTION_MODES",
    "ExecutionMode",
    "TimingAccountConfig",
    "TimingAccountResult",
    "ma_threshold_series",
    "ma_threshold_states",
    "ma_threshold_transitions",
    "simulate_timing_account",
    "threshold_desired_position",
    "validate_timing_parameters",
]
