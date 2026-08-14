#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.paths import DB_PATH  # noqa: E402
from services.fund_analysis import (  # noqa: E402
    FUND_ADJUST_FORWARD_ADDITIVE,
    FUND_ADJUST_NONE,
    build_fund_cache_symbol,
    infer_tickflow_symbol,
)
from services.position_analysis import (  # noqa: E402
    DEFAULT_ETF_CODES,
    ETF_AKSHARE_HISTORY_CODES,
    calculate_etf_timing_snapshot,
    load_or_fetch_etf,
    normalize_etf_base_code,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="预览或串行建立ETF新版复权缓存；默认不联网、不写入。"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行联网迁移并写入新版缓存；省略时只预览。",
    )
    parser.add_argument("--count", type=int, default=5000, help="每只标的请求的日线条数。")
    return parser.parse_args(argv)


def _read_runtime_metadata() -> tuple[set[tuple[str, str, str]], list[str]]:
    if not DB_PATH.exists():
        return set(), []
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    with closing(connection):
        try:
            datasets = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in connection.execute(
                    "SELECT symbol, source, period FROM datasets WHERE status='success'"
                )
            }
        except sqlite3.OperationalError:
            datasets = set()
        try:
            live_symbols = sorted(
                {
                    str(row[0]).strip()
                    for row in connection.execute(
                        "SELECT DISTINCT symbol FROM live_trades WHERE symbol IS NOT NULL"
                    )
                    if str(row[0]).strip()
                }
            )
        except sqlite3.OperationalError:
            live_symbols = []
    return datasets, live_symbols


def build_preview(count: int) -> list[dict[str, object]]:
    datasets, live_symbols = _read_runtime_metadata()
    rows: list[dict[str, object]] = []
    for code in DEFAULT_ETF_CODES:
        symbol = infer_tickflow_symbol(code)
        source = "akshare" if normalize_etf_base_code(code) in ETF_AKSHARE_HISTORY_CODES else "tickflow"
        cache_symbol = build_fund_cache_symbol(
            "fund_close", symbol, FUND_ADJUST_FORWARD_ADDITIVE
        )
        rows.append(
            {
                "用途": "持仓择时",
                "代码": code,
                "复权": FUND_ADJUST_FORWARD_ADDITIVE,
                "缓存键": cache_symbol,
                "现状": (
                    "已存在"
                    if (cache_symbol, source, f"{int(count)}_1d") in datasets
                    else "待建立"
                ),
            }
        )
    for code in live_symbols:
        symbol = infer_tickflow_symbol(code)
        source = "akshare" if normalize_etf_base_code(code) in ETF_AKSHARE_HISTORY_CODES else "tickflow"
        cache_symbol = build_fund_cache_symbol("fund_close", symbol, FUND_ADJUST_NONE)
        rows.append(
            {
                "用途": "实盘估值",
                "代码": code,
                "复权": FUND_ADJUST_NONE,
                "缓存键": cache_symbol,
                "现状": (
                    "已存在"
                    if (cache_symbol, source, f"{int(count)}_1d") in datasets
                    else "待建立"
                ),
            }
        )
    return rows


def validate_159545_acceptance(data: pd.DataFrame) -> list[str]:
    sample = data.copy()
    sample["date"] = pd.to_datetime(sample["date"], errors="coerce")
    sample = sample[sample["date"] <= pd.Timestamp("2026-08-12")].copy()
    if sample.empty or sample["date"].max() != pd.Timestamp("2026-08-12"):
        return ["159545固定验收失败：新版历史未覆盖2026-08-12。"]

    snapshot = calculate_etf_timing_snapshot(
        sample,
        ma_period=10,
        threshold_pct=1.0,
    )
    latest_return = float(sample.iloc[-1]["daily_return_pct"])
    checks = {
        "MA10": abs(float(snapshot.get("策略均线")) - 1.3069) <= 0.00005,
        "偏离率": abs(float(snapshot.get("策略偏离(%)")) - (-1.6757)) <= 0.0001,
        "复权日涨跌": abs(latest_return - (-0.6187)) <= 0.0001,
        "择时状态": snapshot.get("择时判断") == "空仓",
        "最近卖出日": snapshot.get("状态转换时间") == "2026-08-04",
    }
    failures = [name for name, passed in checks.items() if not passed]
    if failures:
        return [f"159545固定验收失败：{'、'.join(failures)}不符。"]
    return ["159545固定验收通过：MA10、偏离率、复权涨跌、空仓状态及卖出日均符合预期。"]


def apply_migration(rows: list[dict[str, object]], *, api_key: str, count: int) -> int:
    failures = 0
    validated_159545 = False
    for index, row in enumerate(rows, start=1):
        code = str(row["代码"])
        adjust = str(row["复权"])
        print(f"[{index}/{len(rows)}] {row['用途']} {code} {adjust} ...", flush=True)
        already_exists = str(row.get("现状")) == "已存在"
        item = None
        if already_exists:
            item = load_or_fetch_etf(
                code,
                api_key=api_key,
                count=int(count),
                adjust=adjust,
                allow_fetch=False,
                force_refresh=False,
                save_to_cache=False,
            )
            already_exists = bool(item.formal_history_valid)
        for attempt in range(1, 4):
            item = load_or_fetch_etf(
                code,
                api_key=api_key,
                count=int(count),
                adjust=adjust,
                allow_fetch=not already_exists,
                force_refresh=False,
                save_to_cache=True,
            )
            error_text = str(item.error or "")
            rate_limit = re.search(r"请求频率超限.*?请\s*(\d+)ms\s*后重试", error_text)
            if item.status != "失败" or rate_limit is None or attempt >= 3:
                break
            wait_seconds = max(float(rate_limit.group(1)) / 1000 + 1.0, 6.2)
            print(f"  触发限频，等待{wait_seconds:.1f}秒后重试。", flush=True)
            time.sleep(wait_seconds)
        assert item is not None
        if item.status == "失败" or not item.formal_history_valid:
            failures += 1
            print(f"  失败：{item.error or item.status}")
            continue
        print(f"  完成：{item.status}，{len(item.dataframe)}条，最新{item.latest_date}")
        if code == "159545" and adjust == FUND_ADJUST_FORWARD_ADDITIVE:
            validated_159545 = True
            for message in validate_159545_acceptance(item.dataframe):
                print(f"  {message}")
                if "失败" in message:
                    failures += 1
        if not already_exists and index < len(rows):
            time.sleep(6.2)
    if not validated_159545:
        failures += 1
        print("159545固定验收未执行。")
    return failures


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = build_preview(args.count)
    preview = pd.DataFrame(rows)
    print(preview.to_string(index=False) if not preview.empty else "没有待迁移标的。")
    if not args.apply:
        print("\n当前为只读预览；传入 --apply 才会联网并写入新版缓存。")
        return 0

    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        print("未设置TICKFLOW_API_KEY，迁移未执行。", file=sys.stderr)
        return 2
    failures = apply_migration(rows, api_key=api_key, count=args.count)
    if failures:
        print(f"迁移完成，但有{failures}项失败。", file=sys.stderr)
        return 1
    print("迁移及159545固定验收全部完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
