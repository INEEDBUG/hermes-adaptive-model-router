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
chk('A6 private key material marked unsafe',
    redact.redact('-----BEGIN RSA PRIVATE KEY-----\nMIIEfake\n')[1]['unsafe'] is True)

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

print('=== D. simulated future auto rules (recorded only) ===')
cases = [
    ({'ok': True, 'choice': 'deepseek_flash', 'confidence': 0.90,
      'probabilities': {'deepseek_flash': 0.90, 'mimo_pro': 0.10}}, 'deepseek_flash'),
    ({'ok': True, 'choice': 'deepseek_flash', 'confidence': 0.55,
      'probabilities': {'deepseek_flash': 0.55, 'mimo_pro': 0.45}}, 'mimo_pro'),
    ({'ok': True, 'choice': 'mimo_pro', 'confidence': 0.95,
      'probabilities': {'mimo_pro': 0.95, 'deepseek_flash': 0.05}}, 'mimo_pro'),
    ({'ok': False, 'error': 'timeout'}, 'mimo_pro'),
]
for i, (dec, expect) in enumerate(cases, 1):
    got = shadow.simulate(dec)
    chk(f'D{i} would_execute={expect}', got == expect, f'got={got}')
chk('D-MiMo validated -> MIMO_AVAILABLE=True', shadow.MIMO_AVAILABLE is True)
shadow.MIMO_AVAILABLE = False
chk('D5 falls back to the fast route when unavailable', shadow.simulate(cases[2][0]) == 'deepseek_flash')
shadow.MIMO_AVAILABLE = True

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

have_key = bool(os.environ.get('TYPESAFE_API_KEY'))
if not have_key:
    try:
        have_key = 'TYPESAFE_API_KEY=' in config.ENV_PATH.read_text(errors='replace')
    except Exception:
        have_key = False

print('=== F. plugin path (live routing call + shadow log) ===')
if not have_key:
    print('  [SKIP] no routing credential configured; group F skipped')
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

print()
fails = [n for n, ok, _ in R if not ok]
print(f"total {len(R)} checks, failed {len(fails)}: {fails}")
sys.exit(1 if fails else 0)
