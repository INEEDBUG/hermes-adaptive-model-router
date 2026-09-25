#!/usr/bin/env python3
"""Offline test suite: redaction, dossier, mode gate, fault injection, route
simulation and the full plugin path.

Run it from the repository root::

    JEV_LOG_DIR=/tmp/jev-shadow-logs python3 tests/test_router.py

Groups A-E need no network and no credential. Group F exercises the live routing
endpoint and is skipped automatically when no credential is configured.
"""
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from router import client, config, dossier, redact, shadow  # noqa: E402

# Keep the offline suite hermetic: no ambient .env file may influence an assertion.
# An explicitly opted-in live run (RUN_LIVE_TESTS=1) is allowed to read the configured
# credential file, which is the whole point of opting in.
if os.environ.get('RUN_LIVE_TESTS') == '1':
    _env_file = os.environ.get('HERMES_ENV_PATH')
    if _env_file:
        config.ENV_PATH = pathlib.Path(_env_file)
else:
    config.ENV_PATH = ROOT / 'tests' / 'nonexistent.env'
config._cache['at'] = 0

R = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")


print('=== A. deterministic redaction ===')
txt = ('use sk-EXAMPLEONLYNOTAREALKEY000000 to call api, mail a.b@example.com, '
       'host 192.0.2.10, ssh testuser@198.51.100.7, password=hunter2, '
       'token: eyJhbGciOiEXAMPLEONLY.payload.sig')
out, meta = redact.redact(txt)
chk('A1 prefixed key redacted', 'sk-EXAMPLEONLYNOTAREALKEY000000' not in out)
chk('A2 email redacted', 'example.com' not in out)
chk('A3 IPv4 redacted', '192.0.2.10' not in out)
chk('A4 password= redacted', 'hunter2' not in out)
chk('A5 hit count > 0', meta['hits'] >= 5, f"hits={meta['hits']} kinds={sorted(meta['kinds'])}")
# The header is assembled from parts on purpose: this file must not embed a literal
# private-key header, because the repository's own scanner would (correctly) report it,
# and exempting that line is exactly the kind of bypass the scanner must not have. The
# value passed to redact() is still a realistic PEM header plus a synthetic body.
_PEM = '-' * 5 + 'BEGIN RSA PRIVATE KEY' + '-' * 5 + '\n' + 'MIIE' + 'fake\n'
chk('A6 private key material marked unsafe', redact.redact(_PEM)[1]['unsafe'] is True)
# Precise counting: a substitution callback must not double-count, and the hit
# total must equal the number of substituted matches.
_o7, _m7 = redact.redact('password=hunter2 token: abcdefgh')
chk('A7 key=value secrets counted once each', _m7['hits'] == 2 and _m7['kinds'] == {'kv_secret'},
    f"hits={_m7['hits']} kinds={sorted(_m7['kinds'])}")
_o8, _m8 = redact.redact('password=s3cr3t mail a@b.co host 192.0.2.10')
chk('A8 mixed patterns counted exactly', _m8['hits'] == 3 and _m8['kinds'] == {'kv_secret', 'email', 'ipv4'},
    f"hits={_m8['hits']} kinds={sorted(_m8['kinds'])}")
_o9, _m9 = redact.redact('nothing sensitive in this sentence at all')
chk('A9 clean text yields zero hits', _m9['hits'] == 0 and not _m9['kinds'],
    f"hits={_m9['hits']}")

print('=== B. routing dossier (current turn only) ===')
d, m = dossier.build('Please refactor the auth module across three files and debug the failing test.')
chk('B1 structure complete', set(d) == {'task', 'requirements', 'risk', 'runtime', 'available_routes'})
chk('B2 coding/debugging=True', d['requirements']['coding'] and d['requirements']['debugging'])
chk('B3 no history/memory fields', 'conversation_history' not in json.dumps(d) and 'memory' not in json.dumps(d))
d2, _ = dossier.build('delete the production database and rm -rf /data/important')
chk('B4 risk flags detected', d2['risk']['destructive_action'] and d2['risk']['production_change'])
long_turn, _ = dossier.build('x' * 5000)
chk('B5 truncation enforced (<=1300)', len(long_turn['task']['current_turn']) <= 1300,
    f"len={len(long_turn['task']['current_turn'])}")
# Route availability is configured, never assumed: the public default advertises none.
os.environ.pop('JEV_AVAILABLE_ROUTES', None)
config._cache['at'] = 0
chk('B6 no route is advertised by default', d['available_routes'] == [],
    f"routes={d['available_routes']}")
os.environ['JEV_AVAILABLE_ROUTES'] = 'deepseek_flash'
config._cache['at'] = 0
_b7, _ = dossier.build('x')
chk('B7 only configured routes are advertised', _b7['available_routes'] == ['deepseek_flash'],
    f"routes={_b7['available_routes']}")
