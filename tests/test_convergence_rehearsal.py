#!/usr/bin/env python3
"""Offline production-convergence rehearsal.

Covers the properties a production → canonical convergence has to preserve, without any
network, credential, gateway or production path:

* deployment-generation metadata and the statistics split it enables (A)
* rejection-counter compatibility with an existing (legacy) counter file (B)
* the route-availability contract the convergence must configure (C)
* telemetry continuity over synthetic legacy + canonical fixtures (D)
* the human-turn boundary, one-decision-per-turn dedupe and two-session isolation (E)
* redaction of every api/access/refresh/auth label spelling (F)
* Routing Dossier structural equivalence with the known production contract (G)

Run from the repository root::

    python3 tests/test_convergence_rehearsal.py

Everything is written under a private temporary directory; the module deletes it on exit.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Redirect every path the router resolves into the throwaway root *before* importing it,
# so no module-level constant can point at a real Hermes home.
TMP = pathlib.Path(tempfile.mkdtemp(prefix='jev-convergence-rehearsal-'))
HOME = TMP / 'hermes-home'
LOGS = TMP / 'logs'
STATE = TMP / 'state'
FIXTURES = TMP / 'fixtures'
for d in (HOME, LOGS, STATE, FIXTURES):
    d.mkdir(parents=True, exist_ok=True)
os.environ.update({
    'HERMES_HOME': str(HOME),
    'JEV_LOG_DIR': str(LOGS),
    'JEV_STATE_DIR': str(STATE),
    'JEV_SKIP_COUNTER_PATH': str(LOGS / 'skipped-non-user-turn.json'),
    'JEV_ROUTER_ROOT': str(ROOT),
    'JEV_ALLOWED_PLATFORMS': 'feishu',
    'JEV_AVAILABLE_ROUTES': 'deepseek_flash,mimo_pro',
    'JEV_DEPLOYMENT_GENERATION': 'canonical-0.2.0-rehearsal',
    'ROUTER_MODE': 'shadow',
    'JEV_MIN_CONFIDENCE': '0.65',
    'JEV_MIN_MARGIN': '0.15',
    'JEV_TIMEOUT_SECONDS': '3',
    'TYPESAFE_API_KEY': 'rehearsal-dummy-key-not-a-credential',
    'HERMES_STATE_DB': str(FIXTURES / 'state.db'),
})
os.environ.pop('JEV_AUTO_APPROVED', None)

from router import client, config, dossier, redact, shadow, skip_telemetry  # noqa: E402

config._cache['at'] = 0
R = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")


def sha(p):
    return __import__('hashlib').sha256(pathlib.Path(p).read_bytes()).hexdigest()


(STATE / 'mode.json').write_text(json.dumps({'mode': 'shadow', 'by': 'rehearsal'}))

print('=== A. deployment generation metadata ===')
gen_checks = [
    (None, 'unversioned'), ('canonical-0.2.0', 'canonical-0.2.0'),
    ('Canonical 0.2.0 Rehearsal', 'canonical-0.2.0-rehearsal'),
    ('/opt/data/private path=secret', 'opt-data-private-path-secret'), ('', 'unversioned'),
]


def set_gen(value):
    if value is None:
        os.environ.pop('JEV_DEPLOYMENT_GENERATION', None)
    else:
        os.environ['JEV_DEPLOYMENT_GENERATION'] = value
    config._cache['at'] = 0
    return config.deployment_generation()


chk('A1 generation resolves to a bounded canonical token', all(set_gen(v) == want for v, want in gen_checks),
    f"got={[set_gen(v) for v, _ in gen_checks]}")
chk('A2 generation length is bounded', len(set_gen('x' * 200)) <= config.GENERATION_MAX_LEN)
chk('A3 generation carries no separators a host or credential would need',
    not any(c in set_gen('/opt/data k=v') for c in '/ =:'))
set_gen('canonical-0.2.0-rehearsal')

print('=== B. rejection-counter compatibility with a legacy counter file ===')
LEGACY_DAY, LEGACY_KEY = '2026-09-20', 'feishu|background_review|origin_not_user'
counter_path = pathlib.Path(os.environ['JEV_SKIP_COUNTER_PATH'])
counter_path.write_text(json.dumps({LEGACY_DAY: {'date': LEGACY_DAY, 'counters': {
    LEGACY_KEY: {'platform': 'feishu', 'turn_origin': 'background_review',
                 'reason': 'origin_not_user', 'count': 41}}}}, indent=1, sort_keys=True))
chk('B1 explicit counter path override is honoured',
    config.skip_counter_path() == counter_path, f"path={config.skip_counter_path()}")
entry = skip_telemetry.bump('feishu', 'background_review', 'origin_not_user')
utc_day = time.strftime('%Y-%m-%d', time.gmtime())
data = skip_telemetry.read()
chk('B2 the legacy bucket keeps its count', data[LEGACY_DAY]['counters'][LEGACY_KEY]['count'] == 41)
chk('B3 new increments land in the canonical UTC bucket',
    utc_day in data and data[utc_day]['counters'][LEGACY_KEY]['count'] == 1, f"days={sorted(data)}")
for _ in range(2):
    entry = skip_telemetry.bump('feishu', 'background_review', 'origin_not_user')
chk('B4 counters are monotonic, never reset', entry['count'] == 3, f"count={entry['count']}")
chk('B5 totals cover both day buckets without double counting',
    skip_telemetry.totals() == {'background_review': 44}, f"totals={skip_telemetry.totals()}")
counter_path.write_text('{ not json')
chk('B6 a corrupt counter file fails safe',
    skip_telemetry.bump('feishu', 'subagent', 'origin_not_user')['count'] == 1)
counter_path.write_text(json.dumps({LEGACY_DAY: {'date': LEGACY_DAY, 'counters': {
    LEGACY_KEY: {'platform': 'feishu', 'turn_origin': 'background_review',
                 'reason': 'origin_not_user', 'count': 41}}}}, indent=1, sort_keys=True))

print('=== C. route-availability convergence contract ===')


def set_routes(value):
    if value is None:
        os.environ.pop('JEV_AVAILABLE_ROUTES', None)
    else:
        os.environ['JEV_AVAILABLE_ROUTES'] = value
    config._cache['at'] = 0
    return config.available_routes()


DECISION = {'ok': True, 'choice': 'mimo_pro', 'confidence': 0.95,
            'probabilities': {'mimo_pro': 0.95, 'deepseek_flash': 0.05}}
set_routes(None)
d, _ = dossier.build('x')
chk('C1 unconfigured: no route advertised, would_execute is null',
    config.available_routes() == () and d['available_routes'] == [] and shadow.simulate(DECISION) is None)
set_routes('deepseek_flash')
chk('C2 one route configured: only that route is advertised and executable',
    config.available_routes() == ('deepseek_flash',) and shadow.simulate(DECISION) == 'deepseek_flash')
set_routes('deepseek_flash,mimo_pro')
d_both, _ = dossier.build('x')
chk('C3 the documented convergence value advertises exactly both routes',
    d_both['available_routes'] == ['deepseek_flash', 'mimo_pro']
    and shadow.simulate(DECISION) == 'mimo_pro', f"routes={d_both['available_routes']}")
set_routes('deepseek_flash,mimo_pro,luna_pro')
chk('C4 unknown route names are ignored (fail-closed)',
    config.available_routes() == ('deepseek_flash', 'mimo_pro'))
set_routes('deepseek_flash,mimo_pro')

print('=== D. telemetry continuity over synthetic fixtures ===')


def record(turn, generation=None, route='deepseek_flash', would='deepseek_flash'):
    r = {'timestamp': '2026-09-20T00:00:00+00:00', 'turn_id': turn, 'platform': 'feishu',
         'turn_origin': 'user', 'jev_model': 'jev-latest', 'route': route, 'confidence': 0.9,
         'p_deepseek': 0.9, 'p_mimo': 0.1, 'latency_ms': 100, 'input_tokens': 10,
         'output_tokens': 2, 'success': True, 'error': None, 'actual_model': 'deepseek-flash',
         'would_execute': would, 'mode': 'shadow', 'task_length': 'short', 'tool_use': False,
         'shell': False, 'coding': True, 'debugging': False, 'research': False,
         'long_context': False, 'destructive_action': False, 'production_change': False,
         'redaction_count': 0, 'dossier_token_estimate': 120}
    if generation:
        r['deployment_generation'] = generation
    return r


legacy_file = LOGS / 'shadow-2026-09-20.jsonl'
canon_file = LOGS / 'shadow-2026-09-21.jsonl'
legacy_file.write_text('\n'.join(json.dumps(record(f'SESSREAL1:L{i}',
                                                  would='deepseek_flash' if i < 2 else 'mimo_pro'))
                                 for i in range(3)) + '\n')
canon_file.write_text('\n'.join(json.dumps(record(f'SESSREAL1:C{i}', 'canonical-0.2.0-rehearsal',
                                                  route='mimo_pro', would='mimo_pro'))
                                for i in range(2)) + '\n')
db = FIXTURES / 'state.db'
con = sqlite3.connect(db)
con.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)')
con.executemany('INSERT INTO sessions VALUES (?, ?)',
                [('SESSREAL1', 'feishu'), ('SESSCLI1', 'oneshot'), ('SESSA', 'feishu'),
                 ('SESSB', 'feishu')])
con.commit()
con.close()
legacy_sha = sha(legacy_file)

spec = importlib.util.spec_from_file_location('rehearsal_stats', ROOT / 'tools' / 'shadow_stats.py')
stats = importlib.util.module_from_spec(spec)
sys.modules['rehearsal_stats'] = stats
spec.loader.exec_module(stats)

print('=== E. human-turn boundary, dedupe and two-session isolation ===')
CALLS = []


def fake_route(payload, timeout=None):
    CALLS.append({'keys': sorted(payload.keys()), 'routes': payload.get('available_routes')})
    return {'ok': True, 'choice': 'mimo_pro', 'confidence': 0.91, 'model': 'jev-latest-rehearsal',
            'probabilities': {'deepseek_flash': 0.09, 'mimo_pro': 0.91}, 'latency_ms': 12,
            'input_tokens': 40, 'output_tokens': 6, 'error': None}


client.route = fake_route
client.BASE_URL = 'http://127.0.0.1:9'
plug_spec = importlib.util.spec_from_file_location('rehearsal_plugin', ROOT / 'plugin' / '__init__.py')
plug = importlib.util.module_from_spec(plug_spec)
sys.modules['rehearsal_plugin'] = plug
plug_spec.loader.exec_module(plug)

MSG = 'Please refactor the auth module across three files and add unit tests.'
TURNS = [
    ('feishu', 'user', 'RN1', 1, None), ('feishu', 'user', 'RN1', 1, None),
    ('feishu', 'user', 'RN1', 2, None), ('feishu', 'user', 'RN1', 1, 1),
    ('feishu', 'background_review', 'RN2', 1, None), ('feishu', 'subagent', 'RN3', 1, None),
    ('feishu', 'internal_notification', 'RN4', 1, None), ('feishu', 'unknown', 'RN5', 1, None),
    ('feishu', None, 'RN6', 1, None), ('telegram', 'user', 'RN7', 1, None),
    ('', 'user', 'RN8', 1, None),
]
for platform, origin, turn, acc, retry in TURNS:
    kwargs = {'user_message': MSG, 'turn_id': f'SESSREAL1:{turn}', 'model': 'deepseek-flash',
              'api_call_count': acc, 'platform': platform}
    if origin is not None:
        kwargs['turn_origin'] = origin
    if retry is not None:
        kwargs['retry_count'] = retry
    plug._on_pre_api_request(**kwargs)
os.environ['JEV_ALLOWED_PLATFORMS'] = ''
config._cache['at'] = 0
plug._on_pre_api_request(user_message=MSG, turn_id='SESSREAL1:RN9', model='deepseek-flash',
                         api_call_count=1, platform='feishu', turn_origin='user')
os.environ['JEV_ALLOWED_PLATFORMS'] = 'feishu'
config._cache['at'] = 0
for sid in ('SESSA', 'SESSB'):
    plug._on_pre_api_request(user_message=MSG, turn_id=f'{sid}:X1', model='deepseek-flash',
                             api_call_count=1, platform='feishu', turn_origin='user')
deadline = time.time() + 15
while time.time() < deadline:
    if len(CALLS) >= 3:
        break
    time.sleep(0.2)
time.sleep(1)
ADMITTED = ('SESSREAL1:RN1', 'SESSA:X1', 'SESSB:X1')
written = []
for f in sorted(LOGS.glob('shadow-*.jsonl')):
    for line in f.read_text().splitlines():
        if line.strip():
            written.append(json.loads(line))
new = [r for r in written if r['turn_id'] in ADMITTED]
chk('E1 one routing call per admitted turn, none for rejected turns',
    len(CALLS) == 3 and len(new) == 3, f"calls={len(CALLS)} records={len(new)}")
chk('E2 duplicate / second API call / retry produced no extra decision',
    sum(1 for r in new if r['turn_id'] == 'SESSREAL1:RN1') == 1)
chk('E3 no rejected origin, missing origin, non-allowlisted platform or empty allowlist produced a record',
    not any(str(r['turn_id']).startswith('SESSREAL1:RN') and r['turn_id'] != 'SESSREAL1:RN1'
            for r in written))
chk('E4 two simultaneous sessions each produced exactly one independent record',
    sum(1 for r in new if r['turn_id'] == 'SESSA:X1') == 1
    and sum(1 for r in new if r['turn_id'] == 'SESSB:X1') == 1)
chk('E5 no cross-session contamination (own id, own provenance labels)',
    all((r['platform'], r['turn_origin']) == ('feishu', 'user') for r in new))
chk('E6 every new record carries the deployment generation',
    all(r.get('deployment_generation') == 'canonical-0.2.0-rehearsal' for r in new))
chk('E7 the routing payload carried the configured route list',
    all(c['routes'] == ['deepseek_flash', 'mimo_pro'] for c in CALLS), f"calls={CALLS}")
counters_now = skip_telemetry.read()
chk('E8 rejections were counted content-free (closed reason vocabulary)',
    all(k.split('|')[2] in skip_telemetry.ALLOWED_REASONS
        for k in counters_now[utc_day]['counters']) and len(counters_now[utc_day]['counters']) >= 5,
    f"keys={sorted(counters_now[utc_day]['counters'])}")
chk('E9 no prompt text reached telemetry or counters',
    MSG[:20] not in json.dumps(written) and MSG[:20] not in json.dumps(counters_now))

chk('D1 the legacy telemetry file is byte-identical after new writes', sha(legacy_file) == legacy_sha)
recs = stats.load_records()
src = stats.session_sources()
s = stats.build(recs, src)
chk('D2 missing generation is reported as legacy_unversioned',
    stats.generation_of({'turn_id': 'x'}) == stats.GENERATION_LEGACY)
chk('D3 both generations are tracked and the mix is flagged',
    set(s['generations']) >= {'legacy_unversioned', 'canonical-0.2.0-rehearsal'}
    and s['mixed_generations'] is True)
chk('D4 mixed record sets never publish a merged would_execute figure',
    s['would_execute'] is None and isinstance(s['would_execute_by_generation'], dict))
chk('D5 per-generation would_execute figures differ (the reason mixing is unsafe)',
    s['would_execute_by_generation']['legacy_unversioned'] == {'deepseek_flash': 2, 'mimo_pro': 1}
    and s['would_execute_by_generation']['canonical-0.2.0-rehearsal'] == {'mimo_pro': 5},
    f"by_gen={s['would_execute_by_generation']}")
one = stats.build(recs, src, generation='canonical-0.2.0-rehearsal')
chk('D6 a single-generation analysis is available for policy figures',
    one['generations'] == ['canonical-0.2.0-rehearsal'] and one['mixed_generations'] is False
    and one['would_execute'] == {'mimo_pro': 5})
chk('D7 production/test separation still classifies non-messaging sources separately',
    s['excluded'].get('excluded_source:oneshot', 0) == 0 or 'excluded_source:oneshot' in s['excluded'])

print('=== F. redaction rehearsal (synthetic values only) ===')
SYN = 'synthetic' + '-value-1234'
forms = {'api<space>key': f'api key={SYN}', 'api<TAB>key': f'api\tkey={SYN}',
         'api-key': f'api-key={SYN}', 'api_key': f'api_key={SYN}', 'apikey': f'apikey={SYN}',
         'access token': f'access token={SYN}', 'refresh token': f'refresh token={SYN}',
         'auth token': f'auth token={SYN}', 'token': f'token={SYN}',
         'client secret': f'client secret={SYN}', 'password': f'password={SYN}',
         'credential': f'credential={SYN}'}
missed = [k for k, v in forms.items() if SYN in redact.redact(v)[0]]
chk('F1 every label spelling is redacted', not missed, f"missed={missed}")
others = {'email': ('mail a.b@example.com', 'example.com'),
          'ipv4': ('host 192.0.2.10', '192.0.2.10'),
          'bearer': (f'authorization: bearer {SYN}abcdefgh', SYN),
          'long token': (f'value {"Z" * 44} end', 'Z' * 44)}
missed2 = [k for k, (text, needle) in others.items() if needle in redact.redact(text)[0]]
chk('F2 email / IPv4 / bearer / long token still handled per design', not missed2, f"missed={missed2}")
chk('F3 ordinary prose is untouched',
    redact.redact('the api sketch key api sketch')[0] == 'the api sketch key api sketch')
chk('F4 the whitespace form is counted exactly once',
    redact.redact(f'api key={SYN} mail a.b@example.com')[1]['hits'] == 2)

print('=== G. Routing Dossier structural equivalence ===')
d, meta = dossier.build(MSG)
chk('G1 key set matches the known production contract',
    set(d) == {'task', 'requirements', 'risk', 'runtime', 'available_routes'}, f"keys={sorted(d)}")
chk('G2 route list equals the documented convergence contract',
    d['available_routes'] == ['deepseek_flash', 'mimo_pro'])
chk('G3 feature schema is boolean flags',
    all(isinstance(d['requirements'][k], bool) for k in ('tool_use', 'shell', 'coding',
                                                         'debugging', 'research', 'long_context')))
chk('G4 metadata types are stable',
    d['task']['length'] in {'short', 'medium', 'long'}
    and isinstance(d['runtime']['previous_failures'], int))
chk('G5 no prompt content leaks into records',
    MSG[:20] not in json.dumps(new))

shutil.rmtree(TMP, ignore_errors=True)
print()
fails = [n for n, ok, _ in R if not ok]
print(f"total {len(R)} checks, failed {len(fails)}: {fails}")
sys.exit(1 if fails else 0)
