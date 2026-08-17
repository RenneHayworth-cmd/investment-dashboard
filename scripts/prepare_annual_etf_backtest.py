#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache import load_dataset, save_dataset  # noqa: E402
from core.paths import OUTPUT_DIR  # noqa: E402
from services.annual_etf_portfolio import (  # noqa: E402
    dividends_for_symbol,
    fetch_annual_dividends,
    fetch_annual_etf_raw_history,
    load_registry,
    normalize_annual_market_data,
)
from services.market_calendar import get_market_window, latest_completed_trade_date  # noqa: E402


CACHE_SOURCE = "annual_etf"
RAW_DATA_TYPE = "raw_history"
DIVIDEND_DATA_TYPE = "corporate_actions"
PERIOD = "full_1d"
DIVIDEND_CACHE_KEY = "annual_etf_dividends_v1"


def raw_cache_key(symbol: str) -> str:
    return f"annual_etf_v1_{symbol}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分批预建历史年度ETF动态组合行情检查点")
    parser.add_argument(
        "--registry",
        default=str(ROOT / "config" / "annual_etf_registry_v1.csv"),
        help="版本化ETF注册表",
    )
    parser.add_argument("--apply", action="store_true", help="确认联网抓取并写入本地运行缓存")
    parser.add_argument("--refresh", action="store_true", help="已存在缓存也增量检查最新正式日线")
    parser.add_argument("--batch-size", type=int, default=8, help="本批最多处理的ETF数量")
    parser.add_argument("--offset", type=int, default=0, help="从筛选后ETF列表的第几个开始")
    parser.add_argument("--symbols", default="", help="逗号分隔ETF代码；留空使用注册表")
    parser.add_argument("--start-date", default="20000101", help="历史行情起始日YYYYMMDD")
    parser.add_argument("--dividend-start-year", type=int, default=2005)
    parser.add_argument(
        "--status-path",
        default=str(OUTPUT_DIR / "annual_etf_backtest" / "prepare_status.csv"),
    )
    return parser.parse_args()


def _date_column(frame: pd.DataFrame) -> str | None:
    for column in ("日期", "trade_date", "date", "datetime"):
        if column in frame.columns:
            return column
    return None


def filter_completed_rows(frame: pd.DataFrame, completed_date) -> pd.DataFrame:
    column = _date_column(frame)
    if column is None:
        raise ValueError("正式日线缺少日期列。")
    dates = pd.to_datetime(frame[column], errors="coerce").dt.date
    return frame.loc[dates <= completed_date].copy().reset_index(drop=True)


