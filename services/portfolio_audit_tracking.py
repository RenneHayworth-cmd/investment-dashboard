from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

from services.portfolio_audit import (
    AuditAllocation,
    AuditRunResult,
    AuditSettings,
    run_portfolio_audit,
)


FROZEN_PARAMETER_VERSION = "etf-timing-20260728-v1"
FROZEN_DATE = "2026-07-28"
RESEARCH_CUTOFF_DATE = "2026-07-27"
NO_OOS_MESSAGE = "尚无真正样本外交易日，不得计算或展示虚假的样本外绩效。"


def create_frozen_strategy_snapshot(
    path: str | Path,
    allocations: list[AuditAllocation],
    *,
    freeze_date: str = FROZEN_DATE,
    data_cutoff_date: str = RESEARCH_CUTOFF_DATE,
    parameter_version: str = FROZEN_PARAMETER_VERSION,
) -> dict[str, object]:
    """Create once; an existing frozen file is validated but never overwritten."""
    target = Path(path)
    payload = {
        "parameter_version": parameter_version,
        "freeze_date": freeze_date,
        "data_cutoff_date": data_cutoff_date,
        "parameters": [_frozen_parameter_row(item) for item in allocations],
        "checksum_algorithm": "sha256",
    }
    expected = {**payload, "file_checksum": _payload_checksum(payload)}
    if target.exists():
        existing = load_frozen_strategy_snapshot(target)
        if existing != expected:
            raise FileExistsError(
                f"冻结参数文件已存在且内容不同，拒绝覆盖：{target}"
            )
        return existing
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return expected


