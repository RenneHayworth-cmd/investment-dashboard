import unittest
from unittest.mock import patch
import pandas as pd
from streamlit.testing.v1 import AppTest
from services.futures_live_strategy import allocate_strategy_trades, build_strategy_analysis
from services.futures_live_daily_pnl import _calculate_daily_account_state


def trade(i, contract, side, action, qty, price, time='10:00:00', day='2026-08-26', asset='期货', strategy=''):
    return dict(id=i, contract=contract, buy_sell=side, open_close=action, quantity=qty,
                price=price, trade_time=time, trade_date=day, asset_type=asset,
                strategy=strategy, fee=qty, multiplier=100, close_pnl=None)


def fixture():
    rows = [trade(1, 'I2609P750', '卖', '未提供', 1, 12, day='2026-08-17', asset='期权'),
            trade(2, 'I2609', '卖', '平', 1, 740, day='2026-08-19'),
            trade(3, 'I2701', '买', '开', 1, 720, day='2026-08-19', time='10:00:01'),
            trade(4, 'I2701', '买', '开', 2, 722, day='2026-08-19', time='11:00:00'),
            trade(5, 'I2705', '卖', '开', 2, 712, day='2026-08-19', time='11:00:00')]
    dates = ['2026-08-17','2026-08-18','2026-08-19']
    prices = {('期权','I2609P750', dates[0]): 13,
              ('期货','I2609', dates[1]): 735,
              ('期货','I2701', dates[2]): 725,
              ('期货','I2705', dates[2]): 713}
    return dict(trading_dates=dates, close_lookup=prices, settlement_lookup=prices,
                valuation_lookup=prices, trade_groups={d: pd.DataFrame([r for r in rows if r['trade_date']==d]) for d in [dates[0],dates[2]]},
                flow_groups={dates[1]: pd.DataFrame([dict(entry_type='行权手续费', amount=2)])},
                event_groups={}, fee_adjustments={dates[2]: 3}, latest_statement_end='2026-08-31')


class StrategyTests(unittest.TestCase):
    def test_nearest_pair_preserves_directional_roll_and_fees(self):
        rows = [trade(1,'I2701','买','开',4,714,'10:00:00'),
                trade(2,'I2701','买','开',3,716,'10:02:00'),
                trade(3,'I2705','卖','开',3,702,'10:02:00')]
        result=allocate_strategy_trades(pd.DataFrame(rows))
        self.assertEqual(result.set_index('id').loc[1,'strategy'],'铁矿石滚贴水')
        self.assertEqual(result.set_index('id').loc[2,'strategy'],'铁矿石跨期')
        self.assertEqual(result.fee.sum(),10)

    def test_split_execution_and_explicit_override(self):
        frame=pd.DataFrame([trade(1,'I2701','买','开',5,714), trade(2,'I2705','卖','开',2,702)])
        result=allocate_strategy_trades(frame)
        self.assertEqual(result[result.id.eq(1)].quantity.sum(),5)
        self.assertEqual(result[result.id.eq(1)].fee.sum(),5)
        frame.loc[0,'strategy']='铁矿石滚贴水'
        result=allocate_strategy_trades(frame)
        self.assertEqual(result[result.id.eq(1)].strategy.tolist(),['铁矿石滚贴水'])
        self.assertEqual(result[result.id.eq(2)].strategy.tolist(),['未归类／不展示'])

    def test_assignment_roll_fee_and_account_reconciliation_both_modes(self):
        for mode in ['close','settlement']:
            inputs=fixture()
            account=_calculate_daily_account_state(inputs,valuation_mode=mode)
            result=build_strategy_analysis(account,valuation_mode=mode,inputs=inputs)
            summary=result['summary'].set_index('strategy')
            carry=summary.loc['铁矿石滚贴水']
            self.assertAlmostEqual(carry.realized_pnl,200) # premium 1200 minus assignment-to-sale loss 1000
            self.assertAlmostEqual(carry.floating_pnl,500)
            self.assertAlmostEqual(carry.fee,5) # 3 executions + exercise fee once
            self.assertEqual(carry.positions,'I2701 多1手')
            self.assertEqual(summary.loc['铁矿石跨期','positions'],'I2701 多2手；I2705 空2手')
            self.assertAlmostEqual(float(result['reconciliation'].iloc[3]['金额']),0)
            self.assertAlmostEqual(float(result['reconciliation'].iloc[2]['金额']),-3 if mode=='close' else 0)

    def test_missing_prices_remain_gaps_and_manual_account_not_allocated(self):
        inputs=fixture()
        del inputs['valuation_lookup'][('期货','I2701','2026-08-19')]
        account=_calculate_daily_account_state(inputs,valuation_mode='settlement')
        account.loc[2, ['status','net_pnl']]=['手工估算',1000]
        result=build_strategy_analysis(account,inputs=inputs)
        self.assertEqual(result['date'],'2026-08-18')
        missing=result['daily'].query("date == '2026-08-19' and strategy == '铁矿石滚贴水'")
        self.assertTrue(missing.net_pnl.isna().all())


def render_fixture():
    from unittest.mock import patch
    from tests.test_futures_live_strategy import fixture
    from services.futures_live_daily_pnl import _calculate_daily_account_state
    from services.futures_live_strategy import build_strategy_analysis
    from components.futures_live.strategy import render_strategy_analysis
    inputs=fixture()
    account=_calculate_daily_account_state(inputs,valuation_mode='settlement')
    result=build_strategy_analysis(account,inputs=inputs)
    with patch('components.futures_live.strategy.build_strategy_analysis',return_value=result):
        render_strategy_analysis(account,account)


class StrategyUITests(unittest.TestCase):
    def test_table_chart_and_filter(self):
        app=AppTest.from_function(render_fixture,default_timeout=30).run()
        self.assertEqual(list(app.exception),[])
        self.assertEqual(len(app.dataframe),3)
        self.assertEqual(len(app.get('plotly_chart')),1)
        self.assertNotIn('玉米',app.dataframe[0].value['策略'].tolist())
        app.multiselect[0].set_value(['铁矿石滚贴水']).run()
        self.assertEqual(list(app.exception),[])
        app.multiselect[0].set_value([]).run()
        self.assertEqual(len(app.get('plotly_chart')),0)


if __name__=='__main__':
    unittest.main()
