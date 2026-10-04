"""Hash-bound, explicitly reviewed timestamp anomalies: metadata repair only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from test_sfu_units import fixture, card, CLI


class ReviewedDateAnomalyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.data = self.base / 'data'
        self.data.mkdir()
        self.path = self.data / '20230828.fits'
        content, self.hsize = fixture()
        self.content = content.replace(card('DATE_END', "'2023-08-28T23:00:00'"),
                                       card('DATE_END', "'2010-05-23T11:48:44.914'"))
        self.path.write_bytes(self.content)
        self.manifest = self.base / 'reviewed.json'
        self.journal = self.base / 'journal.jsonl'
        self.approve()

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self, content=None, directory=None):
        content = self.content if content is None else content
        header = content[:self.hsize]
        offset = header.index(b'BUNIT   ')
        digest = hashlib.sha256(header[:offset] + header[offset + 80:]).hexdigest()
        payload = {'schema': 'spec-v2-reviewed-date-anomalies-v1',
                   'directory': str(self.data if directory is None else directory),
                   'files': [{'filename': self.path.name,
                              'header_outside_bunit_sha256': digest,
                              'reason': 'Reviewed corrupt final timestamp; SFU metadata only; dates unchanged.'}]}
        self.manifest.write_text(json.dumps(payload))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True)

    def test_exact_reviewed_header_apply_verify_and_rollback(self):
        result = self.cli(self.data, '--reviewed-date-anomalies', self.manifest,
                          '--apply', '--journal', self.journal)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = self.path.read_bytes()
        offset = self.content.index(b'BUNIT   ')
        self.assertEqual(updated[:offset], self.content[:offset])
        self.assertEqual(updated[offset + 80:], self.content[offset + 80:])
        self.assertIn(b"'sfu", updated[offset:offset + 80])
        self.assertEqual(self.cli('--verify', self.journal).returncode, 0)
        result = self.cli('--rollback', self.journal, '--apply', '--journal', self.base / 'rollback.jsonl')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_anomalies_still_rejected_without_review(self):
        self.assertNotEqual(self.cli(self.data).returncode, 0)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_review_cannot_authorize_changed_header(self):
        self.path.write_bytes(self.content.replace(card('ORIGIN', "'NJIT'"), card('ORIGIN', "'OTHER'")))
        before = self.path.read_bytes()
        result = self.cli(self.data, '--reviewed-date-anomalies', self.manifest,
                          '--apply', '--journal', self.journal)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_review_does_not_bypass_corrected_product_guard(self):
        content, self.hsize = fixture(extra=(card('PBCOR', 'T'),))
        self.content = content.replace(card('DATE_END', "'2023-08-28T23:00:00'"),
                                       card('DATE_END', "'2010-05-23T11:48:44.914'"))
        self.path.write_bytes(self.content)
        self.approve()
        result = self.cli(self.data, '--reviewed-date-anomalies', self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_inserted_legacy_sfu_header_is_idempotent_with_approval(self):
        from test_sfu_units import legacy_fixture
        content, _ = legacy_fixture()
        self.path.write_bytes(content)
        first = self.cli(self.data, '--allow-missing-bunit', '--apply', '--journal', self.journal)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = self.path.read_bytes()
        repeat = self.cli(self.data, '--allow-missing-bunit')
        self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
        self.assertIn('unchanged=1', repeat.stdout)
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_review_manifest_reports_error_without_writing(self):
        malformed = {'schema': 'spec-v2-reviewed-date-anomalies-v1',
                     'directory': str(self.data), 'files': [{}]}
        self.manifest.write_text(json.dumps(malformed))
        result = self.cli(self.data, '--reviewed-date-anomalies', self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('errors=1', result.stdout)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_manifest_is_bound_to_exact_directory(self):
        self.approve(directory=self.base)
        result = self.cli(self.data, '--reviewed-date-anomalies', self.manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), self.content)


if __name__ == '__main__':
    unittest.main()
