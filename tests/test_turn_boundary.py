#!/usr/bin/env python3
"""Offline suite: the human-turn provenance boundary (v0.2.0).

Run from the repository root::

    python3 tests/test_turn_boundary.py

No network, no credential, no Hermes installation: the plugin is loaded against the real
``router`` package with ``dossier.build`` and ``shadow.submit`` replaced by counting
stand-ins, so a "JEV call" here is exactly a call that would have reached the routing
service in production.

Groups
    A  the dual gate: allow-listed platform AND turn_origin == 'user'
    B  fail-closed paths: missing / empty / unknown origin, missing platform, empty allowlist
    C  per-turn counting: one rejection per turn, not per API call
    D  concurrency isolation: a user turn and a background turn in parallel
    E  skip telemetry is content-free
    F  anomaly detector (defense in depth, never the boundary)
    G  the accepted path carries its provenance into the shadow record
"""
import inspect
import json
import os
import pathlib
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix='jev-boundary-'))
os.environ['JEV_LOG_DIR'] = str(TMP / 'logs')
os.environ['JEV_STATE_DIR'] = str(TMP / 'state')
os.environ['HERMES_HOME'] = str(TMP / 'home')
os.environ['HERMES_ENV_PATH'] = str(TMP / 'home' / '.env')
os.environ['ROUTER_MODE'] = 'shadow'
os.environ.pop('JEV_AUTO_APPROVED', None)

from router import config, skip_telemetry  # noqa: E402

STATE_DIR = pathlib.Path(os.environ['JEV_STATE_DIR'])
STATE_DIR.mkdir(parents=True, exist_ok=True)
(STATE_DIR / 'mode.json').write_text(json.dumps({'mode': 'shadow'}))
config._cache['at'] = 0
config._cache['env'] = {}

import plugin as plugin_mod  # noqa: E402  (repo-root importable package)

R = []
FAILS = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond), extra))
    if not cond:
        FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")


# --------------------------------------------------------------------------- counters
class Counters:
    def __init__(self):
        self.dossier = 0
        self.jev = 0
        self.submits = []
        self.lock = threading.Lock()


C = Counters()


def _stub_dossier_build(message):
    with C.lock:
        C.dossier += 1
    return {'task': {'length': 'short'}}, {'unsafe': False, 'hits': 0}


def _stub_submit(dossier, **kwargs):
    with C.lock:
        C.jev += 1
        C.submits.append((kwargs.get('platform'), kwargs.get('turn_origin')))
    return True


import router.dossier as dossier_mod  # noqa: E402
import router.shadow as shadow_mod  # noqa: E402

dossier_mod.build = _stub_dossier_build
shadow_mod.submit = _stub_submit


def payload(platform='feishu', origin='user', turn='t', api_call_count=1, retry_count=0,
            message='synthetic test message'):
    p = {'platform': platform, 'turn_id': turn, 'api_call_count': api_call_count,
         'retry_count': retry_count, 'user_message': message, 'model': 'test-model'}
    if origin is not _MISSING:
        p['turn_origin'] = origin
    return p


class _Missing:
    pass


_MISSING = _Missing()


def probe(platform='feishu', origin='user', message='synthetic test message'):
    """Call the hook once with a fresh turn id; return what it caused."""
    C.dossier = C.jev = 0
    del C.submits[:]
    probe.n += 1
    plugin_mod._on_pre_api_request(**payload(platform, origin, turn=f'probe-{probe.n}', message=message))
    return C.dossier, C.jev


probe.n = 0


def skip_counts():
    return skip_telemetry.totals()


def skip_counts_by_reason():
    """{turn_origin|reason: count} — a rejection and an observation never share a key."""
    out = {}
    for entry in skip_telemetry.read().values():
        for counter in (entry.get('counters') or {}).values():
            key = f"{counter.get('turn_origin')}|{counter.get('reason')}"
            out[key] = out.get(key, 0) + int(counter.get('count', 0))
    return out


