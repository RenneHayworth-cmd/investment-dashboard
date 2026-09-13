"""Derived chart data for explicitly labelled retrospective estimates."""
from pathlib import Path
import pandas as pd

ESTIMATE_DIR = Path(__file__).resolve().parents[1] / 'output/microcap_union_estimate_20260101_20260908_v2'


def build_estimate_metrics(rows):
    active = rows.copy()
    active['strategy_rank'] = pd.to_numeric(active['strategy_rank'], errors='coerce')
    active['estimated_market_cap_yuan'] = pd.to_numeric(active['estimated_market_cap_yuan'], errors='coerce')
    active = active[active['strategy_rank'].notna()].sort_values(['date', 'strategy_rank'])
    metrics = []
    for day, group in active.groupby('date'):
        top = group[group['strategy_rank'].le(20)]
        boundary = group[group['strategy_rank'].eq(200)]
        r = boundary.iloc[0] if len(boundary) == 1 else None
        metrics.append({'日期': pd.Timestamp(day), '有效股票数': len(group),
            '微盘20均值(亿元)': top['estimated_market_cap_yuan'].mean()/1e8 if len(top)==20 else None,
            '第200名市值(亿元)': r['estimated_market_cap_yuan']/1e8 if r is not None else None,
            '第200名股票': f"{r['代码']} {r['名称']}" if r is not None else None,
            '口径': '固定候选池历史估算', '图表来源': '历史估算'})
    return pd.DataFrame(metrics)


def merge_chart_metrics(snapshots, estimates):
    observed = snapshots.copy()
    observed['图表来源'] = '存量快照'
    frames = [estimates.copy(), observed]
    combined = pd.concat(frames, ignore_index=True)
    combined['日期'] = pd.to_datetime(combined['日期'], errors='coerce').dt.normalize()
    combined = combined.dropna(subset=['日期']).drop_duplicates('日期', keep='last').sort_values('日期')
    combined['日期显示'] = combined['日期'].dt.strftime('%Y-%m-%d')
    return combined.reset_index(drop=True)


def load_estimate_metrics():
    path = ESTIMATE_DIR / 'chart_metrics.csv'
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)
