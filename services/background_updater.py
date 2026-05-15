from __future__ import annotations

import argparse
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import time as datetime_time
from zoneinfo import ZoneInfo

from core.db import finish_job, init_db, start_job
from core.paths import BACKGROUND_PID_PATH, ensure_dirs
from services.update_tasks import run_index_ma20_update


STOP_REQUESTED = False


@dataclass(frozen=True)
class MarketWindow:
    name: str
    timezone: str
    sessions: tuple[tuple[datetime_time, datetime_time], ...]


MARKET_WINDOWS = (
    MarketWindow(
        name="A股",
        timezone="Asia/Shanghai",
        sessions=(
            (datetime_time(9, 30), datetime_time(11, 30)),
            (datetime_time(13, 0), datetime_time(15, 0)),
        ),
    ),
    MarketWindow(
        name="港股",
        timezone="Asia/Hong_Kong",
        sessions=(
            (datetime_time(9, 30), datetime_time(12, 0)),
            (datetime_time(13, 0), datetime_time(16, 0)),
        ),
    ),
    MarketWindow(
        name="日本",
        timezone="Asia/Tokyo",
        sessions=(
            (datetime_time(9, 0), datetime_time(11, 30)),
            (datetime_time(12, 30), datetime_time(15, 30)),
        ),
    ),
    MarketWindow(
        name="韩国",
        timezone="Asia/Seoul",
        sessions=((datetime_time(9, 0), datetime_time(15, 30)),),
    ),
    MarketWindow(
        name="美股",
        timezone="America/New_York",
        sessions=((datetime_time(9, 30), datetime_time(16, 0)),),
    ),
)


def describe_update_windows() -> str:
    return (
        "按主要市场交易时段刷新：A股 09:30-11:30/13:00-15:00，"
        "港股 09:30-12:00/13:00-16:00，日韩约北京时间 08:00-14:30，"
        "美股按美东 09:30-16:00 自动适配夏令时；各市场收盘 5 分钟后补刷一次。"
    )


