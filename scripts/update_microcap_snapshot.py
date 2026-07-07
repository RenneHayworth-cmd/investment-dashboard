from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.cache import save_dataset
from core.db import finish_job, init_db, start_job
from services.microcap import fetch_microcap_stocks, save_microcap_constituent_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="获取 BK1158 微盘股成分并保存真实成分快照。")
    parser.add_argument("--page-size", type=int, default=400, help="拉取成分股数量，默认 400。")
    parser.add_argument("--pool-count", type=int, default=400, help="保存成分快照数量，默认 400。")
    parser.add_argument("--retries", type=int, default=3, help="东方财富请求重试次数，默认 3。")
    parser.add_argument("--require-today", action="store_true", help="仅当行情日期为今天时保存快照。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    job_id = None
    init_db()

    try:
        job_id = start_job("微盘股真实成分快照")
        stocks_df = fetch_microcap_stocks(page_size=args.page_size, retries=args.retries)
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        if args.require_today and "日期" in stocks_df.columns:
            market_dates = pd.to_datetime(stocks_df["日期"], errors="coerce").dropna()
            latest_market_date = market_dates.max().strftime("%Y-%m-%d") if not market_dates.empty else ""
            if latest_market_date != today:
                display_market_date = latest_market_date or "-"
                message = (
                    f"开始时间：{started_at}；行情日期：{display_market_date}；"
                    f"今天：{today}；数据未更新到今天，已跳过保存。"
                )
                finish_job(job_id, "success", message)
                print(f"微盘股真实成分快照跳过。{message}")
                return 0
        save_dataset(
            symbol="microcap_bk1158",
            name="BK1158 微盘股成分",
            source="eastmoney",
            data_type="microcap",
            df=stocks_df,
        )
        snapshot_df, snapshot_date = save_microcap_constituent_snapshot(stocks_df, pool_count=args.pool_count)
        message = (
            f"开始时间：{started_at}；快照日期：{snapshot_date}；"
            f"本次成分：{len(stocks_df)} 行；累计快照：{len(snapshot_df)} 行。"
        )
        finish_job(job_id, "success", message)
        print(f"微盘股真实成分快照更新成功。{message}")
        return 0
    except Exception as exc:
        message = f"开始时间：{started_at}；失败原因：{exc}"
        if job_id is not None:
            finish_job(job_id, "failed", message)
        print(f"微盘股真实成分快照更新失败。{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())