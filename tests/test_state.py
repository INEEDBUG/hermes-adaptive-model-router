#!/usr/bin/env python3
"""Offline tests for the runtime mode resolver (kill switch).

Covers every fail-safe branch: missing state, corrupt state, invalid modes, the
breaker flag, kill-sentinel precedence and the three conditions attached to
``auto``. No model, network or credential is involved.

Run it from the repository root::

    python3 tests/test_state.py
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DIR = os.path.join(tempfile.gettempdir(), 'jev_state_test')
os.environ['JEV_STATE_DIR'] = TEST_DIR
os.environ.pop('JEV_AUTO_APPROVED', None)

from router import state  # noqa: E402

R = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")


def reset():
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    pathlib.Path(TEST_DIR).mkdir(parents=True, exist_ok=True)


now = time.time()

# 1. state directory missing -> off
shutil.rmtree(TEST_DIR, ignore_errors=True)
r = state.resolve(now)
chk('1 missing directory -> off', r['mode'] == 'off', r)

# 2. state file missing -> off
reset()
r = state.resolve(now)
chk('2 missing state file -> off', r['mode'] == 'off' and r['source'] == 'default', r)

# 3. kill sentinel wins over an otherwise valid auto lease
reset()
(pathlib.Path(TEST_DIR) / 'KILL').write_text('')
state.write_state('auto', auto_until=now + 3600, heartbeat=now)
os.environ['JEV_AUTO_APPROVED'] = 'yes'
r = state.resolve(now)
chk('3 KILL beats auto -> off', r['mode'] == 'off' and r['source'] == 'kill_file', r)

# 3b. kill sentinel with a corrupt state file -> still off
(pathlib.Path(TEST_DIR) / 'mode.json').write_text('{broken')
r = state.resolve(now)
chk('3b KILL + corrupt file -> off', r['mode'] == 'off', r)

# 4. corrupt JSON -> off
reset()
(pathlib.Path(TEST_DIR) / 'mode.json').write_text('{"mode": "auto"')
r = state.resolve(now)
chk('4 corrupt JSON -> off', r['mode'] == 'off' and 'invalid' in r['reason'], r)

# 5. non-object JSON -> off
reset()
(pathlib.Path(TEST_DIR) / 'mode.json').write_text('[1,2,3]')
chk('5 non-object JSON -> off', state.resolve(now)['mode'] == 'off')

# 6. unknown mode value -> off
reset()
(pathlib.Path(TEST_DIR) / 'mode.json').write_text(json.dumps({'mode': 'turbo'}))
chk('6 unknown mode -> off', state.resolve(now)['mode'] == 'off')

# 7. state file present but unreadable (directory) -> off
reset()
pathlib.Path(TEST_DIR, 'mode.json').mkdir()
chk('7 unreadable state file -> off', state.resolve(now)['mode'] == 'off')

# 8. breaker flag -> off
reset()
(pathlib.Path(TEST_DIR) / 'mode.json').write_text(json.dumps({'mode': 'shadow', 'tripped': True}))
r = state.resolve(now)
chk('8 tripped -> off', r['mode'] == 'off' and r['source'] == 'breaker', r)

# 9. shadow passthrough
reset()
state.write_state('shadow')
chk('9 mode=shadow -> shadow', state.resolve(now)['mode'] == 'shadow')

# 10. off passthrough
reset()
state.write_state('off')
chk('10 mode=off -> off', state.resolve(now)['mode'] == 'off')

# 11. auto with approval, live lease and fresh heartbeat
reset()
state.write_state('auto', auto_until=now + 600, heartbeat=now)
r = state.resolve(now)
chk('11 auto fully satisfied -> auto', r['mode'] == 'auto' and r['source'] == 'file', r)

# 12. auto without approval -> shadow
reset()
state.write_state('auto', auto_until=now + 600, heartbeat=now)
os.environ.pop('JEV_AUTO_APPROVED', None)
r = state.resolve(now)
chk('12 auto unapproved -> shadow', r['mode'] == 'shadow' and r['source'] == 'approval', r)
os.environ['JEV_AUTO_APPROVED'] = 'yes'

# 13. expired lease -> shadow
reset()
state.write_state('auto', auto_until=now - 1, heartbeat=now)
r = state.resolve(now)
chk('13 auto expired -> shadow', r['mode'] == 'shadow' and r['source'] == 'expiry', r)

# 14. missing lease -> shadow
reset()
state.write_state('auto', heartbeat=now)
r = state.resolve(now)
chk('14 auto_until missing -> shadow', r['mode'] == 'shadow' and r['source'] == 'expiry', r)

# 15. stale heartbeat -> shadow
reset()
state.write_state('auto', auto_until=now + 3600, heartbeat=now - 121)
r = state.resolve(now)
chk('15 stale heartbeat -> shadow', r['mode'] == 'shadow' and r['source'] == 'heartbeat', r)

# 16. heartbeat boundary (119s) still auto
reset()
state.write_state('auto', auto_until=now + 3600, heartbeat=now - 119)
chk('16 heartbeat 119s -> auto', state.resolve(now)['mode'] == 'auto')

# 17. with no state at all the resolver never returns auto
reset()
pathlib.Path(TEST_DIR, 'mode.json').unlink(missing_ok=True)
r = state.resolve(now)
chk('17 no state -> never auto', r['mode'] == 'off', r)

# 18. audit log is writable
reset()
os.environ['JEV_AUDIT_LOG'] = os.path.join(TEST_DIR, 'mode-audit.jsonl')
state.audit({'event': 'test', 'mode': 'off'})
ap = state.audit_path()
chk('18 audit file writable', ap.exists() and 'test' in ap.read_text().splitlines()[-1], str(ap))

# 19. the resolver contains no model-switching code path
src = (ROOT / 'router' / 'state.py').read_text()
banned = ['switch_model', 'register_middleware']
chk('19 no model/auto path', not any(b in src for b in banned))
chk('20 no credential handling in state.py', 'API_KEY=' not in src and 'Authorization' not in src)

# --- the initialiser CLI, and the hard boundary around KILL ----------------------
# These run the real tool as a subprocess: the point is that an operator convenience
# tool cannot undo a stop, which is only checkable through its command line.
reset()
mode_file = pathlib.Path(TEST_DIR, 'mode.json')
kill = pathlib.Path(TEST_DIR, 'KILL')
kill.write_text('stop\n')
before = mode_file.read_text() if mode_file.exists() else ''
initialiser = ROOT / 'tools' / 'init_state.py'

p = subprocess.run([sys.executable, str(initialiser), '--force'],
                   capture_output=True, text=True)
chk('21 --force is not an option any more', p.returncode != 0 and 'force' in p.stderr.lower(),
    f'rc={p.returncode}')

p = subprocess.run([sys.executable, str(initialiser)], capture_output=True, text=True)
chk('22 a KILL sentinel makes the initialiser refuse', p.returncode == 2 and 'REFUSING' in p.stdout,
    f'rc={p.returncode}')
after = mode_file.read_text() if mode_file.exists() else ''
chk('23 the refusal changes no state', after == before)

p = subprocess.run([sys.executable, str(initialiser)], capture_output=True, text=True)
chk('24 it still refuses while the sentinel exists', p.returncode == 2, f'rc={p.returncode}')

kill.unlink()                       # the deliberate, visible action recovery requires
p = subprocess.run([sys.executable, str(initialiser)], capture_output=True, text=True)
chk('25 after deleting the sentinel, initialisation succeeds again',
    p.returncode == 0 and state.resolve(now)['mode'] == 'shadow', f'rc={p.returncode}')

fails = [n for n, ok in R if not ok]
print(f"\ntotal {len(R)} checks, failed {len(fails)}: {fails}")
sys.exit(1 if fails else 0)
