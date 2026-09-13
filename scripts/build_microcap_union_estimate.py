"""User-authorized retrospective fixed-union estimate; never writes observed data.

Optional research dependency: .venv/bin/python -m pip install baostock==0.9.3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATASET = "microcap_fixed_union_estimate"
FIELDS = "date,code,open,close,volume,amount,adjustflag,tradestatus,isST"


SUPPLEMENTAL_ST = {
    '002883': '中设股份', '002193': '如意集团', '002719': '麦趣尔',
    '600543': '莫高股份', '002856': '美芝股份', '603729': '龙韵股份',
}


def add_supplemental_candidates(anchors):
    result = anchors.copy()
    result['candidate_source'] = '近期快照并集'
    result['share_method'] = '快照隐含固定股本'
    extra = [{'代码': code, '名称': name, 'share_anchor_date': None,
              'estimated_shares': float('nan'), 'candidate_source': '用户指定历史ST补充',
              'share_method': '2025三季报固定股本估算'}
             for code, name in SUPPLEMENTAL_ST.items() if code not in set(result['代码'])]
    return pd.concat([result, pd.DataFrame(extra)], ignore_index=True) if extra else result


def candidates(snapshots):
    frame = snapshots.copy()
    frame = frame[frame['快照日期'].between('2026-06-22', '2026-09-08')]
    frame['代码'] = frame['代码'].astype(str).str.zfill(6)
    for col in ['最新价', '总市值(亿元)']:
        frame[col] = pd.to_numeric(frame[col], errors='coerce')
    valid = frame[frame['最新价'].gt(0) & frame['总市值(亿元)'].gt(0)]
    anchors = valid.sort_values('快照日期', kind='stable').drop_duplicates('代码').copy()
    anchors['estimated_shares'] = anchors['总市值(亿元)'] * 1e8 / anchors['最新价']
    universe = frame.sort_values('快照日期').drop_duplicates('代码')[['代码', '名称']]
    return universe.merge(anchors[['代码', '快照日期', 'estimated_shares']],
        on='代码', how='left', validate='one_to_one').rename(
        columns={'快照日期': 'share_anchor_date'})


def rank_estimates(bars, anchors, sessions):
    frame = bars.copy()
    frame['代码'] = frame['code'].str.split('.').str[-1]
    for col in ['close', 'volume', 'amount', 'tradestatus', 'isST', 'adjustflag']:
        frame[col] = pd.to_numeric(frame[col], errors='coerce')
    if frame.duplicated(['date', '代码']).any():
        raise ValueError('重复证券日期')
    frame = frame.merge(anchors, on='代码', how='left', validate='many_to_one')
    frame['estimated_market_cap_yuan'] = frame['close'] * frame['estimated_shares']
    frame['raw_market_cap_rank'] = pd.Series(pd.NA, index=frame.index, dtype='Int64')
    price_ok = frame['close'].gt(0) & frame['adjustflag'].eq(3) & frame['estimated_shares'].gt(0)
    ordered = frame[price_ok].sort_values(['date', 'estimated_market_cap_yuan', '代码'])
    frame.loc[ordered.index, 'raw_market_cap_rank'] = ordered.groupby('date').cumcount().add(1).values
    frame['exclusion_reason'] = ''
    frame.loc[~price_ok, 'exclusion_reason'] = '价格或股本锚点缺失'
    frame.loc[~frame['isST'].isin([0, 1]) | ~frame['tradestatus'].isin([0, 1]), 'exclusion_reason'] = '交易资格未知'
    frame.loc[~frame['volume'].gt(0), 'exclusion_reason'] = '无有效成交量'
    frame.loc[frame['tradestatus'].eq(0), 'exclusion_reason'] = '停牌'
    frame.loc[frame['isST'].eq(1), 'exclusion_reason'] = 'ST/*ST'
    active = price_ok & frame['isST'].eq(0) & frame['tradestatus'].eq(1) & frame['volume'].gt(0)
    frame['strategy_eligible'] = active
    frame['strategy_rank'] = pd.Series(pd.NA, index=frame.index, dtype='Int64')
    ordered = frame[active].sort_values(['date', 'estimated_market_cap_yuan', '代码'])
    frame.loc[ordered.index, 'strategy_rank'] = ordered.groupby('date').cumcount().add(1).values
    frame['selection_date'] = frame['date']
    next_days = dict(zip(sessions, sessions[1:]))
    frame['effective_date'] = frame['date'].map(next_days)
    frame['dataset'] = DATASET
    frame['market_data_asof'] = 'EOD'
    frame['information_available_at'] = None
    frame['quality'] = '固定候选池及固定股本估算，非PIT'
    top = frame[frame['strategy_rank'].le(20).fillna(False)].sort_values(['date', 'strategy_rank'])
    summaries = []
    for day in sessions:
        g = frame[frame['date'].eq(day)]
        t = top[top['date'].eq(day)]
        summaries.append({'日期': day, '候选池数': len(anchors), '日线返回数': len(g),
            '无日线数': len(anchors) - len(g), 'ST剔除数': int(g['isST'].eq(1).sum()),
            '停牌标记数': int(g['tradestatus'].eq(0).sum()),
            '有效股票数': int(g['strategy_eligible'].sum()), 'Top20数量': len(t),
            '微盘20估算均值(亿元)': t['estimated_market_cap_yuan'].mean()/1e8 if len(t)==20 else None,
            '口径': '固定候选池历史估算，非真实BK1158',
            '数据状态': '估算可用' if len(t)==20 else '不足20只'})
    return frame, top, pd.DataFrame(summaries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', default='2026-01-01')
    parser.add_argument('--end', default='2026-09-08')
    parser.add_argument('--reuse-bars', type=Path)
    args = parser.parse_args()
    import baostock as bs
    socket.setdefaulttimeout(30)
    source = ROOT/'data/raw/eastmoney/microcap_bk1158_constituent_snapshots_1d.csv'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    anchors = add_supplemental_candidates(candidates(pd.read_csv(source, dtype={'代码': str})))
    request = {'dataset': DATASET, 'start': args.start, 'end': args.end,
               'snapshot_sha256': digest, 'fields': FIELDS, 'adjustflag': '3',
               'supplemental_st': SUPPLEMENTAL_ST, 'schema_version': 2}
    out = args.output.resolve()
    if not out.is_relative_to(ROOT/'output'):
        raise ValueError('研究产物必须位于output独立目录')
    out.mkdir(parents=True, exist_ok=True)
    manifest = out/'request.json'
    if manifest.exists():
        if json.loads(manifest.read_text()) != request:
            raise ValueError('已有目录参数不同，拒绝覆盖')
    else:
        manifest.write_text(json.dumps(request, ensure_ascii=False, indent=2))
    raw = out/'bars'; raw.mkdir(exist_ok=True)
    if args.reuse_bars:
        import shutil
        previous = json.loads((args.reuse_bars/'request.json').read_text())
        for key in ['start', 'end', 'fields', 'adjustflag']:
            if previous[key] != request[key]:
                raise ValueError(f'旧日线请求口径不匹配：{key}')
        for code in anchors['代码']:
            old = args.reuse_bars/'bars'/f'{code}.csv'
            if old.exists() and not (raw/old.name).exists():
                shutil.copyfile(old, raw/old.name)
    anchors.to_csv(out/'candidate_anchors.csv', index=False)
    login = bs.login()
    if login.error_code != '0':
        raise RuntimeError(login.error_msg)
    failures = []
    frames = []
    try:
        for idx in anchors.index[anchors['candidate_source'].eq('用户指定历史ST补充')]:
            code = anchors.loc[idx, '代码']
            symbol = ('sh.' if code.startswith('6') else 'sz.') + code
            evidence_file = out/f'profit_2025q3_{code}.csv'
            if evidence_file.exists():
                profit = pd.read_csv(evidence_file, dtype=str)
            else:
                response = bs.query_profit_data(code=symbol, year=2025, quarter=3)
                if response.error_code != '0':
                    raise RuntimeError(f'{code}历史股本查询失败：{response.error_msg}')
                profit = response.get_data()
                profit.to_csv(evidence_file, index=False)
            if profit.empty or not profit['code'].eq(symbol).all():
                raise ValueError(f'{code}历史股本无有效记录')
            share_row = profit.iloc[-1]
            shares = float(share_row['totalShare'])
            if not 0 < shares < float('inf'):
                raise ValueError(f'{code}历史股本无效')
            anchors.loc[idx, 'estimated_shares'] = shares
            anchors.loc[idx, 'share_anchor_date'] = share_row['statDate']
            anchors.loc[idx, 'share_publication_date'] = share_row['pubDate']
        calendar = bs.query_trade_dates(start_date=args.start, end_date='2026-09-15')
        if calendar.error_code != '0':
            raise RuntimeError(calendar.error_msg)
        cal = calendar.get_data()
        sessions = cal.loc[cal['is_trading_day'].eq('1'), 'calendar_date'].tolist()
        cal.to_csv(out/'calendar.csv', index=False)
        for i, code in enumerate(anchors['代码'], 1):
            p = raw/f'{code}.csv'
            try:
                if p.exists():
                    d = pd.read_csv(p, dtype=str)
                else:
                    symbol = ('sh.' if code.startswith('6') else 'sz.') + code
                    result = bs.query_history_k_data_plus(symbol, FIELDS,
                        start_date=args.start, end_date=args.end, frequency='d', adjustflag='3')
                    if result.error_code != '0':
                        raise RuntimeError(result.error_msg)
                    d = result.get_data()
                    if d.empty:
                        raise ValueError('来源无日线')
                    if not d['code'].eq(symbol).all():
                        raise ValueError('来源代码不匹配')
                    d.to_csv(p, index=False)
                frames.append(d)
            except Exception as exc:
                failures.append({'代码': code, '错误': str(exc)})
            if i%25==0 or i==len(anchors):
                print(f'日线进度 {i}/{len(anchors)}，失败 {len(failures)}', flush=True)
    finally:
        bs.logout()
    if not frames:
        raise RuntimeError('无可用日线')
    bars = pd.concat(frames, ignore_index=True)
    bars = bars[bars['date'].between(args.start, args.end)]
    # A legacy snapshot may omit price while retaining capitalization.
    # Recover that anchor using the same day's raw close, never today's shares.
    snapshots = pd.read_csv(source, dtype={'代码': str})
    for idx in anchors.index[anchors['estimated_shares'].isna()]:
        code = anchors.loc[idx, '代码']
        observed = snapshots[snapshots['代码'].eq(code)].sort_values('快照日期')
        for _, row in observed.iterrows():
            match = bars[bars['code'].eq('sh.'+code) & bars['date'].eq(row['快照日期'])]
            if not match.empty and float(match.iloc[0]['close']) > 0 and row['总市值(亿元)'] > 0:
                anchors.loc[idx, 'estimated_shares'] = row['总市值(亿元)']*1e8/float(match.iloc[0]['close'])
                anchors.loc[idx, 'share_anchor_date'] = row['快照日期']
                break
    anchors.to_csv(out/'candidate_anchors.csv', index=False)
    all_rows, top, summary = rank_estimates(bars, anchors, sessions)
    summary = summary[summary['日期'].between(args.start, args.end)]
    for name, d in [('all_candidate_days', all_rows), ('daily_top20', top), ('daily_summary', summary)]:
        d.to_csv(out/f'{name}.csv', index=False, encoding='utf-8-sig')
    from services.microcap_estimate_chart import build_estimate_metrics
    build_estimate_metrics(all_rows).to_csv(out/'chart_metrics.csv', index=False, encoding='utf-8-sig')
    all_rows[all_rows['strategy_rank'].le(200).fillna(False)].sort_values(['date', 'strategy_rank']).to_csv(
        out/'daily_top200.csv', index=False, encoding='utf-8-sig')
    report = {**request, 'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'candidate_count': len(anchors), 'bar_rows': len(bars), 'days': len(summary),
        'days_with_20': int(summary['Top20数量'].eq(20).sum()), 'failures': failures,
        'missing_share_anchors': anchors.loc[anchors['estimated_shares'].isna(), '代码'].tolist(),
        'share_method': '快照并集使用快照隐含固定股本；补充ST股票使用2025三季报固定股本估算',
        'biases': ['后期成员并集存在前视与幸存者偏差', '固定股本忽略送转、增发、回购等变化',
                   '历史ST及停牌使用BaoStock逐日标记，未逐一公告验收',
                   '缺失日线不视为已证实停牌，不补价格', '历史信息公开时刻未知'],
        'strict_gate_1a': 'Unknown', 'strict_gate_1b': 'Unknown',
        'observed_unchanged': hashlib.sha256(source.read_bytes()).hexdigest()==digest}
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
