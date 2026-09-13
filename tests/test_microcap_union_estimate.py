import unittest

import pandas as pd

from scripts.build_microcap_union_estimate import candidates, rank_estimates


class UnionEstimateTests(unittest.TestCase):
    def test_union_window_and_missing_anchor_not_dropped(self):
        snapshots = pd.DataFrame({
            '代码': ['000001', '000001', '000002', '000003'],
            '名称': ['甲', '甲', '乙', '丙'],
            '快照日期': ['2026-06-22', '2026-09-08', '2026-06-23', '2026-09-09'],
            '最新价': [10, 20, None, 1], '总市值(亿元)': [1, 5, None, 1]})
        result = candidates(snapshots).set_index('代码')
        self.assertEqual(set(result.index), {'000001', '000002'})
        self.assertEqual(result.loc['000001', 'estimated_shares'], 1e7)
        self.assertTrue(pd.isna(result.loc['000002', 'estimated_shares']))

    def test_historical_st_suspension_and_raw_rank_preserved(self):
        anchors = pd.DataFrame({'代码':['000001','000002','000003'],
            '名称':['甲','乙','丙'], 'estimated_shares':[100,100,100],
            'share_anchor_date':['2026-06-22']*3})
        bars = pd.DataFrame({'date':['2026-01-05']*3 + ['2026-01-06'],
            'code':['sz.000001','sz.000002','sz.000003','sz.000001'],
            'close':[1,2,3,1], 'volume':[100,0,100,100], 'amount':[100]*4,
            'adjustflag':[3]*4, 'tradestatus':[1,0,1,1], 'isST':[1,0,0,0]})
        rows, top, summary = rank_estimates(bars, anchors,
            ['2026-01-05','2026-01-06','2026-01-07'])
        self.assertEqual(top['代码'].tolist(), ['000003','000001'])
        self.assertEqual(rows.iloc[0]['raw_market_cap_rank'], 1)
        self.assertTrue(pd.isna(rows.iloc[0]['strategy_rank']))
        self.assertEqual(top.iloc[0]['effective_date'], '2026-01-06')
        self.assertEqual(summary.iloc[1]['无日线数'], 2)
        self.assertTrue(pd.isna(summary.iloc[0]['微盘20估算均值(亿元)']))

    def test_named_pre_st_stocks_are_added_without_duplicates(self):
        from scripts.build_microcap_union_estimate import add_supplemental_candidates, SUPPLEMENTAL_ST
        anchors = pd.DataFrame({'代码': ['688121', '002883'],
            '名称': ['卓然股份', '中设股份'], 'estimated_shares': [100, 100],
            'share_anchor_date': ['2026-06-22'] * 2})
        result = add_supplemental_candidates(anchors)
        self.assertEqual(set(result['代码']), set(SUPPLEMENTAL_ST) | {'688121'})
        self.assertFalse(result['代码'].duplicated().any())
        self.assertEqual(result.loc[result['代码'].eq('002883'), 'estimated_shares'].iloc[0], 100)

    def test_supplemented_stock_enters_before_st_and_exits_on_st(self):
        from scripts.build_microcap_union_estimate import add_supplemental_candidates
        anchors = pd.DataFrame({'代码': ['688121'], '名称': ['卓然股份'],
            'estimated_shares': [100], 'share_anchor_date': ['2026-06-22']})
        anchors = add_supplemental_candidates(anchors)
        anchors['estimated_shares'] = 100
        bars = pd.DataFrame({'date': ['2026-04-27', '2026-04-29'],
            'code': ['sz.002719'] * 2, 'close': [1, 1], 'volume': [100, 100],
            'amount': [100, 100], 'adjustflag': [3, 3], 'tradestatus': [1, 1], 'isST': [0, 1]})
        rows, top, _ = rank_estimates(bars, anchors, ['2026-04-27', '2026-04-29', '2026-04-30'])
        self.assertEqual(top['date'].tolist(), ['2026-04-27'])
        self.assertEqual(top['代码'].tolist(), ['002719'])
        self.assertEqual(rows.iloc[1]['raw_market_cap_rank'], 1)
        self.assertEqual(rows.iloc[1]['exclusion_reason'], 'ST/*ST')