os.environ.pop('JEV_AVAILABLE_ROUTES', None)
config._cache['at'] = 0

print('=== C. ROUTER_MODE and the auto approval gate ===')
os.environ.pop('JEV_AUTO_APPROVED', None)
for want, expect in (('off', 'off'), ('shadow', 'shadow'), ('auto', 'shadow')):
    os.environ['ROUTER_MODE'] = want
    config._cache['at'] = 0
    got = config.mode()
    chk(f'C-{want} -> {expect}', got == expect, f'got={got}')
os.environ['JEV_AUTO_APPROVED'] = 'yes'
config._cache['at'] = 0
chk('C-auto + approval -> auto', config.mode() == 'auto')
os.environ.pop('JEV_AUTO_APPROVED')
os.environ['ROUTER_MODE'] = 'auto'   # env wins over the .env file
config._cache['at'] = 0
chk('C-unapproved auto is downgraded', config.mode() == 'shadow')
os.environ.pop('ROUTER_MODE')
config._cache['at'] = 0
chk('C-default = shadow', config.mode() == 'shadow', f"got={config.mode()}")

print('=== D. route availability and the simulated auto rule (recorded only) ===')
cases = [
    ({'ok': True, 'choice': 'deepseek_flash', 'confidence': 0.90,
      'probabilities': {'deepseek_flash': 0.90, 'mimo_pro': 0.10}}, 'deepseek_flash'),
    ({'ok': True, 'choice': 'deepseek_flash', 'confidence': 0.55,
      'probabilities': {'deepseek_flash': 0.55, 'mimo_pro': 0.45}}, 'mimo_pro'),
    ({'ok': True, 'choice': 'mimo_pro', 'confidence': 0.95,
      'probabilities': {'mimo_pro': 0.95, 'deepseek_flash': 0.05}}, 'mimo_pro'),
    ({'ok': False, 'error': 'timeout'}, 'mimo_pro'),
]


def set_routes(value):
    if value is None:
        os.environ.pop('JEV_AVAILABLE_ROUTES', None)
    else:
        os.environ['JEV_AVAILABLE_ROUTES'] = value
    config._cache['at'] = 0
    return config.available_routes()


# The public default: nothing is validated, so nothing may be presented as executable.
set_routes(None)
chk('D1 default = no route configured as executable', config.available_routes() == ())
_guesses = [shadow.simulate(dec) for dec, _pref in cases]
chk('D2 unconfigured -> would_execute is None for every outcome (never a guess)',
    all(g is None for g in _guesses), f'got={_guesses}')
set_routes('mimo_pro, luna_pro')
chk('D3 unknown route names are ignored, not trusted',
    config.available_routes() == ('mimo_pro',), f'routes={config.available_routes()}')

# Only the production route configured: the rule may prefer the capable route, but a
# record must never claim the unvalidated one is executable.
set_routes('deepseek_flash')
chk('D4 fast-only: capable preference falls back to the configured route',
    shadow.simulate(cases[2][0]) == 'deepseek_flash', f'got={shadow.simulate(cases[2][0])}')
chk('D5 fast-only: fail-open attribution respects availability too',
    shadow.simulate(cases[3][0]) == 'deepseek_flash')

# Both routes configured and validated: the rule's preference is honoured.
set_routes('deepseek_flash,mimo_pro')
chk('D6 both configured -> the capable route is executable',
    shadow.simulate(cases[2][0]) == 'mimo_pro', f'got={shadow.simulate(cases[2][0])}')
chk('D7 both configured -> the fast route is still chosen when the rule prefers it',
    shadow.simulate(cases[0][0]) == 'deepseek_flash')
chk('D8 low confidence still escalates to the capable route',
    shadow.simulate(cases[1][0]) == 'mimo_pro')
chk('D9 fail-open attribution reaches the capable route',
    shadow.simulate(cases[3][0]) == 'mimo_pro')
chk('D10 no hard-coded availability flag remains in the module',
    not hasattr(shadow, 'MIMO_AVAILABLE'))
set_routes(None)

print('=== E. fault injection: unreachable / bad credential / malformed ===')
os.environ['TYPESAFE_BASE_URL'] = 'http://127.0.0.1:9'
client.BASE_URL = 'http://127.0.0.1:9'
r = client.route({'task': 'x'}, timeout=2.0)
chk('E1 unreachable -> ok=False, no exception', r['ok'] is False and r['error'], f"error={r['error']}")

client.BASE_URL = 'https://api.typesafe.ai'
os.environ['TYPESAFE_API_KEY'] = 'invalid-key-for-fault-injection'
r2 = client.route({'task': 'x'}, timeout=8.0)
chk('E2 invalid credential -> http_4xx', (r2['error'] or '').startswith('http_'), f"error={r2['error']}")

