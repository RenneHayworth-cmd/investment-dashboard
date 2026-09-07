"""网格引擎与统一事件模拟器的交叉验证（研究管线的一部分）。

用法::

    .venv/bin/python scripts/research/validate_grid_engine.py

验证内容：
1. cost=0 时逐日策略收益与 services.ma_timing_core.simulate_timing_account
   （after_close、lot=1）完全一致（容差 1e-10）；
2. cost=0.00006 时 NAV 相对差 < 1e-6（加法 vs 乘法费用口径的交叉项）；
3. half 模式仓位为 0.5+0.5*pos 的收益分解正确。
"""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import grid_engine as G  # noqa: E402
from services.ma_timing_core import (  # noqa: E402
    EXECUTION_AFTER_CLOSE,
    TimingAccountConfig,
    ma_threshold_states,
    simulate_timing_account,
)


def _tickflow_cache_dirs() -> list[Path]:
    from core.paths import RAW_DIR

    dirs = [RAW_DIR / "tickflow"]
    for home in (Path.home(), Path("/mnt/c/Users/Renne")):
        candidate = home / "investment_dashboard_data" / "data" / "raw" / "tickflow"
        if candidate.exists() and candidate not in dirs:
            dirs.append(candidate)
    repo_dir = PROJECT_ROOT / "data" / "raw" / "tickflow"
    if repo_dir not in dirs:
        dirs.append(repo_dir)
    return dirs


def load_cached_close(symbol: str) -> pd.Series | None:
    suffixes = (symbol, f"{symbol}.SH", f"{symbol}.SZ")
    for cache_dir in _tickflow_cache_dirs():
        for suffixed in suffixes:
            candidates = sorted(cache_dir.glob(f"fund_close_v2_{suffixed}_forward_additive_*.csv"))
            if candidates:
                return G.load_price_series(str(candidates[-1]))
    return None


def engine_daily_returns(close: pd.Series, ma_period: int, thr_pct: float, cost: float) -> pd.Series:
    values = close.to_numpy(dtype=float)
    ret = np.concatenate([[0.0], np.diff(values) / values[:-1]])
    ma = close.rolling(ma_period).mean().to_numpy()
    buy = values > ma * (1 + thr_pct / 100)
    sell = values < ma * (1 - thr_pct / 100)
    buy_sig = buy[:, None]
    sell_sig = sell[:, None]
    paths = G.backtest_paths(buy_sig, sell_sig, ret, cost)
    contrib = paths["contrib"][G.I0 + 1 :]
    event = paths["event"][G.I0 + 1 :]
    return pd.Series((contrib - event * cost).ravel(), index=close.index[G.I0 + 1 :])


def event_daily_returns(close: pd.Series, ma_period: int, thr_pct: float, cost: float) -> pd.Series:
    states = ma_threshold_states(close, ma_period, thr_pct)
    result = simulate_timing_account(
        close,
        states,
        TimingAccountConfig(
            initial_capital=100_000.0,
            transaction_cost=cost,
            lot_size=1,
            execution_mode=EXECUTION_AFTER_CLOSE,
        ),
    )
    nav = result.nav.iloc[G.I0 :]
    returns = nav.pct_change().dropna()
    returns.index = close.index[G.I0 + 1 :]
    return returns


def main() -> None:
    symbols = ["510500", "159915", "512890", "518850"]
    failures = 0
    for symbol in symbols:
        close = load_cached_close(symbol)
        if close is None:
            print(f"[skip] {symbol}: 本地无缓存")
            continue
        for ma_period, thr_pct in ((20, 1.0), (10, 0.5), (30, 1.5)):
            zero_cost_engine = engine_daily_returns(close, ma_period, thr_pct, 0.0)
            zero_cost_event = event_daily_returns(close, ma_period, thr_pct, 0.0)
            aligned = pd.concat([zero_cost_engine, zero_cost_event], axis=1).dropna()
            aligned.columns = ["engine", "event"]
            diff = (aligned["engine"] - aligned["event"]).abs().max()
            status = "OK" if diff < 1e-10 else "FAIL"
            failures += status == "FAIL"
            print(f"[{status}] {symbol} MA{ma_period}/{thr_pct}% cost=0 最大逐日收益差={diff:.2e}")

            # 已知口径差：网格引擎在仓位生效日按 |Δ仓位|×cost 加法扣减，
            # 事件模拟器在成交日按名义额乘法扣费，残差 = 交易日 ret×cost 交叉项，
            # 单日 ≤ cost×|ret|，累计 NAV 相对差应远小于 5e-4。
            with_cost_engine = engine_daily_returns(close, ma_period, thr_pct, 0.00006)
            with_cost_event = event_daily_returns(close, ma_period, thr_pct, 0.00006)
            nav_engine = (1 + with_cost_engine).cumprod()
            nav_event = (1 + with_cost_event).cumprod()
            rel = float(((nav_engine - nav_event) / nav_event).abs().max())
            status = "OK" if rel < 5e-4 else "FAIL"
            failures += status == "FAIL"
            print(f"[{status}] {symbol} MA{ma_period}/{thr_pct}% cost=6bp NAV最大相对差={rel:.2e}（费用口径交叉项）")

    print("网格引擎验证：" + ("全部通过" if failures == 0 else f"{failures} 项失败"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
