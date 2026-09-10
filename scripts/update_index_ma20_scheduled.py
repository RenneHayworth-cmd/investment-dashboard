from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.db import init_db  # noqa: E402
from services.index_ma20 import INDEX_CONFIG, INDEX_REPORT_DISPLAY_DAYS  # noqa: E402
from services.index_realtime import find_pending_post_close_index_names  # noqa: E402
from services.update_tasks import (  # noqa: E402
    find_pending_futures_current_contract_index_names,
    run_index_ma20_update,
)


MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
LOCK_PATH = PROJECT_ROOT / "output" / "locks" / "index_ma20_scheduled.lock"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="补齐指数最新已完成交易日的正式日线，并重算指数卡片与MA20汇总缓存。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取本地缓存并列出待更新指数，不联网、不写缓存。",
    )
    return parser.parse_args()


@contextmanager
def single_instance_lock(lock_path: Path = LOCK_PATH):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ordered_index_names(index_names: set[str]) -> list[str]:
    return [index_name for index_name in INDEX_CONFIG if index_name in index_names]


def run_scheduled_update(
    *,
    market_now: datetime | None = None,
    dry_run: bool = False,
    api_key: str | None = None,
) -> tuple[int, str]:
    now = market_now.astimezone(MARKET_TIMEZONE) if market_now else datetime.now(MARKET_TIMEZONE)
    resolved_api_key = os.getenv("TICKFLOW_API_KEY", "") if api_key is None else api_key
    credential_note = (
        "TickFlow密钥已加载"
        if resolved_api_key.strip()
        else "未加载TickFlow密钥，将使用免费/公开数据源"
    )
    pending_formal = find_pending_post_close_index_names(now=now)
    pending_contracts = find_pending_futures_current_contract_index_names(market_now=now)
    pending = pending_formal | pending_contracts
    ordered_pending = ordered_index_names(pending)
    time_text = now.strftime("%Y-%m-%d %H:%M:%S")

    if not ordered_pending:
        return 0, (
            f"{time_text} 检查完成：没有缺失的已完成交易日正式数据，"
            f"无需联网；{credential_note}。"
        )

    names_text = "、".join(ordered_pending)
    if dry_run:
        return 0, (
            f"{time_text} 试运行：将更新 {len(ordered_pending)} 个指数："
            f"{names_text}；{credential_note}。"
        )

    remaining = set(pending)
    errors = []
    attempts = 0
    for delay in (0, 30, 60):
        if delay:
            print(f"正式数据仍缺失：{'、'.join(ordered_index_names(remaining))}；{delay}秒后复核并重试。", flush=True)
            time.sleep(delay)
            # 等待期间手动更新可能已补齐，先复核再决定是否联网。
            remaining = find_pending_post_close_index_names(now=now, index_names=remaining) | find_pending_futures_current_contract_index_names(
                market_now=now, index_names=remaining,
            )
            if not remaining:
                break
        attempts += 1
        result = run_index_ma20_update(
            api_key=resolved_api_key,
            days=INDEX_REPORT_DISPLAY_DAYS,
            cache_source="auto",
            use_fresh_cache=False,
            index_names=remaining,
            max_workers=4 if attempts == 1 else 1,
        )
        errors.extend(result.errors or [])
        if result.status != "success":
            errors.append(result.message)
        remaining = find_pending_post_close_index_names(now=now, index_names=remaining) | find_pending_futures_current_contract_index_names(
            market_now=now, index_names=remaining,
        )
        if not remaining:
            break
    updated = pending - remaining
    parts = [
        f"{time_text} 自动更新{'仍有缺失' if remaining else '完成'}（共{attempts}轮请求）",
        f"已补齐 {len(updated)}/{len(pending)} 个指数",
    ]
    if updated:
        parts.append("已更新：" + "、".join(ordered_index_names(updated)))
    if remaining:
        parts.append("仍待补齐：" + "、".join(ordered_index_names(remaining)))
    if errors:
        parts.append("数据源提示：" + " | ".join(dict.fromkeys(errors)))
    parts.append(credential_note)
    return (2 if remaining else 0), "；".join(parts) + "。"


def main() -> int:
    args = parse_args()
    init_db()
    with single_instance_lock() as acquired:
        if not acquired:
            print("跳过：上一轮指数正式数据更新尚未完成。")
            return 0
        exit_code, message = run_scheduled_update(dry_run=args.dry_run)
    print(message, file=sys.stderr if exit_code else sys.stdout)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
