from datetime import date, datetime
from decimal import Decimal
import unittest

import pandas as pd

from services.microcap_pit import (DecisionClock, UniverseEvidence, ShareInterval, SHANGHAI,
                                 effective_shares, rank_day, EligibilityEvent, recheck_next_session)


class PITTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026,9,4)
        self.available = datetime(2026,9,4,16,tzinfo=SHANGHAI)
        self.clock = DecisionClock(self.day,self.available,date(2026,9,7),
                                   (self.day,date(2026,9,7)), 'fixture-calendar')
        self.rows = pd.DataFrame([{
            'security':f'{n:06d}', 'raw_unadjusted_close':Decimal(n),
            'effective_total_shares':Decimal(100),
            'price_available_at':self.available,'shares_available_at':self.available,
            'status_available_at':self.available,'price_evidence':'fixture-price',
            'shares_evidence':'fixture-shares','status_evidence':'fixture-status',
            'rule_pool_member':True,'index_member':pd.NA,'strategy_eligible':True,
            'tradable':True,'classification':'Fact','price_date':self.day,
            'price_adjustment':'none','shares_valid_from':self.day,
            'shares_valid_through':self.day,'shares_coverage_evidence':'fixture-coverage',
            'status_asof':datetime(2026,9,4,15,tzinfo=SHANGHAI),
        } for n in range(1,23)])
        self.universe = UniverseEvidence(self.day,frozenset(self.rows.security),'Fact','fixture-roster','fixture-reconcile')

    def test_rank_exclusion_preserves_raw_membership_and_rank(self):
        self.rows.loc[0,'strategy_eligible'] = False
        self.rows.loc[0,'index_member'] = True
        result, report = rank_day(self.rows,self.clock,self.universe)
        self.assertEqual(result.iloc[0].raw_market_cap_rank,1)
        self.assertTrue(result.iloc[0].index_member)
        self.assertTrue(pd.isna(result.iloc[0].strategy_rank))
        self.assertEqual(result.iloc[1].strategy_rank,1)
        self.assertTrue(report['trusted_top20'])

    def test_missing_even_largest_security_invalidates_top20(self):
        _, report = rank_day(self.rows.iloc[:-1],self.clock,self.universe)
        self.assertFalse(report['trusted_top20'])
        self.assertEqual(report['missing_eligible_securities'],['000022'])

    def test_future_price_not_used(self):
        self.rows.loc[0,'price_available_at'] = datetime(2026,9,7,9,tzinfo=SHANGHAI)
        result, report = rank_day(self.rows,self.clock,self.universe)
        self.assertTrue(pd.isna(result.set_index('security').loc['000001','market_cap']))
        self.assertFalse(report['trusted_top20'])

    def test_future_status_does_not_enter_selection(self):
        self.rows.loc[0,'status_available_at'] = datetime(2026,9,4,18,tzinfo=SHANGHAI)
        result, report = rank_day(self.rows,self.clock,self.universe)
        self.assertTrue(pd.isna(result.set_index('security').loc['000001','strategy_eligible']))
        self.assertFalse(report['trusted_top20'])

    def test_adjusted_price_is_rejected(self):
        self.rows.loc[0,'price_adjustment'] = 'forward'
        result, report = rank_day(self.rows,self.clock,self.universe)
        self.assertTrue(pd.isna(result.set_index('security').loc['000001','market_cap']))
        self.assertFalse(report['trusted_top20'])

    def test_expired_share_coverage_is_not_carried_forward(self):
        self.rows.loc[0,'shares_valid_through'] = date(2026,9,3)
        _, report = rank_day(self.rows,self.clock,self.universe)
        self.assertFalse(report['trusted_top20'])

    def test_same_day_and_naive_timestamps_fail(self):
        for clock in [DecisionClock(self.day,self.available,self.day,(self.day,),'fixture'),
                      DecisionClock(self.day,datetime(2026,9,4,16),date(2026,9,7),
                                    (self.day,date(2026,9,7)),'fixture')]:
            with self.assertRaises(ValueError):clock.validate()

    def test_shares_ignore_future_announcement_and_fail_conflicts(self):
        row = ShareInterval('000001',Decimal(100),self.day,self.day,self.available,'fixture','fixture','Fact')
        later = ShareInterval('000001',Decimal(200),self.day,self.day,
                              datetime(2026,9,7,9,tzinfo=SHANGHAI),'fixture','fixture','Fact')
        self.assertEqual(effective_shares('000001',self.day,self.available,[row,later]),Decimal(100))
        self.assertIsNone(effective_shares('000001',date(2026,9,7),self.available,[row]))
        conflicting = ShareInterval('000001',Decimal(300),self.day,self.day,self.available,'fixture','fixture','Fact')
        self.assertIsNone(effective_shares('000001',self.day,self.available,[row,conflicting]))

    def test_preopen_removes_suspended_and_uses_frozen_reserve(self):
        ranking,_ = rank_day(self.rows,self.clock,self.universe)
        original = ranking.copy(deep=True)
        event = EligibilityEvent('000001','tradable',False,
                                 datetime(2026,9,7,9,tzinfo=SHANGHAI),
                                 datetime(2026,9,4,18,tzinfo=SHANGHAI),'fixture','Fact')
        coverage = dict.fromkeys(ranking.security,'fixture-event-coverage')
        result,report = recheck_next_session(ranking,self.clock,[event],coverage)
        selected = result[result.selected_for_next_session]
        self.assertEqual(selected.security.tolist(),[f'{n:06d}' for n in range(2,22)])
        self.assertEqual(report['quality'],'complete')
        pd.testing.assert_frame_equal(ranking,original)
        self.assertEqual(result.iloc[0].raw_market_cap_rank,1)

    def test_after_recheck_event_is_ignored(self):
        ranking,_ = rank_day(self.rows,self.clock,self.universe)
        event = EligibilityEvent('000001','tradable',False,
                                 datetime(2026,9,7,9,tzinfo=SHANGHAI),
                                 datetime(2026,9,7,9,26,tzinfo=SHANGHAI),'fixture','Fact')
        result,_ = recheck_next_session(ranking,self.clock,[event],dict.fromkeys(ranking.security,'fixture'))
        self.assertTrue(result.iloc[0].selected_for_next_session)

    def test_missing_event_coverage_suppresses_executable_list(self):
        ranking,_ = rank_day(self.rows,self.clock,self.universe)
        result,report = recheck_next_session(ranking,self.clock,[],{})
        self.assertFalse(result.selected_for_next_session.any())
        self.assertIn('event_coverage_incomplete',report['blockers'])

    def test_after_close_before_data_ready_event_is_applied_at_recheck(self):
        ranking,_ = rank_day(self.rows,self.clock,self.universe)
        event = EligibilityEvent('000001','tradable',False,
                                 datetime(2026,9,4,15,30,tzinfo=SHANGHAI),
                                 datetime(2026,9,4,15,20,tzinfo=SHANGHAI),'fixture','Fact')
        result,_ = recheck_next_session(ranking,self.clock,[event],dict.fromkeys(ranking.security,'fixture'))
        self.assertFalse(result.iloc[0].selected_for_next_session)
        self.assertTrue(result.iloc[0].tradable)

    def test_unknown_evidence_and_wrong_status_asof_cannot_certify(self):
        for field,value in [('price_evidence',float('nan')),('shares_evidence','  '),
                            ('status_asof',datetime(2026,9,7,9,tzinfo=SHANGHAI))]:
            with self.subTest(field=field):
                rows = self.rows.copy()
                rows.loc[0,field] = value
                _,report = rank_day(rows,self.clock,self.universe)
                self.assertFalse(report['trusted_top20'])


if __name__ == '__main__':unittest.main()
