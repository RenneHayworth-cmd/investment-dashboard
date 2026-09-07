"""均线择时参数网格的向量化回测引擎（研究专用）。

约定与 services.ma_timing_core / core.metrics 对齐：
- 信号在 t 日收盘算出（close 相对 MA*(1±阈值)），仓位变化在 t+1 日生效，
  即信号仓位赚取 t→t+1 的收益（等价于"当日收盘信号、当日收盘成交"的口径）；
- T+1 变体 exec_shift=1：仓位变化再延后一日生效（次日收盘才能成交的保守口径）；
- 成本按单边费率乘换手 |Δ仓位| 在生效日线性扣减；
- 年化用 252 个 A 股交易日（core.metrics 的日历日口径只用于事件模拟器主结果）；
- 评估窗口统一从 MA120 首个有效日（i0=119）之后开始，网格间可公平比较。

本引擎用于参数敏感性网格、成本/滑点压力与蒙特卡洛（信号漏单）批量路径；
标题级结果一律以 services.ma_timing_core.simulate_timing_account 为准。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252
MA_PERIODS = np.arange(5, 121)
I0 = 119  # MA120 首个有效索引（0-based），全组合统一评估起点


def load_price_series(csv_path: str) -> pd.Series:
    df = pd.read_csv(csv_path)
    date_col = "日期" if "日期" in df.columns else "trade_date"
    close_col = "收盘价" if "收盘价" in df.columns else "close"
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col, close_col]).sort_values(date_col)
    df = df.drop_duplicates(date_col, keep="last")
    return pd.Series(
        df[close_col].to_numpy(dtype=float), index=pd.DatetimeIndex(df[date_col])
    )


def load_price_frame(csv_path: str) -> pd.DataFrame:
    """加载日线并返回 (日期索引, close, open)。缺开盘价列时 open=close。"""
    df = pd.read_csv(csv_path)
    date_col = "日期" if "日期" in df.columns else "trade_date"
    close_col = "收盘价" if "收盘价" in df.columns else "close"
    open_col = "开盘价" if "开盘价" in df.columns else ("open" if "open" in df.columns else close_col)
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col, close_col]).sort_values(date_col)
    df = df.drop_duplicates(date_col, keep="last")
    index = pd.DatetimeIndex(df[date_col])
    return (
        pd.Series(df[close_col].to_numpy(dtype=float), index=index),
        pd.Series(df[open_col].to_numpy(dtype=float), index=index),
    )


def ma_matrix(close: np.ndarray, periods: np.ndarray = MA_PERIODS) -> np.ndarray:
    """(T, P) 各周期均线，窗口不足为 NaN。"""
    T = close.shape[0]
    cs = np.concatenate([[0.0], np.cumsum(close)])
    out = np.full((T, periods.shape[0]), np.nan, dtype=np.float64)
    for j, period in enumerate(periods):
        p = int(period)
        if T >= p:
            out[p - 1 :, j] = (cs[p:] - cs[:-p]) / p
    return out


def ffill_positions(raw: np.ndarray) -> np.ndarray:
    """raw (T,K)：1=买入 0=卖出 NaN=保持。返回目标仓位序列（初始 0）。"""
    T, K = raw.shape
    idx = np.arange(T, dtype=np.int32)[:, None]
    mask = ~np.isnan(raw)
    last = np.maximum.accumulate(np.where(mask, idx, np.int32(-1)), axis=0)
    cols = np.arange(K, dtype=np.int32)[None, :]
    gathered = raw[np.maximum(last, 0), cols]
    return np.where(last >= 0, gathered, 0.0).astype(np.float32)


def shift_rows(mat: np.ndarray, k: int, fill: float = 0.0) -> np.ndarray:
    if k <= 0:
        return mat
    out = np.full_like(mat, fill, dtype=np.float32)
    out[k:] = mat[:-k]
    return out


def backtest_paths(
    buy_sig: np.ndarray,
    sell_sig: np.ndarray,
    ret: np.ndarray,
    cost: float,
    mode: str = "full",
    exec_shift: int = 0,
) -> dict[str, np.ndarray]:
    """买卖信号 → 仓位路径与逐日收益分解。

    contrib 行 t = 仓位(t-1) × 收益(t)；event 行 t = |仓位(t-1)-仓位(t-2)|。
    """
    raw = np.where(buy_sig, np.float32(1.0), np.where(sell_sig, np.float32(0.0), np.nan))
    pos = ffill_positions(raw)
    if exec_shift:
        pos = shift_rows(pos, exec_shift)
    if mode == "half":
        pos_eff = (0.5 + 0.5 * pos).astype(np.float32)
    else:
        pos_eff = pos

    pos_prev = np.zeros_like(pos_eff)
    pos_prev[1:] = pos_eff[:-1]
    contrib = pos_prev * ret[:, None]
    event = np.abs(np.diff(pos_eff, axis=0, prepend=pos_eff[:1].copy()))
    return {
        "pos": pos,
        "pos_eff": pos_eff,
        "contrib": contrib,
        "event": event,
    }


def metrics_from_paths(
    paths: dict[str, np.ndarray],
    cost: float,
    i0: int = I0,
    ret_override: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """按列计算评估窗口内的 CAGR/Sharpe/MDD/最长水下(交易日)/交易次数/胜率。"""
    contrib = paths["contrib"][i0 + 1 :]
    event = paths["event"][i0 + 1 :]
    R = (contrib - event * cost) if ret_override is None else ret_override
    Tn, K = R.shape

    nav = np.cumprod(1.0 + R.astype(np.float64), axis=0)
    nav = np.vstack([np.ones((1, K)), nav])
    nav = np.maximum(nav, 1e-12)
    years = Tn / TRADING_DAYS
    end = nav[-1]
    cagr = np.where(years > 0, np.power(np.maximum(end, 1e-12), 1.0 / years) - 1.0, np.nan)

    mean_r = R.mean(axis=0)
    std_r = R.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpe = np.where(std_r > 1e-12, mean_r / std_r * np.sqrt(TRADING_DAYS), np.nan)

    peak = np.maximum.accumulate(nav, axis=0)
    dd = (peak - nav) / peak
    mdd = dd.max(axis=0)
    T_idx = np.arange(nav.shape[0], dtype=np.int32)[:, None]
    under = dd > 1e-12
    last_ok = np.maximum.accumulate(np.where(~under, T_idx, np.int32(-1)), axis=0)
    underwater = np.where(under, T_idx - np.maximum(last_ok, 0), 0)
    longest_underwater = underwater.max(axis=0)

    calmar = np.where(mdd > 0, cagr / mdd, np.nan)
    total_return = end - 1.0
    return {
        "cagr": cagr,
        "sharpe": sharpe,
        "mdd": mdd,
        "calmar": calmar,
        "longest_underwater": longest_underwater.astype(np.int32),
        "total_return": total_return,
        "trades": event.sum(axis=0) / 2.0,  # 单边换手合计/2 ≈ 往返次数
        "nav_last": end,
    }


def evaluate_grid(
    close: np.ndarray,
    ma_periods: np.ndarray = MA_PERIODS,
    thr_pcts: np.ndarray | None = None,
    *,
    cost: float = 0.00006,
    mode: str = "full",
    exec_shift: int = 0,
) -> dict[str, np.ndarray]:
    """对 MA×阈值网格批量回测。返回各指标 (P, K) 数组（P=均线数, K=阈值数）。"""
    if thr_pcts is None:
        thr_pcts = np.round(np.arange(0.0, 4.0001, 0.25), 2)
    ret = np.diff(close) / close[:-1]
    ret = np.concatenate([[0.0], ret]).astype(np.float64)
    mas = ma_matrix(close, ma_periods)
    T, P = mas.shape
    K = len(thr_pcts)
    thr = thr_pcts[None, None, :] / 100.0

    close_col = close[:, None, None]
    ma_col = mas[:, :, None]
    buy_sig = close_col > ma_col * (1 + thr)
    sell_sig = close_col < ma_col * (1 - thr)
    buy_sig = np.broadcast_to(buy_sig, (T, P, K)).reshape(T, P * K)
    sell_sig = np.broadcast_to(sell_sig, (T, P, K)).reshape(T, P * K)

    paths = backtest_paths(buy_sig, sell_sig, ret, cost, mode=mode, exec_shift=exec_shift)
    metrics = metrics_from_paths(paths, cost, i0=I0)
    total = P * K
    return {
        key: value.reshape(P, K)
        for key, value in metrics.items()
        if value.ndim == 1 and value.shape[0] == total
    }


def signal_dropout_paths(
    buy_sig: np.ndarray,
    sell_sig: np.ndarray,
    ret: np.ndarray,
    cost: float,
    dropout_rate: float,
    n_paths: int,
    seed: int,
    i0: int = I0,
) -> dict[str, np.ndarray]:
    """漏单蒙特卡洛：每条路径以概率 dropout_rate 随机丢弃当日信号（次日不重试）。"""
    rng = np.random.default_rng(seed)
    T, K = buy_sig.shape
    cagrs = np.empty(n_paths)
    sharpes = np.empty(n_paths)
    mdds = np.empty(n_paths)
    for path_index in range(n_paths):
        keep = rng.random((T, K)) >= dropout_rate
        buy_m = buy_sig & keep
        sell_m = sell_sig & keep
        paths = backtest_paths(buy_m, sell_m, ret, cost)
        metrics = metrics_from_paths(paths, cost, i0=i0)
        cagrs[path_index] = metrics["cagr"][0]
        sharpes[path_index] = metrics["sharpe"][0]
        mdds[path_index] = metrics["mdd"][0]
    return {"cagr": cagrs, "sharpe": sharpes, "mdd": mdds}


def stationary_block_bootstrap(
    daily_returns: np.ndarray,
    n_boot: int = 1000,
    block_length: int = 21,
    seed: int = 42,
) -> dict[str, np.ndarray]:
    """平稳块自助法：返回每次重采样的 CAGR/Sharpe/MDD。"""
    rng = np.random.default_rng(seed)
    T = len(daily_returns)
    n_blocks = int(np.ceil(T / block_length))
    starts_pool = np.arange(T)
    samples = np.empty((n_boot, 3))
    for b in range(n_boot):
        starts = rng.choice(starts_pool, size=n_blocks, replace=True)
        idx = np.concatenate([(start + np.arange(block_length)) % T for start in starts])[:T]
        r = daily_returns[idx]
        nav = np.cumprod(1 + r)
        years = T / TRADING_DAYS
        cagr = nav[-1] ** (1 / years) - 1 if years > 0 and nav[-1] > 0 else -1.0
        sharpe = (
            r.mean() / r.std(ddof=1) * np.sqrt(TRADING_DAYS) if r.std(ddof=1) > 1e-12 else np.nan
        )
        peak = np.maximum.accumulate(nav)
        mdd = ((peak - nav) / peak).max()
        samples[b] = (cagr, sharpe, mdd)
    return {"cagr": samples[:, 0], "sharpe": samples[:, 1], "mdd": samples[:, 2]}


__all__ = [
    "TRADING_DAYS",
    "MA_PERIODS",
    "I0",
    "backtest_paths",
    "evaluate_grid",
    "ffill_positions",
    "load_price_frame",
    "load_price_series",
    "ma_matrix",
    "metrics_from_paths",
    "signal_dropout_paths",
    "stationary_block_bootstrap",
    "shift_rows",
]
