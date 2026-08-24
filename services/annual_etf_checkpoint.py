from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterable

import pandas as pd

from services.annual_etf_models import (
    ALL_SLOTS,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_SAME_CLOSE,
    AnnualBacktestSettings,
    AnnualPortfolioResult,
    AnnualQualificationResult,
    AnnualSelection,
    HistoricalEtfRecord,
    _normalize_symbol,
)
from services.annual_etf_report import (
    _summary_row,
    _yearly_table,
    build_markdown_report,
)
from services.annual_etf_selection import (
    build_annual_selections,
    selections_frame,
)
from services.annual_etf_simulation import (
    _simulate_annual_hold_benchmark,
    _simulate_parking_benchmark,
    simulate_annual_portfolio,
)


def _frame_fingerprint(frame: pd.DataFrame | None) -> dict[str, object]:
    if frame is None or frame.empty:
        return {"rows": 0}
    columns = sorted(str(column) for column in frame.columns)
    stable = frame.reindex(columns=columns).copy()
    for column in columns:
        stable[column] = stable[column].map(lambda value: "" if pd.isna(value) else str(value))
    encoded = stable.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return {
        "rows": len(stable),
        "columns": columns,
        "hash": sha256(encoded).hexdigest(),
    }


def data_fingerprint(
    market_data: dict[str, pd.DataFrame],
    settings: AnnualBacktestSettings,
    *,
    records: Iterable[HistoricalEtfRecord] | None = None,
    whitelist: dict[str, object] | None = None,
    proxy_data: dict[str, pd.DataFrame] | None = None,
) -> str:
    payload: dict[str, object] = {
        "settings": asdict(settings),
        "registry": [asdict(item) for item in sorted(records or [], key=lambda item: item.symbol)],
        "whitelist": whitelist or {},
        "market_data": {},
        "proxy_data": {},
    }
    for symbol in sorted(market_data):
        payload["market_data"][symbol] = _frame_fingerprint(market_data[symbol])
    for symbol in sorted(proxy_data or {}):
        payload["proxy_data"][symbol] = _frame_fingerprint((proxy_data or {})[symbol])
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return sha256(encoded).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


class AnnualCheckpointStore:
    def __init__(self, directory: str | Path, fingerprint: str):
        self.directory = Path(directory) / fingerprint
        self.fingerprint = fingerprint

    def _path(self, stage: str) -> Path:
        return self.directory / f"{stage}.csv"

    def load(self, stage: str) -> pd.DataFrame | None:
        path = self._path(stage)
        if not path.exists():
            return None
        try:
            frame = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        if list(frame.columns) == ["_empty"]:
            return pd.DataFrame()
        return frame

    def save(self, stage: str, frame: pd.DataFrame) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(stage)
        handle, temp_name = tempfile.mkstemp(prefix=f".{stage}.", suffix=".tmp", dir=self.directory)
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            to_save = frame if len(frame.columns) else pd.DataFrame({"_empty": []})
            to_save.to_csv(temp_path, index=False, encoding="utf-8-sig")
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        _atomic_write_json(
            self.directory / "manifest.json",
            {
                "fingerprint": self.fingerprint,
                "stages": sorted(item.stem for item in self.directory.glob("*.csv")),
            },
        )
        return path


def _restore_checkpoint_dates(frame: pd.DataFrame) -> pd.DataFrame:
    restored = frame.copy()
    for column in restored.columns:
        if column == "trade_date" or column.endswith("_date"):
            restored[column] = pd.to_datetime(restored[column], errors="coerce")
    return restored


def _restore_selections(frame: pd.DataFrame) -> list[AnnualSelection]:
    selections: list[AnnualSelection] = []
    for row in frame.to_dict("records"):
        relaxed = str(row.get("return_gate_relaxed", "")).strip().lower() in {"1", "true", "yes"}
        selections.append(
            AnnualSelection(
                year=int(row["year"]),
                slot=str(row["slot"]),
                symbol=_normalize_symbol(row["symbol"]),
                name=str(row["name"]),
                ma_period=int(row["ma_period"]),
                threshold_pct=float(row["threshold_pct"]),
                strategy=str(row["strategy"]),
                validation_score=float(row["validation_score"]),
                validation_annual_return_pct=float(row["validation_annual_return_pct"]),
                validation_sharpe=float(row["validation_sharpe"]),
                return_gate_relaxed=relaxed,
                proxy_ratio_pct=float(row["proxy_ratio_pct"]),
                decision_date=pd.Timestamp(row["decision_date"]),
            )
        )
    return selections



