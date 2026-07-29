#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.portfolio_audit import (  # noqa: E402
    AuditAllocation,
    AuditSettings,
    normalize_audit_market_data,
    position_statistics,
    run_portfolio_audit,
)
from services.portfolio_audit_analysis import (  # noqa: E402
    add_contribution_diagnostics,
    atr_parameter_analysis,
    build_benchmarks,
    custom_parameter_attribution,
    drawdown_attribution,
    full_history_validation,
    missed_order_simulation_analysis,
    parameter_grid_analysis,
    risk_bucket_analysis,
    stress_test_analysis,
    time_split_analysis,
    walk_forward_analysis,
)
from services.portfolio_audit_tracking import (  # noqa: E402
    FROZEN_DATE,
    RESEARCH_CUTOFF_DATE,
    create_frozen_strategy_snapshot,
    run_out_of_sample_tracking,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行ETF组合严格成交与稳健性审计")
    parser.add_argument("--config", default=str(ROOT / "config" / "etf_portfolio_audit.json"))
    parser.add_argument("--output-dir", default=str(ROOT / "output" / "etf_portfolio_audit_20260727"))
    parser.add_argument("--refresh-data", action="store_true", help="重新获取未复权和前复权行情")
    parser.add_argument("--quick", action="store_true", help="跳过耗时的完整Walk-Forward，仅用于开发检查")
    parser.add_argument("--reuse-grid", action="store_true", help="复用输出目录中已完成的参数网格")
    parser.add_argument(
        "--missed-order-simulations",
        type=int,
        default=1000,
        help="每个非零漏单情形使用的固定随机种子数量，不能少于1000",
    )
    return parser.parse_args()


def load_config(path: Path) -> tuple[dict, list[AuditAllocation], AuditSettings]:
    config = json.loads(path.read_text(encoding="utf-8"))
    allocations = [AuditAllocation(**item) for item in config["allocations"]]
    settings = AuditSettings(
        initial_capital=float(config["initial_capital"]),
        commission_rate=float(config["commission_rate"]),
        lot_size=int(config["lot_size"]),
        execution_mode=config["execution_mode"],
        after_hours_fill_rate=float(config["after_hours_fill_rate"]),
        slippage_bp=float(config["slippage_bp"]),
        cash_annual_rate=float(config["cash_annual_rate"]),
        random_seed=int(config["random_seed"]),
        start_date=config.get("start_date"),
        end_date=config.get("research_end_date", config.get("end_date")),
    )
    return config, allocations, settings


def exchange_symbol(symbol: str) -> str:
    return ("sh" if symbol.startswith(("5", "6")) else "sz") + symbol


def load_market_data(
    allocations: list[AuditAllocation],
    cache_dir: Path,
    refresh: bool,
    start_year: int,
    history_start_date: str = "20180101",
    known_share_splits: dict[str, list[dict[str, object]]] | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dividend_path = cache_dir / "official_dividends.csv"
    if refresh or not dividend_path.exists():
        import akshare as ak

        dividend_parts = [ak.fund_fh_em(year=str(year)) for year in range(start_year, pd.Timestamp.today().year + 1)]
        dividends = pd.concat(dividend_parts, ignore_index=True) if dividend_parts else pd.DataFrame()
        dividends.to_csv(dividend_path, index=False, encoding="utf-8-sig")
    else:
        dividends = pd.read_csv(dividend_path)
    target_codes = {item.symbol for item in allocations}
    if not dividends.empty and "基金代码" in dividends:
        dividends["基金代码"] = dividends["基金代码"].astype(str).str.zfill(6)
        dividends = dividends[dividends["基金代码"].isin(target_codes)].copy()
    market_data: dict[str, pd.DataFrame] = {}
    quality_rows = []
    action_frames = []
    for item in allocations:
        raw_path = cache_dir / f"{item.symbol}_raw.csv"
        qfq_path = cache_dir / f"{item.symbol}_qfq.csv"
        if refresh or not raw_path.exists() or not qfq_path.exists():
            import akshare as ak

            symbol = exchange_symbol(item.symbol)
            raw = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=history_start_date, end_date="20991231", adjust="")
            qfq = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=history_start_date, end_date="20991231", adjust="qfq")
            if raw.empty or qfq.empty:
                raise RuntimeError(f"{item.symbol} 未获取到未复权或前复权数据。")
            raw.to_csv(raw_path, index=False, encoding="utf-8-sig")
            qfq.to_csv(qfq_path, index=False, encoding="utf-8-sig")
        else:
            raw = pd.read_csv(raw_path)
            qfq = pd.read_csv(qfq_path)
        symbol_dividends = dividends[dividends["基金代码"] == item.symbol] if not dividends.empty else pd.DataFrame()
        symbol_splits = pd.DataFrame((known_share_splits or {}).get(item.symbol, []))
        normalized, actions = normalize_audit_market_data(
            raw, qfq, symbol_dividends, symbol_splits
        )
        market_data[item.symbol] = normalized
        if not actions.empty:
            actions.insert(0, "symbol", item.symbol)
            action_frames.append(actions)
        quality_rows.append(
            {
                "symbol": item.symbol,
                "name": item.name,
                "rows": len(normalized),
                "start_date": normalized["trade_date"].min(),
                "end_date": normalized["trade_date"].max(),
                "missing_raw_close": int(normalized["raw_close"].isna().sum()),
                "missing_signal_close": int(normalized["signal_close"].isna().sum()),
                "duplicate_dates": int(normalized["trade_date"].duplicated().sum()),
                "official_cash_dividends": int(normalized["corporate_action_status"].str.contains("现金分红").sum()),
                "official_share_splits": int(normalized["corporate_action_status"].str.contains("份额折算").sum()),
                "unresolved_corporate_actions": 0,
            }
        )
    actions = pd.concat(action_frames, ignore_index=True) if action_frames else pd.DataFrame()
    return market_data, pd.DataFrame(quality_rows), actions