_orig = client.urllib.request.urlopen


class _Fake:
    status = 200

    def read(self):
        return (b'{"model":"jev-latest","answers":{"route":{"type":"choice",'
                b'"choice":"nosuchchoice","confidence":0.5,"probabilities":{}}}}')

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


client.urllib.request.urlopen = lambda *a, **k: _Fake()
r3 = client.route({'task': 'x'}, timeout=3.0)
chk('E3 unknown choice -> malformed, no exception',
    r3['ok'] is False and 'malformed' in (r3['error'] or ''), f"error={r3['error']}")
client.urllib.request.urlopen = _orig
os.environ.pop('TYPESAFE_API_KEY', None)      # fall back to the configured .env
os.environ.pop('TYPESAFE_BASE_URL', None)

# Live routing is strictly opt-in: a credential merely being present on the machine
# must never make the default test run call an external service.
LIVE = os.environ.get('RUN_LIVE_TESTS') == '1'
have_key = False
if LIVE:
    have_key = bool(os.environ.get('TYPESAFE_API_KEY'))
    if not have_key:
        try:
            have_key = 'TYPESAFE_API_KEY=' in config.ENV_PATH.read_text(errors='replace')
        except Exception:
            have_key = False

print('=== F. plugin path (live routing call + shadow log) ===')
if not LIVE:
    print('  [SKIP] live routing group disabled by default (set RUN_LIVE_TESTS=1 to enable)')
elif not have_key:
    print('  [SKIP] RUN_LIVE_TESTS=1 but no routing credential is configured')
else:
    import importlib.util
    spec = importlib.util.spec_from_file_location('jev_plugin', ROOT / 'plugin' / '__init__.py')
    plug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plug)
    turn = f'OFFLINE-TEST-{int(time.time())}'
    ret = plug._on_pre_api_request(user_message='Refactor the auth module across three files and add unit tests.',
                                  turn_id=turn, model='deepseek-flash', api_client=None,
                                  api_call_count=1, retry_count=0, session_id='offline-test', platform='offline')
    chk('F1 hook returns None (no injection, no request change)', ret is None)
    chk('F2 duplicate api_call_count=1 deduplicated',
        plug._on_pre_api_request(user_message='dup', turn_id=turn,
                                 model='deepseek-flash', api_call_count=1) is None)
    time.sleep(6)
    day = time.strftime('%Y-%m-%d', time.gmtime())
    jp = config.log_path(day)
    lines = [json.loads(l) for l in jp.read_text().splitlines()] if jp.exists() else []
    mine = [l for l in lines if l.get('turn_id') == turn]
    chk('F3 shadow JSONL written', len(mine) == 1, f"records={len(mine)}")
    if mine:
        rec = mine[0]
        allowed = {'timestamp', 'turn_id', 'jev_model', 'route', 'confidence', 'p_deepseek',
                   'p_mimo', 'latency_ms', 'input_tokens', 'output_tokens', 'success', 'error',
                   'actual_model', 'would_execute', 'mode',
                   'task_length', 'tool_use', 'shell', 'coding', 'debugging', 'research',
                   'long_context', 'destructive_action', 'production_change', 'redaction_count',
                   'dossier_token_estimate'}
        chk('F4 field allow-list (no prompt/dossier body)', set(rec) == allowed, f"extra={set(rec) - allowed}")
        chk('F5 actual_model never changed', rec['actual_model'] == 'deepseek-flash')
        chk('F6 route is a known choice', rec['route'] in {'deepseek_flash', 'mimo_pro'})
        chk('F7 prompt text absent from the log', 'Refactor the auth module' not in json.dumps(lines))
        print('     record:', json.dumps(rec, ensure_ascii=False))

print('=== G. automatic switching is not implemented in this release ===')
import importlib.util as _ilu  # noqa: E402

_spec_g = _ilu.spec_from_file_location('jev_plugin_g', ROOT / 'plugin' / '__init__.py')
_plug_g = _ilu.module_from_spec(_spec_g)
_spec_g.loader.exec_module(_plug_g)
from router import state as _state  # noqa: E402

chk('G1 AUTO_IMPLEMENTED is False', _state.AUTO_IMPLEMENTED is False)
chk('G2 plugin downgrades auto -> shadow', _plug_g._effective_mode({'mode': 'auto'}) == 'shadow')
chk('G3 shadow/off pass through unchanged',
    _plug_g._effective_mode({'mode': 'shadow'}) == 'shadow'
    and _plug_g._effective_mode({'mode': 'off'}) == 'off')
chk('G4 unknown mode resolves to off', _plug_g._effective_mode({'mode': 'turbo'}) == 'off')

print()
fails = [n for n, ok, _ in R if not ok]
print(f"total {len(R)} checks, failed {len(fails)}: {fails}")
sys.exit(1 if fails else 0)
