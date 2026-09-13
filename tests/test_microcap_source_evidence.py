import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from services.microcap_source_evidence import profile_share_events, save_source_evidence


class SourceEvidenceTests(unittest.TestCase):
    def test_report_and_late_publication_never_become_pit_events(self):
        frame = pd.DataFrame({
            '证券代码':['000001','000001'], '公告日期':['2026-08-30','2026-07-01'],
            '变动日期':['2026-06-30','2026-07-01'], '总股本':[100,110],
            '变动原因':['定期报告','送转股'],
        })
        result = profile_share_events(frame)
        self.assertEqual(result['report_period_rows'],1)
        self.assertEqual(result['published_after_change_rows'],1)
        self.assertFalse(result['pit_approved'])
        self.assertEqual(result['classification'],'Unknown')

    def test_missing_fields_fail_closed(self):
        result = profile_share_events(pd.DataFrame({'总股本':[100]}))
        self.assertIn('公告日期',result['missing_columns'])
        self.assertFalse(result['pit_approved'])

    def test_capture_time_is_not_historical_availability_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            frame = pd.DataFrame({'x':[1]})
            meta = save_source_evidence(directory,'shares_000001',frame,'https://example.test',{},'test')
            self.assertIsNone(meta['information_available_at'])
            self.assertEqual(meta['artifact_kind'],'adapter_output')
            before = (directory/'shares_000001.csv').read_bytes()
            with self.assertRaises(FileExistsError):
                save_source_evidence(directory,'shares_000001',pd.DataFrame({'x':[2]}),'url',{},'test')
            self.assertEqual(before,(directory/'shares_000001.csv').read_bytes())

    def test_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                save_source_evidence(Path(d),'../observed',pd.DataFrame(),'url',{},'test')


if __name__ == '__main__':
    unittest.main()