print('=== A. dual gate: platform IN allowlist AND turn_origin == "user" ===')
os.environ['JEV_ALLOWED_PLATFORMS'] = 'feishu'
config._cache['at'] = 0
matrix = [
    ('feishu', 'user', 1, 1, 'allow-listed platform + human origin'),
    ('feishu', 'internal_notification', 0, 0, 'internal notification'),
    ('feishu', 'background_review', 0, 0, 'background review fork (inherits platform=feishu)'),
    ('feishu', 'compaction_continuation', 0, 0, 'compaction continuation'),
    ('subagent', 'subagent', 0, 0, 'subagent'),
    ('cli', 'oneshot', 0, 0, 'CLI one-shot'),
    ('cron', 'cron', 0, 0, 'cron job'),
    ('curator', 'curator', 0, 0, 'curator'),
    ('api_server', 'api_server', 0, 0, 'api server'),
]
for i, (platform, origin, want_d, want_j, label) in enumerate(matrix, 1):
    d, j = probe(platform, origin)
    chk(f'A{i} {label}: dossier={want_d} / JEV={want_j}', (d, j) == (want_d, want_j),
        f'-> dossier={d} JEV={j}')

print('=== B. fail-closed: unusable provenance never reaches the routing service ===')
os.environ['JEV_ALLOWED_PLATFORMS'] = 'feishu'
config._cache['at'] = 0
closed = [
    ('feishu', _MISSING, 'missing turn_origin (integration patch absent)'),
    ('feishu', '', 'empty turn_origin'),
    ('feishu', 'unknown', 'unknown turn_origin'),
    ('feishu', 'weird_unknown_origin', 'unrecognised turn_origin'),
    ('', 'user', 'missing platform'),
    ('', '', 'missing platform and origin'),
    ('lark', 'user', 'platform outside the allowlist'),
]
for i, (platform, origin, label) in enumerate(closed, 1):
    d, j = probe(platform, origin)
    chk(f'B{i} {label}: 0/0', (d, j) == (0, 0), f'-> dossier={d} JEV={j}')

os.environ.pop('JEV_ALLOWED_PLATFORMS')
config._cache['at'] = 0
d, j = probe('feishu', 'user')
chk('B8 empty allowlist: 0/0 even for feishu + user', (d, j) == (0, 0), f'-> dossier={d} JEV={j}')
os.environ['JEV_ALLOWED_PLATFORMS'] = 'feishu'
config._cache['at'] = 0

_before = skip_counts_by_reason()
probe('feishu', _MISSING)
_after = skip_counts_by_reason()
chk('B10 missing origin counted as an explicit missing_origin rejection',
    _after.get('(missing)|missing_origin', 0) == _before.get('(missing)|missing_origin', 0) + 1,
    f'counters={json.dumps(_after, sort_keys=True)}')

print('=== C. per-turn counting: one rejection per turn, not per API call ===')
_before = skip_counts()
for call in (1, 2, 3):
    plugin_mod._on_pre_api_request(**payload('feishu', 'background_review', turn='multi-call-turn',
                                             api_call_count=call))
_after = skip_counts()
delta = _after.get('background_review', 0) - _before.get('background_review', 0)
chk('C1 three API calls of one rejected turn -> exactly one count', delta == 1, f'delta={delta}')

_before = skip_counts()
for call in (1, 2):
    plugin_mod._on_pre_api_request(**payload('feishu', 'user', turn='accepted-multi-call',
                                             api_call_count=call))
chk('C2 an accepted turn is observed once, not once per API call', C.jev == 1, f'JEV={C.jev}')
plugin_mod._on_pre_api_request(**payload('feishu', 'user', turn='accepted-multi-call',
                                           api_call_count=1, retry_count=1))
chk('C3 a retry of the same turn adds no second observation', C.jev == 1, f'JEV={C.jev}')

print('=== D. concurrency isolation: user turn vs background turn in parallel ===')
allowed_turns = {'D-user' + str(i) for i in range(25)}
rejected_turns = {'D-bg' + str(i) for i in range(25)}
C.dossier = C.jev = 0
del C.submits[:]
errors = []


def run(name, platform, origin, turns):
    try:
        for turn in turns:
            plugin_mod._on_pre_api_request(**payload(platform, origin, turn=turn))
    except Exception as exc:            # pragma: no cover - a crash here is the finding
        errors.append(f'{name}:{type(exc).__name__}')


