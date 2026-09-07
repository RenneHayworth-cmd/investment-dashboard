"""冻结参数组合 OOS（审计 P1）：真正意义上的"未见样本"评估。

口径（与动态阈值研究 evaluate_frozen_models 一致的连续路径语义）：

1. 对每只标的做 Walk-Forward：训练窗内用快速打分器选一次 (MA, 阈值)，
   该折测试窗只使用冻结参数（不回头看测试段表现）；
2. 汇总每折选出的参数 → 得到"冻结参数序列"（每折一个参数）；
3. 用冻结参数序列在各自测试窗上跑统一事件模拟器（连续路径：信号状态机
   在全历史展开，指标按测试窗切分、以窗前净值为锚点），与同窗等权
   一直持有基准对比，得出每折超额；
4. 输出 OOS 胜率（超额>0 的折占比）、平均超额、逐折明细。

与 `run_strategy_validation.py` 的"后30% holdout"区别：后者测试的是当前
参数（可能已参考过该区间），前者测试的是"每折只在训练窗内选出的参数"，
才是可外推的 OOS 证据。两者都保留、互为补充。
"""

from __future__ import annotations

import argparse
import json
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
from services.ma_timing_core import (  # noqa: E402
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_OPEN,
    TimingAccountConfig,
    ma_threshold_states,
    simulate_timing_account,
)
from services.position_models import (  # noqa: E402
    ETF_DISPLAY_NAMES,
    ETF_POSITION_STRATEGIES,
    ETF_TIMING_STRATEGIES,
)
from run_strategy_validation import (  # noqa: E402
    CAPITAL,
    FEE,
    LOT_SIZE,
    fund_cache_dirs,
)

SEED = 20260903
TRAIN_TRADING_DAYS = 504  # 2 年训练窗
TEST_TRADING_DAYS = 126  # 半年测试窗
MIN_ROWS = TRAIN_TRADING_DAYS + TEST_TRADING_DAYS + 60


def load_close(symbol: str) -> tuple[pd.Series | None, Path | None]:
    for cache_dir in fund_cache_dirs():
        for suffixed in (symbol, f"{symbol}.SH", f"{symbol}.SZ"):
            candidates = sorted(cache_dir.glob(f"fund_close_v2_{suffixed}_forward_additive_*.csv"))
            if candidates:
                return G.load_price_series(str(candidates[-1])), candidates[-1]
    return None, None


def fast_train_score(close: pd.Series, ma_period: int, threshold_pct: float, cost: float = FEE) -> float:
    """训练窗内的快速 Sharpe 打分（信号与项目口径一致，成本线性扣减）。"""
    values = close.to_numpy(dtype=float)
    ret = np.concatenate([[0.0], np.diff(values) / values[:-1]])
    ma = close.rolling(ma_period).mean().to_numpy()
    buy = (values > ma * (1 + threshold_pct / 100))[:, None]
    sell = (values < ma * (1 - threshold_pct / 100))[:, None]
    paths = G.backtest_paths(buy, sell, ret, cost)
    contrib = paths["contrib"].ravel()
    event = paths["event"].ravel()
    r = (contrib - event * cost)[G.I0 + 1 :]
    if len(r) < 20 or r.std(ddof=1) <= 1e-12:
        return float("-inf")
    return float(r.mean() / r.std(ddof=1) * np.sqrt(G.TRADING_DAYS))


