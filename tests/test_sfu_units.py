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


def legacy_fixture(blank_before_end=False, end_last=False):
    """Real early writer's header signature with a small synthetic payload."""
    content, hsize = fixture(unit=None)
    cards = [content[i:i + 80] for i in range(0, content.index(card('END')), 80)
             if content[i:i + 8] not in (b'BTYPE   ', b'BNAME   ')]
    replacements = {
        b'FILENAME': card('FILENAME', "'/common/lwa/spec_v2/fits/20230828.fits'"),
        b'DATE_OBS': card('DATE_OBS', "'2023-08-28T15:29:34.473'"),
        b'DATE_END': card('DATE_END', "'2023-08-28T17:42:49.694'"),
        b'FREQMIN ': card('FREQMIN', '0.0290339'),
        b'FREQMAX ': card('FREQMAX', '0.0838718'),
    }
    cards = [replacements.get(c[:8], c) for c in cards]
    cards.extend([card('DATE', "'2024-07-11T00:00:00'"),
                  card('OBSERVER', "'EOVSA Team'"), card('XCEN', '0'),
                  card('YCEN', '0'), card('RESOLUTI', '0')])
    if blank_before_end:
        cards.append(b' ' * 80)
    if end_last:
        cards.extend(card('COMMENT') for _ in range(35 - len(cards)))
    cards.append(card('END'))
    header = b''.join(cards).ljust(hsize, b' ')
    return header + content[hsize:], hsize


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

    def test_recording_day_allows_midnight_and_short_next_ut_day(self):
        original, _ = fixture()
        for start, end in [('2023-08-28T15:00:00', '2023-08-29T06:00:00'),
                           ('2023-08-29T02:40:00.133', '2023-08-29T02:44:59.812'),
                           ('2023-08-28T00:00:00', '2023-08-29T12:00:00')]:
            with self.subTest(start=start, end=end):
                content = original.replace(card('DATE_OBS', "'2023-08-28T12:00:00'"),
                                           card('DATE_OBS', repr(start)))
                content = content.replace(card('DATE_END', "'2023-08-28T23:00:00'"),
                                          card('DATE_END', repr(end)))
                self.path.write_bytes(content)
                result = self.run_cli(self.data)
                self.assertIn('would_change=1', result.stdout)
                self.assertEqual(self.path.read_bytes(), content)

    def test_unrelated_dates_reversed_times_and_over_36h_fail(self):
        original, _ = fixture()
        for start, end in [('2023-08-27T23:00:00', '2023-08-28T06:00:00'),
                           ('2023-08-28T15:00:00', '2023-08-30T02:00:00'),
                           ('2023-08-30T02:00:00', '2023-08-30T02:05:00'),
                           ('2023-08-29T02:00:00', '2023-08-28T23:00:00'),
                           ('2023-08-28T00:00:00', '2023-08-29T12:00:01')]:
            with self.subTest(start=start, end=end):
                content = original.replace(card('DATE_OBS', "'2023-08-28T12:00:00'"),
                                           card('DATE_OBS', repr(start)))
                content = content.replace(card('DATE_END', "'2023-08-28T23:00:00'"),
                                          card('DATE_END', repr(end)))
                self.path.write_bytes(content)
                self.run_cli(self.data, success=False)

    def test_historical_absolute_filename_only_matches_basename(self):
        original, _ = fixture()
        absolute = card('FILENAME', "'/common/lwa/spec_v2/fits/20230828.fits'")
        self.path.write_bytes(original.replace(card('FILENAME', "'20230828.fits'"), absolute))
        self.run_cli(self.data)
        self.path.write_bytes(original.replace(card('FILENAME', "'20230828.fits'"),
                                             card('FILENAME', "'/common/lwa/spec_v2/fits/20230829.fits'")))
        self.run_cli(self.data, success=False)

    def test_legacy_missing_unit_requires_flag_even_in_dryrun(self):
        original, _ = legacy_fixture()
        self.path.write_bytes(original)
        self.run_cli(self.data, success=False)
        self.apply(success=False)
        result = self.run_cli(self.data, '--allow-missing-bunit')
        self.assertIn('would_change=1', result.stdout)
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_insertion_verify_and_exact_rollback(self):
        for i, blank in enumerate([False, True]):
            with self.subTest(blank_before_end=blank):
                original, hsize = legacy_fixture(blank_before_end=blank)
                self.path.write_bytes(original)
                journal = self.base / f'insert{i}.jsonl'
                self.run_cli(self.data, '--allow-missing-bunit', '--apply', '--journal', journal)
                changed = self.path.read_bytes()
                record = json.loads(journal.read_text().splitlines()[0])
                span = 80 if blank else 160
                self.assertEqual(record['span_length'], span)
                offset = record['card_offset']
                self.assertEqual(bytes.fromhex(record['original_card_hex']), original[offset:offset + span])
                self.assertEqual(changed[:offset], original[:offset])
                self.assertEqual(changed[offset + span:], original[offset + span:])
                self.assertEqual(changed[hsize:], original[hsize:])
                self.assertEqual(len(changed), len(original))
                self.assertEqual(changed.count(b'BUNIT   ='), 1)
                self.run_cli('--verify', journal)
                for mutated in [changed.replace(b"'NJIT'", b"'NOPE'", 1),
                                changed[:hsize] + b'X' + changed[hsize + 1:]]:
                    self.path.write_bytes(mutated)
                    self.run_cli('--verify', journal, success=False)
                self.path.write_bytes(changed)
                self.run_cli('--rollback', journal)
                self.assertEqual(self.path.read_bytes(), changed)
                rollback = self.base / f'restore{i}.jsonl'
                self.run_cli('--rollback', journal, '--apply', '--journal', rollback)
                self.assertEqual(self.path.read_bytes(), original)
                self.run_cli('--verify', rollback)

    def test_legacy_insertion_rejects_no_room_and_unsafe_padding(self):
        original, _ = legacy_fixture(end_last=True)
        normal, _ = legacy_fixture()
        end = normal.index(card('END'))
        unsafe_after = normal[:end + 80] + b'X' + normal[end + 81:]
        blank, _ = legacy_fixture(blank_before_end=True)
        before = blank.index(card('END')) - 80
        unsafe_before = blank[:before] + b' ' * 8 + b'X' + blank[before + 9:]
        for content in [original, unsafe_after, unsafe_before]:
            self.path.write_bytes(content)
            self.run_cli(self.data, '--allow-missing-bunit', success=False)
            self.assertEqual(self.path.read_bytes(), content)

    def test_allow_missing_does_not_relax_existing_units_or_correction_guards(self):
        legacy, _ = legacy_fixture()
        end = legacy.index(card('END'))
        for unit in ['jy', 'sfu', 'K']:
            content = legacy[:end] + card('BUNIT', repr(unit)) + legacy[end:-80]
            self.path.write_bytes(content)
            self.run_cli(self.data, '--allow-missing-bunit', success=False)
        for key in ['PBCOR', 'TDBCOR', 'CHECKSUM']:
            content = legacy[:end] + card(key, "'present'") + legacy[end:-80]
            self.path.write_bytes(content)
            self.run_cli(self.data, '--allow-missing-bunit', success=False)

    def test_old_v1_journal_readback_and_rollback(self):
        original, _ = self.write()
        self.apply()
        records = [json.loads(line) for line in self.journal.read_text().splitlines()]
        for record in records:
            record['schema'] = 'spec-v2-bunit-card-v1'
            if record['event'] == 'prepare':
                record['header_outside_card_sha256'] = record.pop('header_outside_span_sha256')
                del record['span_length']
                del record['legacy_header']
        self.journal.write_text(''.join(json.dumps(record) + '\n' for record in records))
        self.run_cli('--verify', self.journal)
        self.run_cli('--rollback', self.journal, '--apply', '--journal', self.base / 'v1-rollback.jsonl')
        self.assertEqual(self.path.read_bytes(), original)

    def test_recorded_real_short_nextdate_product(self):
        original, _ = fixture()
        content = original.replace(b'20230828.fits', b'20240407.fits')
        content = content.replace(card('DATE_OBS', "'2023-08-28T12:00:00'"),
                                  card('DATE_OBS', "'2024-04-08T02:40:00.133'"))
        content = content.replace(card('DATE_END', "'2023-08-28T23:00:00'"),
                                  card('DATE_END', "'2024-04-08T02:44:59.812'"))
        self.path = self.data / '20240407.fits'
        self.path.write_bytes(content)
        self.apply()
        corrected = self.path.read_bytes()
        self.assertIn(card('DATE_OBS', "'2024-04-08T02:40:00.133'"), corrected)
        self.assertIn(card('DATE_END', "'2024-04-08T02:44:59.812'"), corrected)

    def test_legacy_blank_before_end_works_without_space_after_end(self):
        content, _ = legacy_fixture(blank_before_end=True, end_last=True)
        self.path.write_bytes(content)
        self.run_cli(self.data, '--allow-missing-bunit', '--apply', '--journal', self.journal)
        record = json.loads(self.journal.read_text().splitlines()[0])
        self.assertEqual(record['span_length'], 80)
        self.run_cli('--verify', self.journal)

    def test_insertion_journal_rejects_invalid_span_and_legacy_flag(self):
        content, _ = legacy_fixture()
        self.path.write_bytes(content)
        self.run_cli(self.data, '--allow-missing-bunit', '--apply', '--journal', self.journal)
        records = [json.loads(line) for line in self.journal.read_text().splitlines()]
        for field, value in [('span_length', 80), ('legacy_header', False),
                             ('original_card_hex', (b'X' * 160).hex())]:
            altered = [dict(r) for r in records]
            altered[0][field] = value
            self.journal.write_text(''.join(json.dumps(r) + '\n' for r in altered))
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
