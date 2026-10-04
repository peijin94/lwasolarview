"""Dependency-free metadata regression tests; never read production data."""
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools' / 'fix_spec_v2_bunit.py'


def card(key, value=None):
    text = key if value is None else f'{key:<8}= {value}'
    return text.ljust(80).encode('ascii')


def fixture(unit='jy', extra=(), long=False, telescope='EOVSA'):
    cards = [card('SIMPLE', 'T'), card('BITPIX', '-64'), card('NAXIS', '4'),
             card('NAXIS1', '4'), card('NAXIS2', '3'), card('NAXIS3', '1'),
             card('NAXIS4', '2'), card('EXTEND', 'T'),
             card('FILENAME', "'20230828.fits'"), card('ORIGIN', "'NJIT'"),
             card('TELESCOP', repr(telescope)), card('OBJ_ID', "'SUN'"),
             card('TYPE', '1'), card('DATE_OBS', "'2023-08-28T12:00:00'"),
             card('DATE_END', "'2023-08-28T23:00:00'"),
             card('FREQMIN', '0.015'), card('FREQMAX', '0.085'),
             card('POLARIZA', "'I'"), card('BTYPE', "'flx'"),
             card('BNAME', "'Flux Density'"), card('DATASUM', "'123'" )]
    if long:
        cards.extend(card('COMMENT', None) for _ in range(70))
    if unit is not None:
        cards.append(card('BUNIT', f"'{unit:<8}' / Data units"))
    cards.extend(extra)
    cards.append(card('END'))
    header = b''.join(cards)
    header += b' ' * (-len(header) % 2880)
    spectrum = b''.join(struct.pack('>d', value) for value in range(24))
    payload = spectrum + b'\0' * (-len(spectrum) % 2880)
    # Opaque extension bytes also must remain untouched.
    extension = card('XTENSION', "'BINTABLE'") + card('TUNIT1', "'Jy'") + card('END')
    extension = extension.ljust(2880, b' ') + bytes(range(256)) * 45
    return header + payload + extension, len(header)


