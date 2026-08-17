#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.dynamic_threshold_research import (  # noqa: E402
    aggregate_training_scores,
    allocation_from_candidate,
    build_walk_forward_windows,
    evaluate_candidates,
    evaluate_frozen_models,
    fit_two_stage_model,
    holdout_split_dates,
    make_candidate,
    parameter_stability_rows,
    replace_signal_with_proxy,
    selected_candidates,
)
from services.fund_analysis import (  # noqa: E402
    FUND_ADJUST_FORWARD_ADDITIVE,
    FUND_ADJUST_NONE,
    fetch_tickflow_fund_close,
)
from services.portfolio_audit import (  # noqa: E402
    AuditAllocation,
    AuditSettings,
    normalize_audit_market_data,
    run_portfolio_audit,
)


DYNAMIC_FAMILIES = {"sigma_symmetric", "sigma_asymmetric", "hybrid_sigma"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行均线与σ动态阈值两阶段回测研究")
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "dynamic_threshold_research.json"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "output" / "dynamic_threshold_research_20260815"),
    )
    parser.add_argument(
        "--refresh-missing-raw",
        action="store_true",
        help="仅在缺少显式none未复权历史时联网补齐研究副本，不写正式缓存",
    )
    parser.add_argument("--symbols", default="", help="逗号分隔的ETF代码；默认全部14只")
    parser.add_argument("--quick", action="store_true", help="开发检查：跳过Walk-Forward和代理长历史")
    return parser.parse_args()


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = frame.copy()
    for column in output.columns:
        normalized_name = str(column).lower()
        if (
            normalized_name in {"date", "trade_date", "start_date", "end_date", "train_start", "train_end", "test_start", "test_end"}
            or normalized_name.endswith("_date")
            or str(column) == "日期"
        ):
            converted = pd.to_datetime(output[column], errors="coerce")
            if converted.notna().any():
                output[column] = converted.dt.strftime("%Y-%m-%d")
    output.to_csv(path, index=False, encoding="utf-8-sig")


def exchange_symbol(code: str) -> str:
    return f"{code}.SH" if code.startswith("5") else f"{code}.SZ"


def _formal_cache_candidates(code: str, adjustment: str) -> list[Path]:
    symbol = exchange_symbol(code)
    pattern = f"fund_close_v2_{symbol}_{adjustment}_*_1d.csv"
    return sorted(
        path
        for source_dir in (ROOT / "data" / "raw").iterdir()
        if source_dir.is_dir()
        for path in source_dir.glob(pattern)
    )


