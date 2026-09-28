"""Offline historical replay using the same selection and ledger as daily simulation."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import hashlib
import pandas as pd
from services.microcap_rotation_engine import initialize, execute, ranking
from services.microcap_rotation_metrics import metrics, display_metrics, LABELS
from services.microcap_rotation_policy import money, transaction_fee

CALC_VERSION='abcd-research-suspension-3'


def replay_research(root, package):
    root,package=Path(root),Path(package)
    paths={
        'panel':root/'output/microcap_union_estimate_20260101_20260908_v2/all_candidate_days.csv',
        'seed':root/'output/microcap_union_estimate_20260101_20260908_v2/candidates_20251231.csv',
        'snapshot':root/'data/raw/eastmoney/microcap_bk1158_constituent_snapshots_1d.csv',
        'index':root/'data/raw/index_long_history/index_raw_微盘股_1d.csv',
        'etf':root/'data/processed/512890_daily_2026.csv',
        'reference':package/'01_核心对比与审计总表/daily_nav_abcd.csv',
    }
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths.values()}
    for name in ('microcap_rotation_engine.py','microcap_rotation_policy.py',Path(__file__).name):
        path=Path(__file__).with_name(name)
        hashes[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
    reference=pd.read_csv(paths['reference'])
    dates=reference.date.astype(str).tolist()
    panel=pd.read_csv(paths['panel'],dtype={'代码':str},low_memory=False)
    seed=pd.read_csv(paths['seed'],dtype={'代码':str},low_memory=False)
    panel=pd.concat([seed,panel],ignore_index=True)
    panel['代码']=panel['代码'].str.zfill(6)
    groups={str(d):f for d,f in panel.groupby('date')}
    snapshot=pd.read_csv(paths['snapshot'],dtype={'代码':str},low_memory=False)
    snapshot['代码']=snapshot['代码'].str.zfill(6)
    snaps={str(d):f for d,f in snapshot.groupby('快照日期')}
    latest_st=panel.sort_values('date').drop_duplicates('代码',keep='last').set_index('代码').isST.to_dict()
    idx=pd.read_csv(paths['index']).rename(columns={'trade_date':'date'})
    # Retain the audited source archive's three late index observations where
    # the old index cache ends early. Never fabricate an index close.
    reference_index=reference[['date','BK1158_close']].rename(columns={'BK1158_close':'close'})
    overlap=idx.merge(reference_index,on='date',suffixes=('_source','_archive'))
    if (abs(overlap.close_source-overlap.close_archive)>.011).any():
        raise ValueError('指数缓存与历史档案冲突')
    idx=pd.concat([idx,reference_index]).drop_duplicates('date').sort_values('date')
    etf=pd.read_csv(paths['etf']).set_index('date').close.to_dict()
    previous_prices={}
    source_rows={}

    def build(day):
        h=idx.loc[idx.date<=day,['date','close']].to_dict('records')
        if not h or h[-1]['date']!=day:
            raise ValueError(day+' 缺指数')
        rows=[]
        if day in groups:
            for r in groups[day].to_dict('records'):
                rows.append(dict(code=r['代码'],name=r['名称'],close=float(r['close']),
                    market_cap=float(r['estimated_market_cap_yuan']),
                    halted=int(r['tradestatus'])==0,is_st=int(r['isST'])==1,
                    source='原始候选日线tradestatus/isST'))
        elif day in snaps:
            for r in snaps[day].to_dict('records'):
                flag=str(r.get('是否停牌','')).lower() in ('true','1','是')
                vol=pd.to_numeric(r.get('成交量'),errors='coerce')
                rows.append(dict(code=r['代码'],name=r['名称'],close=float(r['最新价']),
                    market_cap=float(r['总市值(亿元)'])*1e8,
                    halted=flag or (pd.notna(vol) and vol<=0),
                    is_st='ST' in str(r['名称']).upper() or latest_st.get(r['代码'],0)==1,
                    source='当日快照停牌标记与成交量；ST采用名称及前段记录'))
        else:
            raise ValueError(day+' 缺候选日线或快照')
        if len({r['code'] for r in rows})!=len(rows):
            raise ValueError(day+' 候选代码重复')
        b=dict(date=day,constituents=[],quotes={},index_close=h[-1]['close'],index_history=h,
               events=[],events_complete=True,events_source='研究口径：不考虑分红送转',warnings=[])
        for r in rows:
            c=r['code']; p=r['close']
            if pd.isna(p) or p<=0 or pd.isna(r['market_cap']) or r['market_cap']<=0:
                continue
            b['constituents'].append(dict(code=c,name=r['name'],market_cap=r['market_cap'],
                is_st=r['is_st'],halted=bool(r['halted']),asof=day))
            prev=previous_prices.get(c)
            ratio=Decimal('.20') if c.startswith(('300','301','688','689')) else Decimal('.05') if r['is_st'] else Decimal('.10')
            up=float(money(Decimal(str(prev))*(1+ratio))) if prev else None
            down=float(money(Decimal(str(prev))*(1-ratio))) if prev else None
            b['quotes'][c]=dict(date=day,close=p,formal=True,source=r['source'],adjustment='none',
                halted=bool(r['halted']),halt_source=r['source'],limit_up=bool(up and p>=up-1e-8),
                limit_down=bool(down and p<=down+1e-8),eligible=not r['is_st'],
                min_buy=200 if c.startswith(('688','689')) else 100,eligibility_source=r['source'])
            previous_prices[c]=p
        if day in etf:
            b['quotes']['512890']=dict(date=day,close=etf[day],formal=True,source='已保存512890未复权日线',
                adjustment='none',halted=False,limit_up=False,limit_down=False,eligible=True,
                min_buy=100,eligibility_source='历史ETF交易日线')
        source_rows[day]=b
        return b

    base=build('2025-12-31')
    states={s:initialize(s,base,base['index_history']) for s in 'ABCD'}
    # Preserve the historical sample's initial C/D cash allocation on Jan 5.
    for s in 'CD':
        states[s]['plan'].update(mode='cash',targets=[],buys=[],sells=[],buy_etf=False)
    states['A']['index_base']=str(reference.BK1158_close.iloc[0])
    daily=[]; all_states=[]; trades={s:[] for s in 'BCD'}; holdings={s:[] for s in 'BCD'}
    selections=[]; metrics_out={}; period_rows=[]
    for day in dates:
        b=build(day)
        chosen=ranking(b)
        if len(chosen)!=20:
            raise ValueError(day+' 停牌剔除后候选不足20只')
        for i,c in enumerate(chosen,1):
            selections.append(dict(date=day,rank=i,code=c))
        for s in 'ABCD':
            state=execute(s,states[s],b,b['index_history'])
            states[s]=state
            all_states.append(dict(strategy=s,**deepcopy(state)))
            daily.append(dict(strategy=s,date=day,equity=float(state['equity']),cash=float(state['cash'])))
            if s=='A':
                continue
            for f in state['fills']:
                trades[s].append({'日期':day,'股票代码':f['code'],'操作':'买入' if f['side']=='buy' else '卖出',
                    '股票名称':next((r['name'] for r in b['constituents'] if r['code']==f['code']),'红利低波ETF' if f['code']=='512890' else f['code']),
                    '股数':f['quantity'],'成交价':float(f['price']),'成交金额':float(f['amount']),
                    '手续费':float(f['fee']),'变动后可用现金':float(f['cash_after'])})
            for f in state['failures']:
                trades[s].append({'日期':day,'股票代码':f['code'],'操作':'未成交',
                    '股数':0,'手续费':0.,'备注':f['reason']})
            for c,p in state['positions'].items():
                holdings[s].append({'日期':day,'股票代码':c,'股票名称':p['name'],
                    '股数':p['quantity'],'当日收盘价':float(p['price']),'持仓市值':float(p['value'])})
    for s in 'ABCD':
        frame=pd.DataFrame([r for r in daily if r['strategy']==s])
        filled=[f for r in all_states if r['strategy']==s for f in r['fills']]
        defensive=sum(r['stock_ratio']==0 if s=='C' else r['etf_ratio']>0 if s=='D' else False
                      for r in all_states if r['strategy']==s)
        metrics_out[s]=metrics(frame,fills=len(filled),fees=sum(float(f['fee']) for f in filled),defensive_days=defensive)
    for s in 'CD':
        active=None
        for r in [r for r in all_states if r['strategy']==s]:
            off=r['stock_ratio']==0 if s=='C' else r['etf_ratio']>0
            if off and active is None:
                active={'策略':s,'开始日期':r['date'],'期初总资产':float(r['equity'])}
            if active is not None:
                active.update({'结束日期':r['date'],'期末总资产':float(r['equity'])})
            if active and not off:
                period_rows.append(active); active=None
        if active:
            period_rows.append(active)
    comparison=pd.DataFrame({'指标':LABELS,**{s:display_metrics(metrics_out[s]) for s in 'ABCD'}})
    report=dict(calculation_version=CALC_VERSION,hashes=hashes,metrics=metrics_out,daily=daily,
        comparison=comparison.to_dict('records'),differences=[],trades=trades,holdings=holdings,
        periods=period_rows,transforms=[{'规则':'选股日先剔除ST及停牌股票；执行日停牌按前日冻结候补顺序补位，涨停不补位'}],
        documents={'重算说明':'采用每日模拟统一执行引擎；股票买入每笔5元，卖出每笔5元加成交金额万5印花税；512890仍按万0.6计佣；不考虑分红送转。'
            '沿用原候选池与固定股本估算；不构成严格历史时点回测。B首日用2025-12-31名单，C/D首日现金。'
            '涨跌停按前收盘及板块规则估算；后段ST用名称及前段记录，存在数据局限。'
            '停牌首日按前日冻结候补顺序补位；已持有且无法卖出的股票继续占用20只名额。'},
        disclosure='后期候选池与固定股本估算，非严格历史时点回测；停牌先剔除后顺延。',
        selection_audit=selections,states=all_states,
        selection_adjustments=[dict(strategy=r['strategy'],date=r['date'],**r['selection_adjustment'])
            for r in all_states if r.get('selection_adjustment',{}).get('excluded')])
    validate_replay(report)
    return report


def validate_replay(report):
    """Independent inventory/cash reconciliation of every exported daily state."""
    for s in 'ABCD':
        cash=Decimal('220000'); held={}
        for r in [r for r in report['states'] if r['strategy']==s]:
            if s=='A':
                continue
            for f in r['fills']:
                code=f['code']; qty=f['quantity']; amount=money(Decimal(f['price'])*qty)
                if amount!=money(f['amount']):
                    raise ValueError('成交金额不闭合')
                expected_fee=transaction_fee(code,amount,f['side'])
                if money(f['fee'])!=expected_fee:
                    raise ValueError('成交手续费不符')
                sign=1 if f['side']=='buy' else -1
                cash-=sign*amount+money(f['fee']); held[code]=held.get(code,0)+sign*qty
                if cash!=money(f['cash_after']) or cash<0 or held[code]<0:
                    raise ValueError('逐笔资金/库存不闭合')
            held={c:q for c,q in held.items() if q}
            if held!={c:p['quantity'] for c,p in r['positions'].items()}:
                raise ValueError('持仓与成交不一致')
            if cash!=money(r['cash']) or cash+sum((money(p['value']) for p in r['positions'].values()),Decimal(0))!=money(r['equity']):
                raise ValueError('日结资产不闭合')
            if len(set(held)-{'512890'})>20:
                raise ValueError('超过20只股票')
    for s in 'ABCD':
        states=[r for r in report['states'] if r['strategy']==s]
        m=metrics(pd.DataFrame(states),fills=sum(len(r['fills']) for r in states),
                  fees=sum(float(f['fee']) for r in states for f in r['fills']),
                  defensive_days=sum(r['stock_ratio']==0 if s=='C' else r['etf_ratio']>0 if s=='D' else False for r in states))
        if display_metrics(m)!=[r[s] for r in report['comparison']]:
            raise ValueError('展示指标与日结不一致')
