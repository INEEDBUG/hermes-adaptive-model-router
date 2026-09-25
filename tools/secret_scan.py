#!/usr/bin/env python3
"""Secret / privacy scanner for this repository (also used by CI).

Dependency-free, and it never prints the content it matches: a finding reports the
pattern name and the location only, so running the scanner against a compromised tree
does not copy the secret into a CI log.

Two properties matter more than coverage, because a scan that cannot fail is worse
than no scan at all:

* **A history scan that cannot run must not look clean.** History is read in-process
  from committed blob content instead of shelling out to ``git grep``: git's dialect is
  POSIX ERE, which cannot compile a non-capturing group, an inline ``(?i)`` flag or
  lookahead, and one un-compilable pattern made the whole invocation fail silently.
* **Skipped work must be visible.** Objects above the size cap are reported as
  ``scan incomplete`` and fail the run (exit 3) unless the operator explicitly allows
  them, so "clean" always means "everything was scanned".

Usage::

    python3 tools/secret_scan.py                          # working tree only
    python3 tools/secret_scan.py --git                    # + every committed blob
    python3 tools/secret_scan.py --root /path             # scan another checkout
    python3 tools/secret_scan.py --git --allow-oversized <blob-or-path-prefix>

Exit codes: ``0`` clean **and** complete, ``1`` findings, ``3`` incomplete (objects
were left unscanned).
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import subprocess
import sys

# (name, compiled pattern) — kept in sync with router/redact.py categories plus
# infrastructure-specific shapes this project must never publish.
PATTERNS = [
    ('prefixed_api_key', re.compile(r'\b(?:sk|tp|pk|rk)-[A-Za-z0-9_\-]{16,}\b')),
    ('typesafe_style_key', re.compile(r'\bapikey_[0-9a-fA-F]{16,}')),
    ('github_token', re.compile(r'\b(?:gho|ghp|ghs|github_pat)_[A-Za-z0-9_]{20,}\b')),
    ('jwt_or_long_token', re.compile(r'\beyJ[A-Za-z0-9_\-]{20,}')),
    ('bearer_value', re.compile(r'(?i)\bbearer\s+(?!\[REDACTED\])[A-Za-z0-9._\-]{20,}')),
    ('private_key_block', re.compile(r'-----BEGIN[^-]*PRIVATE KEY-----')),
    ('ipv4_private', re.compile(r'\b(?:192\.168|10|172\.(?:1[6-9]|2[0-9]|3[01]))\.\d{1,3}\.\d{1,3}\b')),
    ('mac_address', re.compile(r'\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b')),
    ('email', re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')),
    ('lark_id', re.compile(r'\b(?:oc|ou|om)_[0-9a-f]{10,}\b')),
    ('session_id_shape', re.compile(r'\b20\d{6}_\d{6}_[0-9a-f]{6}\b')),
    ('host_or_project_identifiers',
     re.compile(r'(?i)\b(racknerd|istoreos|openwrt|mihomo|clash|frigate|go2rtc|drez|dengru)\b')),
    ('absolute_nas_path', re.compile(r'/vol\d\b')),
]

# Strings that legitimately appear in tests, examples and redaction patterns.
ALLOW_LIST = [
    'EXAMPLEONLYNOTAREALKEY',
    'your_typesafe_key_here',
    'your_xiaomi_key_here',
    'your_deepseek_key_here',
    'for-fault-injection',
    'a.b@example.com',
    'testuser@198.51.100.7',
    '192.0.2.10',
    '198.51.100.7',
    'MIIEfake',
    'REDACTED',
    'sk-liveFAKE',
]

SKIP_DIRS = {'.git', '__pycache__', '.venv', 'node_modules', '.mypy_cache', '.pytest_cache'}
TEXT_SUFFIXES = {'.py', '.md', '.sh', '.yaml', '.yml', '.json', '.env', '.txt', '.cfg', '.toml', ''}

# Objects larger than this are not read; they are reported as unscanned instead of
# being skipped quietly (see MAX_OBJECT_BYTES handling in main()).
MAX_OBJECT_BYTES = 2 * 1024 * 1024

# This file's own path inside the project, used when it scans another checkout.
SELF_RELPATH = 'tools/secret_scan.py'
SELF = pathlib.Path(__file__).resolve()
try:
    SELF_SHA256 = hashlib.sha256(SELF.read_bytes()).hexdigest()
except Exception:                                          # pragma: no cover - defensive
    SELF_SHA256 = None


def is_self_file(p: pathlib.Path) -> bool:
    """True only for this scanner's own file: same path, or byte-identical content.

    Deliberately **not** a substring test. A data file, log or note that merely
    mentions a pattern name such as ``host_or_project_identifiers`` is not a copy of
    this scanner and is scanned like any other file — an earlier version skipped any
    blob containing that marker, so one quoted string could hide a real secret.
    """
    if p.resolve() == SELF:
        return True
    if SELF_SHA256 is None:
        return False
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest() == SELF_SHA256
    except Exception:
        return False


def _self_relpath(root: pathlib.Path) -> str:
    try:
        return str(SELF.relative_to(root.resolve()))
    except Exception:
        return SELF_RELPATH


def self_blob_ids(root: pathlib.Path) -> set:
    """Blob ids of every committed version of this file, plus the working-tree one.

    Path-anchored and exact. The scanner's own pattern table legitimately contains the
    identifier strings it searches for, so its committed versions would self-match —
    but only *its* versions, resolved through git, never "any blob containing a
    marker string".
    """
    ids = set()
    rel = _self_relpath(root)
    try:
        revs = subprocess.run(['git', '-C', str(root), 'log', '--all', '--format=%H', '--', rel],
                              capture_output=True, text=True, check=True).stdout.split()
    except Exception:
        revs = []
    for rev in dict.fromkeys(revs):
        try:
            p = subprocess.run(['git', '-C', str(root), 'rev-parse', f'{rev}:{rel}'],
                               capture_output=True, text=True)
        except Exception:
            continue
        if p.returncode == 0 and p.stdout.strip():
            ids.add(p.stdout.strip())
    try:
        w = subprocess.run(['git', '-C', str(root), 'hash-object', '--', str(root / rel)],
                           capture_output=True, text=True)
        if w.returncode == 0 and w.stdout.strip():
            ids.add(w.stdout.strip())
    except Exception:
        pass
    return ids


class Report:
    """Findings plus what was scanned and what was deliberately left out."""

    def __init__(self):
        self.findings: list = []
        self.scanned_files = 0
        self.scanned_blobs = 0
        self.skipped: list = []          # {'where', 'id', 'bytes', 'allowed'}

    @property
    def unscanned(self) -> list:
        return [s for s in self.skipped if not s['allowed']]

    @property
    def allowed_oversized(self) -> list:
        return [s for s in self.skipped if s['allowed']]


def _allowed(ident: str, allow) -> bool:
    return any(ident.startswith(a) for a in allow if a)


def scan_text(text: str, patterns=None):
    patterns = patterns or PATTERNS
    for i, line in enumerate(text.splitlines(), 1):
        if any(a in line for a in ALLOW_LIST):
            continue
        for name, rx in patterns:
            if rx.search(line):
                yield name, i


def iter_files(root: pathlib.Path, report: Report, *, max_bytes: int, allow):
    for p in sorted(root.rglob('*')):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if is_self_file(p):
            continue
        if p.suffix not in TEXT_SUFFIXES:
            continue
        try:
            size = p.stat().st_size
        except Exception:
            continue
        if size > max_bytes:
            rel = str(p.relative_to(root))
            report.skipped.append({'where': 'tree', 'id': rel, 'bytes': size,
                                   'allowed': _allowed(rel, allow)})
            continue
        yield p


def scan_tree(root: pathlib.Path, report: Report, *, max_bytes: int, allow):
    for p in iter_files(root, report, max_bytes=max_bytes, allow=allow):
        try:
            text = p.read_text(errors='replace')
        except Exception:
            continue
        report.scanned_files += 1
        for name, line in scan_text(text):
            report.findings.append((str(p.relative_to(root)), line, name))


def _is_git_repo(root: pathlib.Path) -> bool:
    try:
        check = subprocess.run(['git', '-C', str(root), 'rev-parse', '--is-inside-work-tree'],
                               capture_output=True, text=True)
    except Exception:
        return False
    return check.returncode == 0 and check.stdout.strip() == 'true'


def _read_blobs(root: pathlib.Path, ids):
    """Yield ``(object_id, payload)`` for the requested blob ids."""
    if not ids:
        return
    data = subprocess.run(['git', '-C', str(root), 'cat-file', '--batch'],
                          input=('\n'.join(ids) + '\n').encode(),
                          capture_output=True, check=True).stdout
    pos = 0
    while pos < len(data):
        nl = data.find(b'\n', pos)
        if nl < 0:
            return
        header = data[pos:nl].split()
        if len(header) < 3:
            return
        size = int(header[2])
        start = nl + 1
        yield header[0].decode(), data[start:start + size]
        pos = start + size + 1                              # skip the object's trailing newline


def scan_git(root: pathlib.Path, report: Report, *, max_bytes: int, allow) -> None:
    """Scan the content of every committed blob (commit metadata is never scanned).

    The same compiled patterns as the working-tree scan are used, so tree and history
    coverage cannot drift apart. Oversized blobs are counted as skipped and reported,
    never dropped silently; committed versions of this scanner are identified by blob
    id; matched content is never printed.
    """
    if not _is_git_repo(root):
        return                                          # no repository: no history to scan
    try:
        listing = subprocess.run(
            ['git', '-C', str(root), 'cat-file', '--batch-all-objects',
             '--batch-check=%(objecttype) %(objectname) %(objectsize)'],
            capture_output=True, text=True, check=True).stdout
    except Exception as exc:
        report.findings.append(('git', 0, f'history scan could not run ({type(exc).__name__})'))
        return

    self_ids = self_blob_ids(root)
    wanted = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0] != 'blob':
            continue
        oid, size = parts[1], int(parts[2])
        if oid in self_ids:
            continue                                    # a committed version of this scanner
        if size > max_bytes:
            report.skipped.append({'where': 'history', 'id': oid, 'bytes': size,
                                   'allowed': _allowed(oid, allow)})
            continue
        wanted.append(oid)

    for oid, payload in _read_blobs(root, wanted):
        report.scanned_blobs += 1
        if SELF_SHA256 is not None and hashlib.sha256(payload).hexdigest() == SELF_SHA256:
            continue
        hits = list(scan_text(payload.decode('utf-8', 'replace')))
        if hits:
            # Content-free and readable in the same "[name] location" shape as tree
            # findings; the blob id is enough to find it locally.
            report.findings.append((f'blob {oid[:12]}', 0,
                                    f'{len(hits)} matching line(s); locate with '
                                    f'git log --all --find-object={oid[:12]}'))


def main() -> int:
    ap = argparse.ArgumentParser(description='Scan this repository for secrets and private data.')
    ap.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parents[1]))
    ap.add_argument('--git', action='store_true', help='also scan every committed blob')
    ap.add_argument('--max-object-bytes', type=int, default=MAX_OBJECT_BYTES,
                    help='objects larger than this are reported as unscanned, not skipped '
                         f'(default {MAX_OBJECT_BYTES})')
    ap.add_argument('--allow-oversized', action='append', default=[],
                    metavar='BLOB_OR_PATH_PREFIX',
                    help='explicitly allow one oversized object after reviewing it '
                         '(repeatable)')
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    allow = tuple(args.allow_oversized)
    report = Report()
    scan_tree(root, report, max_bytes=args.max_object_bytes, allow=allow)
    if args.git:
        scan_git(root, report, max_bytes=args.max_object_bytes, allow=allow)

    where = f'{report.scanned_files} file(s) scanned'
    if args.git:
        where += f' + {report.scanned_blobs} blob(s) scanned'

    if report.findings:
        print(f'secret scan: {len(report.findings)} finding(s) — content intentionally not printed')
        for path, line, name in report.findings:
            loc = f'{path}:{line}' if line else path
            print(f'  [{name}] {loc}')
        print('STOP: do not commit or push until every finding is resolved.')
        return 1

    if report.unscanned:
        print(f'secret scan: scan incomplete — {len(report.unscanned)} object(s) above the '
              f'{args.max_object_bytes}-byte cap were NOT scanned')
        for s in report.unscanned:
            print(f'  [oversized {s["where"]}] {s["id"]} ({s["bytes"]} bytes) — not scanned; '
                  f'review it, then allow it with: --allow-oversized {s["id"]}')
        print('Refusing to report clean: unscanned objects make the result incomplete.')
        return 3

    note = ''
    if report.allowed_oversized:
        note = (f" ({len(report.allowed_oversized)} oversized object(s) explicitly allowed "
                f"via --allow-oversized)")
    print(f'secret scan: clean — scan complete: {where}{note}; matched content is never printed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
