#!/usr/bin/env python3
"""Repair only PRIMARY BUNIT in known historical spec_v2 daily spectra.

POSIX, Python stdlib only. Preview is the default. No FITS serializer, array
arithmetic, recursion, or extension edits. Verification covers the full primary
header and deterministic payload samples, NOT a full payload checksum.
See docs/units.md for provenance requirements, recovery, and lock limitations.
"""
import argparse
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import uuid

BLOCK = 2880
CARD = 80
MAX_HEADER = 16 * 1024 * 1024
NAME = re.compile(r'[0-9]{8}\.fits\Z')
KEYWORD = re.compile(r'[A-Z0-9_-]{1,8}\Z')
OLD_SCHEMA = 'spec-v2-bunit-card-v1'
SCHEMA = 'spec-v2-bunit-span-v2'
BLANK = b' ' * CARD
END = b'END'.ljust(CARD, b' ')
SFU_CARD = b"BUNIT   = 'sfu     ' / Data units".ljust(CARD, b' ')


class Rejected(ValueError):
    """File is outside the supported product/format contract."""


class SafetyError(RuntimeError):
    """Identity, concurrency, journal, or verification check failed."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_exact(fd, length, offset):
    result = bytearray()
    while len(result) < length:
        part = os.pread(fd, length - len(result), offset + len(result))
        if not part:
            raise Rejected(f'truncated file at byte {offset + len(result)}')
        result.extend(part)
    return bytes(result)


def stamp(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def value_of(raw):
    """Strict scalar FITS parser; reject unhandled/ambiguous value syntax."""
    text = raw.decode('ascii')
    if text[8:10] != '= ':
        raise Rejected('value card must have "= " in columns 9-10')
    tail = text[10:].lstrip()
    if tail.startswith("'"):
        match = re.match(r"'((?:[^']|'')*)'", tail)
        if not match:
            raise Rejected('unterminated FITS string')
        rest = tail[match.end():].lstrip()
        if rest and not rest.startswith('/'):
            raise Rejected('invalid text after FITS string')
        return match.group(1).replace("''", "'").rstrip()
    token = tail.split('/', 1)[0].strip()
    if token == 'T':
        return True
    if token == 'F':
        return False
    if re.fullmatch(r'[+-]?[0-9]+', token):
        return int(token)
    if re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[EDed][+-]?[0-9]+)?', token):
        value = float(token.upper().replace('D', 'E'))
        if math.isfinite(value):
            return value
    raise Rejected(f'unsupported or malformed FITS scalar: {token!r}')


def read_header(fd):
    """Read every primary-header block through END, including padding."""
    blocks, values, offsets = [], {}, {}
    while (len(blocks) + 1) * BLOCK <= MAX_HEADER:
        start = len(blocks) * BLOCK
        block = read_exact(fd, BLOCK, start)
        blocks.append(block)
        for index in range(0, BLOCK, CARD):
            raw = block[index:index + CARD]
            if any(byte < 32 or byte > 126 for byte in raw):
                raise Rejected('non-ASCII/nonprintable primary header')
            key = raw[:8].decode('ascii').rstrip()
            if key == 'END':
                if raw != b'END'.ljust(CARD, b' '):
                    raise Rejected('malformed END card')
                if block[index + CARD:] != b' ' * (BLOCK - index - CARD):
                    raise Rejected('nonblank primary-header padding after END')
                offsets['END'] = start + index
                return b''.join(blocks), values, offsets
            if not key:
                if raw != b' ' * CARD:
                    raise Rejected('malformed blank card')
                continue
            if not KEYWORD.fullmatch(key) or raw[:8] != key.encode().ljust(8, b' '):
                raise Rejected('malformed FITS keyword')
            if key in ('COMMENT', 'HISTORY'):
                if raw[8:10] == b'= ':
                    raise Rejected('malformed commentary card')
                continue
            if key in values:
                raise Rejected(f'duplicate {key} card')
            values[key] = value_of(raw)
            offsets[key] = start + index
    raise Rejected('primary header has no END within safety limit')


def valid_name(name):
    if not NAME.fullmatch(name):
        raise Rejected('not an exact YYYYMMDD.fits filename')
    try:
        datetime.datetime.strptime(name[:8], '%Y%m%d')
    except ValueError as exc:
        raise Rejected('invalid calendar date in filename') from exc


def inspect(fd, name, size, allow_missing_bunit=False, legacy_header=False, date_anomaly_hash=None):
    valid_name(name)
    header, values, offsets = read_header(fd)
    for key in ('PBCOR', 'TDBCOR', 'CHECKSUM'):
        if key in values:
            raise Rejected(f'{key} present; corrected/checksummed products require separate handling')
    missing = 'BUNIT' not in values
    if missing and not (allow_missing_bunit or legacy_header):
        raise Rejected('absent primary BUNIT')
    unit = values.get('BUNIT')
    if not missing and (not isinstance(unit, str) or unit.lower() not in ('jy', 'sfu')):
        raise Rejected(f'incompatible primary BUNIT: {unit!r}')
    reviewed_date_anomaly = date_anomaly_hash is not None
    if reviewed_date_anomaly:
        if missing or outside_hash(header, offsets['BUNIT']) != date_anomaly_hash:
            raise Rejected('reviewed date-anomaly header digest mismatch')
    required = {'SIMPLE': True, 'NAXIS': 4, 'NAXIS3': 1, 'EXTEND': True,
                'ORIGIN': 'NJIT', 'OBJ_ID': 'SUN', 'TYPE': 1, 'POLARIZA': 'I'}
    # Only an explicitly approved absent-unit product (or its journal-proven
    # inserted/restored form) may omit these legacy writer cards.
    # The exact inserted card is recognizable for an explicitly approved rerun;
    # arbitrary existing-unit legacy cards still require journal provenance.
    inserted_sfu = (unit == 'sfu' and not missing
                    and header[offsets['BUNIT']:offsets['BUNIT'] + CARD] == SFU_CARD)
    legacy = missing or (legacy_header and unit == 'sfu') or (allow_missing_bunit and inserted_sfu)
    for key, expected in {'BTYPE': 'flx', 'BNAME': 'Flux Density'}.items():
        if not legacy or key in values:
            required[key] = expected
    for key, expected in required.items():
        if values.get(key) != expected or type(values.get(key)) is not type(expected):
            raise Rejected(f'foreign/unsupported product: {key} must be {expected!r}')
    filename = values.get('FILENAME')
    if (not isinstance(filename, str) or Path(filename).name != name
            or (filename != name and not Path(filename).is_absolute())):
        raise Rejected('foreign/unsupported product: FILENAME basename must match daily filename')
    # Dspec.tofits historically uses its EOVSA default even for LWA input.
    # Telescope alone cannot establish provenance; restrict shape, frequencies,
    # dates and the other writer-specific primary cards as well.
    if values.get('TELESCOP') not in ('EOVSA', 'LWA', 'OVRO-LWA'):
        raise Rejected('foreign/unsupported telescope')
    if values.get('BITPIX') not in (-32, -64) or type(values['BITPIX']) is not int:
        raise Rejected('expected uncompressed floating point primary spectrum')
    expected_order = ['SIMPLE', 'BITPIX', 'NAXIS'] + [f'NAXIS{i}' for i in range(1, 5)]
    if any(offsets.get(key) != i * CARD for i, key in enumerate(expected_order)):
        raise Rejected('malformed primary structural card ordering')
    for key in ('BSCALE', 'BZERO', 'GROUPS', 'PCOUNT', 'GCOUNT', 'XTENSION'):
        if key in values:
            raise Rejected(f'unsupported primary structure: {key}')
    elements = 1
    for i in range(1, 5):
        n = values.get(f'NAXIS{i}')
        if type(n) is not int or n <= 0:
            raise Rejected('invalid/nonpositive primary axis length')
        elements *= n
    if values['NAXIS4'] not in (1, 2):
        raise Rejected('unsupported historical Stokes axis')
    recording_day = datetime.datetime.strptime(name[:8], '%Y%m%d').date()
    dates = []
    for key in ('DATE_OBS', 'DATE_END'):
        try:
            date = datetime.datetime.fromisoformat(values[key])
            if 'T' not in values[key] or (date.utcoffset() not in (None, datetime.timedelta(0))):
                raise ValueError('expected UTC observation timestamp')
            date = date.replace(tzinfo=None)
        except (KeyError, TypeError, ValueError) as exc:
            raise Rejected(f'malformed/missing {key}') from exc
        if not reviewed_date_anomaly and date.date() not in (recording_day, recording_day + datetime.timedelta(days=1)):
            raise Rejected(f'{key} outside recording day and next UT day')
        dates.append(date)
    if not reviewed_date_anomaly and not datetime.timedelta(0) <= dates[1] - dates[0] <= datetime.timedelta(hours=36):
        raise Rejected('observation times must be ordered and span at most 36 hours')
    fmin, fmax = values.get('FREQMIN'), values.get('FREQMAX')
    if (type(fmin) not in (int, float) or type(fmax) not in (int, float)
            or not 0.010 <= fmin <= fmax <= 0.100):
        raise Rejected('foreign/unsupported spectrum frequencies (expected LWA GHz range)')
    data_size = elements * (abs(values['BITPIX']) // 8)
    padded = ((data_size + BLOCK - 1) // BLOCK) * BLOCK
    if size % BLOCK or size < len(header) + padded:
        raise Rejected('truncated/misaligned FITS file or primary payload')
    return header, offsets.get('BUNIT'), unit


def insertion_span(header):
    """Consume only an existing blank card, or END plus its following blank."""
    end = next(i for i in range(0, len(header), CARD) if header[i:i + CARD] == END)
    blanks = [i for i in range(0, end, CARD) if header[i:i + CARD] == BLANK]
    if blanks:
        return blanks[-1], SFU_CARD
    if header[end + CARD:end + 2 * CARD] == BLANK:
        return end, SFU_CARD + END
    raise Rejected('no safe primary-header padding for BUNIT insertion without header growth')


def corrected_card(raw, unit):
    if unit == 'sfu':
        return raw
    text = raw.decode('ascii')
    match = re.match(r"BUNIT   =\s*'([^']*)'", text)
    if not match:
        raise Rejected('unsafe BUNIT string layout')
    start, end = match.span(1)
    inner = 'sfu'.ljust(max(3, end - start))
    changed = text[:start] + inner + text[end:]
    growth = len(changed) - CARD
    if growth:
        # Keep the closing quote/comment column fixed when spare space permits.
        tail_start = start + len(inner) + 1
        if changed[tail_start:tail_start + growth] == ' ' * growth:
            changed = changed[:tail_start] + changed[tail_start + growth:]
        else:
            changed = changed.rstrip().ljust(CARD)
    if len(changed) != CARD:
        raise Rejected('BUNIT normalization would overflow 80-byte card')
    result = changed.encode('ascii')
    if value_of(result) != 'sfu':
        raise Rejected('BUNIT normalization failed')
    return result


def outside_hash(header, offset, span_length=CARD):
    return digest(header[:offset] + header[offset + span_length:])


def sample_regions(header_size, file_size):
    """At most five 4096-byte windows across all bytes after PRIMARY header."""
    payload_size = file_size - header_size
    length = min(4096, payload_size)
    spread = payload_size - length
    return [(header_size + start, length) for start in
            sorted({spread * i // 4 for i in range(5)})]


def samples(fd, header_size, file_size):
    return [{'offset': offset, 'length': length,
             'sha256': digest(read_exact(fd, length, offset))}
            for offset, length in sample_regions(header_size, file_size)]


def directory(path):
    path = Path(os.path.abspath(path))
    if path != path.resolve():
        raise SafetyError('directory path must not contain symlinks')
    return path


@contextmanager
def open_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def check_identity(fd, dir_fd, name, initial):
    current = os.fstat(fd)
    entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if (not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1
            or stamp(current) != stamp(initial) or stamp(entry) != stamp(initial)):
        raise SafetyError('file identity/size/timestamps changed concurrently')


@contextmanager
def locked_file(dir_fd, name, write=False):
    initial = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise Rejected('requires a regular, non-symlink file with one hard link')
    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        check_identity(fd, dir_fd, name, initial)
        yield fd, initial
    finally:
        os.close(fd)


class Journal:
    def __init__(self, path, data_dirs):
        self.path = Path(os.path.abspath(path))
        if self.path != self.path.resolve():
            raise SafetyError('journal path must not contain symlinks')
        if any(self.path == d or d in self.path.parents for d in data_dirs):
            raise SafetyError('journal must be outside every data directory')
        # Refuse to clobber or append to any previous journal.
        with open_directory(self.path.parent) as parent_fd:
            self.fd = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=parent_fd)
            self.parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.identity = (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino)
            os.fsync(self.fd)
            with open_directory(self.path.parent) as parent_fd:
                self.check_path()
                os.fsync(parent_fd)  # Persist the new journal directory entry too.
        except BaseException:
            os.close(self.fd)
            raise

    def check_path(self):
        if directory(self.path.parent) != self.path.parent:
            raise SafetyError('journal directory path changed')
        parent_st = os.stat(self.path.parent, follow_symlinks=False)
        st = os.stat(self.path, follow_symlinks=False)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or (st.st_dev, st.st_ino) != self.identity
                or (parent_st.st_dev, parent_st.st_ino) != self.parent_identity):
            raise SafetyError('journal path identity changed')

    def append(self, record):
        self.check_path()
        data = (json.dumps(record, sort_keys=True, allow_nan=False) + '\n').encode('ascii')
        while data:
            count = os.write(self.fd, data)
            if count <= 0:
                raise SafetyError('short journal write')
            data = data[count:]
        os.fsync(self.fd)
        self.check_path()

    def close(self):
        os.close(self.fd)


def backup_record(path, dir_fd, st, header, offset, replacement, payload_samples, legacy_header=False, date_anomaly_hash=None):
    dst = os.fstat(dir_fd)
    return dict(schema=SCHEMA, event='prepare', id=uuid.uuid4().hex,
                path=str(path), directory_device=dst.st_dev, directory_inode=dst.st_ino,
                device=st.st_dev, inode=st.st_ino, size=st.st_size,
                mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns,
                header_size=len(header), card_offset=offset, span_length=len(replacement),
                legacy_header=legacy_header, date_anomaly_header_sha256=date_anomaly_hash,
                original_card_hex=header[offset:offset + len(replacement)].hex(),
                replacement_card_hex=replacement.hex(),
                header_outside_span_sha256=outside_hash(header, offset, len(replacement)),
                samples=payload_samples,
                verification='full primary header outside edited span; deterministic payload samples only',
                utc=datetime.datetime.now(datetime.timezone.utc).isoformat())


def check_record(fd, dir_fd, name, st, record, expected_card):
    dst = os.fstat(dir_fd)
    parent_path = directory(Path(record['path']).parent)
    path_dst = os.stat(parent_path, follow_symlinks=False)
    if (path_dst.st_dev, path_dst.st_ino) != (dst.st_dev, dst.st_ino):
        raise SafetyError('data directory path identity changed')
    if (st.st_dev, st.st_ino, st.st_size, dst.st_dev, dst.st_ino) != (
            record['device'], record['inode'], record['size'],
            record['directory_device'], record['directory_inode']):
        raise SafetyError('journal file/directory identity or file size mismatch')
    header, _, _ = inspect(fd, name, st.st_size, legacy_header=record['legacy_header'],
                           date_anomaly_hash=record.get('date_anomaly_header_sha256'))
    offset, length = record['card_offset'], record['span_length']
    if len(header) != record['header_size']:
        raise SafetyError('journal header layout mismatch')
    if header[offset:offset + length] != expected_card:
        raise SafetyError('journal target header span mismatch')
    if outside_hash(header, offset, length) != record['header_outside_span_sha256']:
        raise SafetyError('primary header outside edited span changed')
    if samples(fd, len(header), st.st_size) != record['samples']:
        raise SafetyError('deterministic payload sample mismatch (not a full checksum)')
    check_identity(fd, dir_fd, name, st)
    return header


def write_card(fd, dir_fd, name, st, record, journal):
    original = bytes.fromhex(record['original_card_hex'])
    replacement = bytes.fromhex(record['replacement_card_hex'])
    journal.append(record)  # Exact backup is durable BEFORE touching the FITS.
    before_header = check_record(fd, dir_fd, name, st, record, original)
    written = os.pwrite(fd, replacement, record['card_offset'])
    if written != record['span_length']:
        raise SafetyError('short BUNIT write; retain journal for recovery')
    os.fsync(fd)
    after = os.fstat(fd)
    after_header = check_record(fd, dir_fd, name, after, record, replacement)
    offset = record['card_offset']
    if after_header != before_header[:offset] + replacement + before_header[offset + record['span_length']:]:
        raise SafetyError('primary header bytes outside target span changed')
    journal.append(dict(schema=SCHEMA, event='complete', id=record['id']))


def validate_spans(old, new, length, legacy):
    if len(old) != length or len(new) != length or old == new:
        raise ValueError('invalid journal span length/transition')
    if legacy:
        pairs = [(BLANK, SFU_CARD), (END + BLANK, SFU_CARD + END)]
        if (old, new) not in pairs and (new, old) not in pairs:
            raise ValueError('invalid legacy insertion/restoration span')
    else:
        for c in (old, new):
            if length != CARD or c[:8] != b'BUNIT   ' or value_of(c).lower() not in ('jy', 'sfu'):
                raise ValueError('invalid journal BUNIT card')
        if value_of(old) != 'sfu' and value_of(new) != 'sfu':
            raise ValueError('invalid journal unit transition')


def load_journal(path):
    """Accept only complete JSONL lines and validated records from this tool."""
    path = Path(os.path.abspath(path))
    if path != path.resolve():
        raise SafetyError('journal path must not contain symlinks')
    with open_directory(path.parent) as dir_fd:
        with locked_file(dir_fd, path.name) as (fd, st):
            raw = read_exact(fd, st.st_size, 0)
            check_identity(fd, dir_fd, path.name, st)
    if not raw or not raw.endswith(b'\n'):
        raise SafetyError('empty or incomplete journal; preserve it for manual recovery')
    records, seen, completed, paths = [], set(), set(), set()
    try:
        for line in raw.splitlines():
            r = json.loads(line)
            if r['schema'] not in (SCHEMA, OLD_SCHEMA) or not isinstance(r['id'], str):
                raise ValueError('unknown journal schema/id')
            if r['event'] == 'complete':
                if r['id'] not in seen or r['id'] in completed:
                    raise ValueError('invalid completion record')
                completed.add(r['id'])
                continue
            if r['event'] != 'prepare' or r['id'] in seen:
                raise ValueError('invalid prepare record')
            if r['schema'] == OLD_SCHEMA:
                r['span_length'] = CARD
                r['legacy_header'] = False
                r['header_outside_span_sha256'] = r['header_outside_card_sha256']
            p = Path(r['path'])
            if not p.is_absolute() or str(p) != r['path'] or '..' in p.parts:
                raise ValueError('noncanonical journal data path')
            valid_name(p.name)
            if r['path'] in paths:
                raise ValueError('duplicate journal data path')
            for key in ('device', 'inode', 'size', 'directory_device', 'directory_inode',
                        'header_size', 'card_offset', 'span_length', 'mtime_ns', 'ctime_ns'):
                if type(r[key]) is not int or r[key] < 0:
                    raise ValueError(f'invalid journal {key}')
            hsize, offset, size = r['header_size'], r['card_offset'], r['size']
            length = r['span_length']
            if (not 0 < hsize <= MAX_HEADER or hsize % BLOCK or size <= hsize
                    or size % BLOCK or offset % CARD or length not in (CARD, 2 * CARD)
                    or offset + length > hsize or type(r['legacy_header']) is not bool):
                raise ValueError('invalid journal offsets/sizes')
            old, new = bytes.fromhex(r['original_card_hex']), bytes.fromhex(r['replacement_card_hex'])
            validate_spans(old, new, length, r['legacy_header'])
            if (r.get('date_anomaly_header_sha256') is not None
                    and not re.fullmatch('[0-9a-f]{64}', r['date_anomaly_header_sha256'])):
                raise ValueError('invalid reviewed date-anomaly digest')
            if not re.fullmatch('[0-9a-f]{64}', r['header_outside_span_sha256']):
                raise ValueError('invalid journal header digest')
            expected_regions = sample_regions(hsize, size)
            if [(s['offset'], s['length']) for s in r['samples']] != expected_regions:
                raise ValueError('invalid journal sample regions')
            if any(not re.fullmatch('[0-9a-f]{64}', s['sha256']) for s in r['samples']):
                raise ValueError('invalid journal sample digest')
            seen.add(r['id'])
            paths.add(r['path'])
            records.append(r)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SafetyError(f'malformed journal: {exc}') from exc
    if not records:
        raise SafetyError('journal contains no prepared changes')
    return records, completed


def reviewed_date_anomalies(path, data_dir):
    """Explicitly reviewed exact headers only; never an automatic date bypass."""
    if path is None:
        return {}
    manifest = json.loads(Path(path).read_text())
    if not isinstance(manifest, dict):
        raise SafetyError('reviewed date-anomaly manifest must be an object')
    if (manifest.get('schema') != 'spec-v2-reviewed-date-anomalies-v1'
            or manifest.get('directory') != str(data_dir)
            or not isinstance(manifest.get('files'), list)):
        raise SafetyError('reviewed date-anomaly manifest schema/directory mismatch')
    approved = {}
    for record in manifest['files']:
        if not isinstance(record, dict) or not isinstance(record.get('filename'), str):
            raise SafetyError('reviewed date-anomaly record requires a filename')
        name = record['filename']
        valid_name(name)
        sha = record.get('header_outside_bunit_sha256')
        if (name in approved or not isinstance(sha, str)
                or not re.fullmatch('[0-9a-f]{64}', sha)
                or not isinstance(record.get('reason'), str) or not record['reason'].strip()):
            raise SafetyError('invalid or duplicate reviewed date-anomaly record')
        approved[name] = sha
    return approved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?', help='explicit historical spec_v2 FITS directory')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify', metavar='JOURNAL', help='read-only verification of prepared files')
    mode.add_argument('--rollback', metavar='JOURNAL', help='preview restoring exact original cards')
    parser.add_argument('--apply', action='store_true', help='perform migration or rollback (default: preview)')
    parser.add_argument('--journal', help='NEW durable JSONL outside data directories; required with --apply')
    parser.add_argument('--allow-missing-bunit', action='store_true',
                        help='explicitly approve legacy absent-unit headers; insert SFU using existing padding only')
    parser.add_argument('--reviewed-date-anomalies', metavar='MANIFEST',
                        help='explicit hash-bound exceptions for reviewed corrupt timestamps; no date changes')
    args = parser.parse_args(argv)
    if args.reviewed_date_anomalies and (args.verify or args.rollback):
        parser.error('verify/rollback use only anomaly approval recorded in their journal')
    if args.verify and (args.directory or args.apply or args.journal or args.allow_missing_bunit):
        parser.error('--verify accepts only a journal, with no mutation options/directory')
    if args.rollback and args.directory:
        parser.error('--rollback uses only paths recorded in its journal')
    if args.rollback and args.allow_missing_bunit:
        parser.error('--rollback uses the legacy format approval recorded in its journal')
    if not (args.verify or args.rollback or args.directory):
        parser.error('supply an explicit data directory, --verify, or --rollback')
    if args.apply and not args.journal:
        parser.error('--apply requires --journal outside the data directory')
    if args.journal and not args.apply:
        parser.error('--journal is only used with --apply')
    counts = dict(candidates=0, processed=0, ignored=0, would_change=0, changed=0,
                  unchanged=0, verified=0, rejected=0, errors=0)
    journal = None
    label = 'VERIFY' if args.verify else ('ROLLBACK' if args.rollback else 'MIGRATE')
    print(f'{label} {"APPLY" if args.apply else "DRY RUN" if not args.verify else "READ ONLY"}')
    print('Verification: full primary header; deterministic payload samples only, NOT a full checksum.')
    try:
        if args.verify or args.rollback:
            records, completed = load_journal(args.verify or args.rollback)
            counts['candidates'] = len(records)
            data_dirs = {directory(Path(r['path']).parent) for r in records}
            if args.apply:
                journal = Journal(args.journal, data_dirs)
            for record in records:
                path = Path(record['path'])
                try:
                    with open_directory(directory(path.parent)) as dir_fd:
                        with locked_file(dir_fd, path.name, args.apply) as (fd, st):
                            target = bytes.fromhex(record['replacement_card_hex'])
                            header = check_record(fd, dir_fd, path.name, st, record, target)
                            pending = ' (prepare without completion; freshly verified)' if record['id'] not in completed else ''
                            if args.verify:
                                counts['verified'] += 1
                                print(f'VERIFIED {path}: header + {len(record["samples"])} payload samples{pending}')
                            else:
                                counts['would_change'] += 1
                                print(f'RESTORE {path}: exact original {record["span_length"]}-byte header span{pending}')
                                if args.apply:
                                    restored = backup_record(path, dir_fd, st, header, record['card_offset'],
                                                             bytes.fromhex(record['original_card_hex']), record['samples'],
                                                             legacy_header=record['legacy_header'],
                                                             date_anomaly_hash=record.get('date_anomaly_header_sha256'))
                                    write_card(fd, dir_fd, path.name, st, restored, journal)
                                    counts['changed'] += 1
                                    counts['verified'] += 1
                                    print(f'RESTORED {path}: primary header bytes and '
                                          f'{len(record["samples"])} payload samples verified')
                    counts['processed'] += 1
                except (OSError, ValueError, SafetyError) as exc:
                    key = 'rejected' if isinstance(exc, Rejected) else 'errors'
                    counts[key] += 1
                    counts['processed'] += 1
                    print(f'{key.upper()} {path}: {exc}', file=sys.stderr)
                    if args.apply:
                        break
        else:
            data_dir = directory(args.directory)
            approved_hashes = reviewed_date_anomalies(args.reviewed_date_anomalies, data_dir)
            with open_directory(data_dir) as dir_fd:
                names = sorted(os.listdir(dir_fd))
                candidates = [name for name in names if NAME.fullmatch(name)]
                if set(approved_hashes) - set(candidates):
                    raise SafetyError('reviewed anomaly manifest contains files outside selected candidates')
                counts['candidates'] = len(candidates)
                counts['ignored'] = len(names) - len(candidates)
                if args.apply:
                    journal = Journal(args.journal, {data_dir})
                for name in candidates:
                    path = data_dir / name
                    try:
                        with locked_file(dir_fd, name, args.apply) as (fd, st):
                            header, offset, unit = inspect(fd, name, st.st_size, args.allow_missing_bunit,
                                                          date_anomaly_hash=approved_hashes.get(name))
                            if unit is None:
                                offset, replacement = insertion_span(header)
                            else:
                                replacement = corrected_card(header[offset:offset + CARD], unit)
                            payload_samples = samples(fd, len(header), st.st_size)
                            check_identity(fd, dir_fd, name, st)
                            if replacement == header[offset:offset + len(replacement)]:
                                counts['unchanged'] += 1
                                print(f'UNCHANGED {path}: BUNIT is exactly sfu')
                            else:
                                counts['would_change'] += 1
                                print(f'{"CHANGE" if args.apply else "WOULD CHANGE"} {path}: {unit!r} -> sfu; '
                                      f'{len(replacement)}-byte header span only; {len(payload_samples)} payload samples')
                                if args.apply:
                                    record = backup_record(path, dir_fd, st, header, offset, replacement, payload_samples,
                                                           legacy_header=unit is None,
                                                           date_anomaly_hash=approved_hashes.get(name))
                                    write_card(fd, dir_fd, name, st, record, journal)
                                    counts['changed'] += 1
                                    counts['verified'] += 1
                                    print(f'CHANGED {path}: primary header bytes and '
                                          f'{len(payload_samples)} payload samples verified')
                        counts['processed'] += 1
                    except (OSError, ValueError, SafetyError) as exc:
                        key = 'rejected' if isinstance(exc, Rejected) else 'errors'
                        counts[key] += 1
                        counts['processed'] += 1
                        print(f'{key.upper()} {path}: {exc}', file=sys.stderr)
                        if args.apply:
                            break
    except (OSError, ValueError, SafetyError) as exc:
        key = 'rejected' if isinstance(exc, Rejected) else 'errors'
        counts[key] += 1
        print(f'{key.upper()}: {exc}', file=sys.stderr)
    finally:
        if journal is not None:
            try:
                journal.close()
            except OSError as exc:
                counts['errors'] += 1
                print(f'ERROR closing journal: {exc}', file=sys.stderr)
        counts['unprocessed'] = counts['candidates'] - counts['processed']
        print('SUMMARY ' + ' '.join(f'{key}={value}' for key, value in counts.items()))
        if journal is not None:
            print(f'Journal retained: {journal.path}')
    return 1 if counts['errors'] or counts['rejected'] else 0


if __name__ == '__main__':
    sys.exit(main())
