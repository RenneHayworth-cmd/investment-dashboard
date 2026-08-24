from __future__ import annotations

import numpy as np
import pandas as pd

from services.annual_etf_models import AnnualBacktestSettings
from services.annual_etf_selection import performance_metrics


def calculate_direction_contribution(
    daily: pd.DataFrame,
    weights: dict[str, float],
    initial_capital: float,
) -> pd.DataFrame:
    portfolio_return = pd.to_numeric(daily["portfolio_value"], errors="coerce").pct_change().dropna()
    variance = float(portfolio_return.var(ddof=1)) if len(portfolio_return) > 1 else 0.0
    rows = []
    for slot, weight in weights.items():
        values = pd.to_numeric(daily[f"{slot}_value"], errors="coerce")
        initial = initial_capital * weight / 100
        contribution_return = values.diff().div(pd.to_numeric(daily["portfolio_value"], errors="coerce").shift(1)).dropna()
        aligned = pd.concat([contribution_return.rename("component"), portfolio_return.rename("portfolio")], axis=1).dropna()
        covariance = float(aligned.cov().loc["component", "portfolio"]) if len(aligned) > 1 else 0.0
        risk_share = covariance / variance * 100 if variance > 0 else 0.0
        volatility_points = covariance / np.sqrt(variance) * np.sqrt(252) * 100 if variance > 0 else 0.0
        rows.append(
            {
                "slot": slot,
                "initial_capital": initial,
                "final_value": float(values.iloc[-1]),
                "profit": float(values.iloc[-1] - initial),
                "return_contribution_pct": float((values.iloc[-1] - initial) / initial_capital * 100),
                "risk_contribution_pct": risk_share,
                "annual_volatility_contribution_points": volatility_points,
            }
        )
    return pd.DataFrame(rows)


def _yearly_table(daily: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    series_columns = {
        "portfolio_value": "年度动态组合",
        "annual_hold_value": "年度选择一直持有",
        "parking_value": "全部持有512890",
        "next_close_value": "次日收盘压力",
    }
    rows = []
    for column, label in series_columns.items():
        if column not in daily:
            continue
        previous = initial_capital
        for year, group in daily.groupby(pd.to_datetime(daily["trade_date"]).dt.year):
            values = pd.to_numeric(group[column], errors="coerce").dropna()
            if values.empty:
                continue
            ending = float(values.iloc[-1])
            rows.append(
                {
                    "series": label,
                    "year": int(year),
                    "start_date": pd.Timestamp(group["trade_date"].iloc[0]),
                    "end_date": pd.Timestamp(group["trade_date"].iloc[-1]),
                    "start_value": previous,
                    "end_value": ending,
                    "return_pct": (ending / previous - 1) * 100 if previous else np.nan,
                }
            )
            previous = ending
    return pd.DataFrame(rows)


def _summary_row(
    label: str,
    daily: pd.DataFrame,
    settings: AnnualBacktestSettings,
    trades: pd.DataFrame | None = None,
) -> dict[str, object]:
    metrics = performance_metrics(daily, settings.initial_capital, settings.cash_annual_rate)
    metrics["series"] = label
    if trades is not None:
        metrics["trade_count"] = int(len(trades))
        metrics["commission_cost"] = (
            float(pd.to_numeric(trades.get("commission"), errors="coerce").fillna(0).sum())
            if not trades.empty
            else 0.0
        )
    else:
        metrics["trade_count"] = (
            int(pd.to_numeric(daily["trade_count"], errors="coerce").iloc[-1])
            if "trade_count" in daily and not daily.empty
            else pd.NA
        )
        metrics["commission_cost"] = (
            float(pd.to_numeric(daily["commission_cost"], errors="coerce").iloc[-1])
            if "commission_cost" in daily and not daily.empty
            else 0.0
        )
    return metrics


def build_markdown_report(
    summary: pd.DataFrame,
    selections: pd.DataFrame,
    settings: AnnualBacktestSettings,
    fingerprint: str,
    details: dict[str, pd.DataFrame] | None = None,
) -> str:
    actual_end_date: object = settings.end_date
    if not summary.empty and "end_date" in summary.columns:
        parsed_end_date = pd.to_datetime(summary["end_date"], errors="coerce").max()
        if pd.notna(parsed_end_date):
            actual_end_date = parsed_end_date.strftime("%Y-%m-%d")
    lines = [
        "# 历史年度 ETF 动态组合回测报告",
        "",
        f"- 起投年份：{settings.start_year}",
        f"- 实际结束日期：{actual_end_date}",
        f"- 初始资金：{settings.initial_capital:,.2f} 元，此后不追加资金",
        f"- 单边手续费：{settings.commission_rate * 10000:.2f} 万分点",
        f"- 现金年利率：{settings.cash_annual_rate * 100:.2f}%",
        f"- 整数手：{settings.lot_size} 份",
        f"- 注册表版本：{settings.registry_version}",
        f"- 指数族版本：{settings.whitelist_version}",
        f"- 数据指纹：`{fingerprint}`",
        "- 候选范围：注册表快照日仍上市的ETF，明确存在生存者偏差。",
        "- 主结果：当日收盘产生信号并按同一收盘全额成交的理想化模拟。",
        "- 压力结果：冻结相同年度选择和参数，仅改为下一交易日收盘成交。",
        "- 代理研究：仅用于上市前研究链，不会让未上市ETF提前成为可交易标的。",
        "",
        "## 结果摘要",
        "",
        summary.to_markdown(index=False) if not summary.empty else "无结果。",
        "",
        "## 年度选择",
        "",
        selections.to_markdown(index=False) if not selections.empty else "无年度选择。",
    ]
    detail_frames = details or {}
    for title, key in (
        ("年度收益", "yearly"),
        ("方向收益与风险贡献", "contribution"),
        ("实际迁移", "migrations"),
        ("失败明细", "errors"),
    ):
        frame = detail_frames.get(key)
        lines.extend(["", f"## {title}", ""])
        lines.append(
            frame.to_markdown(index=False)
            if frame is not None and not frame.empty
            else "无明细。"
        )
    lines.extend(
        [
            "",
            "## 附件说明",
            "",
            "页面可另行下载年度资格、25组参数遍历、实际交易、迁移、方向贡献、失败明细和每日净值CSV。",
        ]
    )
    return "\n".join(lines)
