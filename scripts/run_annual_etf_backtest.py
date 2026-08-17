#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache import load_dataset  # noqa: E402
from core.paths import OUTPUT_DIR  # noqa: E402
from services.annual_etf_portfolio import (  # noqa: E402
    ANNUAL_CACHE_PERIOD,
    ANNUAL_CACHE_SOURCE,
    ANNUAL_DIVIDEND_CACHE_KEY,
    ANNUAL_DIVIDEND_DATA_TYPE,
    ANNUAL_RAW_DATA_TYPE,
    AnnualBacktestSettings,
    annual_raw_cache_key,
    dividends_for_symbol,
    load_index_family_config,
    load_registry,
    normalize_annual_market_data,
    run_annual_etf_backtest,
    share_splits_for_symbol,
)
from services.market_calendar import get_market_window, latest_completed_trade_date  # noqa: E402


REGISTRY_PATH = ROOT / "config" / "annual_etf_registry_v1.csv"
WHITELIST_PATH = ROOT / "config" / "annual_etf_index_families.json"
RUNTIME_DIR = OUTPUT_DIR / "annual_etf_backtest"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行历史年度ETF动态组合回测并保存结果")
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD；留空使用最新已完成A股交易日")
    parser.add_argument("--initial-capital", type=float, default=500000.0)
    parser.add_argument("--commission-rate", type=float, default=0.00006)
    parser.add_argument("--cash-annual-rate", type=float, default=0.015)
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    parser.add_argument("--whitelist", default=str(WHITELIST_PATH))
    parser.add_argument("--checkpoint-dir", default=str(RUNTIME_DIR / "checkpoints"))
    parser.add_argument("--output-dir", default=str(RUNTIME_DIR / "runs"))
    return parser.parse_args()


def _completed_date(value: str) -> pd.Timestamp:
    market = get_market_window("A股")
    if market is None:
        raise RuntimeError("找不到A股市场日历。")
    latest = pd.Timestamp(
        latest_completed_trade_date(market, datetime.now(ZoneInfo("Asia/Shanghai")))
    )
    if not value:
        return latest
    requested = pd.Timestamp(value).normalize()
    return min(requested, latest)


def _date_column(frame: pd.DataFrame) -> str | None:
    for column in ("日期", "trade_date", "date", "datetime"):
        if column in frame.columns:
            return column
    return None


def _filter_completed_rows(frame: pd.DataFrame, completed_date: pd.Timestamp) -> pd.DataFrame:
    column = _date_column(frame)
    if column is None:
        raise ValueError("正式日线缺少日期列。")
    dates = pd.to_datetime(frame[column], errors="coerce").dt.normalize()
    return frame.loc[dates <= completed_date].copy().reset_index(drop=True)


def _load_proxy_data(records, completed_date: pd.Timestamp) -> dict[str, pd.DataFrame]:
    proxy_data: dict[str, pd.DataFrame] = {}
    for record in records:
        if not record.proxy_path:
            continue
        path = Path(record.proxy_path)
        if not path.is_absolute():
            path = ROOT / path
        raw = _filter_completed_rows(pd.read_csv(path), completed_date)
        proxy_data[record.symbol] = normalize_annual_market_data(raw)
    return proxy_data


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        to_save = frame if len(frame.columns) else pd.DataFrame({"_empty": []})
        to_save.to_csv(temp_path, index=False, encoding="utf-8-sig")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    completed_date = _completed_date(args.end_date)
    records = load_registry(args.registry)
    whitelist = load_index_family_config(args.whitelist)
    dividends, dividend_meta = load_dataset(
        ANNUAL_DIVIDEND_CACHE_KEY,
        ANNUAL_CACHE_SOURCE,
        ANNUAL_DIVIDEND_DATA_TYPE,
        ANNUAL_CACHE_PERIOD,
    )
    if dividends is None:
        dividends = pd.DataFrame()

    market_data: dict[str, pd.DataFrame] = {}
    coverage_rows: list[dict[str, object]] = []
    missing: list[str] = []
    for record in records:
        raw, meta = load_dataset(
            annual_raw_cache_key(record.symbol),
            ANNUAL_CACHE_SOURCE,
            ANNUAL_RAW_DATA_TYPE,
            ANNUAL_CACHE_PERIOD,
        )
        if raw is None or raw.empty:
            missing.append(record.symbol)
            continue
        raw = _filter_completed_rows(raw, completed_date)
        normalized = normalize_annual_market_data(
            raw,
            dividends_for_symbol(dividends, record.symbol),
            share_splits_for_symbol(whitelist, record.symbol),
        )
        market_data[record.symbol] = normalized
        coverage_rows.append(
            {
                "symbol": record.symbol,
                "name": record.name,
                "direction": record.direction,
                "rows": len(normalized),
                "first_trade_date": normalized["trade_date"].min(),
                "last_trade_date": normalized["trade_date"].max(),
                "raw_cache_update": (meta or {}).get("last_update_time", ""),
            }
        )
    if missing:
        raise RuntimeError("缺少年度专用行情缓存：" + "、".join(missing))

    settings = AnnualBacktestSettings(
        start_year=args.start_year,
        end_date=completed_date,
        initial_capital=args.initial_capital,
        commission_rate=args.commission_rate,
        cash_annual_rate=args.cash_annual_rate,
    )
    proxy_data = _load_proxy_data(records, completed_date)

    def progress(label: str, fraction: float) -> None:
        print(f"[{fraction:6.1%}] {label}", flush=True)

    result = run_annual_etf_backtest(
        records,
        whitelist,
        market_data,
        settings,
        proxy_data=proxy_data,
        checkpoint_dir=args.checkpoint_dir,
        progress_callback=progress,
    )
    actual_end_date = pd.Timestamp(result.summary.iloc[0]["end_date"])
    run_dir = (
        Path(args.output_dir)
        / f"{settings.start_year}_{actual_end_date:%Y-%m-%d}_{result.fingerprint[:12]}"
    )
    tables = {
        "summary": result.summary,
        "daily": result.daily,
        "yearly": result.yearly,
        "selections": result.selections,
        "qualification": result.qualification,
        "parameters": result.parameters,
        "trades": result.trades,
        "migrations": result.migrations,
        "contribution": result.contribution,
        "errors": result.errors,
        "data_coverage": pd.DataFrame(coverage_rows),
    }
    for name, frame in tables.items():
        _atomic_csv(run_dir / f"{name}.csv", frame)
    _atomic_write(run_dir / "report.md", result.report_markdown)
    _atomic_write(
        run_dir / "run_manifest.json",
        json.dumps(
            {
                "fingerprint": result.fingerprint,
                "settings": asdict(settings),
                "requested_completed_date": str(completed_date.date()),
                "actual_end_date": str(actual_end_date.date()),
                "registry_path": str(Path(args.registry).resolve()),
                "whitelist_path": str(Path(args.whitelist).resolve()),
                "market_symbols": sorted(market_data),
                "proxy_symbols": sorted(proxy_data),
                "dividend_cache_update": (dividend_meta or {}).get("last_update_time", ""),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
    )
    print(result.summary.to_string(index=False))
    print(f"结果目录：{run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
