"""Evidence profiling only: source rows are never promoted to PIT facts here."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def profile_share_events(frame: pd.DataFrame) -> dict:
    required = {"证券代码", "公告日期", "变动日期", "总股本", "变动原因"}
    missing = sorted(required - set(frame.columns))
    report = {"classification": "Unknown", "pit_approved": False,
              "rows": len(frame), "missing_columns": missing}
    if missing:
        return report
    announced = pd.to_datetime(frame["公告日期"], errors="coerce")
    changed = pd.to_datetime(frame["变动日期"], errors="coerce")
    shares = pd.to_numeric(frame["总股本"], errors="coerce")
    report.update({
        "report_period_rows": int(frame["变动原因"].astype(str).str.contains("定期报告", regex=False).sum()),
        "published_after_change_rows": int((announced > changed).sum()),
        "invalid_dates": int((announced.isna() | changed.isna()).sum()),
        "invalid_shares": int((shares.isna() | shares.le(0)).sum()),
        "duplicate_event_keys": int(frame.duplicated(["证券代码", "变动日期", "公告日期"]).sum()),
        "blockers": ["单位尚未独立核验", "变动日期尚未证明为实际生效日",
                     "公告日期没有日内发布时间", "历史事件覆盖完整性尚未证明",
                     "定期报告不得直接作为生效事件", "需与原始公告及独立快照验证"],
    })
    return report


def save_source_evidence(directory: Path, name: str, frame: pd.DataFrame,
                         source_url: str, request: dict, adapter_version: str) -> dict:
    """Exclusive files in a dedicated directory; caller owns directory creation.

    CSV is adapter output, explicitly not an original HTTP response. Its capture
    timestamp is retrieval time, never historical information_available_at.
    """
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in name):
        raise ValueError("证据文件名必须为小写字母、数字或下划线")
    payload = frame.to_csv(index=False).encode("utf-8")
    csv = directory / (name + ".csv")
    meta = directory / (name + ".json")
    if csv.exists() or meta.exists():
        raise FileExistsError(name)
    with csv.open("xb") as stream:
        stream.write(payload)
    metadata = {
        "source_url": source_url, "request": request,
        "adapter_version": adapter_version, "artifact_kind": "adapter_output",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "information_available_at": None,
        "classification": "Unknown", "pit_approved": False,
        "sha256": hashlib.sha256(payload).hexdigest(), "rows": len(frame),
        "columns": frame.columns.tolist(),
    }
    with meta.open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
    return metadata