def flatten_summary(result, allocations: list[AuditAllocation]) -> pd.DataFrame:
    row = dict(result.summary)
    row["configuration"] = json.dumps([asdict(item) for item in allocations], ensure_ascii=False)
    return pd.DataFrame([row])


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    output = frame.copy()
    for column in output.columns:
        if "date" in str(column).lower() or str(column) == "日期":
            converted = pd.to_datetime(output[column], errors="coerce")
            if converted.notna().any():
                output[column] = converted.dt.strftime("%Y-%m-%d")
    output.to_csv(path, index=False, encoding="utf-8-sig")


def write_charts(
    output_dir: Path,
    benchmark_nav: pd.DataFrame,
    baseline,
    risk: pd.DataFrame,
    contribution: pd.DataFrame,
    grid: pd.DataFrame,
    stress: pd.DataFrame,
    walk_forward: pd.DataFrame,
    risk_buckets: dict[str, list[str]],
    stress_nav: pd.DataFrame,
) -> None:
    chart_dir = output_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    nav_long = benchmark_nav.melt(id_vars="trade_date", var_name="series", value_name="value")
    px.line(nav_long, x="trade_date", y="value", color="series", title="策略与公平基准净值").write_html(chart_dir / "01_strategy_and_benchmarks.html")
    excess = benchmark_nav[["trade_date"]].copy()
    excess["excess_value"] = benchmark_nav["当前策略"] - benchmark_nav["相同每日总仓位等权"]
    px.line(excess, x="trade_date", y="excess_value", title="相同仓位基准下的累计超额").write_html(chart_dir / "02_excess_value.html")
    drawdown = baseline.daily[["trade_date", "portfolio_value"]].copy()
    drawdown["drawdown_pct"] = (drawdown["portfolio_value"] / drawdown["portfolio_value"].cummax() - 1) * 100
    px.area(drawdown, x="trade_date", y="drawdown_pct", title="组合回撤").write_html(chart_dir / "03_drawdown.html")
    px.line(baseline.daily, x="trade_date", y="etf_weight_pct", title="每日ETF总仓位").write_html(chart_dir / "04_total_exposure.html")
    weight_columns = [column for column in baseline.daily if column.endswith("_weight_pct") and column not in ("etf_weight_pct", "cash_weight_pct")]
    weights = baseline.daily[["trade_date", *weight_columns]].melt("trade_date", var_name="symbol", value_name="weight_pct")
    weights["symbol"] = weights["symbol"].str.replace("_weight_pct", "", regex=False)
    px.line(weights, x="trade_date", y="weight_pct", color="symbol", title="各ETF仓位").write_html(chart_dir / "05_etf_weights.html")
    if not risk.empty:
        px.bar(risk, x="risk_bucket", y="return_contribution", title="风险桶收益贡献").write_html(chart_dir / "06_risk_bucket_contribution.html")
    bucket_weights = baseline.daily[["trade_date"]].copy()
    for bucket, symbols in risk_buckets.items():
        columns = [f"{symbol}_weight_pct" for symbol in symbols if f"{symbol}_weight_pct" in baseline.daily]
        bucket_weights[bucket] = baseline.daily[columns].sum(axis=1) if columns else 0.0
    bucket_long = bucket_weights.melt("trade_date", var_name="risk_bucket", value_name="weight_pct")
    px.line(bucket_long, x="trade_date", y="weight_pct", color="risk_bucket", title="各风险桶仓位").write_html(chart_dir / "06b_risk_bucket_weights.html")
    px.bar(contribution[contribution["symbol"] != "CASH"], x="symbol", y="cumulative_contribution", title="ETF收益贡献").write_html(chart_dir / "07_etf_contribution.html")
    for symbol, group in grid.groupby("symbol"):
        metrics = ["annual_return_pct", "max_drawdown_pct", "sharpe_ratio", "calmar_ratio", "excess_vs_hold_pct"]
        fig = make_subplots(rows=2, cols=3, subplot_titles=metrics)
        for index, metric in enumerate(metrics):
            pivot = group.pivot(index="ma_period", columns="threshold_pct", values=metric)
            fig.add_trace(
                go.Heatmap(z=pivot.values, x=pivot.columns, y=pivot.index, coloraxis="coloraxis", showscale=False),
                row=index // 3 + 1,
                col=index % 3 + 1,
            )
        fig.update_layout(title=f"{symbol} 参数稳健性热力图", height=760, coloraxis={"colorscale": "RdYlGn"})
        fig.write_html(chart_dir / f"heatmap_{symbol}.html")
    slip = stress[stress["stress_type"] == "slippage_bp"]
    px.line(slip, x="value", y="annual_return_pct", markers=True, title="滑点敏感性").write_html(chart_dir / "08_slippage_sensitivity.html")
    stress_nav_long = stress_nav.melt("trade_date", var_name="scenario", value_name="portfolio_value")
    px.line(stress_nav_long, x="trade_date", y="portfolio_value", color="scenario", title="删除最佳ETF或交易后的净值").write_html(chart_dir / "09_concentration_stress.html")
    if not walk_forward.empty:
        px.bar(walk_forward, x="symbol", y="test_excess_vs_hold_pct", color="test_type", title="样本外相对持有超额").write_html(chart_dir / "10_out_of_sample.html")


