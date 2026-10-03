"""Offline ETF MA timing parameter research.

Reads the local CSV directory only, writes CSV/Markdown results under
``output/etf_ma_param_research/<timestamp>/`` and never changes position settings.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.etf_ma_param_research import (  # noqa: E402
    DEFAULT_DATA_DIR,
    FAMILY_FIXED,
    FAMILY_LABELS,
    FAMILY_VOL,
    FIXED_THRESHOLDS_PCT,
    MA_PERIODS,
    RESEARCH_END,
    VOL_MULTIPLIERS,
    round_for_export,
    run_research,
)

KIND_LABELS = {"etf_adjusted": "ETF差值前复权", "index_proxy": "价格指数代理", "spliced": "指数+ETF拼接"}
EXPORT_TABLES = (
    "summary",
    "candidates",
    "base_allocation",
    "benchmarks",
    "sensitivity",
    "common_oos_159545",
)


def _pct(value: float) -> str:
    return "—" if pd.isna(value) else f"{value:.2f}%"


def _days(value: float) -> str:
    return "—" if pd.isna(value) else f"{value:.0f}天"


def _fraction(value: float) -> str:
    return "—" if pd.isna(value) else f"{value * 100:.0f}%"


def _metrics(row, prefix: str) -> str:
    get = row.get if isinstance(row, (dict, pd.Series)) else lambda key: getattr(row, key)
    return (
        f"{_pct(get(f'{prefix}annual_return_pct'))} / {_pct(get(f'{prefix}max_drawdown_pct'))} / "
        f"{_days(get(f'{prefix}longest_underwater_days'))} / {_days(get(f'{prefix}avg_recovery_days'))}"
    )


def _holding(row) -> str:
    code = row.holding_code if isinstance(row.holding_code, str) else "—"
    return f"{code}（{row.relation}）"


def build_report(tables: dict[str, object], end: str) -> str:
    summary: pd.DataFrame = tables["summary"]
    base: pd.DataFrame = tables["base_allocation"]
    benchmarks: pd.DataFrame = tables["benchmarks"]
    sensitivity: pd.DataFrame = tables["sensitivity"]
    rule = tables["plateau_rule"]
    suitable = summary[summary["suitable"]]
    lines = [
        "# ETF 均线择时参数研究：参数平台推荐",
        "",
        f"研究区间 2019-09-30 至 {end}；生成时间 {datetime.now():%Y-%m-%d %H:%M:%S}。",
        "",
        "## 方法",
        "",
        "- 打分：最长水下期 3、年化收益 3、最大回撤 2、平均回撤修复天数 2，在同一序列去重后的 117 组均线规则（MA5–60 × 固定阈值 0–3% / 波动率阈值 0–1.5σ）之间按百分位计分，样本内、样本外分别计分。",
        "- 好参数：把该参数与一直持有放在一起按上述规则打分，样本内和样本外都高于一直持有。",
        f"- 参数平台：本身是好参数，且同族 MA×阈值网格中相邻格子（3×3 范围，至少 {rule.min_neighbors} 个）至少 {rule.neighbor_share:.0%} 也是好参数。没有任何平台参数的序列判定为**不适合均线择时**。",
        "- 推荐参数：平台参数中，取「自身 + 相邻格子」的平均稳健分最高者；稳健分 = (样本内百分位 + 样本外百分位) / 2。",
        "- 底仓：固定推荐参数，在 0/25/50/75/100% 底仓（其余择时，两个子账户独立运行）中，取样本内与样本外 5 档综合分平均最高者，平分取较低底仓。",
        "- 推荐底仓为 100% 表示该参数下择时不如一直持有，按「一直持有」执行；底仓为 0% 表示纯择时。",
        "- 159545 以恒生港股通高息低波指数拼接序列为推荐依据，159201 以国证自由现金流指数（980092）为推荐依据；159545 ETF 自身历史仅作对照。",
        "- 指标格式：年化收益 / 最大回撤 / 最长水下期 / 平均修复天数。",
        "",
        "## 推荐配置",
        "",
        "| 序列 | 持仓对照 | 结论 | 推荐参数 | 底仓 | 平台格数 | 推荐方案 全区间 | 推荐方案 样本内 | 推荐方案 样本外 | 买入持有 全区间 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary.itertuples(index=False):
        if row.suitable:
            lines.append(
                f"| {row.series} {row.name} | {_holding(row)} | 适合择时 | {row.recommended_label} | "
                f"{_fraction(row.recommended_base_fraction)} | {row.plateau_cells} | {_metrics(row, 'rec_full_')} | "
                f"{_metrics(row, 'rec_is_')} | {_metrics(row, 'rec_oos_')} | {_metrics(row, 'hold_full_')} |"
            )
        else:
            lines.append(
                f"| {row.series} {row.name} | {_holding(row)} | **不适合均线择时**（无参数平台） | — | — | 0 | — | — | — | "
                f"{_metrics(row, 'hold_full_')} |"
            )
    lines += [
        "",
        f"适合择时 {len(suitable)} 个，不适合 {len(summary) - len(suitable)} 个。参数平台图见 plateau_maps.md。",
        "",
        "## 推荐参数的平台质量",
        "",
        "| 序列 | 推荐参数 | 阈值族 | 样本内百分位 | 样本外百分位 | 相邻好参数占比 | 平台平均稳健分 | 平台格数 | 样本内优于持有/117 | 样本外优于持有/117 | 两段均优/117 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary.itertuples(index=False):
        counts = f"{row.beats_hold_is_rules} | {row.beats_hold_oos_rules} | {row.good_rules}"
        if not row.suitable:
            lines.append(f"| {row.series} | — | — | — | — | — | — | 0 | {counts} |")
            continue
        lines.append(
            f"| {row.series} | {row.recommended_label} | {FAMILY_LABELS[row.recommended_family]} | "
            f"{row.recommended_is_score_pct:.2f} | {row.recommended_oos_score_pct:.2f} | "
            f"{row.recommended_neighbor_good_share:.0%} | {row.recommended_plateau_score:.3f} | {row.plateau_cells} | {counts} |"
        )
    lines += [
        "",
        "## 底仓配置明细（推荐参数下 5 档）",
        "",
        "| 序列 | 底仓 | 样本内 | 样本外 | 全区间 | 样本内分 | 样本外分 | 平均分 | 推荐 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in base.itertuples(index=False):
        row = item._asdict()
        lines.append(
            f"| {item.series} | {_fraction(item.base_fraction)} | {_metrics(row, 'is_')} | {_metrics(row, 'oos_')} | "
            f"{_metrics(row, 'full_')} | {item.is_score_base:.3f} | {item.oos_score_base:.3f} | "
            f"{item.robust_score_base:.3f} | {'★' if item.chosen else ''} |"
        )
    lines += [
        "",
        "## 样本区间与分档",
        "",
        "| 序列 | 数据 | 评估起点 | 样本内截止 | 样本外起点 | 年数 | 分档 | 样本内波动率 | 修复统计门槛 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary.itertuples(index=False):
        lines.append(
            f"| {row.series} {row.name} | {KIND_LABELS.get(row.kind, row.kind)} | {row.eval_start:%Y-%m-%d} | "
            f"{row.is_end:%Y-%m-%d} | {row.oos_start:%Y-%m-%d} | {row.years:.2f} | {row.split} | "
            f"{_pct(row.is_buy_hold_volatility_pct)} | {_pct(row.min_recovery_depth_pct)} |"
        )
    if not sensitivity.empty:
        lines += [
            "",
            "## 成交时点敏感性（推荐参数，全区间）",
            "",
            "| 序列 | 底仓 | 同日收盘 | 次日收盘 |",
            "|---|---|---|---|",
        ]
        for (series, fraction), group in sensitivity.groupby(["series", "base_fraction"], sort=False):
            same = group[group["execution"] == "same_close"].iloc[0]
            nxt = group[group["execution"] == "next_close"].iloc[0]
            lines.append(f"| {series} | {_fraction(fraction)} | {_metrics(same, 'full_')} | {_metrics(nxt, 'full_')} |")
    common = tables.get("common_oos_159545")
    if isinstance(common, pd.DataFrame) and not common.empty:
        lines += [
            "",
            f"## 159545 共同样本外区间（{common['start_date'].iloc[0]:%Y-%m-%d} 至 {common['end_date'].iloc[0]:%Y-%m-%d}，同一条ETF收益序列）",
            "",
            "| 来源 | 参数 | 底仓 | 年化 / 回撤 / 水下 / 修复 |",
            "|---|---|---|---|",
        ]
        for item in common.itertuples(index=False):
            lines.append(f"| {item.source} | {item.label or '—'} | {_fraction(item.base_fraction)} | {_metrics(item._asdict(), '')} |")
    lines += [
        "",
        "## 附录：诊断与参考",
        "",
        "样本内单点最优仅用于说明单点选参的不稳定性；现行参数仅作参考，未参与推荐。",
        "",
        "| 序列 | 样本内单点最优 | 其样本外排名/117 | 现行参数（底仓） | 现行 全区间 |",
        "|---|---|---|---|---|",
    ]
    for row in summary.itertuples(index=False):
        current = benchmarks[(benchmarks["series"] == row.series) & (benchmarks["scheme"] == "现行参数（仅参考）")]
        current_cells = (
            f"{current.iloc[0]['label']}（{_fraction(current.iloc[0]['base_fraction'])}） | {_metrics(current.iloc[0], 'full_')}"
            if len(current)
            else "— | —"
        )
        lines.append(f"| {row.series} | {row.is_best_label} | {row.is_best_oos_rank:.0f} | {current_cells} |")
    lines += [
        "",
        "## 局限说明",
        "",
        "- 复权/代理序列模拟：按资金比例连续计份额、不设100份一手，单边手续费0.006%，空仓现金年化1.5%；不等于真实成交口径。",
        "- ETF为差值前复权价格，早期低价阶段的百分比收益与全收益口径存在偏差。",
        "- 980092 与 159545 拼接段为价格指数，不含分红；代理序列上的结论不保证迁移到真实持仓。",
        "- 513130/513500/159941/518800 与持仓基金不是同一只基金，结论需在持仓基金上复核。",
        "- 推荐参数综合了样本外数据，样本外不再是独立检验；平台规则用于降低单点过拟合，但不能消除选择偏差。",
        "- 平均修复天数包含期末未修复段（计到期末），期末刚创新低时该段计为0天。",
        "- 主结果按同日收盘信号成交（理想化），次日收盘成交见敏感性表。",
        "",
    ]
    return "\n".join(lines)


def build_plateau_maps(tables: dict[str, object]) -> str:
    candidates: pd.DataFrame = tables["candidates"]
    summary: pd.DataFrame = tables["summary"]
    lines = [
        "# 参数平台图",
        "",
        "格内数字为稳健分×100（样本内、样本外百分位平均）。★ 推荐参数；■ 平台参数；● 好参数（样本内外均优于一直持有）但不在平台；空白为一般参数。",
        "",
    ]
    for row in summary.itertuples(index=False):
        grid = candidates[candidates["series"] == row.series]
        verdict = f"推荐 {row.recommended_label}" if row.suitable else "无参数平台，不适合均线择时"
        lines += [f"## {row.series} {row.name}（{verdict}）", ""]
        for family, params, unit in ((FAMILY_FIXED, FIXED_THRESHOLDS_PCT, "%"), (FAMILY_VOL, VOL_MULTIPLIERS, "σ")):
            lines += [
                f"**{FAMILY_LABELS[family]}**",
                "",
                "| MA \\ 阈值 | " + " | ".join(f"{param:g}{unit}" for param in params) + " |",
                "|---|" + "---|" * len(params),
            ]
            for ma_period in MA_PERIODS:
                cells = []
                for param in params:
                    cell = grid[(grid["family"] == family) & (grid["ma_period"] == ma_period) & (grid["param"] == param)].iloc[0]
                    mark = "★" if cell["recommended"] else "■" if cell["plateau"] else "●" if cell["good"] else ""
                    cells.append(f"{cell['robust_score'] * 100:.0f}{mark}")
                lines.append(f"| MA{ma_period} | " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 均线择时参数研究（只读输入，结果写入 output/）")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--end", default=RESEARCH_END.strftime("%Y-%m-%d"))
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else ROOT / "output" / "etf_ma_param_research" / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    tables = run_research(args.data_dir, end=args.end, progress=lambda spec: print(f"研究 {spec.key} {spec.name} ..."))
    for name in EXPORT_TABLES:
        table = tables.get(name)
        if isinstance(table, pd.DataFrame) and not table.empty:
            round_for_export(table).to_csv(output_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
    (output_dir / "report.md").write_text(build_report(tables, args.end), encoding="utf-8")
    (output_dir / "plateau_maps.md").write_text(build_plateau_maps(tables), encoding="utf-8")

    print(f"\n平台标准：{tables['plateau_rule'].label}")
    for row in tables["summary"].itertuples(index=False):
        verdict = (
            f"推荐 {row.recommended_label} 底仓{row.recommended_base_fraction * 100:.0f}% 平台{row.plateau_cells}格"
            if row.suitable
            else "无参数平台，不适合均线择时"
        )
        print(
            f"{row.series:<11} 样本外{row.oos_start:%Y-%m-%d} 分档{row.split} "
            f"优于持有 样本内{row.beats_hold_is_rules:>3} 样本外{row.beats_hold_oos_rules:>3} 两段{row.good_rules:>3} {verdict}"
        )
    print(f"\n结果已保存：{output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