def walk_forward_select(
    close: pd.Series,
    candidate_grid: list[tuple[int, float]],
    half_timing: bool,
) -> tuple[list[dict[str, object]], pd.DataFrame]:
    """逐折 (训练窗选参 → 测试窗冻结)。返回 (冻结折列表, 选参明细)。"""
    index = close.index
    offset = TRAIN_TRADING_DAYS
    frozen: list[dict[str, object]] = []
    detail_rows: list[dict[str, object]] = []
    fold = 1
    while offset + TEST_TRADING_DAYS <= len(index):
        train_slice = close.iloc[offset - TRAIN_TRADING_DAYS : offset]
        test_slice = close.iloc[offset : offset + TEST_TRADING_DAYS]
        scores = [
            (fast_train_score(train_slice, ma, thr), ma, thr)
            for ma, thr in candidate_grid
        ]
        best_score, best_ma, best_thr = max(scores, key=lambda value: value[0])
        frozen.append(
            {
                "fold": fold,
                "train_start": index[offset - TRAIN_TRADING_DAYS],
                "train_end": index[offset - 1],
                "test_start": index[offset],
                "test_end": index[min(offset + TEST_TRADING_DAYS - 1, len(index) - 1)],
                "ma_period": int(best_ma),
                "threshold_pct": float(best_thr),
                "train_sharpe": round(best_score, 3),
            }
        )
        detail_rows.append(frozen[-1])
        offset += TEST_TRADING_DAYS
        fold += 1
    return frozen, pd.DataFrame(detail_rows)