def request_stop(signum, frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Investment Dashboard 后台更新任务")
    parser.add_argument("--once", action="store_true", help="只运行一次更新任务")
    parser.add_argument("--interval-minutes", type=int, default=60, help="循环更新间隔分钟数")
    parser.add_argument("--days", type=int, default=30, help="生成最近多少天的报表")
    parser.add_argument("--max-workers", type=int, default=4, help="指数并发获取数量")
    parser.add_argument("--api-key", default=os.getenv("TICKFLOW_API_KEY", ""), help="TickFlow API Key")
    parser.add_argument("--force-refresh", action="store_true", help="忽略今日缓存并重新拉取数据")
    return parser.parse_args()


def read_running_pid() -> int | None:
    if not BACKGROUND_PID_PATH.exists():
        return None

    try:
        pid = int(BACKGROUND_PID_PATH.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        BACKGROUND_PID_PATH.unlink(missing_ok=True)
        return None


def write_pid_file() -> None:
    ensure_dirs()
    BACKGROUND_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def remove_pid_file() -> None:
    BACKGROUND_PID_PATH.unlink(missing_ok=True)


def run_once(
    api_key: str,
    days: int,
    force_refresh: bool = False,
    market_names: set[str] | None = None,
    max_workers: int = 4,
) -> object:
    result = run_index_ma20_update(
        api_key=api_key,
        days=days,
        cache_source="auto",
        use_fresh_cache=False if market_names else not force_refresh,
        market_names=market_names,
        max_workers=max_workers,
    )
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {result.message}", flush=True)
    return result


def normalize_datetime(now: datetime | None = None) -> datetime:
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return current


def get_active_market_names(now: datetime | None = None) -> list[str]:
    current = normalize_datetime(now)
    active_markets: list[str] = []
    for market in MARKET_WINDOWS:
        market_now = current.astimezone(ZoneInfo(market.timezone))
        if market_now.weekday() >= 5:
            continue
        current_time = market_now.time()
        if any(start <= current_time <= end for start, end in market.sessions):
            active_markets.append(market.name)
    return active_markets


def is_update_window(now: datetime | None = None) -> bool:
    return bool(get_active_market_names(now))


def get_market_close_due_names(
    now: datetime | None = None,
    closed_run_dates: dict[str, str] | None = None,
    close_delay_minutes: int = 5,
    close_window_minutes: int = 20,
) -> list[str]:
    current = normalize_datetime(now)
    completed_dates = closed_run_dates or {}
    due_markets: list[str] = []
    for market in MARKET_WINDOWS:
        market_now = current.astimezone(ZoneInfo(market.timezone))
        if market_now.weekday() >= 5:
            continue

        close_time = market.sessions[-1][1]
        close_at = market_now.replace(
            hour=close_time.hour,
            minute=close_time.minute,
            second=0,
            microsecond=0,
        )
        due_start = close_at + timedelta(minutes=close_delay_minutes)
        due_end = due_start + timedelta(minutes=close_window_minutes)
        market_date = market_now.date().isoformat()
        if due_start <= market_now <= due_end and completed_dates.get(market.name) != market_date:
            due_markets.append(market.name)
    return due_markets


def get_due_market_names(
    now: datetime | None,
    last_active_runs: dict[str, datetime],
    closed_run_dates: dict[str, str],
    interval_minutes: int,
) -> tuple[set[str], list[str], list[str]]:
    current = normalize_datetime(now)
    interval_seconds = max(interval_minutes, 1) * 60
    active_due: list[str] = []
    for market_name in get_active_market_names(current):
        last_run = last_active_runs.get(market_name)
        if last_run is None or (current - last_run).total_seconds() >= interval_seconds:
            active_due.append(market_name)

    close_due = get_market_close_due_names(current, closed_run_dates)
    return set(active_due) | set(close_due), active_due, close_due


def run_loop(
    api_key: str,
    days: int,
    interval_minutes: int,
    force_refresh: bool = False,
    max_workers: int = 4,
) -> None:
    running_pid = read_running_pid()
    if running_pid and running_pid != os.getpid():
        raise RuntimeError(f"后台更新调度器已在运行，PID={running_pid}")

    write_pid_file()
    scheduler_job_id = start_job("后台更新调度器")
    last_active_runs: dict[str, datetime] = {}
    closed_run_dates: dict[str, str] = {}
    try:
        while not STOP_REQUESTED:
            current = normalize_datetime()
            due_markets, active_due, close_due = get_due_market_names(
                current,
                last_active_runs,
                closed_run_dates,
                interval_minutes,
            )
            if due_markets:
                reasons = []
                if active_due:
                    reasons.append(f"交易中：{'、'.join(active_due)}")
                if close_due:
                    reasons.append(f"收盘补刷：{'、'.join(close_due)}")
                print(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    f"{'；'.join(reasons)}，开始刷新。",
                    flush=True,
                )
                result = run_once(
                    api_key=api_key,
                    days=days,
                    force_refresh=force_refresh,
                    market_names=due_markets,
                    max_workers=max_workers,
                )
                if result.status == "success":
                    for market_name in active_due:
                        last_active_runs[market_name] = current
                    for market_name in close_due:
                        market = next(item for item in MARKET_WINDOWS if item.name == market_name)
                        closed_run_dates[market_name] = current.astimezone(ZoneInfo(market.timezone)).date().isoformat()
            else:
                print(
                    f"[{datetime.now().isoformat(timespec='seconds')}] "
                    f"没有到达市场刷新时点，跳过本轮更新（{describe_update_windows()}）",
                    flush=True,
                )
            for _ in range(60):
                if STOP_REQUESTED:
                    break
                time.sleep(1)
        finish_job(scheduler_job_id, "success", "后台更新调度器已停止")
    except Exception as exc:
        finish_job(scheduler_job_id, "failed", str(exc))
        raise
    finally:
        remove_pid_file()


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    init_db()
    args = parse_args()
    if args.once:
        run_once(api_key=args.api_key, days=args.days, force_refresh=args.force_refresh)
        return

    run_loop(
        api_key=args.api_key,
        days=args.days,
        interval_minutes=args.interval_minutes,
        force_refresh=args.force_refresh,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    main()
