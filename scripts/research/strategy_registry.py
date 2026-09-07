"""自动识别项目中的全部 ETF / 指数策略，输出机器可读注册表。

用法（项目 venv）::

    .venv/bin/python scripts/research/strategy_registry.py [--output output/research_audit]

输出：
    strategy_registry.json  机器可读策略清单（含参数来源 file:line 级模块路径）
    strategy_registry.md    人读表格

识别来源（直接 import 权威常量，而非正则扫描源码，保证与运行时一致）：
1. 持仓分析页 ETF 择时配置  services.position_models.ETF_TIMING_STRATEGIES 等
2. 策略回测页模式            components.backtest.*（单标的MA择时/多基金轮动/多ETF组合/年度动态组合）
3. 年度动态组合注册表        config/annual_etf_registry_v1.csv + index_families.json
4. ETF 组合审计配置          config/etf_portfolio_audit.json
5. 动态阈值研究配置          config/dynamic_threshold_research.json
6. 指数监控 MA20 摘要        services.index_config（监控集合，非交易策略但纳入清点）
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def build_registry() -> dict[str, object]:
    from services.position_models import (
        ETF_DISPLAY_NAMES,
        ETF_PORTFOLIO_WEIGHTS_PCT,
        ETF_POSITION_STRATEGIES,
        ETF_TIMING_STRATEGIES,
    )

    strategies: list[dict[str, object]] = []

    # 1) 持仓分析页：每只 ETF 的 MA/阈值择时
    for code, (ma_period, threshold_pct) in sorted(ETF_TIMING_STRATEGIES.items()):
        strategies.append(
            {
                "strategy_id": f"position_timing_{code}",
                "category": "ETF择时",
                "venue": "持仓分析页",
                "symbol": code,
                "name": ETF_DISPLAY_NAMES.get(code, code),
                "signal_rule": "percent",
                "ma_period": int(ma_period),
                "threshold_pct": float(threshold_pct),
                "strategy_type": ETF_POSITION_STRATEGIES.get(code, "纯择时"),
                "weight_pct": float(ETF_PORTFOLIO_WEIGHTS_PCT.get(code, 0)),
                "parameter_source": "services/position_models.py:ETF_TIMING_STRATEGIES",
            }
        )

    # 2) ETF 组合审计（allocations 即权威基线配置）
    audit_config_path = PROJECT_ROOT / "config" / "etf_portfolio_audit.json"
    if audit_config_path.exists():
        audit_config = json.loads(audit_config_path.read_text(encoding="utf-8"))
        baseline = audit_config.get("allocations") or audit_config.get("baseline_allocations") or []
        for row in baseline:
            strategies.append(
                {
                    "strategy_id": f"portfolio_audit_{row.get('symbol')}",
                    "category": "ETF择时",
                    "venue": "ETF组合审计（冻结基线）",
                    "symbol": str(row.get("symbol")),
                    "name": str(row.get("name", row.get("symbol"))),
                    "signal_rule": str(row.get("signal_rule", "percent")),
                    "ma_period": int(row.get("ma_period", 20)),
                    "threshold_pct": float(row.get("threshold_pct", 1.0)),
                    "strategy_type": str(row.get("strategy")),
                    "weight_pct": float(row.get("weight_pct", 0)),
                    "parameter_source": "config/etf_portfolio_audit.json",
                }
            )

    # 3) 动态阈值研究标的
    research_config_path = PROJECT_ROOT / "config" / "dynamic_threshold_research.json"
    if research_config_path.exists():
        research_config = json.loads(research_config_path.read_text(encoding="utf-8"))
        for row in research_config.get("allocations", []):
            strategies.append(
                {
                    "strategy_id": f"threshold_research_{row.get('symbol')}",
                    "category": "ETF择时",
                    "venue": "动态阈值研究",
                    "symbol": str(row.get("symbol")),
                    "name": str(row.get("name", row.get("symbol"))),
                    "signal_rule": str(row.get("signal_rule", "percent")),
                    "ma_period": int(row.get("ma_period", 0)) or None,
                    "threshold_pct": float(row.get("threshold_pct", 0)) or None,
                    "strategy_type": str(row.get("strategy", "")),
                    "weight_pct": None,
                    "parameter_source": "config/dynamic_threshold_research.json",
                }
            )

    # 4) 年度动态组合注册表
    registry_csv = PROJECT_ROOT / "config" / "annual_etf_registry_v1.csv"
    if registry_csv.exists():
        with registry_csv.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                strategies.append(
                    {
                        "strategy_id": f"annual_dynamic_{row.get('symbol')}",
                        "category": "年度动态组合",
                        "venue": "策略回测页·年度动态组合",
                        "symbol": str(row.get("symbol")),
                        "name": str(row.get("name", row.get("symbol"))),
                        "signal_rule": "percent（年度网格搜索）",
                        "ma_period": None,
                        "threshold_pct": None,
                        "strategy_type": str(row.get("slot") or row.get("direction", "")),
                        "weight_pct": None,
                        "parameter_source": "config/annual_etf_registry_v1.csv",
                        "proxy_symbol": row.get("proxy_symbol") or None,
                        "listing_date": row.get("listing_date") or None,
                    }
                )

    # 5) 回测页运行模式（页面级策略能力，非具体标的）
    page_modes = [
        {
            "strategy_id": "page_ma_timing",
            "category": "回测模式",
            "venue": "策略回测页",
            "name": "单标的 MA±阈值 择时",
            "signal_rule": "percent",
            "default_ma_period": 20,
            "default_threshold_pct": 1.0,
            "execution": "after_close（当日收盘信号/当日收盘成交，盘后固定价机制假设）",
        },
        {
            "strategy_id": "page_fund_rotation",
            "category": "回测模式",
            "venue": "策略回测页",
            "name": "多基金动量轮动",
            "signal_rule": "momentum_top_k",
            "default_momentum_period": 22,
            "execution": "after_close（默认）/ next_open（对比，含双边滑点0.05%）",
        },
        {
            "strategy_id": "page_portfolio_timing",
            "category": "回测模式",
            "venue": "策略回测页",
            "name": "多ETF配置择时",
            "signal_rule": "percent",
            "execution": "after_close",
        },
        {
            "strategy_id": "page_annual_dynamic",
            "category": "回测模式",
            "venue": "策略回测页",
            "name": "年度动态组合（8方向年度选基）",
            "signal_rule": "percent（年度网格搜索）",
            "execution": "same_close（主结果）/ next_close（压力对照）",
        },
    ]
    strategies.extend(page_modes)

    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "etf_timing_count": sum(1 for s in strategies if s["category"] == "ETF择时"),
        "page_mode_count": len(page_modes),
        "strategies": strategies,
    }


def render_markdown(registry: dict[str, object]) -> str:
    lines = [
        "# 项目策略注册表（自动识别）",
        "",
        f"生成时间：{registry['generated_at']}（Asia/Shanghai）",
        "",
        f"ETF/指数择时策略 {registry['etf_timing_count']} 个；回测页模式 {registry['page_mode_count']} 个。",
        "",
        "## ETF / 指数择时策略",
        "",
        "| 策略ID | 标的 | 名称 | 参数 | 类型/方向 | 权重 | 参数来源 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in registry["strategies"]:
        if item["category"] == "回测模式":
            continue
        params = []
        if item.get("ma_period"):
            params.append(f"MA{item['ma_period']}")
        if item.get("threshold_pct") is not None:
            params.append(f"{item['threshold_pct']}%")
        if not params:
            params.append(str(item.get("signal_rule", "-")))
        lines.append(
            f"| {item['strategy_id']} | {item.get('symbol', '-')} | {item.get('name', '-')} "
            f"| {'/'.join(params)} | {item.get('strategy_type') or '-'} "
            f"| {item.get('weight_pct') if item.get('weight_pct') is not None else '-'}% "
            f"| {item.get('parameter_source', '-')} |"
        )
    lines += [
        "",
        "## 回测页策略模式",
        "",
        "| 模式 | 信号规则 | 执行口径 |",
        "|---|---|---|",
    ]
    for item in registry["strategies"]:
        if item["category"] != "回测模式":
            continue
        lines.append(
            f"| {item['name']} | {item.get('signal_rule', '-')} | {item.get('execution', '-')} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "output" / "research_audit"),
        help="输出目录（默认 output/research_audit）",
    )
    args = parser.parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    registry = build_registry()
    (output_dir / "strategy_registry.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "strategy_registry.md").write_text(render_markdown(registry), encoding="utf-8")
    print(f"策略注册表已写入 {output_dir}")


if __name__ == "__main__":
    main()
