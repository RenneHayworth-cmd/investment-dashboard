"""Read-only provenance checks. These checks never certify a PIT universe.

Observed-file location is not proof of observed provenance. In particular an
exact match to a legacy reconstruction must be resolved before validation use.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

OBSERVED = "BK1158_observed"
RECONSTRUCTED = "microcap_rule_reconstructed"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_snapshot_provenance(
    snapshots: pd.DataFrame, legacy: pd.DataFrame | None = None
) -> list[dict]:
    """Compare complete daily member/price/capitalization records, without edits.

    An unmatched day is Unknown as well: absence of a legacy match does not
    prove acquisition origin. Existing raw files have no per-row source proof.
    """
    result = []
    compare = ["代码", "最新价", "总市值(亿元)"]
    for day, rows in snapshots.groupby("快照日期", sort=True):
        members = rows["代码"].astype(str).str.zfill(6)
        duplicate_count = int(members.duplicated().sum())
        exact_match = False
        legacy_rows = None if legacy is None else legacy[legacy["快照日期"].eq(day)]
        if legacy_rows is not None and not legacy_rows.empty:
            def canonical(frame):
                frame = frame[compare].copy()
                frame["代码"] = frame["代码"].astype(str).str.zfill(6)
                for column in compare[1:]:
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
                return frame.sort_values(compare).reset_index(drop=True)
            left, right = canonical(rows), canonical(legacy_rows)
            exact_match = (
                len(left) == len(right)
                and not left.isna().any().any()
                and not right.isna().any().any()
                and left.eq(right).all().all()
            )
        result.append({
            "selection_date": str(day),
            "rows": len(rows),
            "unique_securities": int(members.nunique()),
            "duplicate_securities": duplicate_count,
            "legacy_exact_match": bool(exact_match),
            "provenance": "Unknown",
            "validation_eligible": False,
            "reason": ("与旧重建逐条相同，须核验来源" if exact_match
                       else "尚未取得独立采集来源证据"),
        })
    return result


def inspect_snapshot_files(snapshot_path: Path, legacy_path: Path | None = None) -> dict:
    """Read CSVs directly, avoiding cache/SQLite initialization and writes."""
    paths = [snapshot_path] + ([legacy_path] if legacy_path is not None else [])
    before = {str(path): file_sha256(path) for path in paths}
    snapshots = pd.read_csv(snapshot_path, dtype={"代码": str})
    legacy = pd.read_csv(legacy_path, dtype={"代码": str}) if legacy_path else None
    days = audit_snapshot_provenance(snapshots, legacy)
    after = {str(path): file_sha256(path) for path in paths}
    if before != after:
        raise RuntimeError("审计期间输入文件发生变化，请重新运行审计。")
    return {
        "schema_version": 1,
        "requested_dataset": OBSERVED,
        "classification": "Unknown",
        "input_sha256": before,
        "days": days,
        "summary": {
            "snapshot_days": len(days),
            "legacy_matching_days": sum(row["legacy_exact_match"] for row in days),
            "independently_verified_days": 0,
        },
        "gate_1a": {"status": "Unknown", "reason": "尚无完整PIT全市场、有效股本及事件验证证据"},
        "gate_1b": {"status": "Unknown", "reason": "来源与effective-date alignment尚未验证"},
        "historical_expansion_allowed": False,
    }
