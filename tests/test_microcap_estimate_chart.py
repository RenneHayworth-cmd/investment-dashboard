import unittest
import pandas as pd
from services.microcap_estimate_chart import build_estimate_metrics, merge_chart_metrics


class EstimateChartTests(unittest.TestCase):
    def test_rank200_uses_strategy_rank_not_raw_order(self):
        rows = pd.DataFrame({'date':['2026-01-05']*202, 'strategy_rank':[None]+list(range(1,202)),
            'estimated_market_cap_yuan':[1e8]+[i*1e8 for i in range(1,202)],
            '代码':['000000']+[f'{i:06d}' for i in range(1,202)], '名称':['样本']*202})
        result=build_estimate_metrics(rows).iloc[0]
        self.assertEqual(result['第200名市值(亿元)'],200)
        self.assertEqual(result['第200名股票'],'000200 样本')
        self.assertEqual(result['微盘20均值(亿元)'],10.5)
        short=build_estimate_metrics(rows.iloc[:200]).iloc[0]
        self.assertTrue(pd.isna(short['第200名市值(亿元)']))

    def test_snapshot_wins_overlap_and_inputs_unchanged(self):
        snapshots=pd.DataFrame({'日期':['2026-06-22'],'微盘20均值(亿元)':[15.]})
        estimates=pd.DataFrame({'日期':['2026-01-05','2026-06-22'],'微盘20均值(亿元)':[10.,12.],
            '图表来源':['历史估算']*2})
        original=snapshots.copy(deep=True)
        result=merge_chart_metrics(snapshots,estimates)
        self.assertEqual(result['微盘20均值(亿元)'].tolist(),[10.,15.])
        self.assertEqual(result['图表来源'].tolist(),['历史估算','存量快照'])
        pd.testing.assert_frame_equal(snapshots,original)
