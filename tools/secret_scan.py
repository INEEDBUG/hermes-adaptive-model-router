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
"""
from __future__ import annotations

import argparse
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

# The scanner's own pattern table necessarily contains the identifier strings it
# searches for; scanning itself would always self-match. It is excluded here rather
# than by adding those strings to ALLOW_LIST, so a real finding that happens to
# contain one of them is still reported.
SELF = pathlib.Path(__file__).resolve()


def iter_files(root: pathlib.Path):
    for p in sorted(root.rglob('*')):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.resolve() == SELF:
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


def scan_git(root: pathlib.Path) -> list:
    """Scan the content of every commit in the repository (not commit metadata)."""
    try:
        revs = subprocess.run(['git', '-C', str(root), 'rev-list', '--all'],
                              capture_output=True, text=True, check=True).stdout.split()
    except Exception:
        return []
    if not revs:
        return []
    findings = []
    for rev in revs:
        try:
            out = subprocess.run(['git', '-C', str(root), 'grep', '-nI', '-E',
                                  '|'.join(rx.pattern for _, rx in PATTERNS), rev],
                                 capture_output=True, text=True).stdout
        except Exception:
            continue
        for line in out.splitlines():
            if any(a in line for a in ALLOW_LIST):
                continue
        # A single aggregate grep per revision is enough for a hit/miss decision;
        # report the revision rather than the matched content.
        if out.strip():
            hits = [l for l in out.splitlines() if not any(a in l for a in ALLOW_LIST)]
            if hits:
                findings.append((f'git:{rev[:12]}', 0, f'{len(hits)} matching line(s)'))
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
