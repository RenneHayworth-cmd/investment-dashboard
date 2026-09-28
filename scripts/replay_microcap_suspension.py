"""Offline historical recalculation; publish only after all reconciliation checks."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from services.microcap_rotation_replay import replay_research
from services.microcap_rotation_store import Store,process_lock

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args()
    report=replay_research(Path(__file__).resolve().parents[1],args.package)
    args.output.mkdir(parents=True,exist_ok=True)
    for name,data in [('comparison_summary_abcd',report['comparison']),('daily_nav_abcd',report['daily']),
                      ('selection_audit',report['selection_audit']),('timing_periods',report['periods']),
                      ('suspension_replacements',report['selection_adjustments'])]:
        pd.DataFrame(data).to_csv(args.output/(name+'.csv'),index=False,encoding='utf-8-sig',float_format='%.6f')
    for s in 'BCD':
        pd.DataFrame(report['trades'][s]).to_csv(args.output/f'trade_log_{s.lower()}.csv',index=False,encoding='utf-8-sig',float_format='%.4f')
        pd.DataFrame(report['holdings'][s]).to_csv(args.output/f'holdings_{s.lower()}.csv',index=False,encoding='utf-8-sig',float_format='%.4f')
    payload=pd.Series([report]).to_json(orient='values',force_ascii=False,double_precision=10)[1:-1]
    (args.output/'research.json').write_text(payload,encoding='utf-8')
    if args.publish:
        with process_lock():
            store=Store()
            daily_before=store.days()
            archive_id=store.save_research(report)
            with store.connection(True) as c:
                c.execute('DELETE FROM microcap_rotation_research WHERE id<>?',(archive_id,))
            if store.days()!=daily_before:
                raise AssertionError('历史导入不得变更每日模拟账本')
            from services.microcap_rotation_replay import validate_replay
            validate_replay(store.research())
    print(pd.DataFrame(report['comparison']).to_string(index=False))
    for r in report['states']:
        if r['date']=='2026-05-06' and r['strategy'] in 'BCD':
            print(r['strategy'],r.get('selection_adjustment'))
    print('已核对全部日结、逐笔现金与持仓；'+('工作台已替换历史研究' if args.publish else '尚未发布'))

if __name__=='__main__': main()
