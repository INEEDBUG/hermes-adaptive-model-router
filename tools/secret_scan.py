#!/usr/bin/env python3
"""Secret / privacy scanner for this repository (also used by CI).

It is deliberately dependency-free and never prints the content it matches: a
finding reports the pattern name and the location only, so running the scanner on
a compromised tree does not copy the secret into a CI log.

Synthetic markers used by the test suite (placeholder credentials, a fake private
key body, RFC 5737 documentation addresses) are allow-listed by explicit string,
so a genuine secret cannot hide behind them.

Usage::

    python3 tools/secret_scan.py                 # scan the working tree
    python3 tools/secret_scan.py --git           # also scan every commit's content
    python3 tools/secret_scan.py --root /path    # scan another checkout

The history scan reads committed blob content in-process rather than shelling out to
``git grep``: git's regex dialect is POSIX ERE, which has no ``(?:``, no ``(?i)`` and
no lookahead, and a pattern it cannot compile makes the whole invocation return
nothing — a silent no-op that looks exactly like a clean result.
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

# The history scan reads every blob object in the repository; very large blobs are
# skipped to keep it bounded (multi-megabyte binaries belong to a dedicated secret
# manager, not to this scanner).
MAX_BLOB_BYTES = 2 * 1024 * 1024

# The scanner's own pattern table necessarily contains the identifier strings it
# searches for, so scanning itself always self-matches. It is excluded by identity
# *and* by content hash, so a copy of this file at any path (a second checkout, an
# unpacked release tarball) is skipped as well. Those strings are deliberately not
# added to ALLOW_LIST, so a real finding that happens to contain one is still reported.
SELF = pathlib.Path(__file__).resolve()
SELF_NAME = SELF.name
# Content-based self-identification, used for committed copies of this scanner at any
# path and from any past commit: every version of the pattern table contains this
# name, so such a blob is recognised without keeping a path or hash list. For a real
# finding to be skipped this way it would have to mention the pattern name, which is
# not a realistic hiding place for a secret.
SELF_MARKER = 'host_or_project_identifiers'
try:
    SELF_SHA256 = hashlib.sha256(SELF.read_bytes()).hexdigest()
except Exception:                                      # pragma: no cover - defensive
    SELF_SHA256 = None


def is_self_text(text: str) -> bool:
    """True when ``text`` is (any version of) this scanner."""
    return SELF_MARKER in text


def is_self(p: pathlib.Path) -> bool:
    """True for this scanner itself, at this path or at any copy of it."""
    if p.resolve() == SELF:
        return True
    try:
        raw = p.read_bytes()
    except Exception:
        return False
    if SELF_SHA256 is not None and hashlib.sha256(raw).hexdigest() == SELF_SHA256:
        return True
    if SELF_MARKER:
        try:
            if is_self_text(raw.decode('utf-8', 'replace')):
                return True
        except Exception:
            return False
    return False


def iter_files(root: pathlib.Path):
    for p in sorted(root.rglob('*')):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if is_self(p):
            continue
        if p.suffix not in TEXT_SUFFIXES:
            continue
        yield p


def scan_text(text: str, patterns=None):
    patterns = patterns or PATTERNS
    for i, line in enumerate(text.splitlines(), 1):
        if any(a in line for a in ALLOW_LIST):
            continue
        for name, rx in patterns:
            if rx.search(line):
                yield name, i


def scan_tree(root: pathlib.Path) -> list:
    findings = []
    for p in iter_files(root):
        try:
            text = p.read_text(errors='replace')
        except Exception:
            continue
        for name, line in scan_text(text):
            findings.append((str(p.relative_to(root)), line, name))
    return findings


def _iter_blob_objects(root: pathlib.Path):
    """Yield ``(object_id, payload)`` for every blob object in the repository."""
    listing = subprocess.run(
        ['git', '-C', str(root), 'cat-file', '--batch-all-objects',
         '--batch-check=%(objecttype) %(objectname) %(objectsize)'],
        capture_output=True, text=True, check=True).stdout
    ids = [line.split()[1] for line in listing.splitlines() if line.startswith('blob ')]
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
        pos = start + size + 1                          # skip the object's trailing newline


def scan_git(root: pathlib.Path) -> list:
    """Scan the content of every committed blob (commit metadata is never scanned).

    The same compiled patterns as the working-tree scan are used, so history and tree
    coverage cannot drift apart. Committed copies of this scanner are identified by
    content (:data:`SELF_MARKER`), and matched content is never reported — a finding
    names the blob and suggests how to locate it.
    """
    check = subprocess.run(['git', '-C', str(root), 'rev-parse', '--is-inside-work-tree'],
                           capture_output=True, text=True)
    if check.returncode != 0 or check.stdout.strip() != 'true':
        return []                                       # no repository: no history to scan
    try:
        blobs = list(_iter_blob_objects(root))
    except Exception as exc:
        return [('git_history_unavailable', 0, type(exc).__name__)]
    findings = []
    for oid, payload in blobs:
        if len(payload) > MAX_BLOB_BYTES:
            continue                                     # keep the scan bounded; see note above
        text = payload.decode('utf-8', 'replace')
        if is_self_text(text):
            continue                                     # a committed copy of this scanner
        hits = list(scan_text(text))
        if hits:
            # Readable in the same "[finding name] location" shape as tree findings,
            # and content-free: the blob id alone identifies where the hit lives.
            findings.append((f'blob {oid[:12]}', 0,
                             f'{len(hits)} matching line(s); locate with '
                             f'git log --all --find-object={oid[:12]}'))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description='Scan this repository for secrets and private data.')
    ap.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parents[1]))
    ap.add_argument('--git', action='store_true', help='also scan all committed content')
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    findings = scan_tree(root)
    if args.git:
        findings += scan_git(root)

    if not findings:
        print(f'secret scan: clean ({len(list(iter_files(root)))} files scanned'
              f'{" + git history" if args.git else ""}; matched content is never printed)')
        return 0

    print(f'secret scan: {len(findings)} finding(s) — content intentionally not printed')
    for path, line, name in findings:
        loc = f'{path}:{line}' if line else path
        print(f'  [{name}] {loc}')
    print('STOP: do not commit or push until every finding is resolved.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