class GeneratorTests(unittest.TestCase):
    def test_export_calls_only_set_metadata(self):
        tree = ast.parse((ROOT / 'generate_all_spectra.py').read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'tofits']
        self.assertEqual(len(calls), 3)
        class Spectrum:
            data = (0.125, -0.25, 1234.0)
            def tofits(self, path, *, spec_unit):
                self.asserted_unit = spec_unit
                self.exported = self.data
        for call in calls:
            with self.subTest(line=call.lineno):
                keywords = {kw.arg: kw.value for kw in call.keywords}
                self.assertIn('spec_unit', keywords)
                self.assertEqual(ast.literal_eval(keywords['spec_unit']), 'sfu')
                self.assertEqual(set(keywords), {'spec_unit'})
                self.assertEqual(len(call.args), 1)
                d = Spectrum()
                values = dict(d=d, os=os, fits_dir='/fixture', save_dir='/fixture',
                              year='2023', month='08', day='28')
                eval(compile(ast.Expression(call), '<export>', 'eval'), values)
                self.assertIs(d.data, d.exported)
                self.assertEqual(d.exported, (0.125, -0.25, 1234.0))

    def test_plot_calls_request_sfu(self):
        tree = ast.parse((ROOT / 'generate_all_spectra.py').read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'plot'
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == 'd']
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual({k.arg: ast.literal_eval(k.value) for k in call.keywords
                              if k.arg == 'spec_unit'}, {'spec_unit': 'sfu'})

    def test_standalone_plot_label(self):
        source = (ROOT / 'plot_spec_fits.py').read_text()
        self.assertIn('Flux density (SFU)', source)
        self.assertNotIn('Stokes I (Jy)', source)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.data = self.base / 'data'
        self.data.mkdir()
        self.path = self.data / '20230828.fits'
        self.journal = self.base / 'journal.jsonl'

    def run_cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)],
                                capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def write(self, **kwargs):
        content, size = fixture(**kwargs)
        self.path.write_bytes(content)
        return content, size

    def apply(self, **kwargs):
        return self.run_cli(self.data, '--apply', '--journal', self.journal, **kwargs)

    def test_dry_run_is_default_and_reports_counts(self):
        original, _ = self.write()
        result = self.run_cli(self.data)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.journal.exists())
        self.assertIn('would_change=1', result.stdout)
        self.assertIn('errors=0', result.stdout)
        self.assertIn('rejected=0', result.stdout)

    def test_apply_changes_only_one_card_and_preserves_all_payload(self):
        original, hsize = self.write(long=True)
        result = self.apply()
        changed = self.path.read_bytes()
        offset = original.index(b'BUNIT   =')
        self.assertEqual(changed[:offset], original[:offset])
        self.assertEqual(changed[offset + 80:], original[offset + 80:])
        self.assertEqual(changed[hsize:], original[hsize:])
        self.assertIn(b"'sfu", changed[offset:offset + 80])
        self.assertIn('sample', result.stdout.lower())
        records = [json.loads(line) for line in self.journal.read_text().splitlines()]
        backup = next(r for r in records if r['event'] == 'prepare')
        self.assertEqual(bytes.fromhex(backup['original_card_hex']), original[offset:offset + 80])
        self.assertEqual(backup['card_offset'], offset)
        self.run_cli('--verify', self.journal)

    def test_sfu_idempotence_and_capitalization(self):
        for i, unit in enumerate(['sfu', 'SFU', 'Sfu', 'Jy', 'JY']):
            with self.subTest(unit=unit):
                original, _ = self.write(unit=unit)
                journal = self.base / f'journal{i}.jsonl'
                self.run_cli(self.data, '--apply', '--journal', journal)
                changed = self.path.read_bytes()
                if unit == 'sfu':
                    self.assertEqual(changed, original)
                self.assertIn(b"'sfu", changed)
                again = self.run_cli(self.data)
                self.assertIn('unchanged=1', again.stdout)

    def test_minimal_bunit_string_and_comment_are_safe(self):
        original, _ = self.write()
        padded = card('BUNIT', "'jy      ' / Data units")
        for i, token in enumerate(["'jy' / Data units", "'Jy'", "'SFU' / Data units"]):
            content = original.replace(padded, card('BUNIT', token))
            self.path.write_bytes(content)
            self.run_cli(self.data, '--apply', '--journal', self.base / f'minimal{i}.jsonl')
            changed = self.path.read_bytes()
            self.assertEqual(len(changed), len(content))
            if 'Data units' in token:
                self.assertIn(b'Data units', changed)
            self.assertIn(b"'sfu'", changed)

    def test_rejections_never_write(self):
        cases = [dict(unit='K'), dict(unit=None), dict(unit='mJy'),
                 dict(extra=[card('BUNIT', "'jy'")]),
                 dict(extra=[card('PBCOR', 'F')]), dict(extra=[card('TDBCOR', 'T')]),
                 dict(extra=[card('CHECKSUM', "'0000000000000000'")]),
                 dict(telescope='FOREIGN'),
                 dict(extra=[card('BTYPE', "'Tb'")])]
        for i, kwargs in enumerate(cases):
            with self.subTest(kwargs=kwargs):
                original, _ = self.write(**kwargs)
                self.run_cli(self.data, success=False)
                self.run_cli(self.data, '--apply', '--journal', self.base / f'reject{i}.jsonl',
                             success=False)
                self.assertEqual(self.path.read_bytes(), original)

    def test_truncation_and_malformed_headers(self):
        original, hsize = fixture(long=True)
        cases = [original[:100], original[:hsize - 1], original[:-1],
                 original.replace(card('END'), card('COMMENT'), 1),
                 original.replace(card('NAXIS1', '4'), card('NAXIS1', '-4'), 1),
                 original.replace(card('BUNIT', "'jy      ' / Data units"),
                                  card('BUNIT', "'jy / missing closing quote"), 1)]
        for content in cases:
            with self.subTest(length=len(content)):
                self.path.write_bytes(content)
                self.run_cli(self.data, success=False)
                self.assertEqual(self.path.read_bytes(), content)

    def test_scope_and_symlink_and_hardlink(self):
        original, _ = self.write()
        for name in ['20230828_pbcor.fits', 'other.fits', '20230828.fits.gz']:
            (self.data / name).write_bytes(original)
        nested = self.data / 'nested'
        nested.mkdir()
        (nested / '20230828.fits').write_bytes(original)
        self.apply()
        for name in ['20230828_pbcor.fits', 'other.fits', '20230828.fits.gz']:
            self.assertEqual((self.data / name).read_bytes(), original)
        self.assertEqual((nested / '20230828.fits').read_bytes(), original)
        self.path.unlink()
        target = self.base / 'target.fits'
        target.write_bytes(original)
        self.path.symlink_to(target)
        self.run_cli(self.data, success=False)
        self.assertEqual(target.read_bytes(), original)
        self.path.unlink()
        os.link(target, self.path)
        self.run_cli(self.data, success=False)
        self.assertEqual(target.read_bytes(), original)

    def test_journal_safety(self):
        original, _ = self.write()
        self.run_cli(self.data, '--apply', success=False)
        self.run_cli(self.data, '--apply', '--journal', self.data / 'journal', success=False)
        self.journal.write_text('preexisting\n')
        self.apply(success=False)
        self.assertEqual(self.journal.read_text(), 'preexisting\n')
        self.assertEqual(self.path.read_bytes(), original)

    def test_verify_detects_card_payload_header_size_and_identity_changes(self):
        original, hsize = self.write()
        self.apply()
        corrected = self.path.read_bytes()
        mutations = [original, corrected[:hsize] + b'X' + corrected[hsize + 1:],
                     corrected.replace(b"'NJIT'", b"'NOPE'", 1), corrected + b'X']
        for content in mutations:
            self.path.write_bytes(content)
            self.run_cli('--verify', self.journal, success=False)
        self.path.write_bytes(corrected)
        self.run_cli('--verify', self.journal)
        replacement = self.base / 'replacement'
        replacement.write_bytes(corrected)
        replacement.replace(self.path)
        self.run_cli('--verify', self.journal, success=False)

    def test_rollback_defaults_to_preview_and_restores_exact_card(self):
        original, _ = self.write(unit='Jy', long=True)
        self.apply()
        corrected = self.path.read_bytes()
        self.run_cli('--rollback', self.journal)
        self.assertEqual(self.path.read_bytes(), corrected)
        rollback_journal = self.base / 'rollback.jsonl'
        self.run_cli('--rollback', self.journal, '--apply', '--journal', rollback_journal)
        self.assertEqual(self.path.read_bytes(), original)
        self.run_cli('--verify', rollback_journal)

    def test_empty_and_malformed_journal_fail(self):
        for text in ['', '{}\n', '{broken\n']:
            self.journal.write_text(text)
            self.run_cli('--verify', self.journal, success=False)

    def module(self):
        spec = importlib.util.spec_from_file_location('bunit_migration', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def invoke(self, module, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return module.main(list(map(str, args)))

    def test_backup_is_fsynced_before_write_and_complete_after_verification(self):
        original, _ = self.write()
        module = self.module()
        pwrite = module.os.pwrite
        fsync = module.os.fsync
        syncs = []
        def track_sync(fd):
            syncs.append(os.fstat(fd).st_ino)
            return fsync(fd)
        def inspect_write(fd, data, offset):
            self.assertEqual(self.path.read_bytes(), original)
            records = [json.loads(line) for line in self.journal.read_text().splitlines()]
            self.assertEqual([r['event'] for r in records], ['prepare'])
            self.assertIn(self.journal.stat().st_ino, syncs)
            self.assertEqual(data.hex(), records[0]['replacement_card_hex'])
            self.assertEqual(offset, records[0]['card_offset'])
            return pwrite(fd, data, offset)
        with mock.patch.object(module.os, 'fsync', side_effect=track_sync), \
                mock.patch.object(module.os, 'pwrite', side_effect=inspect_write):
            self.assertEqual(self.invoke(module, self.data, '--apply', '--journal', self.journal), 0)
        records = [json.loads(line) for line in self.journal.read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['prepare', 'complete'])

    def test_concurrent_change_after_backup_aborts_before_card_write(self):
        original, hsize = self.write()
        module = self.module()
        append = module.Journal.append
        def interfere(journal, record):
            append(journal, record)
            if record['event'] == 'prepare':
                with self.path.open('r+b') as f:
                    f.seek(hsize)
                    f.write(b'X')
        with mock.patch.object(module.Journal, 'append', new=interfere), \
                mock.patch.object(module.os, 'pwrite') as write:
            self.assertEqual(self.invoke(module, self.data, '--apply', '--journal', self.journal), 1)
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes()[:hsize], original[:hsize])
        self.assertTrue(self.journal.exists())
        self.run_cli('--verify', self.journal, success=False)

    def test_replacement_after_backup_aborts_before_card_write(self):
        original, _ = self.write()
        module = self.module()
        append = module.Journal.append
        def interfere(journal, record):
            append(journal, record)
            if record['event'] == 'prepare':
                replacement = self.base / 'replacement'
                replacement.write_bytes(original)
                replacement.replace(self.path)
        with mock.patch.object(module.Journal, 'append', new=interfere), \
                mock.patch.object(module.os, 'pwrite') as write:
            self.assertEqual(self.invoke(module, self.data, '--apply', '--journal', self.journal), 1)
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_lock_conflict_fails_and_does_not_write(self):
        import fcntl
        original, _ = self.write()
        with self.path.open('rb') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_cli(self.data, success=False)
            self.assertIn('errors=1', result.stdout)
        self.assertEqual(self.path.read_bytes(), original)

    def test_directory_symlink_and_journal_symlink_are_rejected(self):
        original, _ = self.write()
        alias = self.base / 'alias'
        alias.symlink_to(self.data, target_is_directory=True)
        self.run_cli(alias, success=False)
        target = self.base / 'target-journal'
        target.write_text('do not overwrite\n')
        self.journal.symlink_to(target)
        self.apply(success=False)
        self.assertEqual(target.read_text(), 'do not overwrite\n')
        self.assertEqual(self.path.read_bytes(), original)

    def test_directory_replacement_after_backup_aborts(self):
        original, _ = self.write()
        module = self.module()
        append = module.Journal.append
        moved = self.base / 'moved-data'
        def interfere(journal, record):
            append(journal, record)
            if record['event'] == 'prepare':
                self.data.rename(moved)
                self.data.mkdir()
                self.path.write_bytes(original)
        with mock.patch.object(module.Journal, 'append', new=interfere), \
                mock.patch.object(module.os, 'pwrite') as write:
            self.assertEqual(self.invoke(module, self.data, '--apply', '--journal', self.journal), 1)
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual((moved / self.path.name).read_bytes(), original)

    def test_sample_verification_does_not_claim_unsampled_bytes(self):
        content, hsize = self.write()
        # A larger opaque extension creates gaps between sampled windows.
        self.path.write_bytes(content + bytes(2880 * 40))
        self.apply()
        record = json.loads(self.journal.read_text().splitlines()[0])
        covered = [(s['offset'], s['offset'] + s['length']) for s in record['samples']]
        offset = next(i for i in range(hsize, self.path.stat().st_size)
                      if not any(start <= i < end for start, end in covered))
        with self.path.open('r+b') as f:
            f.seek(offset)
            f.write(b'X')
        result = self.run_cli('--verify', self.journal)
        self.assertIn('NOT a full checksum', result.stdout)

    def test_short_write_keeps_backup_and_returns_failure(self):
        original, _ = self.write()
        module = self.module()
        with mock.patch.object(module.os, 'pwrite', return_value=0):
            self.assertEqual(self.invoke(module, self.data, '--apply', '--journal', self.journal), 1)
        self.assertEqual(self.path.read_bytes(), original)
        records = [json.loads(line) for line in self.journal.read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['prepare'])

    def test_failed_journal_sync_prevents_any_fits_write(self):
        original, _ = self.write()
        module = self.module()
        fsync = module.os.fsync
        def fail_record_sync(fd):
            if os.fstat(fd).st_size and self.journal.exists() and os.fstat(fd).st_ino == self.journal.stat().st_ino:
                raise OSError('simulated journal fsync failure')
            return fsync(fd)
        with mock.patch.object(module.os, 'fsync', side_effect=fail_record_sync), \
                mock.patch.object(module.os, 'pwrite') as write:
            self.assertEqual(self.invoke(module, self.data, '--apply', '--journal', self.journal), 1)
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_apply_aborts_on_first_rejection_and_reports_unprocessed(self):
        self.write(unit='K')
        other = self.data / '20230829.fits'
        original, _ = fixture()
        other.write_bytes(original.replace(b'20230828', b'20230829').replace(b'2023-08-28', b'2023-08-29'))
        before = other.read_bytes()
        result = self.apply(success=False)
        self.assertIn('rejected=1', result.stdout)
        self.assertIn('unprocessed=1', result.stdout)
        self.assertEqual(other.read_bytes(), before)

    def test_pending_prepare_with_corrected_card_is_freshly_verifiable(self):
        self.write()
        self.apply()
        self.journal.write_text(self.journal.read_text().splitlines()[0] + '\n')
        result = self.run_cli('--verify', self.journal)
        self.assertIn('prepare without completion', result.stdout)


if __name__ == '__main__':
    unittest.main()
