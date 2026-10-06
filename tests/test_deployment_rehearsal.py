#!/usr/bin/env python3
"""Offline suite: deployment rehearsal for the prospective outcome telemetry candidate (v0.3.0).

Run from the repository root::

    python3 tests/test_deployment_rehearsal.py

The rehearsal exports the **committed revision** (``git archive HEAD``) into a scratch tree — the
same frozen-export shape a future runtime generation would hold — and then loads that export in a
fresh process through a fake Hermes hook registry, with an isolated ``JEV_LOG_DIR`` and an isolated
shadow state file. It proves what a deployment decision needs:

* the plugin loads from the frozen export and registers all five hooks;
* the existing ``pre_api_request`` routing behaviour is byte-for-byte the same observation as the
  working tree produces (same dossier hand-off arguments, same rejection counters);
* the outcome hooks append one schema-valid record per admitted turn;
* Auto is never enabled and the mode stays ``shadow``;
* nothing opens a socket, and nothing is installed into the production plugin tree.

It deliberately performs **no production install**: the scratch tree is the only thing written.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PRODUCTION_PLUGIN_TREE = pathlib.Path('/opt/data/plugins/jev-shadow-router')
REQUIRED_HOOKS = ['pre_api_request', 'post_api_request', 'api_request_error', 'post_llm_call',
                  'agent_loop_stopped']

FAKE_HERMES = {
    'hermes_cli/__init__.py': '',
    'hermes_cli/lifecycle.py': '''"""Fake Hermes lifecycle dispatch: the registry a plugin registers into."""
_HOOKS = {}


def register_hook(name, fn):
    _HOOKS.setdefault(name, []).append(fn)


def has_hook(name):
    return bool(_HOOKS.get(name))


def registered():
    return {name: len(fns) for name, fns in _HOOKS.items()}


def invoke_hook(name, **kwargs):
    for fn in list(_HOOKS.get(name, [])):
        fn(**kwargs)
''',
    'hermes_cli/plugins.py': '''from hermes_cli.lifecycle import invoke_hook  # noqa: F401
''',
}

DRIVER = '''"""Rehearsal driver: load one candidate tree in a fresh process and report what it did."""
import hashlib
import importlib.util
import json
import os
import pathlib
import socket
import sys

tree = pathlib.Path(sys.argv[1])
log_dir = pathlib.Path(sys.argv[2])
state_dir = pathlib.Path(sys.argv[3])
out_path = pathlib.Path(sys.argv[4])
shim = pathlib.Path(sys.argv[5])
turn_id = sys.argv[6]

NETWORK_ATTEMPTS = []


class _Blocked(Exception):
    pass


_real_connect = socket.socket.connect
_real_create = socket.create_connection


def _blocked_connect(self, address, *a, **k):
    NETWORK_ATTEMPTS.append(address)
    raise _Blocked('rehearsal: network is forbidden')


def _blocked_create(address, *a, **k):
    NETWORK_ATTEMPTS.append(address)
    raise _Blocked('rehearsal: network is forbidden')


socket.socket.connect = _blocked_connect
socket.create_connection = _blocked_create

sys.path.insert(0, str(shim))
sys.path.insert(0, str(tree))

from hermes_cli import lifecycle as registry  # noqa: E402

from router import config, outcome, skip_telemetry, state  # noqa: E402
from router import shadow as shadow_mod  # noqa: E402

SUBMITS = []
shadow_mod.submit = lambda dossier, **kw: SUBMITS.append(
    {k: kw.get(k) for k in ('turn_id', 'actual_model', 'redaction_count', 'mode', 'platform',
                            'turn_origin')})


class Ctx:
    def register_hook(self, name, fn):
        registry.register_hook(name, fn)


spec = importlib.util.spec_from_file_location('rehearsal_plugin', tree / 'plugin' / '__init__.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
plugin.register(Ctx())

registered = registry.registered()

# The synthetic turn the suite compares between trees: one admitted human turn with a tool-loop
# request, ..., a retried error, a second request and a terminal hook, plus one non-human turn that
# must stay outside the boundary.
HUMAN = {'turn_id': turn_id, 'task_id': 'task-1', 'session_id': 'rehearsal-session',
         'api_request_id': 'rr1', 'platform': 'feishu', 'turn_origin': 'user',
         'model': 'deepseek-flash', 'provider': 'deepseek', 'api_mode': 'chat',
         'api_call_count': 1, 'retry_count': 0, 'message_count': 4, 'tool_count': 3,
         'approx_input_tokens': 1024, 'request_char_count': 4096, 'started_at': 1.0,
         'user_message': 'please summarise the last deploy log'}
INTERNAL = dict(HUMAN, turn_id=turn_id + '-internal', turn_origin='background_review',
                user_message='internal follow-up')
POST = {'turn_id': turn_id, 'task_id': 'task-1', 'session_id': 'rehearsal-session',
        'api_request_id': 'rr1', 'platform': 'feishu', 'model': 'deepseek-flash',
        'api_mode': 'chat', 'api_call_count': 1, 'api_duration': 1.25, 'finish_reason': 'stop',
        'response_model': 'deepseek-flash', 'started_at': 1.0, 'ended_at': 2.25,
        'usage': {'input_tokens': 900, 'output_tokens': 90, 'cache_read_tokens': 700,
                  'cache_write_tokens': 30, 'reasoning_tokens': 0}}
ERROR = {'turn_id': turn_id, 'task_id': 'task-1', 'session_id': 'rehearsal-session',
         'api_request_id': 'rr1', 'platform': 'feishu', 'model': 'deepseek-flash',
         'api_call_count': 1, 'api_duration': 0.2, 'retry_count': 0, 'max_retries': 3,
         'retryable': True, 'status_code': 429, 'reason': 'rate_limit'}
FINAL = {'turn_id': turn_id, 'task_id': 'task-1', 'session_id': 'rehearsal-session',
         'model': 'deepseek-flash', 'platform': 'feishu'}

registry.invoke_hook('pre_api_request', **INTERNAL)
registry.invoke_hook('pre_api_request', **HUMAN)
registry.invoke_hook('post_api_request', **POST)
registry.invoke_hook('api_request_error', **ERROR)
registry.invoke_hook('pre_api_request', **dict(HUMAN, api_request_id='rr2', api_call_count=2,
                                               retry_count=1))
registry.invoke_hook('post_api_request', **dict(POST, api_request_id='rr2', api_call_count=2,
                                                api_duration=0.9))
registry.invoke_hook('post_llm_call', **FINAL)

records, rejects = outcome.read_records_strict(log_dir=log_dir)
mode = state.resolve()
report = {
    'tree': str(tree),
    'registered_hooks': registered,
    'plugin_module_loaded': plugin.__name__,
    'submit_count': len(SUBMITS),
    'submits': SUBMITS,
    'skip_counters': skip_telemetry.read(),
    'records': records,
    'reject_counts': rejects,
    'stats': outcome.stats(),
    'state': {k: mode.get(k) for k in ('mode', 'source', 'reason')},
    'auto_implemented': bool(getattr(state, 'AUTO_IMPLEMENTED', False)),
    'auto_env_present': 'JEV_AUTO_APPROVED' in os.environ,
    'network_attempts': NETWORK_ATTEMPTS,
    'manifest_hooks': [line.strip()[2:].strip().strip('"')
                       for line in (tree / 'plugin' / 'plugin.yaml').read_text().splitlines()
                       if line.strip().startswith('- ')],
    'outcome_module_sha256': hashlib.sha256(
        (tree / 'router' / 'outcome.py').read_bytes()).hexdigest(),
    'outcome_log_files': sorted(p.name for p in (log_dir / 'outcomes').glob('*.jsonl'))
    if (log_dir / 'outcomes').is_dir() else [],
}
out_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
print(json.dumps({'ok': True, 'records': len(records)}))
'''

R = []
FAILS = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond), extra))
    if not cond:
        FAILS.append(name)


def fingerprint(path):
    p = pathlib.Path(path)
    if not p.exists():
        return None
    if p.is_dir():
        return {str(q.relative_to(p)): hashlib.sha256(q.read_bytes()).hexdigest()[:16]
                for q in sorted(p.rglob('*')) if q.is_file()}
    return hashlib.sha256(p.read_bytes()).hexdigest()


def export_head(dest: pathlib.Path) -> tuple:
    """Export the candidate exactly as a future runtime generation would hold it.

    ``EXPORT_BASIS = HEAD`` on a clean tree (the CI case, where the candidate *is* the commit).
    Locally, before the fix is committed, the export is HEAD plus an overlay of the files the
    working tree would commit — otherwise a rehearsal would test the previous revision instead of
    the candidate under review. The basis is reported so nobody has to guess which one was tested.
    """
    dest.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                         text=True).stdout.strip()
    basis = 'HEAD'
    tar = subprocess.run(['git', 'archive', '--format=tar', 'HEAD'], cwd=ROOT,
                         capture_output=True)
    extracted = False
    if tar.returncode == 0:
        extracted = subprocess.run(['tar', '-x', '-C', str(dest)], input=tar.stdout,
                                   capture_output=True).returncode == 0
    if not extracted:
        for sub in ('router', 'plugin', 'tools'):
            shutil.copytree(ROOT / sub, dest / sub,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        return sha + '-worktree-copy', 'WORKTREE_COPY'
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True,
                            text=True).stdout
    overlaid = []
    for line in status.splitlines():
        rel = line[3:].strip()
        if not rel or ' -> ' in rel:
            continue
        src = ROOT / rel
        if not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        overlaid.append(rel)
    if overlaid:
        basis = 'HEAD+WORKTREE_OVERLAY:' + ','.join(sorted(overlaid))
    return sha, basis


def rehearse(scratch: pathlib.Path, name: str, tree: pathlib.Path, shim: pathlib.Path,
             turn_id: str) -> dict:
    log_dir = scratch / f'{name}-logs'
    state_dir = scratch / f'{name}-state'
    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'mode.json').write_text(json.dumps({'mode': 'shadow'}))
    out = scratch / f'{name}-report.json'
    env = dict(os.environ)
    env.update({'HERMES_HOME': str(scratch / f'{name}-home'), 'JEV_LOG_DIR': str(log_dir),
                'JEV_STATE_DIR': str(state_dir), 'JEV_DEPLOYMENT_GENERATION': f'rehearsal-{name}',
                'JEV_AVAILABLE_ROUTES': 'deepseek_flash,mimo_pro', 'JEV_ALLOWED_PLATFORMS': 'feishu',
                'JEV_SKIP_COUNTER_PATH': str(log_dir / 'skipped-non-user-turn.json'),
                'ROUTER_MODE': 'shadow', 'NO_PROXY': '*'})
    for key in ('JEV_AUTO_APPROVED', 'JEV_MIN_CONFIDENCE', 'JEV_MIN_MARGIN',
                'JEV_TIMEOUT_SECONDS', 'JEV_INTERNAL_MARKERS'):
        env.pop(key, None)
    (scratch / f'{name}-home').mkdir(exist_ok=True)
    proc = subprocess.run([sys.executable, str(scratch / 'rehearse_driver.py'), str(tree),
                           str(log_dir), str(state_dir), str(out), str(shim), turn_id],
                          capture_output=True, text=True, env=env, cwd=str(scratch))
    return {'proc': proc, 'report': json.loads(out.read_text()) if out.exists() else {}}


def main():
    scratch = pathlib.Path(tempfile.mkdtemp(prefix='jev-deployment-rehearsal-'))
    for rel, text in FAKE_HERMES.items():
        target = scratch / 'fake_hermes' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (scratch / 'rehearse_driver.py').write_text(DRIVER)
    shim = scratch / 'fake_hermes'

    prod_before = fingerprint(PRODUCTION_PLUGIN_TREE)

    frozen = scratch / 'candidate'
    sha, basis = export_head(frozen)
    chk('A0 the frozen export exists and carries the candidate module',
        (frozen / 'router' / 'outcome.py').is_file() and (frozen / 'plugin' / 'plugin.yaml').is_file())
    chk('A1 the export is the committed revision (or an explicit worktree copy)',
        len(sha) == 40 or sha.endswith('-worktree-copy'), sha)
    chk('A2 the rehearsal declares what it exported',
        basis == 'HEAD' or basis.startswith('HEAD+WORKTREE_OVERLAY:')
        or basis == 'WORKTREE_COPY', basis)
    chk('A3 the export carries this candidate, not a stale revision',
        'read_records_strict' in (frozen / 'router' / 'outcome.py').read_text()
        and 'validate_record' in (frozen / 'router' / 'outcome.py').read_text(),
        f'export basis was {basis}')
    chk('A4 the export carries the hardened gate tools',
        (frozen / 'tools' / 'check_outcome_hook_contract.py').is_file())

    frozen_run = rehearse(scratch, 'frozen', frozen, shim, 'rehearsal:1')
    work_run = rehearse(scratch, 'worktree', ROOT, shim, 'rehearsal:2')
    for label, run in (('frozen', frozen_run), ('worktree', work_run)):
        proc = run['proc']
        chk(f'B.{label} the plugin loads in a fresh process', proc.returncode == 0,
            (proc.stderr or proc.stdout)[-400:])

    rep = frozen_run['report']
    if rep:
        chk('B1 all five hooks register through the registry',
            sorted(rep['registered_hooks']) == sorted(REQUIRED_HOOKS),
            json.dumps(rep['registered_hooks']))
        chk('B2 each hook is registered exactly once',
            set(rep['registered_hooks'].values()) == {1}, json.dumps(rep['registered_hooks']))
        chk('B3 the manifest in the export agrees with the registration',
            sorted(rep['manifest_hooks']) == sorted(REQUIRED_HOOKS), json.dumps(rep['manifest_hooks']))
        chk('B4 the non-human turn never reached the routing observation',
            rep['submit_count'] == 1 and all(s['turn_origin'] == 'user' for s in rep['submits']),
            json.dumps(rep['submits']))
        chk('B5 the observation still runs in shadow mode and never changes the model',
            rep['submits'][0]['mode'] == 'shadow'
            and rep['submits'][0]['actual_model'] == 'deepseek-flash', json.dumps(rep['submits']))
        rec = rep['records']
        chk('B6 exactly one terminal record was appended for the admitted turn',
            len(rec) == 1 and rep['reject_counts']['rejected'] == {},
            json.dumps({k: rep[k] for k in ('reject_counts', 'outcome_log_files')}))
        if rec:
            r = rec[0]
            chk('B7 the record is schema-valid and scoped to the routing cohort',
                r['schema_version'] == 'turn-outcome-v1' and r['outcome_scope'] == 'routing_attempt'
                and r['deployment_generation'] == 'rehearsal-frozen')
            chk('B8 request, error, retry and usage accounting are correct',
                r['api_request_count'] == 2 and r['api_success_count'] == 2
                and r['api_error_count'] == 1 and r['retry_count_observed'] == 1
                and r['api_input_tokens_sum'] == 1800 and r['api_output_tokens_sum'] == 180,
                json.dumps({k: r[k] for k in ('api_request_count', 'api_success_count',
                                              'api_error_count', 'retry_count_observed')}))
            chk('B9 the turn is complete and exactly attributed',
                r['terminal_status'] == 'completed' and r['attribution_quality'] == 'EXACT_PROSPECTIVE'
                and r['runtime_error_observed'] is True)
            chk('B10 no content and no raw identifier reached the record',
                'summarise the last deploy log' not in json.dumps(r)
                and 'rehearsal:1' not in json.dumps(r) and 'rehearsal-session' not in json.dumps(r))
        chk('B11 the append-only day file is the only telemetry written',
            rep['outcome_log_files'] == ['turn-outcome-%s.jsonl'
                                         % __import__('time').strftime('%Y-%m-%d')],
            json.dumps(rep['outcome_log_files']))
        chk('B12 mode stayed shadow and Auto was never enabled',
            rep['state']['mode'] == 'shadow' and rep['auto_implemented'] is False
            and rep['auto_env_present'] is False, json.dumps(rep['state']))
        chk('B13 no socket was opened', rep['network_attempts'] == [],
            json.dumps(rep['network_attempts']))

    wrep = work_run['report']
    if rep and wrep:
        # The two runs use different synthetic turn ids by construction; everything the routing
        # observation hands over must still be identical.
        strip_id = lambda run: [{k: v for k, v in s.items() if k != 'turn_id'}
                                for s in run['report']['submits']]
        chk('C1 the frozen export and the working tree observe the same turn identically',
            strip_id(frozen_run) == strip_id(work_run),
            json.dumps([strip_id(frozen_run), strip_id(work_run)]))
        chk('C2 the rejection counters are identical',
            json.dumps(rep['skip_counters'], sort_keys=True)
            == json.dumps(wrep['skip_counters'], sort_keys=True))
        frozen_rec = [{k: v for k, v in r.items() if k not in ('deployment_generation',
                                                               'turn_correlation', 'duration_ms')}
                      for r in rep['records']]
        work_rec = [{k: v for k, v in r.items() if k not in ('deployment_generation',
                                                             'turn_correlation', 'duration_ms')}
                    for r in wrep['records']]
        chk('C3 the outcome record content is identical except generation and timing',
            frozen_rec == work_rec, json.dumps([frozen_rec, work_rec])[:400])
        chk('C4 both runs leave Auto disabled',
            wrep['auto_implemented'] is False and wrep['state']['mode'] == 'shadow')

    # ---------------- D: nothing was installed ----------------
    chk('D0 the rehearsal tree is not the production plugin tree',
        str(frozen) != str(PRODUCTION_PLUGIN_TREE))
    chk('D1 the working tree is clean of rehearsal artefacts',
        not (ROOT / 'rehearsal-report.json').exists() and not (ROOT / 'outcomes').exists())
    if prod_before is not None:
        chk('D2 the production plugin tree is byte-identical after the rehearsal',
            fingerprint(PRODUCTION_PLUGIN_TREE) == prod_before,
            'the rehearsal touched the production plugin tree')
    else:
        chk('D2 no production plugin tree on this host (nothing to compare)', True)

    print('=' * 74)
    print('deployment rehearsal — results')
    print('=' * 74)
    for name, ok, extra in R:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  <- {extra}' if extra and not ok else ''))
    print(f'\n{len(R) - len(FAILS)}/{len(R)} passed')
    if FAILS:
        print('FAILED: ' + ', '.join(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
