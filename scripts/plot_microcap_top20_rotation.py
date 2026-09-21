#!/usr/bin/env python3
"""微盘股最小20只轮动策略回测：图表输出（红涨绿跌，中文标注）。

读取 run_backtest 产出的 CSV，生成 6 张图到 <out-dir>/charts/。
用法：python scripts/plot_microcap_top20_rotation.py [--out-dir ...]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "output" / "microcap_top20_rotation_20260105_20260908"

UP = "#E24B4A"      # 红：涨
DOWN = "#3B8C5A"    # 绿：跌
VERSION_COLORS = {
    "A_原始规则": "#8C8C8C",
    "B_无未来函数": "#185FA5",
    "B2_严格无未来函数": "#BA7517",
}


def setup_font() -> str:
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            font_manager.fontManager.addfont(path)
            name = font_manager.FontProperties(fname=path).get_name()
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return name
    plt.rcParams["axes.unicode_minus"] = False
    return "DejaVu Sans"


def style(ax, title: str, ylabel: str | None = None):
    ax.set_title(title, fontsize=12, pad=10)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(True, linestyle=":", linewidth=0.6, alpha=0.6)
    ax.tick_params(labelsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    out_dir: Path = args.out_dir
    charts = out_dir / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    setup_font()

    nav = pd.read_csv(out_dir / "daily_nav.csv", encoding="utf-8-sig", parse_dates=["日期"])
    rebal = pd.read_csv(out_dir / "weekly_rebalance.csv", encoding="utf-8-sig", parse_dates=["调仓日期"])
    holdings = pd.read_csv(out_dir / "weekly_holdings.csv", encoding="utf-8-sig", parse_dates=["调仓日期"])
    annual = pd.read_csv(out_dir / "annual_performance.csv", encoding="utf-8-sig")

    # 1) 净值曲线
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=150)
    for version, color in VERSION_COLORS.items():
        sub = nav[nav["版本"] == version].sort_values("日期")
        if sub.empty:
            continue
        ax.plot(sub["日期"], sub["账户净值"], color=color, linewidth=1.5, label=f"{version} · 账户净值")
    b = nav[nav["版本"] == "B_无未来函数"].sort_values("日期")
    ax.plot(b["日期"], b["策略净值"], color="#0F6E56", linewidth=1.2, linestyle="--", label="B · 策略净值（剔除备用现金）")
    ax.axhline(1.0, color="#888780", linewidth=0.7, linestyle="-")
    style(ax, "微盘股最小20只轮动策略 · 净值曲线（2026-01-05 起，初始=1）", "净值")
    ax.legend(fontsize=9, frameon=False, loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    fig.tight_layout()
    fig.savefig(charts / "01_nav_curve.png")
    plt.close(fig)

    # 2) 回撤曲线
    fig, ax = plt.subplots(figsize=(11, 4.6), dpi=150)
    for version, color in VERSION_COLORS.items():
        sub = nav[nav["版本"] == version].sort_values("日期")
        if sub.empty:
            continue
        series = sub["账户净值"].to_numpy()
        dd = series / np.maximum.accumulate(series) - 1.0
        ax.plot(sub["日期"], dd * 100, color=color, linewidth=1.3, label=version)
    ax.fill_between(b["日期"], (b["账户净值"].to_numpy() / np.maximum.accumulate(b["账户净值"].to_numpy()) - 1.0) * 100, 0, color="#185FA5", alpha=0.12)
    style(ax, "回撤曲线（账户口径）", "回撤 (%)")
    ax.legend(fontsize=9, frameon=False, loc="lower left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    fig.tight_layout()
    fig.savefig(charts / "02_drawdown.png")
    plt.close(fig)

    # 3) 每周收益率（Version B）
    fig, ax = plt.subplots(figsize=(11, 4.8), dpi=150)
    wp = pd.read_csv(out_dir / "weekly_nav_points.csv", encoding="utf-8-sig", parse_dates=["周最后交易日"])
    wb = wp[wp["版本"] == "B_无未来函数"].sort_values("周最后交易日").reset_index(drop=True)
    wr = wb["总资产"].pct_change() * 100
    colors = [UP if v >= 0 else DOWN for v in wr.fillna(0)]
    ax.bar(wb["周最后交易日"], wr, color=colors, width=4.0)
    ax.axhline(0, color="#5F5E5A", linewidth=0.7)
    style(ax, "每周收益率 · Version B（自然周，按周最后交易日权益计）", "周收益率 (%)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    fig.tight_layout()
    fig.savefig(charts / "03_weekly_returns.png")
    plt.close(fig)

    # 4) 年度收益率
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=150)
    ann = annual.copy()
    labels = [f"{r['年份']}\n{r['版本']}" for _, r in ann.iterrows()]
    vals = ann["年度收益率"].to_numpy() * 100
    colors = [UP if v >= 0 else DOWN for v in vals]
    bars = ax.bar(labels, vals, color=colors, width=0.55)
    for bar, v in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width() / 2, v + (0.6 if v >= 0 else -1.6), f"{v:.2f}%", ha="center", fontsize=9)
    ax.axhline(0, color="#5F5E5A", linewidth=0.7)
    style(ax, "年度收益率（2026 为不完整年度）", "年度收益率 (%)")
    fig.tight_layout()
    fig.savefig(charts / "04_annual_returns.png")
    plt.close(fig)

    # 5) 每周换入换出股票数量（Version B）
    fig, ax = plt.subplots(figsize=(11, 4.8), dpi=150)
    rb = rebal[rebal["版本"] == "B_无未来函数"].sort_values("调仓日期")
    x = np.arange(len(rb))
    width = 0.4
    ax.bar(x - width / 2, rb["新进入"], width=width, color=UP, label="新进入（买入）")
    ax.bar(x + width / 2, -rb["被剔除"], width=width, color=DOWN, label="被剔除（卖出）")
    ax.axhline(0, color="#5F5E5A", linewidth=0.7)
    step = max(1, len(rb) // 12)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([d.strftime("%m-%d") for d in rb["调仓日期"][::step]], fontsize=8)
    style(ax, "每周换入 / 换出股票数量 · Version B", "只数")
    ax.legend(fontsize=9, frameon=False)
    fig.tight_layout()
    fig.savefig(charts / "05_weekly_turnover.png")
    plt.close(fig)

    # 6) 持仓市值分布
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), dpi=150)
    hb = holdings[holdings["版本"] == "B_无未来函数"]
    last_date = hb["调仓日期"].max()
    latest = hb[hb["调仓日期"] == last_date].sort_values("持仓市值", ascending=False)
    axes[0].barh(
        [f"{r['股票名称']}({r['股票代码']})" for _, r in latest.iterrows()],
        latest["持仓市值"] / 10000,
        color="#185FA5",
    )
    axes[0].invert_yaxis()
    style(axes[0], f"最新调仓日持仓市值 · {last_date.strftime('%Y-%m-%d')}", "持仓市值（万元）")
    axes[0].tick_params(labelsize=8)
    axes[1].hist(hb["持仓市值"] / 10000, bins=24, color="#85B7EB", edgecolor="#185FA5", linewidth=0.6)
    axes[1].axvline((hb["持仓市值"] / 10000).median(), color=UP, linewidth=1.4, linestyle="--", label=f"中位数 {(hb['持仓市值']/10000).median():.2f} 万元")
    style(axes[1], "全部调仓快照的单只持仓市值分布 · Version B", "出现次数")
    axes[1].set_xlabel("单只持仓市值（万元）", fontsize=10)
    axes[1].legend(fontsize=9, frameon=False)
    fig.tight_layout()
    fig.savefig(charts / "06_holdings_distribution.png")
    plt.close(fig)

    print("charts written to", charts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