def load_frozen_strategy_snapshot(path: str | Path) -> dict[str, object]:
    target = Path(path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    checksum = payload.get("file_checksum")
    unsigned = {key: value for key, value in payload.items() if key != "file_checksum"}
    expected = _payload_checksum(unsigned)
    if checksum != expected:
        raise ValueError(f"冻结参数文件校验失败：{target}")
    if pd.Timestamp(payload["freeze_date"]) <= pd.Timestamp(
        payload["data_cutoff_date"]
    ):
        raise ValueError("冻结日期必须晚于历史研究截止日期。")
    return payload


def frozen_allocations(snapshot: dict[str, object]) -> list[AuditAllocation]:
    return [
        AuditAllocation(
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            weight_pct=float(row["weight_pct"]),
            strategy=str(row["strategy"]),
            ma_period=int(row["ma_period"]),
            threshold_pct=float(row["threshold_pct"]),
            signal_rule=str(row.get("signal_rule", "percent")),
            atr_k=float(row.get("atr_k", 0.0)),
        )
        for row in snapshot["parameters"]
    ]


def run_out_of_sample_tracking(
    market_data: dict[str, pd.DataFrame],
    frozen_path: str | Path,
    settings: AuditSettings,
    output_dir: str | Path,
) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Append only dates after the frozen research cutoff using frozen parameters."""
    snapshot = load_frozen_strategy_snapshot(frozen_path)
    allocations = frozen_allocations(snapshot)
    cutoff = pd.Timestamp(snapshot["data_cutoff_date"]).normalize()
    freeze_date = pd.Timestamp(snapshot["freeze_date"]).normalize()
    if freeze_date != cutoff + pd.Timedelta(days=1):
        raise ValueError("当前跟踪器要求冻结日紧接历史研究截止日。")

    tracking_settings = replace(
        settings,
        end_date=None,
        missed_signal_rate=0.0,
        missed_order_side="none",
    )
    strategy = run_portfolio_audit(market_data, allocations, tracking_settings)
    hold = run_portfolio_audit(
        market_data,
        [replace(item, strategy="hold") for item in allocations],
        tracking_settings,
    )
    unified = run_portfolio_audit(
        market_data,
        [
            replace(item, ma_period=20, threshold_pct=1.0, signal_rule="percent")
            if item.strategy in ("timing", "half_timing")
            else item
            for item in allocations
        ],
        tracking_settings,
    )
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    daily_path = output_path / "out_of_sample_daily.csv"
    new_daily = _build_oos_daily(
        strategy,
        hold,
        unified,
        allocations,
        cutoff,
        settings.initial_capital,
    )
    existing = _read_existing_oos_daily(daily_path, cutoff)
    if existing.empty:
        combined = new_daily.copy()
    elif new_daily.empty:
        combined = existing.copy()
    else:
        existing_dates = set(
            pd.to_datetime(existing["trade_date"], errors="coerce").dt.normalize()
        )
        unseen = new_daily[
            ~pd.to_datetime(new_daily["trade_date"]).dt.normalize().isin(existing_dates)
        ]
        combined = pd.concat([existing, unseen], ignore_index=True, sort=False)
    if not combined.empty:
        combined["trade_date"] = pd.to_datetime(
            combined["trade_date"], errors="coerce"
        ).dt.normalize()
        combined = (
            combined[combined["trade_date"] >= freeze_date]
            .sort_values("trade_date")
            .drop_duplicates("trade_date", keep="first")
            .reset_index(drop=True)
        )
    else:
        combined = _empty_oos_daily(allocations)

    summary = _build_oos_summary(
        combined,
        strategy,
        allocations,
        cutoff,
        settings.initial_capital,
    )
    report = _out_of_sample_report(summary, combined, cutoff)
    _write_tracking_csv(combined, daily_path)
    _write_tracking_csv(summary, output_path / "out_of_sample_summary.csv")
    (output_path / "out_of_sample_report.md").write_text(report, encoding="utf-8")
    return combined, summary, report


def _frozen_parameter_row(item: AuditAllocation) -> dict[str, object]:
    base_fraction = (
        1.0 if item.strategy == "hold" else 0.5 if item.strategy == "half_timing" else 0.0
    )
    timing_fraction = 1.0 - base_fraction
    return {
        **asdict(item),
        "base_fraction": base_fraction,
        "timing_fraction": timing_fraction,
        "base_weight_pct": item.weight_pct * base_fraction,
        "timing_weight_pct": item.weight_pct * timing_fraction,
    }


def _payload_checksum(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _build_oos_daily(
    strategy: AuditRunResult,
    hold: AuditRunResult,
    unified: AuditRunResult,
    allocations: list[AuditAllocation],
    cutoff: pd.Timestamp,
    initial_capital: float,
) -> pd.DataFrame:
    strategy_daily = strategy.daily.copy()
    strategy_daily["trade_date"] = pd.to_datetime(
        strategy_daily["trade_date"]
    ).dt.normalize()
    history = strategy_daily[strategy_daily["trade_date"] <= cutoff]
    future = strategy_daily[strategy_daily["trade_date"] > cutoff].copy()
    if history.empty or future.empty:
        return _empty_oos_daily(allocations)
    anchor_date = history["trade_date"].max()
    strategy_anchor = float(
        history.loc[history["trade_date"] == anchor_date, "portfolio_value"].iloc[-1]
    )
    scale = initial_capital / strategy_anchor

    daily = future[
        [
            "trade_date",
            "portfolio_value",
            "etf_weight_pct",
            "cash_weight_pct",
        ]
    ].rename(
        columns={
            "portfolio_value": "strategy_value",
            "etf_weight_pct": "strategy_etf_weight_pct",
            "cash_weight_pct": "strategy_cash_weight_pct",
        }
    )
    daily["strategy_value"] = daily["strategy_value"] * scale
    for label, result in (("hold_value", hold), ("unified_value", unified)):
        frame = result.daily[["trade_date", "portfolio_value"]].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
        anchor_rows = frame[frame["trade_date"] <= cutoff]
        if anchor_rows.empty:
            raise ValueError(f"{label} 缺少研究期锚点。")
        anchor_value = float(anchor_rows.iloc[-1]["portfolio_value"])
        frame[label] = frame["portfolio_value"] / anchor_value * initial_capital
        daily = daily.merge(
            frame[frame["trade_date"] > cutoff][["trade_date", label]],
            on="trade_date",
            how="inner",
        )

    components = strategy.component_daily.copy()
    components["trade_date"] = pd.to_datetime(
        components["trade_date"]
    ).dt.normalize()
    component_values = components.pivot(
        index="trade_date", columns="symbol", values="component_value"
    )
    if anchor_date not in component_values.index:
        raise ValueError("样本外贡献缺少研究期末组件锚点。")
    for item in allocations:
        daily[f"{item.symbol}_contribution"] = daily["trade_date"].map(
            (component_values[item.symbol] - component_values.loc[anchor_date, item.symbol])
            * scale
        )
    daily["research_anchor_date"] = anchor_date
    daily["parameter_version"] = FROZEN_PARAMETER_VERSION
    return daily.reset_index(drop=True)


def _empty_oos_daily(allocations: list[AuditAllocation]) -> pd.DataFrame:
    columns = [
        "trade_date",
        "strategy_value",
        "strategy_etf_weight_pct",
        "strategy_cash_weight_pct",
        "hold_value",
        "unified_value",
        *[f"{item.symbol}_contribution" for item in allocations],
        "research_anchor_date",
        "parameter_version",
    ]
    return pd.DataFrame(columns=columns)


def _read_existing_oos_daily(
    path: Path, cutoff: pd.Timestamp
) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    existing = pd.read_csv(path)
    if existing.empty:
        return existing
    if "trade_date" not in existing:
        raise ValueError("已有样本外日表缺少 trade_date。")
    dates = pd.to_datetime(existing["trade_date"], errors="coerce").dt.normalize()
    if dates.isna().any() or (dates <= cutoff).any():
        raise ValueError("已有样本外日表包含无效日期或研究期数据。")
    existing["trade_date"] = dates
    return existing


def _build_oos_summary(
    daily: pd.DataFrame,
    strategy: AuditRunResult,
    allocations: list[AuditAllocation],
    cutoff: pd.Timestamp,
    initial_capital: float,
) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(
            [
                {
                    "record_type": "portfolio",
                    "status": NO_OOS_MESSAGE,
                    "parameter_version": FROZEN_PARAMETER_VERSION,
                    "research_cutoff_date": cutoff,
                    "out_of_sample_start_date": pd.NaT,
                    "out_of_sample_end_date": pd.NaT,
                    "out_of_sample_trading_days": 0,
                    "final_value": np.nan,
                    "cumulative_return_pct": np.nan,
                    "annual_return_pct": np.nan,
                    "max_drawdown_pct": np.nan,
                    "average_position_pct": np.nan,
                    "excess_vs_hold_pct": np.nan,
                    "excess_vs_unified_pct": np.nan,
                    "actual_trade_count": 0,
                }
            ]
        )

    values = pd.to_numeric(daily["strategy_value"], errors="coerce").to_numpy(
        dtype=float
    )
    dates = pd.DatetimeIndex(pd.to_datetime(daily["trade_date"])).normalize()
    seeded = np.concatenate([[initial_capital], values])
    total_return = values[-1] / initial_capital - 1
    elapsed_days = max(1, int((dates[-1] - cutoff).days))
    annual_return = (1 + total_return) ** (365 / elapsed_days) - 1
    drawdown = seeded / np.maximum.accumulate(seeded) - 1
    hold_return = float(daily["hold_value"].iloc[-1] / initial_capital - 1)
    unified_return = float(daily["unified_value"].iloc[-1] / initial_capital - 1)
    trade_dates = pd.to_datetime(
        strategy.trades.get("execution_date", pd.Series(dtype="datetime64[ns]")),
        errors="coerce",
    )
    rows: list[dict[str, object]] = [
        {
            "record_type": "portfolio",
            "status": "已计算",
            "parameter_version": FROZEN_PARAMETER_VERSION,
            "research_cutoff_date": cutoff,
            "out_of_sample_start_date": dates[0],
            "out_of_sample_end_date": dates[-1],
            "out_of_sample_trading_days": len(daily),
            "final_value": values[-1],
            "cumulative_return_pct": total_return * 100,
            "annual_return_pct": annual_return * 100,
            "max_drawdown_pct": float(drawdown.min() * 100),
            "average_position_pct": float(
                pd.to_numeric(
                    daily["strategy_etf_weight_pct"], errors="coerce"
                ).mean()
            ),
            "excess_vs_hold_pct": (total_return - hold_return) * 100,
            "excess_vs_unified_pct": (total_return - unified_return) * 100,
            "actual_trade_count": int((trade_dates > cutoff).sum()),
        }
    ]
    for item in allocations:
        contribution = float(daily[f"{item.symbol}_contribution"].iloc[-1])
        rows.append(
            {
                "record_type": "etf_contribution",
                "status": "已计算",
                "parameter_version": FROZEN_PARAMETER_VERSION,
                "symbol": item.symbol,
                "name": item.name,
                "contribution_amount": contribution,
                "contribution_pct_of_initial": contribution / initial_capital * 100,
            }
        )
    return pd.DataFrame(rows)


def _out_of_sample_report(
    summary: pd.DataFrame, daily: pd.DataFrame, cutoff: pd.Timestamp
) -> str:
    portfolio = summary.iloc[0]
    if daily.empty:
        return f"""# ETF策略真正样本外跟踪

- 历史研究期截止：{cutoff:%Y-%m-%d}
- 参数冻结版本：{FROZEN_PARAMETER_VERSION}
- 状态：{NO_OOS_MESSAGE}
"""
    return f"""# ETF策略真正样本外跟踪

- 历史研究期截止：{cutoff:%Y-%m-%d}
- 参数冻结版本：{FROZEN_PARAMETER_VERSION}
- 样本外区间：{pd.Timestamp(portfolio['out_of_sample_start_date']):%Y-%m-%d} 至 {pd.Timestamp(portfolio['out_of_sample_end_date']):%Y-%m-%d}
- 样本外交易日：{int(portfolio['out_of_sample_trading_days'])}
- 累计收益：{portfolio['cumulative_return_pct']:.2f}%
- 年化收益：{portfolio['annual_return_pct']:.2f}%
- 最大回撤：{portfolio['max_drawdown_pct']:.2f}%
- 平均ETF仓位：{portfolio['average_position_pct']:.2f}%
- 相对一直持有超额：{portfolio['excess_vs_hold_pct']:.2f}%
- 相对统一MA20/1%超额：{portfolio['excess_vs_unified_pct']:.2f}%
- 参数冻结后实际成交：{int(portfolio['actual_trade_count'])} 次

样本外结果只按冻结参数追加，不用于反向修改参数。
"""


def _write_tracking_csv(frame: pd.DataFrame, path: Path) -> None:
    output = frame.copy()
    for column in output.columns:
        if "date" in str(column).lower():
            converted = pd.to_datetime(output[column], errors="coerce")
            if converted.notna().any():
                output[column] = converted.dt.strftime("%Y-%m-%d")
    output.to_csv(path, index=False, encoding="utf-8-sig")