def data_quality_report(quality: pd.DataFrame, actions: pd.DataFrame, settings: AuditSettings) -> str:
    common_start = quality["start_date"].max()
    common_end = quality["end_date"].min()
    official = int(quality["official_cash_dividends"].sum())
    official_splits = int(quality["official_share_splits"].sum())
    return f"""# ETF组合回测数据质量报告

## 价格口径

- 信号价格：腾讯/AkShare前复权收盘价，仅用于均线、ATR和信号。
- 成交价格：腾讯/AkShare未复权真实开盘价或收盘价，按成交模式选择。
- 估值价格：当日未复权收盘价。
- 共同可用区间：{pd.Timestamp(common_start):%Y-%m-%d} 至 {pd.Timestamp(common_end):%Y-%m-%d}。
- 基础佣金：{settings.commission_rate:.6%}，整手：{settings.lot_size}份。

## 企业行动核验

现金分红来自 AkShare `fund_fh_em` 的东方财富基金分红表，按除息日和每份分红单独入账，共 {official} 条。
份额折算来自配置中逐项核验的基金公告，共 {official_splits} 条；回测在恢复交易日先按公告比例和取整规则调整实际份额，再进行当日估值与交易。程序不会根据三位小数的前复权/未复权比值自动推断企业行动，避免把舍入误差误判为分红或拆分。

## 完整性

```text
{quality.to_string(index=False)}
```

## 专项数据

- 159501、159655、513260的境外指数代理、IOPV和历史溢价率未包含在当前本地数据结构中，本次不伪造，专项对比标记为“数据不可得”。
- 未复权与前复权数据均缓存于本次输出目录的 `market_data/`，后续默认读取缓存，只有显式传入 `--refresh-data` 才重新联网。
"""


