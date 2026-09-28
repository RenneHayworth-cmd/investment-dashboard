import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from services.microcap_rotation_replay import validate_replay


class ReplayedResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=os.environ.get('MICROCAP_REPLAY_RESULT')
        if not path:
            raise unittest.SkipTest('需要已生成的完整历史重算结果')
        cls.report=json.loads(Path(path).read_text())

    def test_full_ledger_and_display_metrics(self):
        validate_replay(self.report)
        self.assertEqual(len(self.report['daily']),174*4)
        self.assertEqual(len(self.report['comparison']),18)

    def test_zhuoran_replaced_on_first_suspension_day_and_excluded_afterward(self):
        for s in 'BCD':
            state=next(r for r in self.report['states'] if r['strategy']==s and r['date']=='2026-05-06')
            self.assertIn('688121',state['selection_adjustment']['excluded'])
            self.assertTrue(state['selection_adjustment']['replacements'])
            self.assertNotIn('688121',state['positions'])
            self.assertFalse(any(f['code']=='688121' for f in state['failures']))
        chosen=[r for r in self.report['selection_audit'] if '2026-05-06'<=r['date']<='2026-07-06']
        self.assertFalse(any(r['code']=='688121' for r in chosen))

    def test_summary_tampering_fails(self):
        report=copy.deepcopy(self.report)
        report['comparison'][0]['B']='999999.00 元'
        with self.assertRaisesRegex(ValueError,'展示指标'):
            validate_replay(report)

    def test_fill_fee_tampering_fails(self):
        report=copy.deepcopy(self.report)
        state=next(r for r in report['states'] if r['fills'])
        state['fills'][0]['fee']='0.00'
        with self.assertRaisesRegex(ValueError,'手续费'):
            validate_replay(report)

    def test_research_page_reads_new_report_without_network(self):
        app_code='from components.microcap_rotation import research\nresearch({"research":report})'
        with patch('services.microcap_rotation.update_simulation') as update:
            app=AppTest.from_string('import json\nreport=json.loads('+repr(json.dumps(self.report))+')\n'+app_code).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            update.assert_not_called()

if __name__=='__main__': unittest.main()