ta = threading.Thread(target=run, args=('user', 'feishu', 'user', allowed_turns))
tb = threading.Thread(target=run, args=('background', 'feishu', 'background_review', rejected_turns))
ta.start()
tb.start()
ta.join()
tb.join()
observed = [s for s in C.submits]
chk('D1 25 human turns -> 25 observations', C.jev == 25, f'JEV={C.jev}')
chk('D2 the 25 concurrent background turns produced 0 observations',
    len([s for s in observed if s[1] != 'user']) == 0)
chk('D3 no cross-contamination: every observation carries (feishu, user)',
    all(s == ('feishu', 'user') for s in observed), f'set={sorted(set(observed))}')
chk('D4 no thread raised', not errors, f'{errors}')

print('=== E. skip telemetry is content-free ===')
counter_path = config.skip_counter_path()
raw = counter_path.read_text() if counter_path.exists() else '{}'
data = json.loads(raw)
allowed_keys = {'date', 'counters'}
allowed_counter_keys = {'platform', 'turn_origin', 'reason', 'count'}
bad = [k for entry in data.values() for k in entry if k not in allowed_keys]
for entry in data.values():
    for counter in (entry.get('counters') or {}).values():
        bad += [k for k in counter if k not in allowed_counter_keys]
chk('E1 only date / platform / turn_origin / reason / count are stored', not bad, f'unexpected={sorted(set(bad))}')

banned_tokens = ('synthetic test message', 'user_message', 'sk-', 'eyJ', 'BEGIN', '@', 'turn_id',
                 'session_id', 'model"')
chk('E2 no content, identifier or credential-like token anywhere in the file',
    not [t for t in banned_tokens if t in raw], f'pattern hits={[t for t in banned_tokens if t in raw]}')

sig = inspect.signature(skip_telemetry.bump)
chk('E3 the write path has no message parameter at all',
    set(sig.parameters) == {'platform', 'turn_origin', 'reason', 'path'}, f'{sig}')

weird = skip_telemetry.bump('feishu', 'sk-' + 'A' * 60 + ' leading text', 'origin_not_user')
chk('E4 labels are bounded tokens, so a message-shaped string cannot be stored verbatim',
    len(weird['turn_origin']) <= skip_telemetry._MAX_LABEL and ' ' not in weird['turn_origin']
    and weird['turn_origin'].isascii(), f'stored={weird["turn_origin"][:24]}...')

unknown_reason = skip_telemetry.bump('feishu', 'user', 'free-form reason text from a caller')
chk('E5 an unknown reason collapses to a closed-vocabulary category',
    unknown_reason['reason'] == 'other', f'reason={unknown_reason["reason"]}')

print('=== F. anomaly detector (defense in depth, not the boundary) ===')
os.environ['JEV_INTERNAL_MARKERS'] = '||'.join(['[SYNTHETIC INTERNAL CARRIER]', '[SYNTHETIC REVIEW]'])
config._cache['at'] = 0
_before = skip_counts_by_reason()
d, j = probe('feishu', 'user', message='[SYNTHETIC INTERNAL CARRIER] body')
delta = skip_counts_by_reason().get('user|invariant_violation', 0)
chk('F1 origin=user with an internal marker is withheld: 0/0', (d, j) == (0, 0), f'-> {d}/{j}')
chk('F2 counted once as an invariant violation', delta == 1, f'delta={delta}')
chk('F3 content never grants eligibility: markers only withhold it', d == 0 and j == 0)
os.environ.pop('JEV_INTERNAL_MARKERS')
config._cache['at'] = 0

print('=== G. the accepted path carries its provenance into the record ===')
_before = skip_counts_by_reason()
d, j = probe('feishu', 'user', message='ordinary human message')
chk('G1 feishu + user -> dossier 1 / JEV 1', (d, j) == (1, 1), f'-> {d}/{j}')
chk('G2 the observation is labelled (feishu, user)', C.submits and C.submits[0] == ('feishu', 'user'),
    f'{C.submits[:1]}')
_after = skip_counts_by_reason()
chk('G3 a human turn adds no rejection count at all', _after == _before,
    f'{sorted(set(_after) - set(_before))}')
rec_fields = ('platform', 'turn_origin')
src = (ROOT / 'router' / 'shadow.py').read_text()
chk('G4 the shadow record has provenance fields', all(f"'{f}':" in src for f in rec_fields))

print()
print(f'total {len(R)} checks, failed {len(FAILS)}: {FAILS}')
sys.exit(1 if FAILS else 0)