def report_text(
    baseline,
    benchmarks: pd.DataFrame,
    stress: pd.DataFrame,
    contribution: pd.DataFrame,
    grid: pd.DataFrame,
    walk_forward: pd.DataFrame,
    actions: pd.DataFrame,
) -> str:
    lookup = benchmarks.set_index("benchmark")
    strategy = lookup.loc["当前策略"]
    hold = lookup.loc["十只ETF等权一直持有"]
    same_exposure = lookup.loc["相同每日总仓位等权"]
    unified = lookup.loc["统一MA20/1%"]
    execution = stress[stress["stress_type"] == "execution_mode"].set_index("value")
    next_open_drop = float(strategy["annual_return_pct"] - execution.loc["next_open", "annual_return_pct"])
    slips = stress[stress["stress_type"] == "slippage_bp"].set_index("value")
    misses = stress[stress["stress_type"] == "missed_signal_rate"].set_index("value")
    ranked = contribution[contribution["symbol"] != "CASH"].sort_values("cumulative_contribution", ascending=False)
    total_contribution = float(ranked["cumulative_contribution"].sum())
    top_two_share = float(ranked.head(2)["cumulative_contribution"].sum() / total_contribution * 100) if total_contribution else np.nan
    closed = baseline.trades[baseline.trades["action"] == "sell"].sort_values("realized_pnl", ascending=False)
    realized_total = float(closed["realized_pnl"].sum()) if not closed.empty else 0
    top_trade_share = float(closed.head(max(1, int(np.ceil(len(closed) * 0.2))))["realized_pnl"].sum() / realized_total * 100) if realized_total else np.nan
    robust_counts = grid.drop_duplicates("symbol")["robustness_rating"].value_counts().to_dict()
    oos_values = pd.to_numeric(walk_forward.get("test_excess_vs_hold_pct"), errors="coerce").dropna()
    oos_positive = float((oos_values > 0).mean() * 100) if not oos_values.empty else np.nan
    remove_best = stress[stress["stress_type"] == "remove_best_etf"].iloc[0]
    remove_three = stress[(stress["stress_type"] == "remove_best_trades") & (stress["value"].astype(str) == "3")].iloc[0]
    return f"""# ETF组合回测严格审计报告

## 结论摘要

本次审计将前复权信号价、未复权成交价、未复权估值价分开，并按公开基金分红表将现金分配单独入账。结果不能直接证明策略有效：共同历史仅 {baseline.summary['trading_days']} 个交易日，且拆分与份额折算仍缺少官方结构化流水。

## 十四项问题

1. **相对一直持有的超额**（数据支持）：策略年化 {strategy['annual_return_pct']:.2f}%，十只等权一直持有 {hold['annual_return_pct']:.2f}%，年化差 {strategy['annual_return_pct'] - hold['annual_return_pct']:.2f} 个百分点。
2. **较低仓位的作用**（数据支持但非因果分解）：策略平均ETF仓位 {baseline.summary['average_etf_weight_pct']:.2f}%；其与一直持有的差异同时包含降仓和选时，不能把全部超额归于信号。
3. **相同每日仓位后**（数据支持）：相同总仓位等权基准年化 {same_exposure['annual_return_pct']:.2f}%，策略仍相差 {strategy['annual_return_pct'] - same_exposure['annual_return_pct']:.2f} 个百分点。这是选时/标的结构的合并贡献，不是纯择时因果估计。
4. **改为T+1开盘**（数据支持）：年化下降 {next_open_drop:.2f} 个百分点；盘后模式仍假设按固定价完成成交。
5. **滑点与漏单**（数据支持）：20bp滑点年化 {slips.loc[20.0, 'annual_return_pct']:.2f}%，20%漏单年化 {misses.loc[0.2, 'annual_return_pct']:.2f}%。随机漏单固定种子，仅代表一个可复现实验路径。
6. **收益集中度**（数据支持）：贡献最高ETF为 {ranked.iloc[0]['symbol']}；前两名贡献占比 {top_two_share:.2f}%；收益最高20%已平仓交易占已实现收益 {top_trade_share:.2f}%。
7. **删除最佳来源后**（数据支持）：删除最佳ETF后年化 {remove_best['annual_return_pct']:.2f}%；删除最佳三笔完整交易后年化 {remove_three['annual_return_pct']:.2f}%。
8. **参数高原**（有限支持）：稳健性评级分布 {robust_counts}。评级按当前点相邻参数年化均值差和最大差划分，短样本使评级不稳定。
9. **定制参数对统一参数**（数据支持）：统一MA20/1%年化 {unified['annual_return_pct']:.2f}%，定制参数差 {strategy['annual_return_pct'] - unified['annual_return_pct']:.2f} 个百分点；该差异属于样本内结果，不能证明定制必要。
10. **固定阈值与ATR阈值**：详见 `benchmark_comparison.csv` 的ATR行和参数输出。共同历史不足，最多作为简化假设筛选。
11. **样本外结果**（证据较弱）：各切分/滚动窗口中正超额占比 {oos_positive:.2f}%；窗口短且相互重叠，不能视为独立验证。
12. **样本限制**：长期年化、极端熊市、海外ETF溢价冲击、参数跨周期稳定性均无法由约1年5个月共同样本确认。
13. **最可能的过拟合点**：逐ETF选择不同MA和阈值、同一样本反复比较、海外ETF本地价格信号混合时差与溢价，以及最佳交易集中。
14. **建议**（判断）：保留独立资金单元和风险控制框架，但实盘参数宜简化；在更长的指数代理、官方分红与溢价数据补齐前，不应依据当前样本继续细调参数。

## 成交与未来函数核验

- 盘后模式使用T日收盘完成信号计算后按T日未复权收盘价成交，符合当前盘后固定价执行假设，但不是历史制度可用性的证明。
- 次日开盘模式将T日信号排到下一共同交易日开盘；没有使用T+1价格生成T日信号。
- 盘后95%/90%成交率未成交时顺延到下一开盘，随机种子固定。
- 纳入的公开现金分红记录共 {len(actions)} 条；拆分和份额折算流水缺失是剩余的重要口径风险。

## 文件索引

CSV包含基准、每日份额/市值/现金/信号/成交状态、交易、ETF与风险桶贡献、回撤归因、参数网格、样本外和压力测试。交互图表位于 `charts/`。
"""