def run_annual_etf_backtest_core(
    records: list[HistoricalEtfRecord],
    whitelist: dict[str, object],
    market_data: dict[str, pd.DataFrame],
    settings: AnnualBacktestSettings,
    *,
    proxy_data: dict[str, pd.DataFrame] | None = None,
    checkpoint_dir: str | Path | None = None,
    progress_callback: Callable[[str, float], None] | None = None,
    preflight_fn: Callable[..., AnnualQualificationResult],
) -> AnnualPortfolioResult:
    registered_versions = {item.registry_version for item in records if item.registry_version}
    if registered_versions and registered_versions != {settings.registry_version}:
        raise ValueError(
            f"注册表版本{sorted(registered_versions)}与回测设置{settings.registry_version}不一致。"
        )
    configured_whitelist_version = str(whitelist.get("version", ""))
    if (
        configured_whitelist_version
        and configured_whitelist_version != settings.whitelist_version
    ):
        raise ValueError(
            f"白名单版本{configured_whitelist_version}与回测设置{settings.whitelist_version}不一致。"
        )
    fingerprint = data_fingerprint(
        market_data,
        settings,
        records=records,
        whitelist=whitelist,
        proxy_data=proxy_data,
    )
    store = AnnualCheckpointStore(checkpoint_dir, fingerprint) if checkpoint_dir else None
    final_stages = (
        "summary",
        "daily",
        "yearly",
        "selections",
        "qualification",
        "parameters",
        "trades",
        "migrations",
        "contribution",
        "errors",
    )
    if store:
        cached_final = {stage: store.load(stage) for stage in final_stages}
        if all(frame is not None for frame in cached_final.values()):
            if progress_callback:
                progress_callback("读取完整检查点", 1.0)
            restored = {
                stage: _restore_checkpoint_dates(frame)
                for stage, frame in cached_final.items()
            }
            report = build_markdown_report(
                restored["summary"],
                restored["selections"],
                settings,
                fingerprint,
                restored,
            )
            return AnnualPortfolioResult(
                summary=restored["summary"],
                daily=restored["daily"],
                yearly=restored["yearly"],
                selections=restored["selections"],
                qualification=restored["qualification"],
                parameters=restored["parameters"],
                trades=restored["trades"],
                migrations=restored["migrations"],
                contribution=restored["contribution"],
                errors=restored["errors"],
                report_markdown=report,
                fingerprint=fingerprint,
            )

    if progress_callback:
        progress_callback("准备数据", 0.02)
    cached_selection = store.load("selections") if store else None
    cached_qualification = store.load("qualification") if store else None
    cached_parameters = store.load("parameters") if store else None
    if (
        cached_selection is not None
        and not cached_selection.empty
        and cached_qualification is not None
        and cached_parameters is not None
    ):
        selection_df = _restore_checkpoint_dates(cached_selection)
        selections = _restore_selections(selection_df)
        parameters = _restore_checkpoint_dates(cached_parameters)
        preflight_errors = store.load("preflight_errors")
        selection_errors = store.load("selection_errors")
        preflight = AnnualQualificationResult(
            qualification=_restore_checkpoint_dates(cached_qualification),
            errors=preflight_errors if preflight_errors is not None else pd.DataFrame(),
        )
        if selection_errors is None:
            selection_errors = pd.DataFrame()
        if progress_callback:
            progress_callback("读取年度筛选检查点", 0.50)
    else:
        preflight = preflight_fn(
            records, whitelist, market_data, settings, proxy_data
        )
        if store:
            store.save("qualification", preflight.qualification)
            store.save("preflight_errors", preflight.errors)
        selection_progress = (
            (lambda label, fraction: progress_callback(label, 0.10 + 0.40 * fraction))
            if progress_callback
            else None
        )
        selections, parameters, selection_errors = build_annual_selections(
            records, preflight, settings, selection_progress
        )
        selection_df = selections_frame(selections)
        if store:
            store.save("selections", selection_df)
            store.save("parameters", parameters)
            store.save("selection_errors", selection_errors)

    missing_start = set(ALL_SLOTS) - {
        item.slot for item in selections if item.year == settings.start_year
    }
    if missing_start:
        errors = pd.concat([preflight.errors, selection_errors], ignore_index=True)
        detail = "、".join(sorted(missing_start))
        raise ValueError(
            f"起投年度缺少合格方向：{detail}。请先补齐注册表、代理或正式行情。\n"
            f"{errors.to_string(index=False)}"
        )

    main_stage_names = ("daily", "trades", "migrations", "contribution")
    cached_main = {stage: store.load(stage) for stage in main_stage_names} if store else {}
    if store and all(frame is not None for frame in cached_main.values()):
        daily = _restore_checkpoint_dates(cached_main["daily"])
        trades = _restore_checkpoint_dates(cached_main["trades"])
        migrations = _restore_checkpoint_dates(cached_main["migrations"])
        contribution = cached_main["contribution"]
        if progress_callback:
            progress_callback("读取主模拟检查点", 0.70)
    else:
        if progress_callback:
            progress_callback("逐日模拟：主结果", 0.55)
        main_progress = (
            (lambda label, fraction: progress_callback(label, 0.55 + 0.15 * fraction))
            if progress_callback
            else None
        )
        daily, trades, migrations, contribution = simulate_annual_portfolio(
            market_data,
            selections,
            settings,
            execution_mode=EXECUTION_SAME_CLOSE,
            progress_callback=main_progress,
        )
        if store:
            for stage, frame in zip(
                main_stage_names, (daily, trades, migrations, contribution)
            ):
                store.save(stage, frame)

    cached_stress = store.load("stress") if store else None
    cached_stress_trades = store.load("stress_trades") if store else None
    if cached_stress is not None and cached_stress_trades is not None:
        stress = _restore_checkpoint_dates(cached_stress)
        stress_trades = _restore_checkpoint_dates(cached_stress_trades)
    else:
        if progress_callback:
            progress_callback("逐日模拟：次日收盘压力", 0.75)
        stress, stress_trades, _stress_migrations, _stress_contribution = simulate_annual_portfolio(
            market_data, selections, settings, execution_mode=EXECUTION_NEXT_CLOSE
        )
        if store:
            store.save("stress", stress)
            store.save("stress_trades", stress_trades)

    hold = store.load("annual_hold") if store else None
    if hold is None:
        hold = _simulate_annual_hold_benchmark(market_data, selections, settings)
        if store:
            store.save("annual_hold", hold)
    else:
        hold = _restore_checkpoint_dates(hold)
    parking = store.load("parking_benchmark") if store else None
    if parking is None:
        parking = _simulate_parking_benchmark(market_data, daily["trade_date"], settings)
        if store:
            store.save("parking_benchmark", parking)
    else:
        parking = _restore_checkpoint_dates(parking)

    comparison = daily[["trade_date", "portfolio_value"]].rename(
        columns={"portfolio_value": "main_value"}
    )
    comparison = (
        comparison.merge(
            hold[["trade_date", "portfolio_value"]].rename(
                columns={"portfolio_value": "annual_hold_value"}
            ),
            on="trade_date",
            how="left",
        )
        .merge(
            parking[["trade_date", "portfolio_value"]].rename(
                columns={"portfolio_value": "parking_value"}
            ),
            on="trade_date",
            how="left",
        )
        .merge(
            stress[["trade_date", "portfolio_value"]].rename(
                columns={"portfolio_value": "next_close_value"}
            ),
            on="trade_date",
            how="left",
        )
    )
    for column in ("annual_hold_value", "parking_value", "next_close_value"):
        comparison[column] = pd.to_numeric(comparison[column], errors="coerce").ffill()
    comparison_indexed = comparison.set_index("trade_date")
    for column in comparison.columns:
        if column.endswith("_value"):
            daily[column] = comparison_indexed[column].reindex(daily["trade_date"]).to_numpy()

    summary = pd.DataFrame(
        [
            _summary_row("年度动态组合", daily, settings, trades),
            _summary_row("年度选择一直持有", hold, settings),
            _summary_row("全部持有512890", parking, settings),
            _summary_row("次日收盘压力", stress, settings, stress_trades),
        ]
    )
    yearly = _yearly_table(daily, settings.initial_capital)
    errors = pd.concat([preflight.errors, selection_errors], ignore_index=True)
    report = build_markdown_report(
        summary,
        selection_df,
        settings,
        fingerprint,
        {
            "yearly": yearly,
            "contribution": contribution,
            "migrations": migrations,
            "errors": errors,
        },
    )
    if store:
        final_frames = {
            "summary": summary,
            "daily": daily,
            "yearly": yearly,
            "selections": selection_df,
            "qualification": preflight.qualification,
            "parameters": parameters,
            "trades": trades,
            "migrations": migrations,
            "contribution": contribution,
            "errors": errors,
        }
        for stage, frame in final_frames.items():
            store.save(stage, frame)
    if progress_callback:
        progress_callback("生成报告", 1.0)
    return AnnualPortfolioResult(
        summary=summary,
        daily=daily,
        yearly=yearly,
        selections=selection_df,
        qualification=preflight.qualification,
        parameters=parameters,
        trades=trades,
        migrations=migrations,
        contribution=contribution,
        errors=errors,
        report_markdown=report,
        fingerprint=fingerprint,
    )
