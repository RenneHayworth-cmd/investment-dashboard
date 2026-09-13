#!/usr/bin/env python3
"""Bounded source feasibility sampling. Does not build a stock universe/rank."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import random
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
from services.microcap_source_evidence import profile_share_events, save_source_evidence
from services.microcap_research_audit import file_sha256


def main():
    parser = argparse.ArgumentParser(description="PIT来源可行性抽样，仅生成独立证据，不计算排名")
    parser.add_argument('--snapshots', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help="必须是不存在的新目录")
    parser.add_argument('--sample-size', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.sample_size <= 30:
        parser.error('单次探针限定1至30只')
    source_hash = file_sha256(args.snapshots)
    df = pd.read_csv(args.snapshots, dtype={'代码': str})
    codes = sorted(df['代码'].dropna().unique().tolist())
    samples = random.Random(20260908).sample(codes, min(args.sample_size, len(codes)))
    args.output.mkdir(parents=True, exist_ok=False)
    import akshare as ak
    def timeout(*_):
        raise TimeoutError('source probe exceeded 20 seconds')
    signal.signal(signal.SIGALRM, timeout)
    records = []
    jobs = [('sse_delisted', ak.stock_info_sh_delist, {},
             'https://www.sse.com.cn/assortment/stock/list/delisting/'),
            ('szse_delisted', ak.stock_info_sz_delist, {'symbol':'终止上市公司'},
             'https://www.szse.cn/market/stock/suspend/index.html')]
    for code in samples:
        jobs.append(('shares_' + code, ak.stock_share_change_cninfo,
                     {'symbol':code, 'start_date':'20250101', 'end_date':'20260908'},
                     'https://webapi.cninfo.com.cn/api/stock/p_stock2215'))
    for name, fetch, request, url in jobs:
        signal.alarm(20)
        try:
            frame = fetch(**request)
            metadata = save_source_evidence(args.output, name, frame, url, request,
                                            importlib.metadata.version('akshare'))
            profile = profile_share_events(frame) if name.startswith('shares_') else {}
            records.append({'name':name, 'metadata':metadata, 'profile':profile})
            print(name, 'rows=',len(frame), flush=True)
        except Exception as error:
            # Do not put headers/credentials or verbose request objects in logs.
            records.append({'name':name, 'classification':'Unknown', 'error_type':type(error).__name__})
            print(name, type(error).__name__, flush=True)
        finally:
            signal.alarm(0)
    report = {'purpose':'source_feasibility_only', 'sample_seed':20260908,
              'sample_codes':samples, 'sample_is_pit_universe':False,
              'share_validation_passed':False, 'snapshot_sha256':source_hash,
              'snapshot_unchanged':file_sha256(args.snapshots) == source_hash,
              'records':records}
    with (args.output/'probe_report.json').open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)


if __name__ == '__main__':
    main()