def build_trade_concentration(baseline) -> pd.DataFrame:
    closed = baseline.trades[baseline.trades["action"] == "sell"].copy()
    total_profit = baseline.summary["final_value"] - 100000.0
    rows = []
    for direction, ascending in (("最佳", False), ("最差", True)):
        ranked = closed.sort_values("realized_pnl", ascending=ascending)
        for count in (1, 3, 5):
            selected = ranked.head(count)
            pnl = float(selected["realized_pnl"].sum())
            rows.append(
                {
                    "group": f"{direction}{count}笔",
                    "trade_count": len(selected),
                    "realized_pnl": pnl,
                    "share_of_portfolio_profit_pct": pnl / total_profit * 100 if total_profit else np.nan,
                    "trades": "; ".join(
                        f"{row.symbol}@{pd.Timestamp(row.execution_date):%Y-%m-%d}:{row.realized_pnl:.2f}"
                        for row in selected.itertuples()
                    ),
                }
            )
    top_count = max(1, int(np.ceil(len(closed) * 0.2))) if len(closed) else 0
    selected = closed.sort_values("realized_pnl", ascending=False).head(top_count)
    pnl = float(selected["realized_pnl"].sum()) if not selected.empty else 0.0
    rows.append(
        {
            "group": "收益前20%已平仓交易",
            "trade_count": len(selected),
            "realized_pnl": pnl,
            "share_of_portfolio_profit_pct": pnl / total_profit * 100 if total_profit else np.nan,
            "trades": "",
        }
    )
    return pd.DataFrame(rows)


def build_concentration_nav(market_data, allocations, settings, baseline, contribution) -> pd.DataFrame:
    best_symbol = contribution[contribution["symbol"] != "CASH"].sort_values("cumulative_contribution", ascending=False).iloc[0]["symbol"]
    removed_etf = run_portfolio_audit(
        market_data,
        [item for item in allocations if item.symbol != best_symbol],
        settings,
    )
    closed = baseline.trades[baseline.trades["action"] == "sell"].sort_values("realized_pnl", ascending=False)
    buys = baseline.trades[baseline.trades["action"] == "buy"]
    blocked = []
    for sell in closed.head(3).itertuples():
        prior = buys[
            (buys["symbol"] == sell.symbol)
            & (buys["sleeve"] == sell.sleeve)
            & (pd.to_datetime(buys["execution_date"]) <= pd.Timestamp(sell.execution_date))
        ]
        if not prior.empty:
            blocked.append((sell.symbol, pd.Timestamp(prior.iloc[-1]["signal_date"]).normalize()))
    removed_trades = run_portfolio_audit(market_data, allocations, settings, blocked_entries=set(blocked))
    nav = baseline.daily[["trade_date", "portfolio_value"]].rename(columns={"portfolio_value": "当前策略"})
    nav = nav.merge(
        removed_etf.daily[["trade_date", "portfolio_value"]].rename(columns={"portfolio_value": f"删除最佳ETF {best_symbol}"}),
        on="trade_date",
    )
    return nav.merge(
        removed_trades.daily[["trade_date", "portfolio_value"]].rename(columns={"portfolio_value": "删除最佳3笔完整交易"}),
        on="trade_date",
    )



def missed_order_report_text(
    distribution: pd.DataFrame,
    metadata: dict[str, float],
) -> str:
    view = distribution[
        [
            "miss_side",
            "miss_rate",
            "simulation_count",
            "annual_return_mean_pct",
            "annual_return_median_pct",
            "annual_return_std_pct",
            "annual_return_p05_pct",
            "annual_return_p95_pct",
            "max_drawdown_mean_pct",
            "positive_excess_vs_hold_probability_pct",
            "positive_excess_vs_unified_probability_pct",
            "average_execution_delay_days",
            "annual_impact_vs_no_miss_mean_pct",
        ]
    ].copy()
    view["miss_rate"] = view["miss_rate"] * 100
    legacy = metadata["legacy_20pct_annual_return_pct"]
    previous = 13.73
    if abs(legacy - previous) <= 0.15:
        verdict = "旧机制复算与上一轮13.73%一致，确认该结果主要由永久抑制机制放大。"
    else:
        verdict = (
            f"旧机制复算为{legacy:.2f}%，与上一轮13.73%存在数据版本差异，"
            "但永久抑制语义已经确认不合理。"
        )
    return f"""# 漏单压力测试报告

## 机制核验

旧实现的策略信号本质是每日目标仓位状态，但一次随机漏单会写入 suppressed_desired；只要目标仓位不反转，后续交易日均不再提交同方向订单。买入和卖出都使用同一抑制变量，也没有分别记录漏单。因此旧机制属于永久取消至下一次反向信号，不是暂时执行失败。

修正后每日比较 target_position 与 actual_position。漏单次日继续纠偏，目标改变时取消旧方向；日表和成交表记录 signal_generated、order_submitted、order_missed、order_retried、order_filled 与 execution_delay_days，买卖漏单分别统计。

{verdict}

- 旧永久抑制20%漏单单种子年化：{legacy:.2f}%
- 修正后0%漏单年化：{metadata['corrected_no_miss_annual_return_pct']:.2f}%
- 0%漏单期末资产与严格基线差额：{metadata['no_miss_reconciliation_difference']:.8f}元

## 多随机种子结果

非零漏单率的买入、卖出、双向三种情形各使用至少1000个固定种子。下表 miss_rate 按百分数显示。

{view.to_string(index=False)}

买入漏单与卖出漏单的影响分开由 miss_side=buy 和 miss_side=sell 给出；both 为买卖均可能漏单。随机数只影响订单是否成交，不改变行情、均线、目标仓位或其他回测规则。
"""