def append_unseen_dates(existing: pd.DataFrame | None, fetched: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        return fetched.copy()
    existing_date = _date_column(existing)
    fetched_date = _date_column(fetched)
    if existing_date is None or fetched_date is None:
        raise ValueError("正式日线缺少日期列，拒绝覆盖原缓存。")
    old = existing.copy()
    new = fetched.copy()
    old["_merge_date"] = pd.to_datetime(old[existing_date], errors="coerce").dt.normalize()
    new["_merge_date"] = pd.to_datetime(new[fetched_date], errors="coerce").dt.normalize()
    old_dates = set(old["_merge_date"].dropna())
    new = new[~new["_merge_date"].isin(old_dates)]
    merged = pd.concat([old, new], ignore_index=True, sort=False)
    return merged.sort_values("_merge_date").drop(columns="_merge_date").reset_index(drop=True)


def append_dividend_rows(existing: pd.DataFrame | None, fetched: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        return fetched.drop_duplicates().reset_index(drop=True)
    return pd.concat([existing, fetched], ignore_index=True, sort=False).drop_duplicates(keep="first")


def atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        frame.to_csv(temp_path, index=False, encoding="utf-8-sig")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    records = load_registry(args.registry)
    requested = {value.strip() for value in args.symbols.split(",") if value.strip()}
    if requested:
        records = [record for record in records if record.symbol in requested]
    cache_rows: list[dict[str, object]] = []
    pending = []
    for record in records:
        cached, meta = load_dataset(raw_cache_key(record.symbol), CACHE_SOURCE, RAW_DATA_TYPE, PERIOD)
        status = "已有缓存" if cached is not None and not cached.empty else "缺少缓存"
        cache_rows.append(
            {
                "symbol": record.symbol,
                "name": record.name,
                "status": status,
                "rows": len(cached) if cached is not None else 0,
                "last_trade_date": (meta or {}).get("last_trade_date", ""),
                "error": "",
            }
        )
        if args.refresh or cached is None or cached.empty:
            pending.append(record)

    selected = pending[max(0, args.offset) : max(0, args.offset) + max(1, args.batch_size)]
    print(pd.DataFrame(cache_rows).to_string(index=False))
    print(f"本批待处理 {len(selected)} 只；剩余 {max(0, len(pending) - args.offset - len(selected))} 只。")
    if not args.apply:
        print("当前为只读预览；确认后加 --apply 才会联网和写入本地缓存。")
        return 0

    market = get_market_window("A股")
    if market is None:
        raise RuntimeError("找不到A股市场日历。")
    completed_date = latest_completed_trade_date(
        market, datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    status_by_symbol = {row["symbol"]: row for row in cache_rows}
    for record in selected:
        try:
            fetched = fetch_annual_etf_raw_history(
                record,
                start_date=args.start_date,
                end_date=pd.Timestamp(completed_date).strftime("%Y%m%d"),
            )
            fetched = filter_completed_rows(fetched, completed_date)
            cached, _meta = load_dataset(
                raw_cache_key(record.symbol), CACHE_SOURCE, RAW_DATA_TYPE, PERIOD
            )
            merged = append_unseen_dates(cached, fetched)
            normalize_annual_market_data(merged)
            save_dataset(
                raw_cache_key(record.symbol),
                record.name,
                CACHE_SOURCE,
                RAW_DATA_TYPE,
                merged,
                PERIOD,
            )
            status_by_symbol[record.symbol].update(
                status="已补齐",
                rows=len(merged),
                last_trade_date=str(
                    pd.to_datetime(merged[_date_column(merged)], errors="coerce").max().date()
                ),
            )
        except Exception as exc:
            status_by_symbol[record.symbol].update(status="失败，保留原缓存", error=str(exc))

    existing_dividends, _meta = load_dataset(
        DIVIDEND_CACHE_KEY, CACHE_SOURCE, DIVIDEND_DATA_TYPE, PERIOD
    )
    dividend_parts = []
    dividend_errors = []
    for year in range(int(args.dividend_start_year), pd.Timestamp.today().year + 1):
        try:
            dividend_parts.append(fetch_annual_dividends(year))
        except Exception as exc:
            dividend_errors.append(f"{year}: {exc}")
    if dividend_parts:
        merged_dividends = append_dividend_rows(
            existing_dividends,
            pd.concat(dividend_parts, ignore_index=True, sort=False),
        )
        # 确认注册表代码至少可被过滤，防止错误页面覆盖原分红缓存。
        if any(not dividends_for_symbol(merged_dividends, record.symbol).empty for record in records):
            save_dataset(
                DIVIDEND_CACHE_KEY,
                "年度ETF官方分红",
                CACHE_SOURCE,
                DIVIDEND_DATA_TYPE,
                merged_dividends,
                PERIOD,
            )
    if dividend_errors:
        print("分红抓取失败（已保留原缓存）：" + "；".join(dividend_errors))

    status = pd.DataFrame(status_by_symbol.values())
    atomic_write_csv(Path(args.status_path), status)
    print(status.to_string(index=False))
    return 1 if (status["status"] == "失败，保留原缓存").any() else 0


if __name__ == "__main__":
    raise SystemExit(main())