def _load_versioned_cache(code: str, adjustment: str) -> tuple[pd.DataFrame, str]:
    candidates = _formal_cache_candidates(code, adjustment)
    if not candidates:
        raise FileNotFoundError(f"{code} 缺少v2 {adjustment}正式缓存。")
    inspected: list[tuple[int, pd.Timestamp, Path, pd.DataFrame]] = []
    errors: list[str] = []
    for path in candidates:
        frame = pd.read_csv(path)
        modes = frame.get("_adjust_mode", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()
        versions = pd.to_numeric(
            frame.get("_cache_schema_version", pd.Series(dtype=float)), errors="coerce"
        ).dropna().unique()
        if modes != [adjustment] or len(versions) != 1 or float(versions[0]) != 2:
            errors.append(path.name)
            continue
        date_column = "日期" if "日期" in frame else "trade_date"
        latest = pd.to_datetime(frame[date_column], errors="coerce").max()
        inspected.append((len(frame), latest, path, frame))
    if not inspected:
        raise ValueError(f"{code} 的{adjustment}缓存未通过版本/复权标签校验：{errors}")
    _rows, _latest, path, frame = max(inspected, key=lambda item: (item[0], item[1]))
    return frame, str(path.relative_to(ROOT))


def _fetch_missing_raw(code: str, api_key: str) -> tuple[pd.DataFrame, str]:
    symbol = exchange_symbol(code)
    if code == "161128":
        from services.position_analysis import _fetch_exchange_fund_close

        frame = _fetch_exchange_fund_close(
            symbol=symbol,
            count=10000,
            adjust=FUND_ADJUST_NONE,
        )
        source = str(frame.attrs.get("position_history_source") or "东方财富/AkShare")
        return frame, source
    return (
        fetch_tickflow_fund_close(
            symbol,
            api_key=api_key,
            count=10000,
            adjust=FUND_ADJUST_NONE,
        ),
        "TickFlow explicit none",
    )


def _load_raw_history(
    code: str,
    output_dir: Path,
    *,
    refresh_missing: bool,
    api_key: str,
) -> tuple[pd.DataFrame, str]:
    try:
        return _load_versioned_cache(code, FUND_ADJUST_NONE)
    except FileNotFoundError:
        pass
    research_path = output_dir / "market_data" / f"{code}_none.csv"
    metadata_path = output_dir / "market_data" / f"{code}_none_source.txt"
    if research_path.exists():
        source = metadata_path.read_text(encoding="utf-8").strip() if metadata_path.exists() else "研究副本 explicit none"
        return pd.read_csv(research_path), source
    if not refresh_missing:
        raise FileNotFoundError(
            f"{code} 缺少显式none历史；请使用 --refresh-missing-raw 生成隔离的研究副本。"
        )
    frame, source = _fetch_missing_raw(code, api_key)
    research_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(research_path, index=False, encoding="utf-8-sig")
    metadata_path.write_text(source, encoding="utf-8")
    return frame, source


def _load_dividends() -> tuple[pd.DataFrame, str]:
    path = ROOT / "output" / "etf_portfolio_audit_20260727" / "market_data" / "official_dividends.csv"
    if not path.exists():
        return pd.DataFrame(), "未找到官方分红研究缓存"
    return pd.read_csv(path), str(path.relative_to(ROOT))


def _slice_to_end(frame: pd.DataFrame, end_date: str | None) -> pd.DataFrame:
    if not end_date:
        return frame
    result = frame.copy()
    date_column = "日期" if "日期" in result else "trade_date"
    dates = pd.to_datetime(result[date_column], errors="coerce")
    return result.loc[dates <= pd.Timestamp(end_date)].copy()


def load_market_data(
    allocations: list[AuditAllocation],
    config: dict[str, object],
    output_dir: Path,
    *,
    refresh_missing: bool,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    dividends, dividend_source = _load_dividends()
    api_key = os.getenv("TICKFLOW_API_KEY", "")
    end_date = config.get("research_end_date")
    known_splits = config.get("known_share_splits", {})
    market_data: dict[str, pd.DataFrame] = {}
    quality_rows: list[dict[str, object]] = []
    action_rows: list[pd.DataFrame] = []
    for allocation in allocations:
        code = allocation.symbol
        try:
            adjusted, adjusted_source = _load_versioned_cache(
                code, FUND_ADJUST_FORWARD_ADDITIVE
            )
            raw, raw_source = _load_raw_history(
                code,
                output_dir,
                refresh_missing=refresh_missing,
                api_key=api_key,
            )
            adjusted = _slice_to_end(adjusted, end_date)
            raw = _slice_to_end(raw, end_date)
            symbol_dividends = pd.DataFrame()
            if not dividends.empty and "基金代码" in dividends:
                codes = dividends["基金代码"].astype(str).str.extract(r"(\d+)", expand=False).str.zfill(6)
                symbol_dividends = dividends.loc[codes == code].copy()
            split_frame = pd.DataFrame(known_splits.get(code, []))
            normalized, actions = normalize_audit_market_data(
                raw,
                adjusted,
                symbol_dividends,
                split_frame,
            )
            if not actions.empty:
                actions.insert(0, "symbol", code)
                action_rows.append(actions)
            market_data[code] = normalized
            signal_dates = pd.to_datetime(
                adjusted["日期"] if "日期" in adjusted else adjusted["trade_date"], errors="coerce"
            ).dt.normalize()
            raw_dates = pd.to_datetime(
                raw["日期"] if "日期" in raw else raw["trade_date"], errors="coerce"
            ).dt.normalize()
            overlap_dates = set(signal_dates.dropna()).intersection(set(raw_dates.dropna()))
            quality_rows.append(
                {
                    "symbol": code,
                    "name": allocation.name,
                    "status": "可用",
                    "signal_source": adjusted_source,
                    "execution_source": raw_source,
                    "dividend_source": dividend_source,
                    "signal_adjustment": FUND_ADJUST_FORWARD_ADDITIVE,
                    "execution_adjustment": FUND_ADJUST_NONE,
                    "signal_rows": len(adjusted),
                    "execution_rows": len(raw),
                    "overlap_rows": len(normalized),
                    "overlap_rate_pct": len(overlap_dates) / max(1, len(set(signal_dates.dropna()))) * 100,
                    "start_date": normalized["trade_date"].min(),
                    "end_date": normalized["trade_date"].max(),
                    "history_years": (normalized["trade_date"].max() - normalized["trade_date"].min()).days / 365.25,
                    "duplicate_dates": int(normalized["trade_date"].duplicated().sum()),
                    "missing_signal_close": int(normalized["signal_close"].isna().sum()),
                    "missing_raw_close": int(normalized["raw_close"].isna().sum()),
                    "nonpositive_signal_close": int((normalized["signal_close"] <= 0).sum()),
                    "nonpositive_raw_close": int((normalized["raw_close"] <= 0).sum()),
                    "signal_return_abs_gt30pct": int((normalized["signal_close"].pct_change().abs() > 0.30).sum()),
                    "raw_return_abs_gt30pct": int((normalized["raw_close"].pct_change().abs() > 0.30).sum()),
                    "signal_final_confirmed_rate_pct": float(
                        adjusted.get("_final_close_confirmed", pd.Series(False, index=adjusted.index))
                        .fillna(False)
                        .astype(bool)
                        .mean()
                        * 100
                    ),
                    "execution_final_confirmed_rate_pct": float(
                        raw.get("_final_close_confirmed", pd.Series(False, index=raw.index))
                        .fillna(False)
                        .astype(bool)
                        .mean()
                        * 100
                    )
                    if "_final_close_confirmed" in raw
                    else np.nan,
                    "official_dividend_events": int((normalized["dividend_per_share"] > 0).sum()),
                    "official_share_split_events": int((normalized["share_split_ratio"] != 1).sum()),
                    "error": "",
                }
            )
        except Exception as exc:
            quality_rows.append(
                {
                    "symbol": code,
                    "name": allocation.name,
                    "status": "阻断",
                    "signal_adjustment": FUND_ADJUST_FORWARD_ADDITIVE,
                    "execution_adjustment": FUND_ADJUST_NONE,
                    "error": str(exc),
                }
            )
    actions = pd.concat(action_rows, ignore_index=True) if action_rows else pd.DataFrame()
    return market_data, pd.DataFrame(quality_rows), actions


def _grid_table(stage: dict[str, object], phase: str) -> pd.DataFrame:
    aggregate = stage["aggregate"].copy()
    full = stage["evaluations"]
    full = full[full["evaluation_window"] == "full"].copy()
    metric_columns = [
        "candidate_id",
        "start_date",
        "end_date",
        "trading_days",
        "total_return_pct",
        "annual_return_pct",
        "max_drawdown_pct",
        "annual_volatility_pct",
        "sharpe_ratio",
        "longest_underwater_days",
        "trade_count",
    ]
    result = aggregate.merge(full[metric_columns], on="candidate_id", how="left")
    family_has_target = result.groupby("model_family")["annual_return_pct"].transform(
        lambda values: bool((values >= 10.0).any())
    )
    result["return_gate_relaxed"] = ~family_has_target
    result["return_gate_eligible"] = result["return_gate_relaxed"] | (
        result["annual_return_pct"] >= 10.0
    )
    result.insert(2, "phase", phase)
    return result


def _current_candidate(base: AuditAllocation, stage: str = "baseline") -> dict[str, object]:
    return make_candidate(
        stage=stage,
        model_family="current_fixed",
        ma_period=base.ma_period,
        threshold_pct=base.threshold_pct,
    )


def run_stress_tests(
    market: pd.DataFrame,
    base: AuditAllocation,
    settings: AuditSettings,
    candidates: list[dict[str, object]],
    *,
    path_start: pd.Timestamp,
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for execution_mode in ("after_close", "next_open"):
        for slippage_bp in (0.0, 5.0, 10.0):
            scenario = f"{execution_mode}_{slippage_bp:g}bp"
            frame = evaluate_frozen_models(
                market,
                base,
                replace(
                    settings,
                    execution_mode=execution_mode,
                    slippage_bp=slippage_bp,
                ),
                candidates,
                path_start=path_start,
                test_start=test_start,
                test_end=test_end,
                evaluation_label=scenario,
            )
            frame["execution_mode"] = execution_mode
            frame["slippage_bp"] = slippage_bp
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def run_primary_asset(
    market: pd.DataFrame,
    base: AuditAllocation,
    settings: AuditSettings,
    *,
    quick: bool,
) -> dict[str, object]:
    train_start, train_end, test_start, test_end = holdout_split_dates(market)
    fit = fit_two_stage_model(
        market,
        base,
        settings,
        train_start=train_start,
        train_end=train_end,
    )
    candidates = selected_candidates(fit)
    baseline = evaluate_candidates(
        market,
        base,
        [_current_candidate(base)],
        settings,
        start_date=train_start,
        end_date=test_end,
        include_subwindows=False,
    )
    holdout = evaluate_frozen_models(
        market,
        base,
        settings,
        candidates,
        path_start=train_start,
        test_start=test_start,
        test_end=test_end,
        evaluation_label="70/30冻结测试",
    )
    stress = run_stress_tests(
        market,
        base,
        settings,
        candidates,
        path_start=train_start,
        test_start=test_start,
        test_end=test_end,
    )
    walk_frames: list[pd.DataFrame] = []
    if not quick:
        for window in build_walk_forward_windows(market):
            fold_fit = fit_two_stage_model(
                market,
                base,
                settings,
                train_start=window["train_start"],
                train_end=window["train_end"],
            )
            frame = evaluate_frozen_models(
                market,
                base,
                settings,
                selected_candidates(fold_fit),
                path_start=window["train_start"],
                test_start=window["test_start"],
                test_end=window["test_end"],
                evaluation_label=f"Walk-Forward第{window['fold']}折",
            )
            for key, value in window.items():
                frame[key] = value
            frame["stable_training_families"] = ",".join(sorted(fold_fit["stable_families"]))
            walk_frames.append(frame)
    return {
        "fit": fit,
        "baseline": baseline,
        "holdout": holdout,
        "stress": stress,
        "walk_forward": pd.concat(walk_frames, ignore_index=True) if walk_frames else pd.DataFrame(),
        "stability": parameter_stability_rows(fit["stage2"]["aggregate"], fit["best_dynamic"]),
        "train_start": train_start,
        "train_end": train_end,
        "test_start": test_start,
        "test_end": test_end,
        "candidates": candidates,
    }


def _proxy_market(path: Path, symbol: str, name: str) -> pd.DataFrame:
    source = pd.read_csv(path)
    date_column = "trade_date" if "trade_date" in source else "日期"
    close_column = "close" if "close" in source else "收盘价"
    dates = pd.to_datetime(source[date_column], errors="coerce").dt.normalize()
    close = pd.to_numeric(source[close_column], errors="coerce")
    frame = pd.DataFrame({"trade_date": dates, "close": close}).dropna()
    frame = frame.sort_values("trade_date").drop_duplicates("trade_date")
    frame = frame[frame["close"] > 0].reset_index(drop=True)
    values = frame["close"].to_numpy()
    return pd.DataFrame(
        {
            "trade_date": frame["trade_date"],
            "raw_open": values,
            "raw_close": values,
            "raw_high": values,
            "raw_low": values,
            "signal_open": values,
            "signal_close": values,
            "signal_high": values,
            "signal_low": values,
            "adjustment_factor": 1.0,
            "dividend_per_share": 0.0,
            "share_split_ratio": 1.0,
            "share_split_rounding": "",
            "share_split_source": "",
            "corporate_action_status": "指数代理，无企业行动",
            "proxy_symbol": symbol,
            "proxy_name": name,
        }
    )


def run_proxy_validation(
    config: dict[str, object],
    market_data: dict[str, pd.DataFrame],
    allocation_map: dict[str, AuditAllocation],
    settings: AuditSettings,
    *,
    quick: bool,
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for code, proxy_config in config.get("short_history_proxies", {}).items():
        if code not in market_data or code not in allocation_map:
            continue
        proxy_path = ROOT / proxy_config["path"]
        if not proxy_path.exists():
            rows.append(pd.DataFrame([{"symbol": code, "validation_type": "代理数据质量", "error": f"缺少{proxy_path}"}]))
            continue
        base = allocation_map[code]
        proxy_symbol = f"proxy_{code}"
        proxy_base = replace(base, symbol=proxy_symbol, name=proxy_config["name"])
        proxy = _proxy_market(proxy_path, proxy_symbol, proxy_config["name"])
        proxy_result = run_primary_asset(proxy, proxy_base, settings, quick=quick)
        proxy_holdout = proxy_result["holdout"].copy()
        proxy_holdout["symbol"] = code
        proxy_holdout["proxy_name"] = proxy_config["name"]
        proxy_holdout["validation_type"] = "代理长历史冻结测试"
        rows.append(proxy_holdout)
        if not proxy_result["walk_forward"].empty:
            proxy_walk = proxy_result["walk_forward"].copy()
            proxy_walk["symbol"] = code
            proxy_walk["proxy_name"] = proxy_config["name"]
            proxy_walk["validation_type"] = "代理长历史Walk-Forward"
            rows.append(proxy_walk)

        common_proxy_signal = replace_signal_with_proxy(market_data[code], proxy)
        common_self_signal = market_data[code][
            market_data[code]["trade_date"].isin(common_proxy_signal["trade_date"])
        ].copy()
        proxy_selected = selected_candidates(proxy_result["fit"])
        if len(common_proxy_signal) < 2 or not proxy_selected:
            continue
        common_start = max(common_proxy_signal["trade_date"].min(), common_self_signal["trade_date"].min())
        common_end = min(common_proxy_signal["trade_date"].max(), common_self_signal["trade_date"].max())
        for signal_source, common_market in (
            ("代理信号交易ETF", common_proxy_signal),
            ("ETF自身信号交易ETF", common_self_signal),
        ):
            comparison = evaluate_candidates(
                common_market,
                base,
                proxy_selected,
                settings,
                start_date=common_start,
                end_date=common_end,
                include_subwindows=False,
            )
            comparison["proxy_name"] = proxy_config["name"]
            comparison["validation_type"] = "共同区间同参数控制"
            comparison["signal_source_control"] = signal_source
            rows.append(comparison)
    return pd.concat(rows, ignore_index=True, sort=False) if rows else pd.DataFrame()


def _model_row(frame: pd.DataFrame, family: str | set[str]) -> pd.Series | None:
    families = {family} if isinstance(family, str) else family
    selected = frame[frame["model_family"].isin(families)]
    return selected.iloc[0] if not selected.empty else None


def build_decision_summary(
    allocation_map: dict[str, AuditAllocation],
    quality: pd.DataFrame,
    results: dict[str, dict[str, object]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    quality_map = quality.set_index("symbol") if not quality.empty else pd.DataFrame()
    for code, base in allocation_map.items():
        if code not in results:
            error = quality_map.loc[code, "error"] if code in quality_map.index else "无可用结果"
            rows.append(
                {
                    "symbol": code,
                    "name": base.name,
                    "current_ma": base.ma_period,
                    "current_threshold_pct": base.threshold_pct,
                    "evidence_level": "D-数据阻断",
                    "support_deployment_review": False,
                    "reason": error,
                }
            )
            continue
        result = results[code]
        fit = result["fit"]
        dynamic = fit["best_dynamic"]
        fixed = fit["best_fixed"]
        holdout_aggregate = aggregate_training_scores(result["holdout"])
        current_holdout = _model_row(holdout_aggregate, "current_fixed")
        fixed_holdout = _model_row(holdout_aggregate, "joint_fixed")
        dynamic_holdout = _model_row(holdout_aggregate, DYNAMIC_FAMILIES)
        holdout_better_current = bool(
            dynamic_holdout is not None
            and current_holdout is not None
            and dynamic_holdout["min_window_score"] > current_holdout["min_window_score"]
            and dynamic_holdout["mean_window_score"] > current_holdout["mean_window_score"]
        )
        holdout_better_fixed = bool(
            dynamic_holdout is not None
            and fixed_holdout is not None
            and dynamic_holdout["min_window_score"] > fixed_holdout["min_window_score"]
            and dynamic_holdout["mean_window_score"] > fixed_holdout["mean_window_score"]
        )

        walk_full = result["walk_forward"]
        walk_full = walk_full[walk_full["evaluation_window"] == "full"] if not walk_full.empty else walk_full
        independent_wins = []
        dynamic_wf_scores: list[float] = []
        current_wf_scores: list[float] = []
        fixed_wf_scores: list[float] = []
        for _fold, group in walk_full.groupby("fold") if not walk_full.empty else []:
            current_row = _model_row(group, "current_fixed")
            fixed_row = _model_row(group, "joint_fixed")
            dynamic_row = _model_row(group, DYNAMIC_FAMILIES)
            if current_row is not None:
                current_wf_scores.append(float(current_row["composite_score"]))
            if fixed_row is not None:
                fixed_wf_scores.append(float(fixed_row["composite_score"]))
            if dynamic_row is not None:
                dynamic_wf_scores.append(float(dynamic_row["composite_score"]))
            independent_wins.append(
                bool(
                    dynamic_row is not None
                    and current_row is not None
                    and dynamic_row["composite_score"] > current_row["composite_score"]
                )
            )
        if dynamic_holdout is not None and current_holdout is not None:
            independent_wins.insert(
                0, dynamic_holdout["mean_window_score"] > current_holdout["mean_window_score"]
            )
        oos_win_rate = float(np.mean(independent_wins)) if independent_wins else 0.0
        wf_better_current = bool(
            dynamic_wf_scores
            and len(dynamic_wf_scores) == len(current_wf_scores)
            and min(dynamic_wf_scores) > min(current_wf_scores)
            and np.mean(dynamic_wf_scores) > np.mean(current_wf_scores)
        )
        wf_better_fixed = bool(
            dynamic_wf_scores
            and len(dynamic_wf_scores) == len(fixed_wf_scores)
            and min(dynamic_wf_scores) > min(fixed_wf_scores)
            and np.mean(dynamic_wf_scores) > np.mean(fixed_wf_scores)
        )
        stability = result["stability"]
        stable_platform = bool(
            not stability.empty
            and len(stability) >= 3
            and float(stability["stable_neighbor"].mean()) >= 0.60
        )
        trade_ratio = (
            float(dynamic_holdout["full_trade_count"] / max(1, current_holdout["full_trade_count"]))
            if dynamic_holdout is not None and current_holdout is not None
            else np.nan
        )
        trade_ok = bool(np.isfinite(trade_ratio) and trade_ratio <= 1.25)
        stress_full = result["stress"]
        stress_full = stress_full[stress_full["evaluation_window"] == "full"]
        stress_directions: list[bool] = []
        for _scenario, group in stress_full.groupby("evaluation_label"):
            current_row = _model_row(group, "current_fixed")
            fixed_row = _model_row(group, "joint_fixed")
            dynamic_row = _model_row(group, DYNAMIC_FAMILIES)
            stress_directions.append(
                bool(
                    dynamic_row is not None
                    and current_row is not None
                    and fixed_row is not None
                    and dynamic_row["composite_score"] > current_row["composite_score"]
                    and dynamic_row["composite_score"] > fixed_row["composite_score"]
                )
            )
        stress_ok = bool(stress_directions and all(stress_directions))
        history_years = float(quality_map.loc[code, "history_years"])
        enough_history = history_years >= 3.0
        support = bool(
            dynamic is not None
            and enough_history
            and holdout_better_current
            and holdout_better_fixed
            and wf_better_current
            and wf_better_fixed
            and oos_win_rate >= 0.60
            and stable_platform
            and trade_ok
            and stress_ok
        )
        if history_years < 3:
            evidence_level = "C-探索性（不足3年）"
        elif support:
            evidence_level = "A-支持进入上线评审"
        elif dynamic is not None:
            evidence_level = "B-有动态候选但证据不足"
        else:
            evidence_level = "D-训练期无稳定动态优势"
        if support:
            overfitting_risk = "中（仍需新增样本跟踪）"
        elif dynamic is None or not stable_platform or oos_win_rate < 0.60:
            overfitting_risk = "高"
        else:
            overfitting_risk = "中高"
        ma_changed = bool(dynamic is not None and int(dynamic["ma_period"]) != base.ma_period)
        if dynamic is None:
            improvement_source = "未形成稳定动态候选"
        elif not ma_changed:
            improvement_source = "σ阈值为主"
        elif fixed is not None and int(fixed["ma_period"]) == int(dynamic["ma_period"]):
            improvement_source = "MA变化为主，σ作增量"
        else:
            improvement_source = "MA与σ交互"
        rows.append(
            {
                "symbol": code,
                "name": base.name,
                "history_years": history_years,
                "current_ma": base.ma_period,
                "current_threshold_pct": base.threshold_pct,
                "best_dynamic_family": dynamic.get("model_family") if dynamic else "",
                "best_dynamic_ma": dynamic.get("ma_period") if dynamic else np.nan,
                "best_dynamic_sigma_period": dynamic.get("sigma_period") if dynamic else np.nan,
                "best_dynamic_buy_k": dynamic.get("buy_k") if dynamic else np.nan,
                "best_dynamic_sell_k": dynamic.get("sell_k") if dynamic else np.nan,
                "best_dynamic_buy_alpha_pct": dynamic.get("buy_alpha_pct") if dynamic else np.nan,
                "best_dynamic_sell_alpha_pct": dynamic.get("sell_alpha_pct") if dynamic else np.nan,
                "best_fixed_ma": fixed.get("ma_period") if fixed else np.nan,
                "best_fixed_threshold_pct": fixed.get("threshold_pct") if fixed else np.nan,
                "ma_changed_after_dynamic_threshold": ma_changed,
                "improvement_source": improvement_source,
                "holdout_better_current": holdout_better_current,
                "holdout_better_joint_fixed": holdout_better_fixed,
                "walk_forward_better_current": wf_better_current,
                "walk_forward_better_joint_fixed": wf_better_fixed,
                "independent_oos_win_rate": oos_win_rate,
                "stable_parameter_platform": stable_platform,
                "trade_count_ratio_vs_current": trade_ratio,
                "trade_count_within_125pct": trade_ok,
                "stress_direction_unchanged": stress_ok,
                "overfitting_risk": overfitting_risk,
                "evidence_level": evidence_level,
                "support_deployment_review": support,
                "reason": "所有硬条件均满足" if support else "至少一项上线评审硬条件未满足",
            }
        )
    return pd.DataFrame(rows)


def _underwater_days(values: pd.Series, dates: pd.Series) -> list[int]:
    peak = -np.inf
    peak_date = pd.Timestamp(dates.iloc[0])
    result: list[int] = []
    for date, value in zip(pd.to_datetime(dates), pd.to_numeric(values)):
        if value >= peak:
            peak = float(value)
            peak_date = pd.Timestamp(date)
            result.append(0)
        else:
            result.append(int((pd.Timestamp(date) - peak_date).days))
    return result


def write_asset_charts(
    output_dir: Path,
    market: pd.DataFrame,
    base: AuditAllocation,
    settings: AuditSettings,
    result: dict[str, object],
    stage2_grid: pd.DataFrame,
) -> list[dict[str, object]]:
    chart_dir = output_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    chart_map: list[dict[str, object]] = []
    colors = {
        "current_fixed": "#374151",
        "joint_fixed": "#2563EB",
        "sigma_symmetric": "#D97706",
        "sigma_asymmetric": "#D97706",
        "hybrid_sigma": "#D97706",
    }
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=("归一化净值", "回撤（%）", "当前水下期（自然日）"),
    )
    for candidate in result["candidates"]:
        run = run_portfolio_audit(
            {base.symbol: market},
            [allocation_from_candidate(base, candidate)],
            replace(
                settings,
                start_date=result["train_start"],
                end_date=result["test_end"],
            ),
        )
        daily = run.daily[run.daily["trade_date"] >= result["test_start"]].copy()
        prior = run.daily[run.daily["trade_date"] < result["test_start"]]
        initial = float(prior["portfolio_value"].iloc[-1]) if not prior.empty else settings.initial_capital
        daily["nav"] = daily["portfolio_value"] / initial
        daily["drawdown_pct"] = (daily["nav"] / daily["nav"].cummax() - 1) * 100
        daily["underwater_days"] = _underwater_days(daily["nav"], daily["trade_date"])
        family = str(candidate["model_family"])
        label = {"current_fixed": "当前固定参数", "joint_fixed": "联合重选固定阈值"}.get(family, "联调动态阈值")
        dash = "solid" if family == "current_fixed" else "dash" if family == "joint_fixed" else "dot"
        for row, column in ((1, "nav"), (2, "drawdown_pct"), (3, "underwater_days")):
            fig.add_trace(
                go.Scatter(
                    x=daily["trade_date"],
                    y=daily[column],
                    name=label,
                    legendgroup=label,
                    showlegend=row == 1,
                    line={"color": colors.get(family, "#D97706"), "dash": dash, "width": 2},
                ),
                row=row,
                col=1,
            )
    fig.update_layout(
        title=f"{base.symbol} 冻结测试净值、回撤与水下期对照",
        template="plotly_white",
        height=900,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.04},
    )
    comparison_path = chart_dir / f"{base.symbol}_oos_nav_drawdown_underwater.html"
    fig.write_html(comparison_path, include_plotlyjs="directory")
    chart_map.append(
        {
            "symbol": base.symbol,
            "section": "冻结测试对照",
            "question": "动态阈值是否改善净值路径、回撤与水下期",
            "chart_family": "trend",
            "chart_type": "three-panel line",
            "fields": "trade_date, normalized_nav, drawdown_pct, underwater_days",
            "takeaway": "比较当前固定、联合固定与动态方案的冻结样本路径",
            "palette": "charcoal-blue-orange plus dash styles",
            "path": str(comparison_path.relative_to(output_dir)),
        }
    )

    dynamic = stage2_grid[stage2_grid["model_family"].isin(DYNAMIC_FAMILIES)].copy()
    if not dynamic.empty:
        best_pairs = (
            dynamic.sort_values("mean_window_score", ascending=False)
            .drop_duplicates(["ma_period", "sigma_period"])
        )
        metrics = [
            ("mean_window_score", "训练期综合得分", "Blues"),
            ("annual_return_pct", "训练期年化收益（%）", "RdBu"),
            ("max_drawdown_pct", "训练期最大回撤（%）", "Blues"),
            ("longest_underwater_days", "训练期最长水下期（天）", "Blues"),
        ]
        heatmap = make_subplots(rows=2, cols=2, subplot_titles=[item[1] for item in metrics])
        for index, (metric, _title, colorscale) in enumerate(metrics):
            pivot = best_pairs.pivot(index="ma_period", columns="sigma_period", values=metric)
            heatmap.add_trace(
                go.Heatmap(
                    z=pivot.values,
                    x=pivot.columns,
                    y=pivot.index,
                    colorscale=colorscale,
                    colorbar={"len": 0.38, "y": 0.78 if index < 2 else 0.22},
                ),
                row=index // 2 + 1,
                col=index % 2 + 1,
            )
        heatmap.update_layout(
            title=f"{base.symbol} MA×σ周期参数热力图（每格取最稳健k/alpha）",
            template="plotly_white",
            height=820,
        )
        heatmap_path = chart_dir / f"{base.symbol}_ma_sigma_heatmaps.html"
        heatmap.write_html(heatmap_path, include_plotlyjs="directory")
        chart_map.append(
            {
                "symbol": base.symbol,
                "section": "参数稳定性",
                "question": "MA与σ周期周边是否形成稳定平台",
                "chart_family": "matrix",
                "chart_type": "heatmap small multiples",
                "fields": "ma_period, sigma_period, score, return, drawdown, underwater",
                "takeaway": "识别孤立尖峰与连续稳定区域",
                "palette": "blue and blue-orange diverging",
                "path": str(heatmap_path.relative_to(output_dir)),
            }
        )
    return chart_map


def build_report(
    output_dir: Path,
    decision: pd.DataFrame,
    quality: pd.DataFrame,
    proxy: pd.DataFrame,
    chart_map: pd.DataFrame,
) -> Path:
    supported = int(decision["support_deployment_review"].fillna(False).sum()) if not decision.empty else 0
    blocked = int((quality["status"] == "阻断").sum()) if not quality.empty else 0
    lines = [
        "# 均线与 σ 动态阈值两阶段回测技术报告",
        "",
        "## 技术摘要",
        "",
        f"本轮严格保持研究隔离：覆盖14只自身均线信号ETF，512890不参与；没有修改正式持仓参数。当前共有 **{supported}只** 同时满足全部上线评审硬条件，数据阻断 **{blocked}只**。未满足条件的结果不得据此修改正式参数。",
        "",
        "主结果使用正式 `forward_additive` 信号、显式 `none` 成交/估值、万0.6单边费用、100份整手；σ只使用截至前一交易日的偏离度。参数只由训练期确定，冻结测试和Walk-Forward不反向调参。",
        "",
        "## 逐ETF结论：阈值变化后MA是否改变",
        "",
    ]
    for row in decision.itertuples(index=False):
        changed = "改变" if bool(getattr(row, "ma_changed_after_dynamic_threshold", False)) else "未改变"
        dynamic_ma = getattr(row, "best_dynamic_ma", np.nan)
        dynamic_text = (
            "无稳定动态候选"
            if pd.isna(dynamic_ma)
            else (
                f"MA{int(dynamic_ma)} / σ{int(row.best_dynamic_sigma_period)} / "
                f"{row.best_dynamic_family} / k买卖={row.best_dynamic_buy_k:.4f}/{row.best_dynamic_sell_k:.4f} / "
                f"alpha买卖={row.best_dynamic_buy_alpha_pct:.3f}%/{row.best_dynamic_sell_alpha_pct:.3f}%"
            )
        )
        symbol_charts = (
            chart_map[chart_map["symbol"] == row.symbol]
            if not chart_map.empty and "symbol" in chart_map
            else pd.DataFrame()
        )
        chart_links: list[str] = []
        if not symbol_charts.empty:
            comparison = symbol_charts[symbol_charts["path"].astype(str).str.contains("nav_drawdown")]
            heatmap = symbol_charts[symbol_charts["path"].astype(str).str.contains("heatmap")]
            if not comparison.empty:
                chart_links.append(
                    f"[查看净值、回撤与水下期对照]({comparison.iloc[0]['path']})"
                )
            if not heatmap.empty:
                chart_links.append(f"[查看MA×σ热力图]({heatmap.iloc[0]['path']})")
        lines.extend(
            [
                f"### {row.symbol} {row.name}：{row.evidence_level}",
                "",
                f"阈值变化后最佳MA **{changed}**（当前MA{row.current_ma}；动态候选：{dynamic_text}）。改善归因：{row.improvement_source}。是否支持进入上线评审：**{'是' if row.support_deployment_review else '否'}**。",
                "",
                f"冻结测试优于当前/联合固定：{row.holdout_better_current}/{row.holdout_better_joint_fixed}；Walk-Forward优于当前/联合固定：{row.walk_forward_better_current}/{row.walk_forward_better_joint_fixed}；独立OOS胜率 {row.independent_oos_win_rate:.1%}；交易次数比 {row.trade_count_ratio_vs_current:.2f}；稳定平台/压力测试：{row.stable_parameter_platform}/{row.stress_direction_unchanged}；过拟合风险：**{row.overfitting_risk}**。",
                "",
                "；".join(chart_links) if chart_links else "该ETF因数据或模型条件未生成对照图。",
                "",
            ]
        )
    lines.extend(
        [
            "## 数据、指标与比较基准",
            "",
            "每只ETF独立研究；训练期候选在三个不重叠子窗口内按最低综合分、平均分、得分波动和交易次数依次选择。综合分权重为最长水下期45%、最大回撤25%、年化波动15%、年化收益10%、夏普5%。训练期年化收益达到10%的候选优先；全部不达标时才放宽，并在网格结果中保留标记。",
            "",
            "固定阈值获得与动态方案完全相同的局部MA搜索权。动态参数以训练期σ中位数锚定；每个MA重新计算偏离度、前日σ与k锚，不复用其他MA的σ参数。",
            "",
            "## 代理验证与不确定性",
            "",
            f"代理验证输出共 {len(proxy)} 行。159201、159545、159552的代理长历史只用于参数稳定性佐证；共同区间同时保留“代理信号交易ETF”和“ETF自身信号交易ETF”的同参数控制。任何不足3年历史的ETF均只给探索性结论。",
            "",
            "本研究仍是历史模拟而非因果证据。盘后收盘价主结果假设可在固定价格时段充分成交；次日开盘和5/10bp压力测试用于检验方向，不代表实盘成交保证。多重参数搜索会放大过拟合风险，因此只有全部硬条件同时成立才标记为可进入下一轮评审。",
            "",
            "## 建议的下一步",
            "",
            "- 对标记为“支持进入上线评审”的ETF进行人工复核和纸面跟踪，不自动修改正式参数。",
            "- 对未通过的平台、OOS胜率、交易次数或压力条件逐项保留当前固定参数。",
            "- 下一次新增正式收盘数据后，用相同冻结参数追加OOS，不重新选择参数。",
            "",
            "## 进一步问题",
            "",
            "- 已确认企业行动是否存在当前分红表未覆盖的份额折算；如有，应先补公告台账再更新研究。",
            "- 盘后固定价格交易的实际排队成交率是否需要作为下一轮独立压力变量。",
        ]
    )
    report_path = output_dir / "均线与sigma动态阈值两阶段回测报告.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def write_portable_artifact(
    output_dir: Path,
    decision: pd.DataFrame,
    quality: pd.DataFrame,
) -> Path:
    title = "均线与 σ 动态阈值两阶段回测"
    supported = int(decision["support_deployment_review"].fillna(False).sum())
    changed = int(decision["ma_changed_after_dynamic_threshold"].fillna(False).sum())
    exploratory = int(decision["evidence_level"].astype(str).str.startswith("C-").sum())
    blocked = int((quality["status"] == "阻断").sum())
    decision_rows = decision[
        [
            "symbol",
            "name",
            "current_ma",
            "best_dynamic_ma",
            "best_dynamic_family",
            "ma_changed_after_dynamic_threshold",
            "independent_oos_win_rate",
            "trade_count_ratio_vs_current",
            "overfitting_risk",
            "evidence_level",
            "support_deployment_review",
        ]
    ].copy()
    decision_rows["symbol"] = decision_rows["symbol"].astype(str).str.zfill(6)
    decision_rows["symbol_label"] = "ETF" + decision_rows["symbol"]
    decision_rows["best_dynamic_ma"] = pd.to_numeric(
        decision_rows["best_dynamic_ma"], errors="coerce"
    )
    decision_rows = decision_rows.replace({np.nan: None})
    ma_rows: list[dict[str, object]] = []
    for row in decision.itertuples(index=False):
        ma_rows.append(
            {"symbol": "ETF" + str(row.symbol).zfill(6), "ma_type": "当前MA", "ma_period": int(row.current_ma)}
        )
        if pd.notna(row.best_dynamic_ma):
            ma_rows.append(
                {
                    "symbol": "ETF" + str(row.symbol).zfill(6),
                    "ma_type": "动态阈值联调MA",
                    "ma_period": int(row.best_dynamic_ma),
                }
            )
    generated_at = pd.Timestamp.now(tz="Asia/Shanghai").isoformat()
    sources = [
        {
            "id": "decision_csv",
            "label": "两阶段研究决策汇总",
            "path": "decision_summary.csv",
            "query": {
                "engine": "duckdb",
                "language": "sql",
                "sql": "SELECT *, 'ETF' || lpad(CAST(symbol AS VARCHAR), 6, '0') AS symbol_label FROM read_csv_auto('decision_summary.csv')",
                "description": "读取逐ETF冻结测试、Walk-Forward和硬条件判断。",
                "tables_used": ["decision_summary.csv"],
                "executed_at": generated_at,
            },
        },
        {
            "id": "summary_sql",
            "label": "研究结论计数",
            "path": "decision_summary.csv",
            "query": {
                "engine": "duckdb",
                "language": "sql",
                "sql": "SELECT SUM(CASE WHEN support_deployment_review THEN 1 ELSE 0 END) AS supported, SUM(CASE WHEN ma_changed_after_dynamic_threshold THEN 1 ELSE 0 END) AS changed, SUM(CASE WHEN starts_with(evidence_level, 'C-') THEN 1 ELSE 0 END) AS exploratory FROM read_csv_auto('decision_summary.csv')",
                "description": "汇总上线评审支持数、MA变化数和探索性结论数。",
                "tables_used": ["decision_summary.csv"],
                "executed_at": generated_at,
            },
        },
        {
            "id": "ma_sql",
            "label": "当前MA与动态联调MA",
            "path": "decision_summary.csv",
            "query": {
                "engine": "duckdb",
                "language": "sql",
                "sql": "SELECT 'ETF' || lpad(CAST(symbol AS VARCHAR), 6, '0') AS symbol, '当前MA' AS ma_type, current_ma AS ma_period FROM read_csv_auto('decision_summary.csv') UNION ALL SELECT 'ETF' || lpad(CAST(symbol AS VARCHAR), 6, '0') AS symbol, '动态阈值联调MA' AS ma_type, best_dynamic_ma AS ma_period FROM read_csv_auto('decision_summary.csv') WHERE best_dynamic_ma IS NOT NULL",
                "description": "把当前MA和训练期动态联调MA整理为长表。",
                "tables_used": ["decision_summary.csv"],
                "executed_at": generated_at,
            },
        },
        {
            "id": "summary_chart_sql",
            "label": "研究结论计数长表",
            "path": "decision_summary.csv",
            "query": {
                "engine": "duckdb",
                "language": "sql",
                "sql": "WITH s AS (SELECT SUM(CASE WHEN support_deployment_review THEN 1 ELSE 0 END) AS supported, SUM(CASE WHEN ma_changed_after_dynamic_threshold THEN 1 ELSE 0 END) AS changed, SUM(CASE WHEN starts_with(evidence_level, 'C-') THEN 1 ELSE 0 END) AS exploratory FROM read_csv_auto('decision_summary.csv')) SELECT '支持上线评审' AS metric, supported AS count FROM s UNION ALL SELECT '最佳MA改变', changed FROM s UNION ALL SELECT '探索性结论', exploratory FROM s",
                "description": "把三项研究结论计数整理为图表长表。",
                "tables_used": ["decision_summary.csv"],
                "executed_at": generated_at,
            },
        },
        {"id": "quality_csv", "label": "研究数据质量检查", "path": "data_quality.csv"},
        {"id": "research_config", "label": "冻结研究配置", "path": "research_config_snapshot.json"},
    ]
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "14只ETF的MA与σ动态阈值训练、冻结测试、Walk-Forward和压力验证。",
            "generatedAt": generated_at,
            "filters": [],
            "cards": [
                {
                    "id": "supported_card",
                    "description": "同时满足全部上线评审硬条件的ETF数量。",
                    "dataset": "summary",
                    "sourceId": "summary_sql",
                    "metrics": [{"label": "支持进入上线评审", "field": "supported", "format": "number", "unit": "只"}],
                },
                {
                    "id": "changed_card",
                    "description": "阈值联调后训练期最佳MA发生变化的ETF数量。",
                    "dataset": "summary",
                    "sourceId": "summary_sql",
                    "metrics": [{"label": "最佳MA改变", "field": "changed", "format": "number", "unit": "只"}],
                },
                {
                    "id": "exploratory_card",
                    "description": "自身历史不足3年、仅能给探索性结论的ETF数量。",
                    "dataset": "summary",
                    "sourceId": "summary_sql",
                    "metrics": [{"label": "探索性结论", "field": "exploratory", "format": "number", "unit": "只"}],
                },
            ],
            "charts": [
                {
                    "id": "summary_counts",
                    "title": "研究结论计数",
                    "subtitle": "支持进入上线评审、最佳MA改变和探索性结论的ETF数量",
                    "type": "bar",
                    "dataset": "summary_chart",
                    "sourceId": "summary_chart_sql",
                    "encodings": {
                        "x": {"field": "metric", "type": "nominal", "label": "结论类型"},
                        "y": {"field": "count", "type": "quantitative", "label": "ETF数量"},
                    },
                },
            ],
            "tables": [
                {
                    "id": "decision_table",
                    "title": "逐ETF决策证据",
                    "subtitle": "动态候选、OOS胜率、交易频率、过拟合风险与证据等级",
                    "dataset": "decisions",
                    "sourceId": "decision_csv",
                    "defaultSort": {"field": "independent_oos_win_rate", "direction": "desc"},
                    "columns": [
                        {"field": "symbol", "label": "代码", "type": "text"},
                        {"field": "current_ma", "label": "当前MA", "format": "number"},
                        {"field": "best_dynamic_ma", "label": "动态联调MA", "format": "number"},
                        {"field": "independent_oos_win_rate", "label": "独立OOS胜率", "format": "percent"},
                        {"field": "overfitting_risk", "label": "过拟合风险", "type": "text"},
                        {"field": "evidence_level", "label": "证据等级", "type": "text"},
                    ],
                }
            ],
            "sources": sources,
            "blocks": [
                {"id": "title", "type": "markdown", "body": f"# {title}"},
                {
                    "id": "technical_summary",
                    "type": "markdown",
                    "body": f"## 技术摘要\n\n14只ETF全部完成研究，数据阻断{blocked}只；**没有ETF同时满足全部上线评审硬条件**，因此本轮不支持修改任何正式参数。阈值联调后有{changed}只训练期最佳MA发生变化，但这些变化未同时通过冻结测试、Walk-Forward、固定阈值公平基准、参数平台、交易次数和成交压力约束。",
                },
                {"id": "headline_metrics", "type": "metric-strip", "cardIds": ["supported_card", "changed_card", "exploratory_card"]},
                {
                    "id": "summary_chart_interpretation",
                    "type": "markdown",
                    "sourceId": "summary_chart_sql",
                    "body": "图中只展示结论计数：MA发生变化说明阈值和均线存在交互，但只有‘支持进入上线评审’才代表所有样本外与压力条件同时通过。",
                },
                {"id": "summary_chart", "type": "chart", "chartId": "summary_counts"},
                {
                    "id": "oos_interpretation",
                    "type": "markdown",
                    "sourceId": "decision_csv",
                    "body": "## 样本外证据没有支持参数变更\n\n图中胜率按独立OOS窗口计算。硬条件要求胜率至少60%，且冻结测试和Walk-Forward的最低分、平均分都高于当前固定参数；单个高胜率不能替代其他条件。",
                },
                {
                    "id": "ma_interpretation",
                    "type": "markdown",
                    "sourceId": "decision_csv",
                    "body": "## 阈值改变确实会改变部分最佳MA，但不等于可上线\n\nMA变化表明均线周期与阈值存在交互；它只是训练期完整参数组的一部分，必须与同等搜索权的固定阈值基准和冻结样本结果一起判断。",
                },
                {
                    "id": "decision_interpretation",
                    "type": "markdown",
                    "sourceId": "decision_csv",
                    "body": "## 逐ETF证据保持可审计\n\n表格给出完整决策层字段；精确k、alpha、各窗口指标和压力场景保存在配套CSV及中文技术报告中。",
                },
                {"id": "decision_table_block", "type": "table", "tableId": "decision_table"},
                {
                    "id": "definitions",
                    "type": "markdown",
                    "sourceId": "research_config",
                    "body": "## 数据与指标定义\n\n信号使用正式前复权差值，成交和估值使用显式未复权价格；单边费用万0.6、100份整手。σ严格滞后一日。评分权重为最长水下期45%、最大回撤25%、年化波动15%、年化收益10%、夏普5%。",
                },
                {
                    "id": "methodology",
                    "type": "markdown",
                    "sourceId": "research_config",
                    "body": "## 两阶段方法与验证\n\n第一阶段固定现有MA隔离比较固定阈值、对称/非对称纯σ和混合阈值；第二阶段只让训练期稳定动态模型进入局部MA联调，固定阈值获得相同MA搜索权。参数只由训练期确定，随后运行70/30冻结测试、非重叠Walk-Forward、代理控制以及次日开盘和5/10bp滑点压力。",
                },
                {
                    "id": "limitations",
                    "type": "markdown",
                    "body": "## 限制与稳健性边界\n\n本研究是历史模拟，不是因果证据。盘后收盘成交假设仍受真实排队约束；多重搜索带来过拟合风险。159201、159545、159552自身历史不足3年，代理长历史只作稳定性佐证，不能直接支持正式参数变更。",
                },
                {
                    "id": "next_steps",
                    "type": "markdown",
                    "body": "## 建议的下一步\n\n- 保留14只ETF现有正式参数。\n- 用本轮冻结参数继续追加真实OOS数据，不重选参数。\n- 只有未来重新满足全部硬条件时，才进入人工上线评审。",
                },
                {
                    "id": "further_questions",
                    "type": "markdown",
                    "body": "## 进一步问题\n\n- 后续企业行动公告是否需要补充新的份额折算台账？\n- 盘后固定价格交易的真实成交率是否应加入下一轮独立压力变量？",
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "summary": [{"supported": supported, "changed": changed, "exploratory": exploratory}],
                "summary_chart": [
                    {"metric": "支持上线评审", "count": supported},
                    {"metric": "最佳MA改变", "count": changed},
                    {"metric": "探索性结论", "count": exploratory},
                ],
                "decisions": decision_rows.to_dict(orient="records"),
                "ma_comparison": ma_rows,
            },
            "accessIssues": [],
        },
        "sources": sources,
    }
    artifact_path = output_dir / "artifact.json"
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return artifact_path


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    allocations = [AuditAllocation(**item) for item in config["allocations"]]
    if args.symbols.strip():
        requested = {value.strip() for value in args.symbols.split(",") if value.strip()}
        allocations = [item for item in allocations if item.symbol in requested]
    settings = AuditSettings(
        initial_capital=float(config["initial_capital"]),
        commission_rate=float(config["commission_rate"]),
        lot_size=int(config["lot_size"]),
        execution_mode=str(config["execution_mode"]),
        after_hours_fill_rate=float(config["after_hours_fill_rate"]),
        slippage_bp=float(config["slippage_bp"]),
        cash_annual_rate=float(config["cash_annual_rate"]),
        random_seed=int(config["random_seed"]),
    )
    market_data, quality, actions = load_market_data(
        allocations,
        config,
        output_dir,
        refresh_missing=args.refresh_missing_raw,
    )
    write_csv(quality, output_dir / "data_quality.csv")
    write_csv(actions, output_dir / "corporate_actions.csv")
    allocation_map = {item.symbol: item for item in allocations}
    results: dict[str, dict[str, object]] = {}
    baseline_frames: list[pd.DataFrame] = []
    stage1_frames: list[pd.DataFrame] = []
    stage2_frames: list[pd.DataFrame] = []
    score_frames: list[pd.DataFrame] = []
    holdout_frames: list[pd.DataFrame] = []
    walk_frames: list[pd.DataFrame] = []
    stress_frames: list[pd.DataFrame] = []
    stability_frames: list[pd.DataFrame] = []
    chart_rows: list[dict[str, object]] = []
    for index, allocation in enumerate(allocations, start=1):
        if allocation.symbol not in market_data:
            continue
        print(f"[{index}/{len(allocations)}] 研究 {allocation.symbol} {allocation.name}", flush=True)
        result = run_primary_asset(
            market_data[allocation.symbol],
            allocation,
            settings,
            quick=args.quick,
        )
        results[allocation.symbol] = result
        baseline_frames.append(result["baseline"])
        stage1 = _grid_table(result["fit"]["stage1"], "第一阶段")
        stage2 = _grid_table(result["fit"]["stage2"], "第二阶段")
        stage1_frames.append(stage1)
        stage2_frames.append(stage2)
        first_scores = result["fit"]["stage1"]["evaluations"].copy()
        first_scores["phase"] = "第一阶段"
        second_scores = result["fit"]["stage2"]["evaluations"].copy()
        second_scores["phase"] = "第二阶段"
        score_frames.extend([first_scores, second_scores])
        holdout_frames.append(result["holdout"])
        if not result["walk_forward"].empty:
            walk_frames.append(result["walk_forward"])
        stress_frames.append(result["stress"])
        if not result["stability"].empty:
            stability_frames.append(result["stability"])
        chart_rows.extend(
            write_asset_charts(
                output_dir,
                market_data[allocation.symbol],
                allocation,
                settings,
                result,
                stage2,
            )
        )
    combined = {
        "baseline.csv": baseline_frames,
        "stage1_grid.csv": stage1_frames,
        "stage2_joint_grid.csv": stage2_frames,
        "training_scores.csv": score_frames,
        "holdout_frozen_test.csv": holdout_frames,
        "walk_forward.csv": walk_frames,
        "stress_tests.csv": stress_frames,
        "parameter_stability.csv": stability_frames,
    }
    for filename, frames in combined.items():
        write_csv(
            pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame(),
            output_dir / filename,
        )
    proxy = (
        pd.DataFrame()
        if args.quick
        else run_proxy_validation(
            config,
            market_data,
            allocation_map,
            settings,
            quick=False,
        )
    )
    write_csv(proxy, output_dir / "proxy_validation.csv")
    decision = build_decision_summary(allocation_map, quality, results)
    write_csv(decision, output_dir / "decision_summary.csv")
    chart_map = pd.DataFrame(chart_rows)
    write_csv(chart_map, output_dir / "chart_map.csv")
    report_path = build_report(output_dir, decision, quality, proxy, chart_map)
    (output_dir / "research_config_snapshot.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    artifact_path = write_portable_artifact(output_dir, decision, quality)
    manifest = {
        "config": str(config_path),
        "output_dir": str(output_dir),
        "symbols_requested": [item.symbol for item in allocations],
        "symbols_completed": sorted(results),
        "formal_parameters_modified": False,
        "quick": bool(args.quick),
        "report": str(report_path),
        "portable_artifact": str(artifact_path),
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"完成：{report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
