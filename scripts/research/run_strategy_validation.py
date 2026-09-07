"""ETF 均线择时策略族研究级验证管线（审计交付物）。

用法::

    .venv/bin/python scripts/research/run_strategy_validation.py \
        [--output output/research_audit] [--fast]

对策略注册表（services.position_models 权威配置）中的每只 ETF：
  1. 统一框架全量回测：after_close（项目口径）/ next_open / next_close（保守口径）
  2. 样本内 70% / 样本外 30%（连续路径口径：信号用全历史、指标按段切分）
  3. 滚动窗口：逐年非重叠测试段的 Sharpe/CAGR 均值与离散度
  4. 参数邻域敏感性：MA ±5/±10 × 阈值 ×0.5/0.75/1/1.25/1.5/2（事件模拟器逐点）
  5. 全参数网格：MA5..120 × 阈值 0..4%（向量化引擎）+ 平台/尖峰判定
  6. 平稳块 Bootstrap（1000 次）与漏单蒙特卡洛（200 路径 × 5%/10% 丢信号）
  7. 成本/滑点压力：0.5/1/5/10bp 单边费用情景
  8. 牛/熊分段（ trailing 120 日趋势）策略表现
跨策略：
  9. 策略日收益相关矩阵（含与自身标的的相关）
 10. 组合层面回测：持仓页 10 标的加权配置（含 512890 停泊承接，独立资金袖套）
全部结果写入 output/research_audit/，并生成 run_manifest.json（输入 sha256 + 参数）
以保证可复现。标题级指标以事件模拟器 + core.metrics（日历日年化）为准。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import grid_engine as G  # noqa: E402
from core.metrics import annual_volatility, sharpe_ratio  # noqa: E402
from services.ma_timing_core import (  # noqa: E402
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_NEXT_OPEN,
    TimingAccountConfig,
    ma_threshold_states,
    simulate_timing_account,
)
from services.fund_rotation_models import (  # noqa: E402
    PORTFOLIO_INITIAL_ENTRY_FRESH_BUY,
    PORTFOLIO_STRATEGY_HALF_TIMING,
    PORTFOLIO_STRATEGY_TIMING,
    PortfolioTimingAllocation,
    RotationInput,
)
from services.fund_rotation_timing import run_portfolio_timing_backtest  # noqa: E402
from services.position_models import (  # noqa: E402
    ETF_DISPLAY_NAMES,
    ETF_PORTFOLIO_WEIGHTS_PCT,
    ETF_POSITION_STRATEGIES,
    ETF_TIMING_STRATEGIES,
)

CAPITAL = 100_000.0
FEE = 0.00006
NEXT_OPEN_SLIPPAGE = 0.0005
LOT_SIZE = 1  # 研究口径：不施加整手取整，专注信号与成本敏感性
BOOTSTRAP_N = 1000
BOOTSTRAP_BLOCK = 21
MC_N = 200
MC_DROPOUT_RATES = (0.05, 0.10)
SEED = 20260903
COST_SCENARIOS_BP = (0.0, 0.5, 1.0, 5.0, 10.0)
IS_RATIO = 0.70


def tickflow_cache_dirs() -> list[Path]:
    dirs = [PROJECT_ROOT / "data" / "raw" / "tickflow"]
    for home in (Path.home(), Path("/mnt/c/Users/Renne")):
        candidate = home / "investment_dashboard_data" / "data" / "raw" / "tickflow"
        if candidate.exists() and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def fund_cache_dirs() -> list[Path]:
    """项目全部基金日线缓存目录：tickflow 主源 + akshare（161128 等东财主源标的）。

    同一 symbol 可能在多个目录出现；按目录顺序取第一个命中，
    与页面 load_or_fetch_etf 的"本地缓存优先、来源标签校验"语义一致。
    """
    dirs: list[Path] = []
    bases = [PROJECT_ROOT / "data" / "raw"]
    for home in (Path.home(), Path("/mnt/c/Users/Renne")):
        bases.append(home / "investment_dashboard_data" / "data" / "raw")
    for base in bases:
        for sub in ("tickflow", "akshare"):
            candidate = base / sub
            if candidate.exists() and candidate not in dirs:
                dirs.append(candidate)
    return dirs


def load_cached_close(symbol: str) -> tuple[pd.Series | None, Path | None]:
    close, source = load_cached_prices(symbol)
    return close, source


def load_cached_prices(symbol: str) -> tuple[pd.Series | None, Path | None]:
    """返回 (close, 源文件路径)。tickflow 主源优先，akshare 缓存兜底（161128 等）。"""
    for cache_dir in fund_cache_dirs():
        for suffixed in (symbol, f"{symbol}.SH", f"{symbol}.SZ"):
            candidates = sorted(cache_dir.glob(f"fund_close_v2_{suffixed}_forward_additive_*.csv"))
            if candidates:
                return G.load_price_series(str(candidates[-1])), candidates[-1]
    return None, None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_strategy_event(
    close: pd.Series,
    ma_period: int,
    threshold_pct: float,
    half_timing: bool,
    mode: str = EXECUTION_AFTER_CLOSE,
    slippage: float = 0.0,
) -> tuple[pd.Series, dict[str, float]]:
    """统一框架回测；half_timing 时长期半仓在首次有效买入日建仓后永不卖出
    （与持仓页 _run_delayed_timing_sleeve 的 fresh_buy 语义一致）。
    返回 (日收益, 指标)。"""
    states = ma_threshold_states(close, ma_period, threshold_pct)
    config = TimingAccountConfig(
        initial_capital=CAPITAL,
        transaction_cost=FEE,
        lot_size=LOT_SIZE,
        slippage=slippage,
        execution_mode=mode,
    )
    result = simulate_timing_account(close, states, config)
    if half_timing:
        timing_shares = result.shares
        first_buy = timing_shares.index[timing_shares > 0][0] if (timing_shares > 0).any() else None
        hold_states = pd.Series(0.0, index=states.index)
        if first_buy is not None:
            hold_states.loc[first_buy:] = 1.0
        hold_config = TimingAccountConfig(
            initial_capital=CAPITAL / 2,
            transaction_cost=FEE,
            lot_size=LOT_SIZE,
            slippage=slippage,
            execution_mode=mode,
        )
        hold_result = simulate_timing_account(close, hold_states, hold_config)
        full_nav = (result.nav + hold_result.nav).dropna()
    else:
        full_nav = result.nav.dropna()
    returns = full_nav.pct_change().dropna()
    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    if returns.empty:
        return returns, {}
    calendar_days = max(int((full_nav.index[-1] - full_nav.index[0]).days), 1)
    total = float(full_nav.iloc[-1] / full_nav.iloc[0] - 1)
    years = calendar_days / 365
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else -1.0
    peak = full_nav.cummax()
    mdd = float((full_nav / peak - 1).min())
    metrics = {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe": round(sharpe_ratio(returns), 3),
        "mdd_pct": round(mdd * 100, 2),
        "vol_pct": round(annual_volatility(returns) * 100, 2),
        "trading_days": int(len(returns)),
        "start_date": str(returns.index[0].date()),
        "end_date": str(returns.index[-1].date()),
    }
    return returns, metrics


def split_is_oos(returns: pd.Series, ratio: float = IS_RATIO) -> tuple[pd.Series, pd.Series]:
    if len(returns) < 60:
        return returns, returns.iloc[0:0]
    cut = int(len(returns) * ratio)
    return returns.iloc[:cut], returns.iloc[cut:]


def segment_metrics(returns: pd.Series) -> dict[str, float]:
    if returns.empty:
        return {"sharpe": np.nan, "cagr_pct": np.nan, "mdd_pct": np.nan, "trading_days": 0}
    nav = (1 + returns).cumprod()
    years = max(len(returns) / G.TRADING_DAYS, 1e-9)
    cagr = nav.iloc[-1] ** (1 / years) - 1 if nav.iloc[-1] > 0 else -1.0
    peak = nav.cummax()
    mdd = float((nav / peak - 1).min())
    return {
        "sharpe": round(sharpe_ratio(returns), 3),
        "cagr_pct": round(cagr * 100, 2),
        "mdd_pct": round(mdd * 100, 2),
        "trading_days": int(len(returns)),
    }


def yearly_windows(returns: pd.Series) -> list[tuple[str, pd.Series]]:
    if returns.empty:
        return []
    years = returns.index.year
    windows = []
    for year in sorted(pd.unique(years)):
        segment = returns[years == year]
        if len(segment) >= 60:
            windows.append((str(year), segment))
    return windows


def neighborhood_configs(ma_period: int, threshold_pct: float) -> list[tuple[int, float]]:
    mas = sorted({m for m in (ma_period - 10, ma_period - 5, ma_period, ma_period + 5, ma_period + 10) if 2 <= m <= 120})
    thr_values = sorted({round(threshold_pct * k, 3) for k in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)})
    return [(m, t) for m in mas for t in thr_values]


def regime_labels(close: pd.Series, lookback: int = 120) -> pd.Series:
    momentum = close / close.shift(lookback) - 1
    labels = pd.Series("震荡", index=close.index)
    labels[momentum > 0.02] = "牛"
    labels[momentum < -0.02] = "熊"
    return labels


def analyze_symbol(
    symbol: str,
    close: pd.Series,
    ma_period: int,
    threshold_pct: float,
    half_timing: bool,
    output_dir: Path,
    fast: bool = False,
    open_prices: pd.Series | None = None,
) -> dict[str, object]:
    name = ETF_DISPLAY_NAMES.get(symbol, symbol)
    tag = f"{symbol}_{name}"
    print(f"== {tag} (MA{ma_period}/{threshold_pct}%, {'半仓' if half_timing else '纯择时'}, {len(close)}行) ==")
    rows: dict[str, object] = {
        "symbol": symbol,
        "name": name,
        "ma_period": ma_period,
        "threshold_pct": threshold_pct,
        "half_timing": half_timing,
        "history_rows": int(len(close)),
        "history_start": str(close.index[0].date()),
        "history_end": str(close.index[-1].date()),
    }

    # 1) 三种执行口径（next_open 额外计 ±0.05% 双边滑点，与项目轮动口径一致）
    baseline_returns, baseline_metrics = run_strategy_event(close, ma_period, threshold_pct, half_timing)
    rows.update({f"after_close_{k}": v for k, v in baseline_metrics.items()})
    _, next_open_metrics = run_strategy_event(
        close, ma_period, threshold_pct, half_timing, EXECUTION_NEXT_OPEN, NEXT_OPEN_SLIPPAGE
    )
    rows.update({f"next_open_{k}": v for k, v in next_open_metrics.items()})
    _, next_close_metrics = run_strategy_event(
        close, ma_period, threshold_pct, half_timing, EXECUTION_NEXT_CLOSE
    )
    rows.update({f"next_close_{k}": v for k, v in next_close_metrics.items()})
    if open_prices is not None:
        # next_open 口径用真实开盘价成交（close 系列缓存含开盘价）
        _, next_open_real_metrics = run_strategy_event(
            open_prices, ma_period, threshold_pct, half_timing, EXECUTION_NEXT_OPEN, NEXT_OPEN_SLIPPAGE
        )
        rows.update({f"next_open_real_{k}": v for k, v in next_open_real_metrics.items()})

    # 2) 样本内/样本外
    is_returns, oos_returns = split_is_oos(baseline_returns)
    rows.update({f"is_{k}": v for k, v in segment_metrics(is_returns).items()})
    rows.update({f"oos_{k}": v for k, v in segment_metrics(oos_returns).items()})

    # 3) 滚动（逐年）窗口
    windows = yearly_windows(baseline_returns)
    if windows:
        window_stats = [segment_metrics(segment) for _, segment in windows]
        sharpe_values = [stats["sharpe"] for stats in window_stats]
        cagr_values = [stats["cagr_pct"] for stats in window_stats]
        rows["wf_positive_sharpe_ratio"] = round(
            sum(1 for v in sharpe_values if v and v > 0) / len(sharpe_values), 3
        )
        rows["wf_sharpe_mean"] = round(float(np.nanmean(sharpe_values)), 3)
        rows["wf_sharpe_std"] = round(float(np.nanstd(sharpe_values, ddof=1)), 3) if len(sharpe_values) > 1 else np.nan
        rows["wf_cagr_mean_pct"] = round(float(np.nanmean(cagr_values)), 2)
        rows["wf_window_count"] = len(windows)
        pd.DataFrame(
            [
                {"window": label, **segment_metrics(segment)}
                for label, segment in windows
            ]
        ).to_csv(output_dir / f"{symbol}_rolling_windows.csv", index=False, encoding="utf-8-sig")

    # 4) 参数邻域敏感性（事件模拟器逐点）
    neighborhood_rows = []
    for neighbor_ma, neighbor_thr in neighborhood_configs(ma_period, threshold_pct):
        _, neighbor_metrics = run_strategy_event(close, neighbor_ma, neighbor_thr, half_timing)
        neighborhood_rows.append(
            {
                "ma_period": neighbor_ma,
                "threshold_pct": neighbor_thr,
                "is_current": neighbor_ma == ma_period and neighbor_thr == threshold_pct,
                **neighbor_metrics,
            }
        )
    neighborhood = pd.DataFrame(neighborhood_rows)
    neighborhood.to_csv(output_dir / f"{symbol}_neighborhood.csv", index=False, encoding="utf-8-sig")
    current_row = neighborhood[neighborhood["is_current"]].iloc[0]
    neighbors = neighborhood[~neighborhood["is_current"]]
    rows["nb_neighbor_sharpe_mean"] = round(float(neighbors["sharpe"].mean()), 3)
    rows["nb_current_minus_neighbors"] = round(
        float(current_row["sharpe"] - neighbors["sharpe"].mean()), 3
    )
    rows["nb_neighbor_positive_ratio"] = round(
        float((neighbors["sharpe"] > 0).mean()), 3
    )

    # 5) 全参数网格 + 平台/尖峰判定
    thr_grid = np.round(np.arange(0.0, 4.0001, 0.25), 2)
    grid = G.evaluate_grid(
        close.to_numpy(dtype=float),
        thr_pcts=thr_grid,
        mode="half" if half_timing else "full",
    )
    np.savez_compressed(
        output_dir / f"{symbol}_grid.npz",
        ma_periods=G.MA_PERIODS,
        thr_pcts=thr_grid,
        **{key: value for key, value in grid.items()},
    )
    ma_index = int(np.argmin(np.abs(G.MA_PERIODS - ma_period)))
    thr_index = int(np.argmin(np.abs(thr_grid - threshold_pct)))
    rows["grid_sharpe_at_config"] = round(float(grid["sharpe"][ma_index, thr_index]), 3)
    rows["grid_sharpe_percentile"] = round(
        float((grid["sharpe"] < grid["sharpe"][ma_index, thr_index]).mean()), 3
    )
    # 平台判定：邻域（MA±10、相邻阈值）均值 / 配置点
    ma_lo, ma_hi = max(ma_index - 10, 0), min(ma_index + 11, len(G.MA_PERIODS))
    thr_lo, thr_hi = max(thr_index - 1, 0), min(thr_index + 2, len(thr_grid))
    neighbor_block = np.array(grid["sharpe"][ma_lo:ma_hi, thr_lo:thr_hi], dtype=float)
    keep = np.ones(neighbor_block.shape, dtype=bool)
    keep[ma_index - ma_lo, thr_index - thr_lo] = False  # 排除配置点本身
    neighbor_values = neighbor_block[keep]
    neighbor_mean = float(np.nanmean(neighbor_values)) if neighbor_values.size and np.isfinite(neighbor_values).any() else np.nan
    rows["grid_neighbor_mean_sharpe"] = round(neighbor_mean, 3) if np.isfinite(neighbor_mean) else np.nan
    config_sharpe = float(grid["sharpe"][ma_index, thr_index])
    rows["grid_plateau_ratio"] = (
        round(config_sharpe / neighbor_mean, 3)
        if np.isfinite(neighbor_mean) and neighbor_mean > 0
        else np.nan
    )

    # 6) Bootstrap 与漏单蒙特卡洛
    if not fast and len(baseline_returns) >= 120:
        boot = G.stationary_block_bootstrap(
            baseline_returns.to_numpy(dtype=float),
            n_boot=BOOTSTRAP_N,
            block_length=BOOTSTRAP_BLOCK,
            seed=SEED + int(symbol),
        )
        rows["boot_sharpe_ci95"] = f"[{np.nanpercentile(boot['sharpe'], 2.5):.2f}, {np.nanpercentile(boot['sharpe'], 97.5):.2f}]"
        rows["boot_cagr_ci95_pct"] = f"[{np.nanpercentile(boot['cagr'], 2.5) * 100:.1f}, {np.nanpercentile(boot['cagr'], 97.5) * 100:.1f}]"
        rows["boot_mdd_p95_pct"] = round(float(np.nanpercentile(boot["mdd"], 95) * 100), 2)
        rows["boot_sharpe_positive_prob"] = round(float(np.nanmean(boot["sharpe"] > 0)), 3)

        values = close.to_numpy(dtype=float)
        ret = np.concatenate([[0.0], np.diff(values) / values[:-1]])
        ma = close.rolling(ma_period).mean().to_numpy()
        buy = (values > ma * (1 + threshold_pct / 100))[:, None]
        sell = (values < ma * (1 - threshold_pct / 100))[:, None]
        mc_summary = {}
        for rate in MC_DROPOUT_RATES:
            mc = G.signal_dropout_paths(buy, sell, ret, FEE, rate, MC_N, SEED + int(symbol))
            mc_summary[rate] = {
                "sharpe_p5": float(np.nanpercentile(mc["sharpe"], 5)),
                "sharpe_p50": float(np.nanpercentile(mc["sharpe"], 50)),
                "cagr_p5_pct": float(np.nanpercentile(mc["cagr"], 5)) * 100,
                "mdd_p95_pct": float(np.nanpercentile(mc["mdd"], 95)) * 100,
            }
        rows["mc_dropout_5pct_sharpe_p5"] = round(mc_summary[0.05]["sharpe_p5"], 3)
        rows["mc_dropout_10pct_sharpe_p5"] = round(mc_summary[0.10]["sharpe_p5"], 3)
        rows["mc_dropout_10pct_mdd_p95_pct"] = round(mc_summary[0.10]["mdd_p95_pct"], 2)

    # 7) 成本/滑点压力（向量化引擎）
    cost_rows = []
    for bp in COST_SCENARIOS_BP:
        grid_cost = G.evaluate_grid(
            close.to_numpy(dtype=float),
            ma_periods=np.array([ma_period]),
            thr_pcts=np.array([threshold_pct]),
            cost=bp / 10000,
            mode="half" if half_timing else "full",
        )
        cost_rows.append(
            {
                "cost_bp": bp,
                "cagr_pct": round(float(grid_cost["cagr"][0, 0]) * 100, 2),
                "sharpe": round(float(grid_cost["sharpe"][0, 0]), 3),
                "mdd_pct": round(float(grid_cost["mdd"][0, 0]) * 100, 2),
            }
        )
    pd.DataFrame(cost_rows).to_csv(output_dir / f"{symbol}_cost_stress.csv", index=False, encoding="utf-8-sig")
    rows["cost_10bp_cagr_pct"] = cost_rows[-1]["cagr_pct"]
    rows["cost_10bp_sharpe"] = cost_rows[-1]["sharpe"]

    # 8) 牛/熊/震荡分段
    labels = regime_labels(close)
    aligned_labels = labels.reindex(baseline_returns.index).ffill()
    regime_rows = []
    for regime in ("牛", "熊", "震荡"):
        segment = baseline_returns[aligned_labels == regime]
        regime_rows.append({"regime": regime, **segment_metrics(segment)})
    pd.DataFrame(regime_rows).to_csv(output_dir / f"{symbol}_regimes.csv", index=False, encoding="utf-8-sig")
    bear = next(r for r in regime_rows if r["regime"] == "熊")
    rows["bear_sharpe"] = bear["sharpe"]
    rows["bear_cagr_pct"] = bear["cagr_pct"]

    baseline_returns.to_frame("strategy_return").to_csv(
        output_dir / f"{symbol}_strategy_daily_returns.csv", encoding="utf-8-sig"
    )
    return rows


def run_portfolio_backtest(output_dir: Path) -> dict[str, object]:
    """持仓页 10 标的加权配置的组合回测（独立资金袖套 + 512890 停泊承接）。"""
    symbols = [code for code, weight in ETF_PORTFOLIO_WEIGHTS_PCT.items() if weight > 0]
    # 停泊承接标的 512890 权重为 0 但被 empty_position_symbol 引用，必须加载
    if "512890" not in symbols:
        symbols.append("512890")
    frames: dict[str, pd.Series] = {}
    sources: dict[str, Path] = {}
    for symbol in symbols:
        close, source = load_cached_close(symbol)
        if close is None:
            print(f"[portfolio] 缺少 {symbol} 缓存，跳过该标的")
            continue
        frames[symbol] = close
        if source:
            sources[symbol] = source
    if not frames:
        return {"error": "无可用缓存数据"}
    funds = [
        RotationInput(
            symbol=symbol,
            name=ETF_DISPLAY_NAMES.get(symbol, symbol),
            dataframe=pd.DataFrame({"trade_date": close.index, "close": close.values}),
        )
        for symbol, close in frames.items()
    ]
    allocations = []
    for symbol, close in frames.items():
        weight = float(ETF_PORTFOLIO_WEIGHTS_PCT.get(symbol, 0))
        if weight <= 0 or symbol not in ETF_TIMING_STRATEGIES:
            continue
        strategy_type = (
            PORTFOLIO_STRATEGY_HALF_TIMING
            if ETF_POSITION_STRATEGIES.get(symbol) == "半仓持有半仓择时"
            else PORTFOLIO_STRATEGY_TIMING
        )
        parking = "512890" if symbol in ("510500", "159967", "159552") else ""
        allocations.append(
            PortfolioTimingAllocation(
                symbol=symbol,
                name=ETF_DISPLAY_NAMES.get(symbol, symbol),
                weight_pct=weight,
                strategy=strategy_type,
                ma_period=int(ETF_TIMING_STRATEGIES[symbol][0]),
                threshold_pct=float(ETF_TIMING_STRATEGIES[symbol][1]),
                initial_entry_policy=PORTFOLIO_INITIAL_ENTRY_FRESH_BUY,
                empty_position_symbol=parking,
                empty_position_activation="after_primary_entry",
            )
        )
    common_start = max(series.index[0] for series in frames.values())
    common_end = min(series.index[-1] for series in frames.values())

    summaries: dict[str, dict[str, object]] = {}
    nav_by_mode: dict[str, pd.DataFrame] = {}
    for mode, slippage in (
        (EXECUTION_AFTER_CLOSE, 0.0),
        (EXECUTION_NEXT_OPEN, 0.0005),
        (EXECUTION_NEXT_CLOSE, 0.0),
    ):
        result = run_portfolio_timing_backtest(
            funds=funds,
            allocations=allocations,
            initial_capital=500_000.0,
            transaction_cost=FEE,
            lot_size=100,
            start_date=common_start,
            end_date=common_end,
            execution_mode=mode,
            slippage=slippage,
        )
        summaries[mode] = dict(result.summary)
        nav_by_mode[mode] = result.nav_data
    # after_close 明细落盘为主文件；三口径净值合并落盘
    result_summary = summaries[EXECUTION_AFTER_CLOSE]
    nav = nav_by_mode[EXECUTION_AFTER_CLOSE]
    nav.to_csv(output_dir / "portfolio_nav.csv", index=False, encoding="utf-8-sig")
    comparison = pd.DataFrame(
        {
            mode: {
                "总收益率(%)": summary_mode.get("总收益率(%)"),
                "年化收益率(%)": summary_mode.get("年化收益率(%)"),
                "夏普比率": summary_mode.get("夏普比率"),
                "策略最大回撤(%)": summary_mode.get("策略最大回撤(%)"),
                "一直持有收益率(%)": summary_mode.get("一直持有收益率(%)"),
                "累计总成本": summary_mode.get("累计总成本"),
            }
            for mode, summary_mode in summaries.items()
        }
    )
    comparison.to_csv(output_dir / "portfolio_execution_modes.csv", encoding="utf-8-sig")
    all_trades = None
    result = run_portfolio_timing_backtest(
        funds=funds,
        allocations=allocations,
        initial_capital=500_000.0,
        transaction_cost=FEE,
        lot_size=100,
        start_date=common_start,
        end_date=common_end,
    )
    all_trades = result.trades
    if all_trades is not None and not all_trades.empty:
        all_trades.to_csv(output_dir / "portfolio_trades.csv", index=False, encoding="utf-8-sig")
    summary = result_summary
    summary["portfolio_common_start"] = str(pd.Timestamp(common_start).date())
    summary["portfolio_common_end"] = str(pd.Timestamp(common_end).date())
    summary["symbols_included"] = "、".join(sorted(frames))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "output" / "research_audit"))
    parser.add_argument("--fast", action="store_true", help="跳过 bootstrap 与蒙特卡洛")
    args = parser.parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    return_frames: dict[str, pd.Series] = {}
    underlying_frames: dict[str, pd.Series] = {}
    used_sources: list[str] = []
    for symbol, (ma_period, threshold_pct) in sorted(ETF_TIMING_STRATEGIES.items()):
        close, source = load_cached_close(symbol)
        if close is None:
            print(f"[skip] {symbol}: 本地无 forward_additive 缓存（如 161128 需东财源）")
            all_rows.append(
                {
                    "symbol": symbol,
                    "name": ETF_DISPLAY_NAMES.get(symbol, symbol),
                    "ma_period": ma_period,
                    "threshold_pct": threshold_pct,
                    "error": "本地无缓存数据",
                }
            )
            continue
        if source is not None:
            used_sources.append(str(source))
        half_timing = ETF_POSITION_STRATEGIES.get(symbol) == "半仓持有半仓择时"
        _close2, source2 = load_cached_prices(symbol)
        open_prices = None
        if source2 is not None:
            try:
                _close_series, open_prices = G.load_price_frame(str(source2))
            except Exception:
                open_prices = None
        rows = analyze_symbol(
            symbol, close, ma_period, threshold_pct, half_timing, output_dir, fast=args.fast,
            open_prices=open_prices,
        )
        all_rows.append(rows)
        returns_path = output_dir / f"{symbol}_strategy_daily_returns.csv"
        if returns_path.exists():
            frame = pd.read_csv(returns_path, index_col=0, parse_dates=True)
            return_frames[symbol] = frame.iloc[:, 0]
            underlying = close / close.iloc[G.I0]
            underlying_frames[symbol] = close.pct_change().dropna()

    summary = pd.DataFrame(all_rows)
    summary.to_csv(output_dir / "summary_all.csv", index=False, encoding="utf-8-sig")

    # 9) 策略相关矩阵
    if return_frames:
        returns_matrix = pd.DataFrame(return_frames).dropna(how="all")
        correlation = returns_matrix.corr(min_periods=60).round(4)
        correlation.to_csv(output_dir / "strategy_correlation_matrix.csv", encoding="utf-8-sig")
        underlying_matrix = pd.DataFrame(underlying_frames).dropna(how="all")
        underlying_corr = underlying_matrix.corr(min_periods=60).round(4)
        underlying_corr.to_csv(output_dir / "underlying_correlation_matrix.csv", encoding="utf-8-sig")
        corr_values = correlation.to_numpy(dtype=float).copy()
        np.fill_diagonal(corr_values, np.nan)
        correlation = pd.DataFrame(corr_values, index=correlation.index, columns=correlation.columns)
        correlation.to_csv(output_dir / "strategy_correlation_matrix.csv", encoding="utf-8-sig")
        avg_pairwise = float(correlation.mean(skipna=True).mean())
        strategy_vs_underlying = {
            symbol: round(float(returns_matrix[symbol].corr(underlying_matrix[symbol])), 3)
            if symbol in underlying_matrix.columns
            else np.nan
            for symbol in returns_matrix.columns
        }
        pd.DataFrame(
            [{"symbol": k, "strategy_vs_underlying_corr": v} for k, v in strategy_vs_underlying.items()]
        ).to_csv(output_dir / "strategy_vs_underlying_corr.csv", index=False, encoding="utf-8-sig")
        print(f"策略间平均两两相关：{avg_pairwise:.3f}")

    # 10) 组合层面回测
    portfolio_summary = run_portfolio_backtest(output_dir)
    pd.DataFrame([portfolio_summary]).to_csv(
        output_dir / "portfolio_summary.csv", index=False, encoding="utf-8-sig"
    )

    manifest = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "seed": SEED,
        "capital": CAPITAL,
        "fee": FEE,
        "lot_size_research": LOT_SIZE,
        "next_open_slippage": NEXT_OPEN_SLIPPAGE,
        "bootstrap": {"n": BOOTSTRAP_N, "block": BOOTSTRAP_BLOCK},
        "monte_carlo": {"n": MC_N, "dropout_rates": list(MC_DROPOUT_RATES)},
        "is_oos_ratio": IS_RATIO,
        "input_files_sha256": {path: sha256_of(Path(path)) for path in used_sources},
        "fast_mode": bool(args.fast),
        "portfolio_summary": portfolio_summary,
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"研究管线完成，结果已写入 {output_dir}")


if __name__ == "__main__":
    main()
