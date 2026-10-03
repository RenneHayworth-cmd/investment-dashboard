"""Local strategy attribution; reuse the account's formal valuation engine.

No personal executions or inferred labels are persisted in source or the ledger.
Explicit trade labels win. Unlabelled paired futures legs use nearest execution
 times within the same trading day; the attribution remains inspectable in the UI.
"""
from __future__ import annotations

import re
import pandas as pd

from services.futures_live_daily_pnl import (
    _load_daily_account_pnl_inputs, _calculate_daily_account_state,
)
from services.futures_live_models import exercise_fee_mask

STRATEGIES = ("铁矿石滚贴水", "铁矿石跨期", "IM跨期", "中证1000卖Put")
UNASSIGNED = "未归类／不展示"


def allocate_strategy_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Split quantity and execution fees, never copy a fee onto both sleeves."""
    if trades.empty:
        return trades.copy()
    frame = trades.reset_index(drop=True).copy()
    remaining = frame['quantity'].astype(int).to_dict()
    rows = []

    def assign(i, quantity, strategy, reason):
        if quantity <= 0:
            return
        row = frame.loc[i].to_dict()
        ratio = quantity / int(row['quantity'])
        row['quantity'] = quantity
        for field in ('fee', 'turnover', 'close_pnl'):
            if field in row and pd.notna(row[field]):
                row[field] = float(row[field]) * ratio
        row['strategy'] = strategy
        row['attribution'] = reason
        rows.append(row)
        remaining[i] -= quantity

    for i, row in frame.iterrows():
        explicit = str(row.get('strategy') or '').strip()
        if explicit and explicit != 'nan':
            assign(i, remaining[i], explicit if explicit in STRATEGIES else UNASSIGNED,
                   '成交已标注：' + explicit)
        elif row['asset_type'] == '期权':
            contract = str(row['contract'])
            strategy = ('铁矿石滚贴水' if re.fullmatch(r'I\d{4}P\d+', contract)
                        else '中证1000卖Put' if re.fullmatch(r'MO\d{4}P\d+', contract)
                        else UNASSIGNED)
            assign(i, remaining[i], strategy, '按期权品种归属；买卖方向由持仓链核算')

    # Match both opening and closing legs. Nearest time comes before row order:
    # a directional roll and a spread may share the same contract on the same day.
    futures = frame[frame['asset_type'].eq('期货')].copy()
    futures['_product'] = futures['contract'].str.extract(r'^([A-Z]+)', expand=False)
    futures['_day'] = pd.to_datetime(futures['trade_date']).dt.strftime('%Y-%m-%d')
    for (_, product, _), group in futures.groupby(['_day', '_product', 'open_close']):
        if product not in ('I', 'IM'):
            continue
        candidates = []
        for i, buy in group[group['buy_sell'].eq('买')].iterrows():
            for j, sell in group[group['buy_sell'].eq('卖')].iterrows():
                if buy['contract'] == sell['contract']:
                    continue
                a = pd.to_timedelta(str(buy.get('trade_time') or ''), errors='coerce')
                b = pd.to_timedelta(str(sell.get('trade_time') or ''), errors='coerce')
                if pd.isna(a) or pd.isna(b):
                    continue
                distance = abs((a - b).total_seconds())
                if distance <= 600:
                    candidates.append((distance, i, j))
        for _, i, j in sorted(candidates):
            quantity = min(remaining[i], remaining[j])
            strategy = '铁矿石跨期' if product == 'I' else 'IM跨期'
            for index in (i, j):
                assign(index, quantity, strategy, '同交易日十分钟内异月反向腿，按最近成交时间配对')
    for i, row in frame.iterrows():
        is_long = (row['buy_sell'], row['open_close']) in (('买', '开'), ('卖', '平'))
        carry = bool(re.fullmatch(r'I\d{4}', str(row['contract']))) and is_long
        assign(i, remaining[i], '铁矿石滚贴水' if carry else UNASSIGNED,
               '未配对铁矿石多头，沿用已确认滚贴水口径' if carry else '未纳入四项策略')
    return pd.DataFrame(rows).sort_values(['trade_date', 'trade_time', 'id']).reset_index(drop=True)


def build_strategy_analysis(account_daily: pd.DataFrame, *, valuation_mode='settlement', inputs=None):
    """Return same-date summary, gap-preserving curves and explicit reconciliation."""
    empty = {'summary': pd.DataFrame(), 'daily': pd.DataFrame(),
             'allocations': pd.DataFrame(), 'reconciliation': pd.DataFrame(), 'date': None}
    if account_daily.empty:
        return empty
    if inputs is None:
        inputs = _load_daily_account_pnl_inputs(
            as_of=str(account_daily['date'].max()), valuation_mode=valuation_mode)
    if inputs is None or not inputs['trade_groups']:
        return empty
    trades = pd.concat(inputs['trade_groups'].values(), ignore_index=True)
    allocated = allocate_strategy_trades(trades)
    curves = []
    for strategy in (*STRATEGIES, UNASSIGNED):
        selected = allocated[allocated['strategy'].eq(strategy)]
        local = dict(inputs)
        local['trade_groups'] = {
            str(day): group for day, group in selected.groupby(
                pd.to_datetime(selected['trade_date']).dt.strftime('%Y-%m-%d'))}
        options = set(selected.loc[selected['asset_type'].eq('期权'), 'contract'])
        local['event_groups'] = {
            day: group[group['option_contract'].isin(options)]
            for day, group in inputs['event_groups'].items()}
        # An explicitly identifiable iron-ore exercise charge belongs to carry.
        # Ambiguous multi-product exercise days stay in the public-fee bridge.
        local['flow_groups'] = {}
        if strategy == '铁矿石滚贴水':
            from services.futures_live_positions import iron_ore_option_expiry_date
            expiry_days = {iron_ore_option_expiry_date(c) for c in options}
            for day, group in inputs['flow_groups'].items():
                events = inputs['event_groups'].get(day, pd.DataFrame())
                explicit = set(events.get('option_contract', pd.Series(dtype=str)))
                if (day in expiry_days or explicit & options) and not (explicit - options):
                    local['flow_groups'][day] = group[exercise_fee_mask(group)]
        local['fee_adjustments'] = {}
        daily = _calculate_daily_account_state(local, valuation_mode=valuation_mode)
        daily['strategy'] = strategy
        curves.append(daily)
    daily = pd.concat(curves, ignore_index=True)
    valid = daily.pivot(index='date', columns='strategy', values='net_pnl').notna().all(axis=1)
    eligible = account_daily[account_daily['status'].isin(['完整', '手工估算']) & account_daily['net_pnl'].notna()]
    eligible = eligible[eligible['date'].isin(valid.index[valid])]
    result = dict(empty, daily=daily, allocations=allocated)
    if eligible.empty:
        return result
    date = str(eligible.iloc[-1]['date'])
    snapshot = daily[daily['date'].eq(date)]
    summary = []
    for row in snapshot[snapshot['strategy'].isin(STRATEGIES)].to_dict('records'):
        holdings = []
        for contract in row['contract_pnl']:
            for key, label in [('long_quantity', '多'), ('short_quantity', '空')]:
                quantity = contract.get(key, 0)
                if quantity:
                    holdings.append(f"{contract['contract']} {label}{quantity}手")
        summary.append({**{key: row[key] for key in ['strategy', 'realized_pnl', 'floating_pnl', 'fee', 'net_pnl']},
                        'positions': '；'.join(holdings) or '无持仓'})
    account = eligible.iloc[-1]
    included = snapshot[snapshot['strategy'].isin(STRATEGIES)]['net_pnl'].sum()
    excluded = snapshot[snapshot['strategy'].eq(UNASSIGNED)]['net_pnl'].sum()
    public_fee = float(account['fee']) - float(snapshot['fee'].sum())
    residual = float(account['net_pnl']) - (included + excluded - public_fee)
    bridge = pd.DataFrame([
        {'项目': '策略净盈亏合计', '金额': included},
        {'项目': '未展示交易净盈亏（含玉米）', '金额': excluded},
        {'项目': '账户公共费用调整', '金额': -public_fee},
        {'项目': '未分配差额（含账户级手工调整）', '金额': residual},
        {'项目': '账户净盈亏', '金额': float(account['net_pnl'])},
    ])
    return dict(result, summary=pd.DataFrame(summary), reconciliation=bridge, date=date)
