from datetime import date
import unittest

import pandas as pd

from services.microcap_validation import (
    AlignmentDecision, EvidenceCheck, GATE_1A_REQUIREMENTS, GATE_1B_REQUIREMENTS,
    alignment_test, formal_comparison, evaluate_gates,
)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.friday,self.monday = date(2026,9,4),date(2026,9,7)
        self.sessions = (self.friday,self.monday)
        self.observed = pd.DataFrame({'security':[f'{n:06d}' for n in range(1,401)],
                                      'rank':range(1,401),'market_cap':[float(n*100) for n in range(1,401)]})
        self.alignment = AlignmentDecision(self.monday,self.friday,1,'Fact',
                                            'fixture-source-evidence',frozenset(self.sessions))

    def compare(self,other,alignment=None,valuation='fixture-time'):
        return formal_comparison(self.observed.assign(snapshot_date=self.monday),
                                 other.assign(selection_date=self.friday,quality='complete'),
                                 alignment or self.alignment,self.sessions,
                                 observed_provenance_evidence='fixture-provenance',
                                 same_valuation_time_evidence=valuation,
                                 missing_eligible_securities=[],uncertain_boundary_securities=[])

    def test_identity_all_metrics(self):
        result = self.compare(self.observed)
        for n in [20,50,200,400]:
            self.assertEqual(result[f'top{n}_overlap']['intersection_count'],n)
        self.assertEqual(result['top20_jaccard'],1)
        self.assertEqual(result['top20_rank_mae'],0)
        self.assertEqual(result['top20_market_cap_mape_pct'],0)
        self.assertEqual(len(result['rank_15_25_boundary']),11)

    def test_boundary_exchange_has_known_overlap_jaccard_and_mae(self):
        other = self.observed.copy()
        other.loc[19,'rank'],other.loc[20,'rank'] = 21,20
        other.loc[0,'rank'],other.loc[1,'rank'] = 2,1
        other['market_cap'] *= 1.1
        result = self.compare(other)
        self.assertEqual(result['top20_overlap']['intersection_count'],19)
        self.assertAlmostEqual(result['top20_jaccard'],19/21)
        self.assertAlmostEqual(result['top20_rank_mae'],2/19)
        self.assertAlmostEqual(result['top20_market_cap_mape_pct'],10)

    def test_missing_rows_do_not_claim_top400_or_top20(self):
        result = self.compare(self.observed.iloc[:20])
        self.assertEqual(result['top400_overlap']['status'],'Unknown')
        result = self.compare(self.observed.iloc[:19])
        self.assertIsNone(result['top20_jaccard'])
        self.assertIsNone(result['top20_rank_mae'])

    def test_wrong_valuation_time_suppresses_mape(self):
        self.assertIsNone(self.compare(self.observed,valuation='')['top20_market_cap_mape_pct'])

    def test_alignment_diagnostics_compare_previous_session_and_never_certify(self):
        shifted = self.observed.copy();shifted['security'] = 'x'+shifted['security']
        result = alignment_test({self.monday:self.observed.assign(snapshot_date=self.monday)},
                                 {self.friday:self.observed.assign(selection_date=self.friday),
                                  self.monday:shifted.assign(selection_date=self.monday)},self.sessions)
        self.assertEqual([r['intersection_count'] for r in result],[0,20])
        self.assertTrue(all(r['alignment_classification']=='Unknown' for r in result))
        self.assertEqual(result[1]['reconstructed_date'],self.friday)

    def test_unverified_alignment_is_not_formal(self):
        bad = AlignmentDecision(self.monday,self.friday,1,'Unknown','',frozenset(self.sessions))
        with self.assertRaisesRegex(ValueError,'alignment未验证'):
            self.compare(self.observed,bad)

    def test_wrong_date_and_incomplete_cannot_reuse_alignment_proof(self):
        for day,quality in [(self.monday,'complete'),(self.friday,'incomplete')]:
            with self.assertRaises(ValueError):
                formal_comparison(self.observed.assign(snapshot_date=self.monday),
                                  self.observed.assign(selection_date=day,quality=quality),
                                  self.alignment,self.sessions,
                                  observed_provenance_evidence='fixture',same_valuation_time_evidence='fixture',
                                  missing_eligible_securities=[],uncertain_boundary_securities=[])

    def test_gate_b_failure_does_not_override_gate_a(self):
        dates = frozenset(self.sessions)
        a = {key:EvidenceCheck('passed','fixture',dates) for key in GATE_1A_REQUIREMENTS}
        b = {key:EvidenceCheck('failed','fixture',dates) for key in GATE_1B_REQUIREMENTS}
        result = evaluate_gates(a,b,dates,frozenset(str(n) for n in range(20)))
        self.assertEqual(result['gate_1a']['status'],'passed')
        self.assertEqual(result['gate_1b']['status'],'failed')
        self.assertTrue(result['historical_expansion_allowed'])

    def test_insufficient_share_sample_and_empty_scope_block_a(self):
        dates = frozenset(self.sessions)
        a = {key:EvidenceCheck('passed','fixture',dates) for key in GATE_1A_REQUIREMENTS}
        result = evaluate_gates(a,{},dates,frozenset(str(n) for n in range(19)))
        self.assertFalse(result['historical_expansion_allowed'])
        result = evaluate_gates(a,{},frozenset(),frozenset(str(n) for n in range(20)))
        self.assertFalse(result['historical_expansion_allowed'])

    def test_checks_with_wrong_dates_or_empty_evidence_cannot_pass(self):
        dates = frozenset(self.sessions)
        for evidence,scope in [('',dates),('fixture',frozenset([self.friday]))]:
            a = {key:EvidenceCheck('passed',evidence,scope) for key in GATE_1A_REQUIREMENTS}
            self.assertFalse(evaluate_gates(a,{},dates,frozenset(str(n) for n in range(20)))['historical_expansion_allowed'])


if __name__ == '__main__':unittest.main()
