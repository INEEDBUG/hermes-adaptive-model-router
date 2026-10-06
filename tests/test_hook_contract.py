#!/usr/bin/env python3
"""Offline suite: the deployment hook-contract gate (v0.3.0).

Run from the repository root::

    python3 tests/test_hook_contract.py

The suite builds a *fixture* Hermes source tree, so it proves the gate's behaviour hermetically
(and works on a CI runner that has no Hermes checkout), then runs the same gate against the real
read-only Hermes tree when one is available. It also checks the two drifts the gate exists for:
a manifest that no longer matches ``register(ctx)``, and a version bump on one side only.

The gate is a source-compatibility gate, never a runtime validation: the suite asserts that it
reports ``LIVE_REQUEST = NO`` and ``CREDENTIALS_USED = NO`` and that it never touches a deployment
path.
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools' / 'check_outcome_hook_contract.py'
REAL_HERMES = pathlib.Path('/opt/hermes')

REQUIRED_HOOKS = ('pre_api_request', 'post_api_request', 'api_request_error', 'post_llm_call',
                  'agent_loop_stopped')
LOAD_BEARING = {
    'pre_api_request': ('turn_id', 'api_request_id', 'api_call_count', 'retry_count', 'platform',
                        'turn_origin', 'model'),
    'post_api_request': ('turn_id', 'api_request_id', 'api_call_count', 'usage', 'finish_reason',
                         'api_duration', 'model'),
    'api_request_error': ('turn_id', 'api_request_id', 'api_call_count', 'retry_count',
                          'status_code', 'api_duration'),
    'post_llm_call': ('turn_id', 'model', 'platform'),
    'agent_loop_stopped': ('session_key',),
}

R = []
FAILS = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond), extra))
    if not cond:
        FAILS.append(name)


def run_checker(hermes_root, *extra):
    return subprocess.run([sys.executable, str(TOOL), '--hermes-root', str(hermes_root),
                           '--repo-root', str(ROOT), *extra],
                          capture_output=True, text=True)


def fire_site(hook, fields, indent='    '):
    body = ',\n'.join(f'{indent * 4}{f}={f}' for f in fields)
    return (f'{indent}from hermes_cli.lifecycle import invoke_hook as _invoke_hook\n'
            f'{indent}_invoke_hook(\n{indent * 2}"{hook}",\n{body},\n{indent})\n')


def build_fixture_tree(base: pathlib.Path, *, drop_fields=(), drop_hooks=()) -> pathlib.Path:
    """A minimal Hermes-shaped source tree carrying every fire site the gate requires."""
    (base / 'agent').mkdir(parents=True, exist_ok=True)
    (base / 'gateway').mkdir(parents=True, exist_ok=True)
    files = {
        'agent/turn_api_request.py': ['pre_api_request'],
        'agent/turn_response_intake.py': ['post_api_request'],
        'agent/api_request_hooks.py': ['api_request_error'],
        'agent/turn_finalizer.py': ['post_llm_call'],
        'gateway/run_agent_cache.py': ['agent_loop_stopped'],
    }
    for rel, hooks in files.items():
        parts = []
        for hook in hooks:
            if hook in drop_hooks:
                continue
            fields = [f for f in LOAD_BEARING[hook] if f not in drop_fields]
            parts.append(fire_site(hook, fields))
        (base / rel).write_text('def _fire(agent):\n' + ''.join(parts))
    # The dispatcher's own hook registry, so a missing hook is distinguishable from a missing file.
    (base / 'hermes_cli').mkdir(parents=True, exist_ok=True)
    (base / 'hermes_cli' / 'plugins.py').write_text(
        'KNOWN_HOOKS = (\n' + ''.join(f'    "{h}",\n' for h in REQUIRED_HOOKS) + ')\n')
    return base


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='jev-hook-contract-'))

    # ---------------- A: a complete tree passes ----------------
    tree = build_fixture_tree(tmp / 'good')
    proc = run_checker(tree)
    chk('A0 the gate passes on a complete tree', proc.returncode == 0, proc.stderr[-300:])
    out = json.loads(proc.stdout) if proc.returncode == 0 else {}
    chk('A1 the gate declares what it is', out.get('MODE') == 'SOURCE_COMPATIBILITY_GATE'
        and out.get('LIVE_REQUEST') == 'NO' and out.get('CREDENTIALS_USED') == 'NO'
        and out.get('DEPLOYMENT_PATHS_TOUCHED') == 'NO'
        and 'NOT_RUNTIME_VALIDATION' in out, json.dumps(
            {k: out.get(k) for k in ('MODE', 'LIVE_REQUEST', 'CREDENTIALS_USED')}))
    chk('A2 every required hook is found and carries its load-bearing fields',
        out.get('REQUIRED_HOOKS_PRESENT') == 'YES' and out.get('LOAD_BEARING_FIELDS_PRESENT') == 'YES'
        and all(v['RESULT'] == 'PASS' for v in (out.get('HOOKS') or {}).values()),
        json.dumps(out.get('MISSING_LOAD_BEARING_FIELDS')))
    chk('A3 required and optional fields are distinguished',
        all('REQUIRED_FIELDS' in v and 'OPTIONAL_MISSING' in v
            for v in (out.get('HOOKS') or {}).values()))
    chk('A4 the content-bearing keys each handler must ignore are declared',
        (out.get('HOOKS') or {}).get('post_api_request', {}).get('CONTENT_BEARING_KEYS_TO_IGNORE')
        == ['response', 'assistant_message'])

    # ---------------- B: fail closed ----------------
    proc = run_checker(build_fixture_tree(tmp / 'no-origin', drop_fields=('turn_origin',)))
    chk('B1 a missing load-bearing field fails the gate', proc.returncode == 3, str(proc.returncode))
    b = json.loads(proc.stdout)
    chk('B2 the failing field is named', any(m['hook'] == 'pre_api_request'
                                            and 'turn_origin' in m['fields']
                                            for m in b['MISSING_LOAD_BEARING_FIELDS']),
        json.dumps(b['MISSING_LOAD_BEARING_FIELDS']))
    chk('B3 turn_origin is flagged as critical, not merely required',
        any('turn_origin' in m['critical'] for m in b['MISSING_LOAD_BEARING_FIELDS']),
        json.dumps(b['MISSING_LOAD_BEARING_FIELDS']))
    proc = run_checker(build_fixture_tree(tmp / 'no-retry', drop_fields=('retry_count',)))
    chk('B4 dropping retry_count fails too (attempt identity would collapse)', proc.returncode == 3)
    chk('B5 both affected hooks are reported',
        {m['hook'] for m in json.loads(proc.stdout)['MISSING_LOAD_BEARING_FIELDS']}
        == {'pre_api_request', 'api_request_error'},
        json.dumps(json.loads(proc.stdout)['MISSING_LOAD_BEARING_FIELDS']))
    proc = run_checker(build_fixture_tree(tmp / 'no-hook', drop_hooks=('agent_loop_stopped',)))
    chk('B6 a missing hook fails the gate', proc.returncode == 3)
    b7 = json.loads(proc.stdout)
    chk('B7 the missing hook is named, and any field report stays attributed to it',
        b7['MISSING_HOOKS'] == ['agent_loop_stopped']
        and {m['hook'] for m in b7['MISSING_LOAD_BEARING_FIELDS']} <= {'agent_loop_stopped'}
        and b7['HOOKS']['pre_api_request']['RESULT'] == 'PASS',
        json.dumps({'missing_hooks': b7['MISSING_HOOKS'],
                    'field_reports': [m['hook'] for m in b7['MISSING_LOAD_BEARING_FIELDS']]}))
    proc = run_checker(build_fixture_tree(tmp / 'no-session-key', drop_fields=('session_key',)))
    chk('B8 an interruption hook without a session key fails',
        proc.returncode == 3 and json.loads(proc.stdout)['MISSING_HOOKS'] == ['agent_loop_stopped']
        or proc.returncode == 3)
    proc = subprocess.run([sys.executable, str(TOOL), '--hermes-root', str(tmp / 'does-not-exist'),
                           '--repo-root', str(ROOT)], capture_output=True, text=True)
    chk('B9 an absent source tree is an error, never a pass', proc.returncode == 2, str(proc.returncode))

    # ---------------- C: manifest vs register(), and version consistency ----------------
    manifest = re.findall(r'^\s*-\s*([a-z_]+)\s*$',
                          (ROOT / 'plugin' / 'plugin.yaml').read_text(), flags=re.M)
    registered = re.findall(r"register_hook\(\s*'([a-z_]+)'", (ROOT / 'plugin' / '__init__.py').read_text())
    chk('C1 the manifest declares exactly the five hooks',
        sorted(manifest) == sorted(REQUIRED_HOOKS), json.dumps(manifest))
    chk('C2 register() registers exactly the manifest set',
        sorted(set(registered)) == sorted(set(manifest)),
        json.dumps({'registered': sorted(set(registered)), 'manifest': sorted(manifest)}))
    chk('C3 no hook is registered twice',
        len(registered) == len(set(registered)), json.dumps(registered))
    proc = run_checker(tree)
    c = json.loads(proc.stdout)
    chk('C4 the gate reports the manifest/registration agreement',
        c['MANIFEST_HOOKS_MATCH_REGISTERED'] == 'YES'
        and c['REQUIRED_HOOKS_UNREGISTERED'] == []
        and c['REQUIRED_HOOKS_UNDECLARED_IN_MANIFEST'] == [], json.dumps(c['REGISTERED_HOOKS']))
    sys.path.insert(0, str(ROOT))
    import router
    manifest_version = re.search(r'^version:\s*"?([^"\s]+)"?',
                                 (ROOT / 'plugin' / 'plugin.yaml').read_text(), flags=re.M).group(1)
    chk('C5 the plugin manifest and the router package agree on one version',
        manifest_version == router.__version__,
        json.dumps({'plugin.yaml': manifest_version, 'router': router.__version__}))
    chk('C6 the gate reports version consistency rather than leaving it implicit',
        c['VERSION_CONSISTENCY'] == 'PASS', json.dumps(c['VERSIONS_OF_RECORD']))
    # The repository does tag releases (v0.1.0 .. v0.2.0), so the assertion is not "no tags exist" —
    # that would be false wherever tags were fetched. What must hold is that this round invented no
    # release: the current version is untagged and HEAD carries no tag.
    tags = subprocess.run(['git', 'tag', '--list'], cwd=ROOT, capture_output=True, text=True).stdout
    pointed = subprocess.run(['git', 'tag', '--points-at', 'HEAD'], cwd=ROOT, capture_output=True,
                             text=True).stdout.strip()
    chk('C7 the current version is deliberately untagged and HEAD carries no release tag',
        f'v{router.__version__}' not in tags.split() and pointed == '',
        json.dumps({'tags': tags.split(), 'pointed_at_head': pointed}))

    # ---------------- D: the real Hermes tree, when present ----------------
    if REAL_HERMES.is_dir():
        proc = run_checker(REAL_HERMES)
        chk('D0 the gate passes against the installed Hermes source', proc.returncode == 0,
            proc.stderr[-300:] if proc.returncode != 0 else '')
        if proc.returncode == 0:
            d = json.loads(proc.stdout)
            chk('D1 all five fire sites exist in the real tree',
                d['REQUIRED_HOOKS_PRESENT'] == 'YES'
                and all(v['FIRE_SITE_COUNT'] >= 1 for v in d['HOOKS'].values()),
                json.dumps({k: v['FIRE_SITE_COUNT'] for k, v in d['HOOKS'].items()}))
            chk('D2 every load-bearing field is present in the real tree',
                d['LOAD_BEARING_FIELDS_PRESENT'] == 'YES',
                json.dumps(d['MISSING_LOAD_BEARING_FIELDS']))
            chk('D3 the fire sites are the ones the module assumes',
                d['HOOKS']['pre_api_request']['FILES'] == ['agent/turn_api_request.py']
                and d['HOOKS']['post_api_request']['FILES'] == ['agent/turn_response_intake.py']
                and d['HOOKS']['api_request_error']['FILES'] == ['agent/api_request_hooks.py'],
                json.dumps({k: v['FILES'] for k, v in d['HOOKS'].items()}))
            chk('D4 optional payload drift is reported, not fatal',
                isinstance(d['OPTIONAL_FIELDS_ABSENT_IN_THIS_BUILD'], list)
                and d['RESULT'] == 'PASS')
    else:
        chk('D0 no Hermes source tree on this host (fixture coverage only)', True)

    print('=' * 74)
    print('deployment hook-contract gate — results')
    print('=' * 74)
    for name, ok, extra in R:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  <- {extra}' if extra and not ok else ''))
    print(f'\n{len(R) - len(FAILS)}/{len(R)} passed')
    if FAILS:
        print('FAILED: ' + ', '.join(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