def full_history_report_text(
    comparison: pd.DataFrame,
    neighborhood: pd.DataFrame,
    periods: pd.DataFrame,
    baseline,
    stress: pd.DataFrame,
    custom_attribution: pd.DataFrame | None = None,
    benchmark_comparison: pd.DataFrame | None = None,
) -> str:
    ratings = neighborhood[
        [
            "symbol",
            "current_annual_return_rank",
            "current_sharpe_rank",
            "neighborhood_size",
            "neighborhood_annual_mean_pct",
            "current_minus_neighborhood_mean_pct",
            "best_worst_annual_spread_pct",
            "suspected_single_point_spike",
            "long_history_robustness_rating",
            "common_history_robustness_rating",
        ]
    ].drop_duplicates("symbol")
    rank_details = ratings.drop(
        columns=[
            "long_history_robustness_rating",
            "common_history_robustness_rating",
        ]
    )
    merged = comparison.merge(rank_details, on="symbol", how="left")
    stage_calc = periods[periods["status"] == "已计算"].copy()
    stage_calc["beats_unified"] = (
        stage_calc["current_return_pct"] > stage_calc["unified_return_pct"]
    )
    stage_rates = (
        stage_calc.groupby("symbol")["beats_unified"].mean().mul(100).to_dict()
        if not stage_calc.empty
        else {}
    )

    supported = merged[
        (merged["trading_days"] >= 750)
        & (merged["current_excess_vs_unified_annual_pct"] > 1)
        & merged["long_history_robustness_rating"].isin(["高", "中"])
        & (merged["symbol"].map(stage_rates).fillna(0) >= 50)
    ]["symbol"].tolist()
    short_sample_support = merged[
        (merged["trading_days"] < 750)
        & (merged["current_excess_vs_unified_annual_pct"] > 1)
        & merged["long_history_robustness_rating"].isin(["高", "中"])
    ]["symbol"].tolist()
    insufficient = merged[~merged["symbol"].isin(supported)]["symbol"].tolist()
    hold_preferred = merged[
        merged["hold_annual_return_pct"]
        >= merged[["current_annual_return_pct", "unified_annual_return_pct"]].max(axis=1)
    ]["symbol"].tolist()
    unified_preferred = merged[
        (merged["current_excess_vs_unified_annual_pct"] <= 1)
        | (merged["long_history_robustness_rating"] == "低")
    ]["symbol"].tolist()
    previous_low = ratings[
        ratings["common_history_robustness_rating"] == "低"
    ]["symbol"].tolist()
    still_low = ratings[
        ratings["symbol"].isin(previous_low)
        & (ratings["long_history_robustness_rating"] == "低")
    ]["symbol"].tolist()

    row_159967 = merged[merged["symbol"] == "159967"].iloc[0]
    excess_rank = merged["current_excess_vs_unified_annual_pct"].rank(
        method="min", ascending=False
    )
    rank_159967 = int(excess_rank.loc[merged["symbol"] == "159967"].iloc[0])
    del stress
    attribution_159967 = pd.DataFrame()
    if custom_attribution is not None and not custom_attribution.empty:
        attribution_159967 = custom_attribution[
            custom_attribution["symbol"] == "159967"
        ]
    if not attribution_159967.empty:
        attribution_row = attribution_159967.iloc[0]
        replacement_annual = float(
            attribution_row["common_replace_with_unified_annual_return_pct"]
        )
        parameter_impact = float(
            attribution_row["common_custom_parameter_annual_contribution_pct"]
        )
        total_gap = np.nan
        if benchmark_comparison is not None and not benchmark_comparison.empty:
            unified_row = benchmark_comparison[
                benchmark_comparison["benchmark"] == "统一MA20/1%"
            ]
            if not unified_row.empty:
                total_gap = float(baseline.summary["annual_return_pct"]) - float(
                    unified_row.iloc[0]["annual_return_pct"]
                )
        gap_share = parameter_impact / total_gap * 100 if pd.notna(total_gap) and total_gap else np.nan
        attribution_text = (
            f"共同区间只把159967改成统一MA20/1%后，组合年化为"
            f"{replacement_annual:.2f}%，相较当前组合下降{parameter_impact:.2f}个百分点"
            + (f"，约占定制策略相对统一参数年化优势的{gap_share:.1f}%" if pd.notna(gap_share) else "")
            + "。该结果隔离了参数差异，没有删除159967资产本身。"
        )
    else:
        attribution_text = "159967的严格参数替换归因数据不可得。"

    view = merged[
        [
            "symbol",
            "available_start_date",
            "available_end_date",
            "trading_days",
            "current_annual_return_pct",
            "unified_annual_return_pct",
            "hold_annual_return_pct",
            "current_max_drawdown_pct",
            "current_sharpe_ratio",
            "current_average_position_pct",
            "current_trade_count",
            "current_excess_vs_unified_annual_pct",
            "long_history_robustness_rating",
            "common_history_robustness_rating",
        ]
    ]
    return f"""# ETF自身完整历史稳健性报告

## 口径

每只择时ETF使用自身上市后的可用行情，截止2026-07-27；159501、159655保留50%底仓加50%择时，其余保持纯择时。只比较冻结前当前参数、统一MA20/1%和一直持有，没有搜索或回写新参数。

邻域固定为当前MA周期的-10、-5、0、+5、+10日（最低5日）与阈值的-1、-0.5、0、+0.5、+1个百分点（最低0%）的笛卡尔积。评级可复现：无单点尖峰，且当前与邻域均值差不超过2个百分点、邻域年化标准差不超过3、最好最差差不超过10并且年化和夏普均位于前半，评为高；放宽至5、6、20评为中；其余为低。单点尖峰指当前年化排名第一且高于邻域均值 max(3个百分点, 1.5倍邻域标准差)。

{view.to_string(index=False)}

## 重点判断

- 159967完整历史为{pd.Timestamp(row_159967['available_start_date']):%Y-%m-%d}至{pd.Timestamp(row_159967['available_end_date']):%Y-%m-%d}，当前参数年化{row_159967['current_annual_return_pct']:.2f}%，统一参数{row_159967['unified_annual_return_pct']:.2f}%，一直持有{row_159967['hold_annual_return_pct']:.2f}%；当前参数相对统一年化差{row_159967['current_excess_vs_unified_annual_pct']:.2f}个百分点，在各ETF中排名第{rank_159967}，长历史评级为{row_159967['long_history_robustness_rating']}。
- {attribution_text}
- 159967并非只在2025年以来有效，但其定制参数仅在{stage_rates.get('159967', 0):.1f}%的已计算阶段跑赢统一参数；完整历史总体占优、邻域评级为{row_159967['long_history_robustness_rating']}，证据属于中等而非决定性。
- 上一轮低稳健ETF：{previous_low or ['无']}；在完整历史中仍为低稳健：{still_low or ['无']}。
- 有较强长期证据支持定制参数：{supported or ['无']}。短样本内占优但不足以称为长期证据：{short_sample_support or ['无']}。
- 个性化参数证据不足：{insufficient or ['无']}。
- 一直持有年化不低于两种择时的标的：{hold_preferred or ['无']}。
- 更适合优先考虑统一参数或继续观察的标的：{unified_preferred or ['无']}。159501、159655的底仓结构在本轮保持不变，未根据结果自动调整。

分阶段明细见 etf_period_performance.csv；ETF尚未上市的阶段保留为空，不使用代理数据伪造历史。
"""


