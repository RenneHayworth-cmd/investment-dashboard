import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from services.microcap import build_microcap_snapshot_metrics
from services.microcap_research_audit import audit_snapshot_provenance, inspect_snapshot_files

ROOT = Path(__file__).resolve().parents[1]


class ResearchAuditTests(unittest.TestCase):
    def setUp(self):
        self.rows = pd.DataFrame({
            "快照日期": ["2026-06-24"] * 2,
            "代码": ["000001", "600001"],
            "最新价": [10.0, 12.0],
            "总市值(亿元)": [15.0, 16.0],
        })

    def test_exact_match_ignores_order_but_never_certifies_source(self):
        original = self.rows.copy(deep=True)
        result = audit_snapshot_provenance(self.rows, self.rows.iloc[::-1])[0]
        self.assertTrue(result["legacy_exact_match"])
        self.assertFalse(result["validation_eligible"])
        pd.testing.assert_frame_equal(original, self.rows)

    def test_partial_match_is_not_full_match_and_duplicates_are_reported(self):
        rows = pd.concat([self.rows, self.rows.head(1)], ignore_index=True)
        result = audit_snapshot_provenance(rows, self.rows)[0]
        self.assertFalse(result["legacy_exact_match"])
        self.assertEqual(result["duplicate_securities"], 1)

    def test_no_legacy_does_not_prove_observed(self):
        result = audit_snapshot_provenance(self.rows)[0]
        self.assertEqual(result["provenance"], "Unknown")
        self.assertFalse(result["validation_eligible"])

    def test_missing_prices_do_not_count_as_exact_match(self):
        self.rows.loc[0, "最新价"] = float('nan')
        self.assertFalse(audit_snapshot_provenance(self.rows, self.rows)[0]["legacy_exact_match"])

    def test_files_unchanged_and_gates_remain_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "observed.csv"
            self.rows.to_csv(source, index=False)
            before = source.read_bytes()
            report = inspect_snapshot_files(source, source)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(report["summary"]["legacy_matching_days"], 1)
            self.assertEqual(report["gate_1a"]["status"], "Unknown")
            self.assertFalse(report["historical_expansion_allowed"])

    def test_report_path_cannot_overwrite_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "observed.csv"
            self.rows.to_csv(source, index=False)
            before = source.read_bytes()
            result = subprocess.run([
                str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/audit_microcap_research_inputs.py'),
                '--snapshots', str(source), '--report', str(source),
            ], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(before, source.read_bytes())

    def test_metrics_do_not_load_legacy_extension_even_when_empty(self):
        with patch('services.microcap.load_microcap_history_extension') as load:
            self.assertTrue(build_microcap_snapshot_metrics(pd.DataFrame()).empty)
            load.assert_not_called()
            with self.assertRaisesRegex(ValueError, '禁止混入'):
                build_microcap_snapshot_metrics(pd.DataFrame(), include_extension=True)
            load.assert_not_called()

    def test_old_entrypoints_fail_before_fetch_or_write(self):
        for filename, function, kwargs in [
            ('reconstruct_missing_microcap_snapshots.py', 'run_reconstruction', {'dry_run': False}),
            ('reconstruct_missing_microcap_snapshots.py', 'run_reconstruction', {'dry_run': True}),
            ('build_microcap_history_extension.py', 'run_build_history_extension', {}),
        ]:
            with self.subTest(filename=filename, kwargs=kwargs):
                spec = importlib.util.spec_from_file_location('legacy_script', ROOT / 'scripts' / filename)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with patch.object(module.TickFlow, 'free') as fetch, patch.object(module.Path, 'mkdir') as mkdir:
                    with self.assertRaisesRegex(RuntimeError, '已停用'):
                        getattr(module, function)(**kwargs)
                    fetch.assert_not_called()
                    mkdir.assert_not_called()


if __name__ == '__main__':
    unittest.main()
