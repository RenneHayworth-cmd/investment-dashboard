from __future__ import annotations

import argparse
import os
import signal
import time
from datetime import datetime

from core.db import finish_job, init_db, start_job
from core.paths import BACKGROUND_PID_PATH, ensure_dirs
from services.update_tasks import run_index_ma20_update


STOP_REQUESTED = False


def request_stop(signum, frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Investment Dashboard 后台更新任务")
    parser.add_argument("--once", action="store_true", help="只运行一次更新任务")
    parser.add_argument("--interval-minutes", type=int, default=60, help="循环更新间隔分钟数")
    parser.add_argument("--days", type=int, default=30, help="生成最近多少天的报表")
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


def run_once(api_key: str, days: int, force_refresh: bool = False) -> None:
    result = run_index_ma20_update(
        api_key=api_key,
        days=days,
        cache_source="auto",
        use_fresh_cache=not force_refresh,
    )
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {result.message}", flush=True)


def run_loop(api_key: str, days: int, interval_minutes: int, force_refresh: bool = False) -> None:
    interval_seconds = max(interval_minutes, 1) * 60
    running_pid = read_running_pid()
    if running_pid and running_pid != os.getpid():
        raise RuntimeError(f"后台更新调度器已在运行，PID={running_pid}")

    write_pid_file()
    scheduler_job_id = start_job("后台更新调度器")
    try:
        while not STOP_REQUESTED:
            run_once(api_key=api_key, days=days, force_refresh=force_refresh)
            for _ in range(interval_seconds):
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
    )


if __name__ == "__main__":
    main()