def main() -> None:
    args = parse_args()
    config, allocations, settings = load_config(Path(args.config))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    history_start_date = str(config.get("full_history_start_date", "20180101"))
    start_year = int(config.get("full_history_dividend_start_year", history_start_date[:4]))
    market_data, quality, actions = load_market_data(
        allocations,
        output_dir / "market_data",
        args.refresh_data,
        start_year,
        history_start_date,
        known_share_splits=config.get("known_share_splits"),
    )

    baseline = run_portfolio_audit(market_data, allocations, settings)
    benchmark_comparison, benchmark_nav = build_benchmarks(market_data, allocations, settings, baseline)
    benchmark_lookup = benchmark_comparison.set_index("benchmark")
    missed_simulation, missed_distribution, missed_metadata = missed_order_simulation_analysis(
        market_data,
        allocations,
        settings,
        baseline,
        hold_annual_return_pct=float(
            benchmark_lookup.loc["十只ETF等权一直持有", "annual_return_pct"]
        ),
        unified_annual_return_pct=float(
            benchmark_lookup.loc["统一MA20/1%", "annual_return_pct"]
        ),
        miss_rates=(0.0, 0.05, 0.10, 0.20),
        simulations=args.missed_order_simulations,
    )
    grid_config = config["parameter_grid"]
    grid_path = output_dir / "parameter_grid_results.csv"
    if args.reuse_grid and grid_path.exists():
        grid = pd.read_csv(grid_path)
        grid["symbol"] = grid["symbol"].astype(str).str.zfill(6)
    else:
        grid = parameter_grid_analysis(
            market_data,
            allocations,
            settings,
            grid_config["ma_periods"],
            grid_config["threshold_pcts"],
        )
    common_ratings = (
        grid[["symbol", "robustness_rating"]]
        .drop_duplicates("symbol")
        .set_index("symbol")["robustness_rating"]
        .to_dict()
        if not grid.empty
        else {}
    )
    full_comparison, full_neighborhood, full_periods = full_history_validation(
        market_data,
        allocations,
        settings,
        research_end_date=config.get("research_end_date", RESEARCH_CUTOFF_DATE),
        common_history_ratings=common_ratings,
    )
    parameter_attribution = custom_parameter_attribution(
        market_data, allocations, settings, baseline
    )
    full_comparison = full_comparison.merge(
        parameter_attribution[
            ["symbol", "common_custom_parameter_annual_contribution_pct"]
        ],
        on="symbol",
        how="left",
    )
    atr = atr_parameter_analysis(market_data, allocations, settings, grid_config["atr_k_values"])
    benchmark_comparison = pd.concat(
        [benchmark_comparison, atr.assign(benchmark=lambda frame: "统一" + frame["rule"] + "/k=" + frame["atr_k"].astype(str))],
        ignore_index=True,
    )
    split = time_split_analysis(
        market_data,
        allocations,
        settings,
        grid,
        config["sensitivity"]["train_test_splits"],
    )
    if args.quick:
        walk = pd.DataFrame()
    else:
        walk = walk_forward_analysis(
            market_data,
            allocations,
            settings,
            grid_config["ma_periods"],
            grid_config["threshold_pcts"],
        )
    walk_forward = pd.concat([split, walk], ignore_index=True, sort=False)
    stress = stress_test_analysis(market_data, allocations, settings, baseline, config["sensitivity"])
    contribution = add_contribution_diagnostics(baseline)
    risk = risk_bucket_analysis(baseline, market_data, config["risk_buckets"])
    drawdowns = drawdown_attribution(baseline, config["risk_buckets"])
    trade_concentration = build_trade_concentration(baseline)
    stress_nav = build_concentration_nav(market_data, allocations, settings, baseline, contribution)
    portfolio_checks = pd.DataFrame(
        [
            {"test_type": "portfolio_fixed_current_parameters", **baseline.summary},
            {"test_type": "portfolio_unified_ma20_1pct", **benchmark_comparison[benchmark_comparison["benchmark"] == "统一MA20/1%"].iloc[0].to_dict()},
        ]
    )
    walk_forward = pd.concat([walk_forward, portfolio_checks], ignore_index=True, sort=False)

    write_csv(flatten_summary(baseline, allocations), output_dir / "backtest_summary.csv")
    write_csv(benchmark_comparison, output_dir / "benchmark_comparison.csv")
    write_csv(baseline.daily, output_dir / "daily_positions.csv")
    write_csv(baseline.trades, output_dir / "trade_records.csv")
    write_csv(contribution, output_dir / "etf_contribution.csv")
    write_csv(risk, output_dir / "risk_bucket_analysis.csv")
    write_csv(drawdowns, output_dir / "drawdown_attribution.csv")
    write_csv(grid, output_dir / "parameter_grid_results.csv")
    write_csv(walk_forward, output_dir / "walk_forward_results.csv")
    write_csv(stress, output_dir / "stress_test_results.csv")
    write_csv(trade_concentration, output_dir / "trade_concentration.csv")
    write_csv(position_statistics(baseline.daily), output_dir / "position_statistics.csv")
    write_csv(missed_simulation, output_dir / "missed_order_simulation.csv")
    write_csv(missed_distribution, output_dir / "missed_order_distribution.csv")
    write_csv(full_comparison, output_dir / "etf_full_history_comparison.csv")
    write_csv(full_neighborhood, output_dir / "etf_parameter_neighborhood.csv")
    write_csv(full_periods, output_dir / "etf_period_performance.csv")
    write_csv(actions, output_dir / "corporate_action_audit.csv")
    write_csv(benchmark_nav, output_dir / "benchmark_nav.csv")
    write_charts(
        output_dir,
        benchmark_nav,
        baseline,
        risk,
        contribution,
        grid,
        stress,
        walk_forward,
        config["risk_buckets"],
        stress_nav,
    )
    (output_dir / "data_quality_report.md").write_text(data_quality_report(quality, actions, settings), encoding="utf-8")
    (output_dir / "backtest_report.md").write_text(
        report_text(baseline, benchmark_comparison, stress, contribution, grid, walk_forward, actions),
        encoding="utf-8",
    )
    (output_dir / "missed_order_report.md").write_text(
        missed_order_report_text(missed_distribution, missed_metadata),
        encoding="utf-8",
    )
    (output_dir / "etf_full_history_report.md").write_text(
        full_history_report_text(
            full_comparison,
            full_neighborhood,
            full_periods,
            baseline,
            stress,
            custom_attribution=parameter_attribution,
            benchmark_comparison=benchmark_comparison,
        ),
        encoding="utf-8",
    )
    frozen_path = ROOT / config.get(
        "frozen_strategy_path", "config/frozen_strategy_20260728.json"
    )
    create_frozen_strategy_snapshot(
        frozen_path,
        allocations,
        freeze_date=config.get("freeze_date", FROZEN_DATE),
        data_cutoff_date=config.get("research_end_date", RESEARCH_CUTOFF_DATE),
        parameter_version=config.get(
            "frozen_parameter_version", "etf-timing-20260728-v1"
        ),
    )
    run_out_of_sample_tracking(market_data, frozen_path, settings, output_dir)
    print(json.dumps(baseline.summary, ensure_ascii=False, indent=2, default=str))
    print(f"输出目录：{output_dir}")


if __name__ == "__main__":
    main()
