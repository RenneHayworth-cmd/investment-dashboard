"""Formal-close attempt records; never store fetch arguments or credentials."""
from contextlib import closing

import pandas as pd

from core.db import get_conn, start_job, finish_job


def fetch_audited_close(code, *, fetcher, target_date, **kwargs):
    job_id = start_job(f"ETF正式收盘 {code} {target_date}")
    try:
        item = fetcher(code, **kwargs)
    except Exception:
        finish_job(job_id, "failed", "数据源请求失败；保留原缓存")
        raise
    stamp = pd.to_datetime(item.latest_date, errors="coerce")
    complete = bool(item.formal_history_valid and pd.notna(stamp) and stamp.date() >= target_date)
    finish_job(job_id, "success" if complete else "stale", f"最新正式日期：{item.latest_date or '无'}")
    return item


def recent_close_attempts(limit=100):
    with closing(get_conn()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
            return pd.DataFrame()
        return pd.read_sql_query(
            "SELECT job_name AS 标的及目标日期, status AS 状态, "
            "replace(started_at, 'T', ' ') AS 开始时间, "
            "replace(finished_at, 'T', ' ') AS 完成时间, message AS 结果 "
            "FROM jobs WHERE job_name LIKE 'ETF正式收盘 %' ORDER BY id DESC LIMIT ?",
            conn, params=(max(1, min(int(limit), 1000)),),
        )