def frozen_window_metrics(
    full_close: pd.Series,
    window: dict[str, object],
    half_timing: bool,
) -> tuple[dict[str, float], dict[str, float]]:
    """连续路径口径：全历史展开信号与账户，按测试窗切指标（窗前净值为锚点）。"""
    ma_period = int(window["ma_period"])
    threshold_pct = float(window["threshold_pct"])

    def _run(mode: str, slippage: float, config_capital: float) -> pd.Series:
        states = ma_threshold_states(full_close, ma_period, threshold_pct)
        if half_timing:
            timing_capital = config_capital / 2
        else:
            timing_capital = config_capital
        result = simulate_timing_account(
            full_close,
            states,
            TimingAccountConfig(
                initial_capital=timing_capital,
                transaction_cost=FEE,
                lot_size=LOT_SIZE,
                slippage=slippage,
                execution_mode=mode,
            ),
        )
        nav = result.nav
        if half_timing:
            hold_states = pd.Series(0.0, index=full_close.index)
            first_buy = result.shares.index[(result.shares > 0)][0] if (result.shares > 0).any() else None
            if first_buy is not None:
                hold_states.loc[first_buy:] = 1.0
            hold_result = simulate_timing_account(
                full_close,
                hold_states,
                TimingAccountConfig(
                    initial_capital=config_capital / 2,
                    transaction_cost=FEE,
                    lot_size=LOT_SIZE,
                    slippage=slippage,
                    execution_mode=mode,
                ),
            )
            nav = (result.nav + hold_result.nav).dropna()
        return nav

    test_start = pd.Timestamp(window["test_start"])
    test_end = pd.Timestamp(window["test_end"])

    def _window_metrics(nav: pd.Series) -> dict[str, float]:
        window_nav = nav.loc[test_start:test_end]
        prior = nav.loc[:test_start]
        anchor = float(prior.iloc[-2]) if len(prior) >= 2 else float(window_nav.iloc[0])
        if len(window_nav) < 10:
            return {"sharpe": np.nan, "total_return_pct": np.nan, "mdd_pct": np.nan, "trading_days": 0}
        seeded = pd.concat([pd.Series([anchor]), window_nav.reset_index(drop=True)])
        rets = seeded.pct_change().dropna()
        total = float(window_nav.iloc[-1] / anchor - 1)
        peak = seeded.cummax()
        dd = float((seeded / peak - 1).min())
        return {
            "sharpe": round(
                float(rets.mean() / rets.std(ddof=1) * np.sqrt(G.TRADING_DAYS))
                if rets.std(ddof=1) > 1e-12
                else np.nan,
                3,
            ),
            "total_return_pct": round(total * 100, 2),
            "mdd_pct": round(dd * 100, 2),
            "trading_days": int(len(window_nav)),
        }

    strategy_nav = _run(EXECUTION_AFTER_CLOSE, 0.0, CAPITAL)
    strategy_next = _run(EXECUTION_NEXT_OPEN, 0.0005, CAPITAL)
    hold_nav = full_close / full_close.iloc[0] * CAPITAL
    return (
        _window_metrics(strategy_nav),
        _window_metrics(strategy_next),
        _window_metrics(hold_nav),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "output" / "research_audit"))
    args = parser.parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    candidate_grid = [
        (ma, thr)
        for ma in (10, 15, 20, 25, 30)
        for thr in (0.5, 1.0, 1.5, 2.0, 2.5)
    ]
    all_rows: list[dict[str, object]] = []
    for symbol, (ma_current, thr_current) in sorted(ETF_TIMING_STRATEGIES.items()):
        close, _source = load_close(symbol)
        if close is None:
            print(f"[skip] {symbol}: 无缓存")
            continue
        if len(close) < MIN_ROWS:
            print(f"[skip] {symbol}: 历史 {len(close)} 行 < {MIN_ROWS}，折数不足")
            continue
        half_timing = ETF_POSITION_STRATEGIES.get(symbol) == "半仓持有半仓择时"
        frozen, detail = walk_forward_select(close, candidate_grid, half_timing)
        if not frozen:
            continue
        detail.to_csv(output_dir / f"{symbol}_frozen_oos_selections.csv", index=False, encoding="utf-8-sig")
        rows = []
        for window in frozen:
            strategy_metrics, next_open_metrics, hold_metrics = frozen_window_metrics(
                close, window, half_timing
            )
            rows.append(
                {
                    **window,
                    "current_params": f"MA{ma_current}/{thr_current}%",
                    "oos_sharpe": strategy_metrics["sharpe"],
                    "oos_total_return_pct": strategy_metrics["total_return_pct"],
                    "oos_mdd_pct": strategy_metrics["mdd_pct"],
                    "oos_next_open_sharpe": next_open_metrics["sharpe"],
                    "oos_next_open_return_pct": next_open_metrics["total_return_pct"],
                    "hold_sharpe": hold_metrics["sharpe"],
                    "hold_total_return_pct": hold_metrics["total_return_pct"],
                    "excess_vs_hold_pct": round(
                        strategy_metrics["total_return_pct"] - hold_metrics["total_return_pct"], 2
                    ),
                    "excess_next_open_vs_hold_pct": round(
                        next_open_metrics["total_return_pct"] - hold_metrics["total_return_pct"], 2
                    ),
                }
            )
        frame = pd.DataFrame(rows)
        frame.to_csv(output_dir / f"{symbol}_frozen_oos.csv", index=False, encoding="utf-8-sig")
        excess = frame["excess_vs_hold_pct"].dropna()
        excess_next = frame["excess_next_open_vs_hold_pct"].dropna()
        all_rows.append(
            {
                "symbol": symbol,
                "name": ETF_DISPLAY_NAMES.get(symbol, symbol),
                "folds": len(frame),
                "distinct_params": frame[["ma_period", "threshold_pct"]].drop_duplicates().shape[0],
                "oos_win_rate": round(float((excess > 0).mean()), 3) if len(excess) else np.nan,
                "oos_mean_excess_pct": round(float(excess.mean()), 2) if len(excess) else np.nan,
                "oos_next_open_win_rate": round(float((excess_next > 0).mean()), 3) if len(excess_next) else np.nan,
                "oos_next_open_mean_excess_pct": round(float(excess_next.mean()), 2) if len(excess_next) else np.nan,
            }
        )
        print(
            f"{symbol}: {len(frame)} 折, "
            f"OOS胜率 {all_rows[-1]['oos_win_rate']}, 平均超额 {all_rows[-1]['oos_mean_excess_pct']}%, "
            f"next_open胜率 {all_rows[-1]['oos_next_open_win_rate']}"
        )

    summary = pd.DataFrame(all_rows)
    summary.to_csv(output_dir / "frozen_oos_summary.csv", index=False, encoding="utf-8-sig")
    manifest = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "seed": SEED,
        "train_trading_days": TRAIN_TRADING_DAYS,
        "test_trading_days": TEST_TRADING_DAYS,
        "candidate_grid": "MA(10,15,20,25,30) x thr(0.5,1.0,1.5,2.0,2.5)",
        "note": "冻结参数逐折选择；测试窗指标为连续路径口径（窗前净值为锚点）",
    }
    (output_dir / "frozen_oos_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"冻结 OOS 完成，写入 {output_dir}")


if __name__ == "__main__":
    main()
