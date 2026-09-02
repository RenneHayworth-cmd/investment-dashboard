#!/usr/bin/env python3
"""正股历史波动率计算器（转债定价输入）

用于计算可转债正股的历史波动率，作为 BS 模型等期权定价方法的输入。
支持多窗口（20/60/120/250 交易日）年化波动率，同时给出 Parkinson
高低价波动率估计作为交叉校验。

数据源：akshare 日线（前复权）。

用法：
    python calc_underlying_volatility.py 601229
    python calc_underlying_volatility.py 601229 --windows 20,60,250
    python calc_underlying_volatility.py 601229 --start 20250101

口径说明：
    close-to-close : 对数收益率标准差 × sqrt(252)
    parkinson      : 基于日内高低价的估计量，对跳空不敏感，通常低于 close-to-close
    年化系数默认 252（A 股一年约 243~252 个交易日，这里是国际惯例口径）
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from datetime import datetime, timedelta

TRADING_DAYS = 252


def to_returns(closes: list[float]) -> list[float]:
    """对数收益率序列。"""
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def close_to_close_vol(closes: list[float], window: int) -> float | None:
    """收盘价口径年化波动率。"""
    if len(closes) < window + 1:
        return None
    seg = closes[-(window + 1):]
    rets = to_returns(seg)
    return statistics.stdev(rets) * math.sqrt(TRADING_DAYS)


def parkinson_vol(highs: list[float], lows: list[float], window: int) -> float | None:
    """Parkinson 高低价口径年化波动率。

    sigma = sqrt( (1/(4*ln2*n)) * sum( ln(H_i/L_i)^2 ) * 252 )
    只利用日内振幅，不含跳空，因此对隔夜跳空不敏感。
    """
    if len(highs) < window:
        return None
    h, l = highs[-window:], lows[-window:]
    if any(x <= 0 for x in l):
        return None
    total = sum(math.log(h[i] / l[i]) ** 2 for i in range(window))
    return math.sqrt(total / (4 * math.log(2) * window) * TRADING_DAYS)


def fetch_daily(symbol: str, start: str, end: str):
    """拉取前复权日线。返回 (dates, closes, highs, lows)。"""
    import akshare as ak

    df = ak.stock_zh_a_hist(
        symbol=symbol, period="daily", start_date=start, end_date=end, adjust="qfq"
    )
    df = df.sort_values("日期").reset_index(drop=True)
    return (
        df["日期"].astype(str).tolist(),
        df["收盘"].astype(float).tolist(),
        df["最高"].astype(float).tolist(),
        df["最低"].astype(float).tolist(),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="正股历史波动率计算")
    ap.add_argument("symbol", help="正股代码，如 601229")
    ap.add_argument("--windows", default="20,60,120,250", help="波动率窗口，逗号分隔")
    ap.add_argument("--start", default=None, help="起始日期 YYYYMMDD，默认回溯 3 年")
    ap.add_argument("--end", default=None, help="结束日期 YYYYMMDD，默认今天")
    args = ap.parse_args()

    end = args.end or datetime.now().strftime("%Y%m%d")
    start = args.start or (datetime.now() - timedelta(days=365 * 3)).strftime("%Y%m%d")

    dates, closes, highs, lows = fetch_daily(args.symbol, start, end)
    windows = [int(w) for w in args.windows.split(",") if w.strip()]

    print(f"\n正股 {args.symbol} 历史波动率")
    print(f"样本区间: {dates[0]} ~ {dates[-1]}  共 {len(dates)} 根日K")
    print(f"最新收盘: {closes[-1]:.2f}")
    print(f"年化系数: {TRADING_DAYS}\n")
    print(f"{'窗口':>6} | {'收盘价口径':>12} | {'Parkinson':>12} | {'样本量':>6}")
    print("-" * 46)

    for w in windows:
        cc = close_to_close_vol(closes, w)
        pk = parkinson_vol(highs, lows, w)
        cc_s = f"{cc * 100:.2f}%" if cc is not None else "样本不足"
        pk_s = f"{pk * 100:.2f}%" if pk is not None else "样本不足"
        n = min(len(closes) - 1, w)
        print(f"{w:>6} | {cc_s:>12} | {pk_s:>12} | {n:>6}")

    print("\n注: Parkinson 只反映日内振幅、不含跳空，通常低于收盘价口径。")
    print("    转债定价建议以收盘价口径为主，Parkinson 作稳健性校验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
