#!/usr/bin/env python3
"""Print a read-only source audit; optionally save to a NEW report file."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.microcap_research_audit import inspect_snapshot_files


def main():
    parser = argparse.ArgumentParser(description="微盘研究输入来源审计，不写正式数据")
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--legacy", type=Path)
    parser.add_argument("--report", type=Path, help="可选：新建JSON报告，已有文件拒绝覆盖")
    args = parser.parse_args()
    report = inspect_snapshot_files(args.snapshots, args.legacy)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        # Exclusive creation prevents accidental overwrite of an input or report.
        with args.report.open("x", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    print(payload)


if __name__ == "__main__":
    main()
