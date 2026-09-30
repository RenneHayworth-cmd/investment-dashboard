"""Save today's formal closes for every microcap live-account stock after the close.

The 微盘实盘 page only fetches while it is the open Streamlit page, so a day on which
it is not opened after 15:05 used to wait for next-day daily bars. This runs the same
append-only loader (post-close snapshot first, daily bars otherwise) from a scheduled
task. It skips the network when the cache already covers today.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.db import finish_job, init_db, start_job
from services.market_calendar import get_market_window, is_market_trading_day
from services.microcap_live_market import load_microcap_histories, microcap_target_date
from services.microcap_live_trading import (
    list_microcap_cash_flows,
    list_microcap_position_adjustments,
    list_microcap_trades,
    microcap_first_event_date,
    microcap_ledger_codes,
)

TZ = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="收盘后保存微盘实盘持仓股票的当日正式收盘价。")
    parser.add_argument("--dry-run", action="store_true", help="只检查本地缓存，不联网。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(TZ)
    today = now.date().isoformat()
    if not is_market_trading_day(get_market_window("A股"), now):
        print(f"微盘实盘收盘价跳过：{today} 不是A股交易日。")
        return 0
    if microcap_target_date(now) != today:
        print(f"微盘实盘收盘价跳过：{now:%H:%M} 今日收盘尚未结算（15:05后执行）。")
        return 0

    trades = list_microcap_trades()
    flows = list_microcap_cash_flows()
    adjustments = list_microcap_position_adjustments()
    codes = microcap_ledger_codes(trades, adjustments)
    if not codes:
        print("微盘实盘收盘价跳过：账本中没有股票。")
        return 0
    first_date = microcap_first_event_date(trades, flows, adjustments)

    _, missing = load_microcap_histories(codes, start_date=first_date, allow_fetch=False, market_now=now)
    if not missing:
        print(f"微盘实盘收盘价跳过：本地已有{today}全部{len(codes)}只正式收盘价，无需联网。")
        return 0
    if args.dry_run:
        print(f"微盘实盘收盘价检查：{len(missing)}/{len(codes)}只缺{today}收盘价（dry-run未联网）。")
        return 0

    init_db()
    job_id = start_job("微盘实盘收盘价")
    _, failures = load_microcap_histories(
        codes, start_date=first_date, api_key=os.getenv("TICKFLOW_API_KEY", ""),
        allow_fetch=True, market_now=now,
    )
    if failures:
        detail = "；".join(f"{code}：{reason}" for code, reason in sorted(failures.items()))
        message = f"{today} 已补齐{len(codes) - len(failures)}/{len(codes)}只，仍缺{len(failures)}只：{detail}"
        finish_job(job_id, "failed", message)
        print(f"微盘实盘收盘价仍缺失。{message}")
        return 1
    message = f"{today} 已保存全部{len(codes)}只正式收盘价（本次补{len(missing)}只）。"
    finish_job(job_id, "success", message)
    print(f"微盘实盘收盘价更新成功。{message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
