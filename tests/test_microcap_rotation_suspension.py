import unittest
import pandas as pd
from services.microcap_rotation_engine import ranking, initialize, execute
from services.microcap_rotation_data import snapshot_records
from services.microcap_rotation_auto import basic_batch
from tests.test_microcap_rotation import batch, history


class SuspensionSelectionTests(unittest.TestCase):
    def test_snapshot_flag_survives_until_ranking(self):
        day='2026-09-07'
        rows=[{'代码':f'{600000+i:06d}','名称':'股票','总市值(亿元)':i+1,
               '快照日期':day,'快照时间':day+' 15:05:00','是否停牌':i==0,
               '成交量':0 if i==0 else 100} for i in range(21)]
        b=basic_batch(day,history(day),snapshot_records(pd.DataFrame(rows)))
        self.assertEqual(ranking(b),[f'{600000+i:06d}' for i in range(1,21)])

    def test_bcd_known_suspension_replaced_before_next_day_execution(self):
        b=batch('2026-09-07')
        b['quotes']['600000'].update(halted=True,halt_source='test',close=None)
        for strategy in 'BCD':
            with self.subTest(strategy=strategy):
                state=initialize(strategy,b,history(b['date']))
                self.assertNotIn('600000',state['plan']['targets'])
                self.assertIn('600020',state['plan']['targets'])
                done=execute(strategy,state,batch('2026-09-08'),history('2026-09-08'))
                self.assertEqual(len(done['positions']),20)
                self.assertEqual(len(done['failures']),0)

    def test_future_halt_is_not_used_for_previous_ranking(self):
        b=batch('2026-09-07')
        b['quotes']['600000'].update(date='2026-09-08',halted=True)
        self.assertIn('600000',ranking(b))

    def test_missing_quote_is_not_suspension(self):
        b=batch('2026-09-07'); b['quotes']={}
        self.assertIn('600000',ranking(b))

    def test_execution_day_suspension_uses_frozen_reserve_order(self):
        state=initialize('B',batch('2026-09-07'),history('2026-09-07'))
        b=batch('2026-09-08')
        b['quotes']['600000'].update(halted=True,halt_source='test',close=None)
        b['constituents'][-1]['market_cap']=999999
        result=execute('B',state,b,history(b['date']))
        self.assertEqual(len(result['positions']),20)
        self.assertIn('600020',result['positions'])
        self.assertEqual(result['selection_adjustment']['excluded'],['600000'])
        self.assertEqual(result['selection_adjustment']['replacements'],['600020'])

if __name__=='__main__':
    unittest.main()
