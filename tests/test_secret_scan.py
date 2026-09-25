#!/usr/bin/env python3
"""Offline tests for ``tools/secret_scan.py``.

Adversarial by design. A scanner is only trustworthy once it has been shown to catch
something, and once a file that merely *mentions* a marker string cannot silence it.

Run from the repository root::

    python3 tests/test_secret_scan.py
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCANNER = ROOT / 'tools' / 'secret_scan.py'

# Synthetic values are assembled at run time so that this file never contains a literal
# its own scanner would (correctly) report.
FAKE_KEY = 'sk-' + 'abcDEF1234567890xyzABC'
FAKE_MAIL = 'somebody' + '@' + 'realcorp.cn'
MARKER = 'host_or_project_identifiers'          # a pattern *name*, not a matched value

R = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")


def run_scan(root, *args):
    p = subprocess.run([sys.executable, str(SCANNER), '--root', str(root), *args],
                       capture_output=True, text=True)
    return p.returncode, (p.stdout or '') + (p.stderr or '')


def write_tree(base: pathlib.Path, files: dict):
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def git(base: pathlib.Path, *args):
    return subprocess.run(['git', '-C', str(base), *args], capture_output=True, text=True)


def init_repo(base: pathlib.Path):
    git(base, 'init', '-q')
    git(base, 'config', 'user.email', 'scanner-test')
    git(base, 'config', 'user.name', 'test')
    git(base, 'config', 'commit.gpgsign', 'false')


def commit_all(base: pathlib.Path, msg='c'):
    git(base, 'add', '-A')
    git(base, 'commit', '-qm', msg)


tmp = pathlib.Path(tempfile.mkdtemp(prefix='jev-scan-test-'))
try:
    # --- working tree ---------------------------------------------------------------
    print('=== A. working tree ===')
    a = tmp / 'clean'
    a.mkdir()
    write_tree(a, {'docs/readme.md': 'nothing sensitive here\n'})
    rc, out = run_scan(a)
    chk('A1 clean tree -> exit 0 + "scan complete"',
        rc == 0 and 'scan complete' in out and 'incomplete' not in out, f'rc={rc}')

    b = tmp / 'leak'
    b.mkdir()
    write_tree(b, {'config.txt': f'DEEPSEEK_API_KEY={FAKE_KEY}\n'})
    rc, out = run_scan(b)
    chk('A2 a real-shaped secret -> exit 1', rc == 1 and 'prefixed_api_key' in out, f'rc={rc}')
    chk('A3 findings never print the matched content', FAKE_KEY not in out and 'abcDEF' not in out)

    # The bypass this hardening exists for: a file that mentions the scanner's own
    # pattern name must still be scanned.
    c = tmp / 'bypass'
    c.mkdir()
    write_tree(c, {'notes.txt': f'{MARKER}\nDEEPSEEK_API_KEY={FAKE_KEY}\n'
                                f'contact {FAKE_MAIL}\n'})
    rc, out = run_scan(c)
    chk('A4 quoting a pattern name cannot silence the scanner (MUST fail)',
        rc == 1 and 'prefixed_api_key' in out, f'rc={rc}')
    chk('A5 the same file is also reported for its email', 'email' in out)

    d = tmp / 'marker_only'
    d.mkdir()
    write_tree(d, {'notes.txt': f'{MARKER}\nno secrets in this file at all\n'})
    rc, out = run_scan(d)
    chk('A6 a marker with no secret is clean (no false positive)', rc == 0, f'rc={rc}')

    # --- history -------------------------------------------------------------------
    print('=== B. history ===')
    e = tmp / 'history_leak'
    e.mkdir()
    init_repo(e)
    write_tree(e, {'ok.md': 'fine\n', 'leak.env': f'DEEPSEEK_API_KEY={FAKE_KEY}\n'})
    commit_all(e)
    (e / 'leak.env').unlink()
    commit_all(e, 'remove')
    rc_tree, _ = run_scan(e)
    rc_hist, out_hist = run_scan(e, '--git')
    chk('B1 the deleted secret is gone from the worktree', rc_tree == 0, f'rc={rc_tree}')
    chk('B2 but is still caught in history', rc_hist == 1 and 'find-object' in out_hist,
        f'rc={rc_hist}')

    f = tmp / 'history_bypass'
    f.mkdir()
    init_repo(f)
    write_tree(f, {'notes.txt': f'{MARKER}\nDEEPSEEK_API_KEY={FAKE_KEY}\n'})
    commit_all(f)
    (f / 'notes.txt').unlink()
    commit_all(f, 'remove')
    rc, out = run_scan(f, '--git')
    chk('B3 history bypass attempt is caught too (MUST fail)', rc == 1, f'rc={rc}')

    # --- oversized objects must not be silently skipped ----------------------------
    print('=== C. oversized objects ===')
    g = tmp / 'oversized'
    g.mkdir()
    init_repo(g)
    write_tree(g, {'small.txt': 'nothing sensitive\n', 'big.txt': 'x' * 4096 + '\n'})
    commit_all(g)
    (g / 'big.txt').unlink()
    commit_all(g, 'remove big')
    rc, out = run_scan(g, '--git', '--max-object-bytes', '1024')
    chk('C1 an unscanned oversized blob -> exit 3 + "scan incomplete"',
        rc == 3 and 'scan incomplete' in out, f'rc={rc}')
    chk('C2 the oversized object is named with an actionable hint', '--allow-oversized' in out)
    m = re.search(r'--allow-oversized (\S+)', out)
    oid = m.group(1) if m else ''
    chk('C3 the hint carries a blob id', bool(oid), f'oid={oid[:12]}')
    if oid:
        rc2, out2 = run_scan(g, '--git', '--max-object-bytes', '1024',
                             '--allow-oversized', oid)
        chk('C4 explicit whitelist -> exit 0 and "scan complete"',
            rc2 == 0 and 'scan complete' in out2 and 'explicitly allowed' in out2, f'rc={rc2}')
        rc3, out3 = run_scan(g, '--git', '--max-object-bytes', '1024',
                             '--allow-oversized', 'some-other-object')
        chk('C5 a whitelist entry for something else does not silence the report',
            rc3 == 3, f'rc={rc3}')
    else:
        chk('C4 (skipped: no blob id parsed)', False)
        chk('C5 (skipped: no blob id parsed)', False)

    # --- self-identification is precise, not substring-based -----------------------
    print('=== D. scanner self-identification ===')
    h = tmp / 'selfcopy'
    h.mkdir()
    (h / 'tools').mkdir()
    shutil.copy2(SCANNER, h / 'tools' / 'secret_scan.py')
    write_tree(h, {'notes.md': 'ordinary content\n'})
    rc, out = run_scan(h)
    chk('D1 its own source does not self-flag in the tree scan', rc == 0, f'rc={rc}')
    (h / 'tools' / 'renamed_scan.py').write_text((h / 'tools' / 'secret_scan.py').read_text())
    rc, out = run_scan(h)
    chk('D2 a byte-identical copy at another path is also recognised', rc == 0, f'rc={rc}')
    init_repo(h)
    commit_all(h)
    rc, out = run_scan(h, '--git')
    chk('D3 committed versions of the scanner do not self-flag in history', rc == 0,
        f'rc={rc}')
    write_tree(h, {'notes2.md': f'{MARKER}\nDEEPSEEK_API_KEY={FAKE_KEY}\n'})
    commit_all(h, 'add secret next to a marker')
    rc, out = run_scan(h, '--git')
    chk('D4 the same repo still catches a secret committed beside a marker', rc == 1,
        f'rc={rc}')
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
fails = [n for n, ok in R if not ok]
print(f'total {len(R)} checks, failed {len(fails)}: {fails}')
sys.exit(1 if fails else 0)
