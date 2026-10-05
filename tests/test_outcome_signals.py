#!/usr/bin/env python3
"""Offline suite: privacy-safe outcome-signal audit (v0.3.0).

Run from the repository root::

    python3 tests/test_outcome_signals.py

Everything happens inside a private temporary tree. The suite emulates a live deployment by
exporting JEV_ROUTER_ROOT / JEV_SKIP_COUNTER_PATH / JEV_LOG_DIR / JEV_STATE_DIR /
JEV_DEPLOYMENT_GENERATION / JEV_AVAILABLE_ROUTES / HERMES_HOME plus the candidate usage-database
path variables (HERMES_STATE_DB / HERMES_USAGE_DB) at sentinel locations, then proves that neither
this suite nor the tool it drives reads, writes or mutates them. A live deployment's telemetry,
counter, runtime tree, state database and usage database must all stay byte-identical.

Groups
    A  join coverage and turn-boundary evidence on a synthetic mixed-grain fixture
    B  anti-replication: a session aggregate is never spread over sibling turns
    C  generation separation (legacy is never merged into canonical figures)
    D  content and identifier safety (no ids, no content, no scalar quality score)
    E  isolation: hostile parent environment, sentinel tree and databases byte-identical
    F  the tool refuses to write an aggregate inside a deployment
    G  the routing-quality integration is optional and never turns a feature into ground truth
    H  hook audit and prospective design honesty
"""
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools' / 'outcome_signals.py'
QUALITY = ROOT / 'tools' / 'routing_quality.py'
CANON = 'canonical-dff8b11'
LEGACY = 'legacy_unversioned'

R = []
FAILS = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond), extra))
    if not cond:
        FAILS.append(name)


def rec(turn_id, *, generation, route='deepseek_flash', conf=0.58, would='mimo_pro', ts='2026-10-05T10:00:00Z'):
    r = {'turn_id': turn_id, 'platform': 'feishu', 'turn_origin': 'user', 'route': route,
         'confidence': conf, 'success': True, 'error': None, 'would_execute': would,
         'mode': 'shadow', 'actual_model': 'deepseek-flash', 'timestamp': ts, 'latency_ms': 1000,
         'jev_model': 'jev-test', 'p_deepseek': 0.79, 'p_mimo': 0.21}
    if generation:
        r['deployment_generation'] = generation
    return r


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as fh:
        for r in records:
            fh.write(json.dumps(r) + '\n')


def fingerprint(path):
    p = pathlib.Path(path)
    if not p.exists():
        return None
    if p.is_dir():
        return {str(q.relative_to(p)): (q.stat().st_size, int(q.stat().st_mtime))
                for q in sorted(p.rglob('*'))}
    return (p.stat().st_size, int(p.stat().st_mtime), hashlib.sha256(p.read_bytes()).hexdigest())


def build_state_db(path, sessions):
    """Minimal structural state database. Only structural columns; no message content."""
    con = sqlite3.connect(path)
    con.executescript("""
        create table sessions (id text primary key, source text, input_tokens integer,
            output_tokens integer, api_call_count integer, tool_call_count integer,
            rewind_count integer, end_reason text, handoff_error text,
            compression_failure_error text, estimated_cost_usd real);
        create table session_model_usage (session_id text, model text, input_tokens integer,
            output_tokens integer, cache_read_tokens integer, cache_write_tokens integer,
            api_call_count integer, first_seen real, last_seen real);
        create table messages (session_id text, role text, timestamp real, finish_reason text);
    """)
    for i, (sid, rows) in enumerate(sessions.items()):
        con.execute('insert into sessions values (?,?,?,?,?,?,?,?,?,?,?)',
                    (sid, 'feishu', 1000 + i, 200, 5, 3, 0, None, None, None, 0.11))
        con.execute('insert into session_model_usage values (?,?,?,?,?,?,?,?,?)',
                    (sid, 'deepseek-flash', 1000, 200, 800, 100, 5, 1.0, 2.0))
        for role, ts, fr in rows:
            con.execute('insert into messages values (?,?,?,?)', (sid, role, ts, fr))
    con.commit()
    con.close()


def run_tool(env, tool, *args):
    base = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.run([sys.executable, str(tool), *args], capture_output=True, text=True,
                          env=dict(base, **env))


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='jev-outcome-signals-'))
    logs = tmp / 'logs'
    sentinel = tmp / 'sentinel'
    for sub in ('logs', 'state', 'runtime/router', 'db'):
        (sentinel / sub).mkdir(parents=True, exist_ok=True)

    # ---- fixture: mixed grain, two generations, one single-turn and one multi-turn session ----
    write_jsonl(logs / 'shadow-2026-10-05.jsonl', [
        rec('fx-canon-single:t1', generation=CANON, conf=0.40),
        rec('fx-canon-multi:t1', generation=CANON, conf=0.90),
        rec('fx-canon-multi:t2', generation=CANON, conf=0.70),
        rec('fx-legacy-single:t1', generation=None, conf=0.55),
        rec('fx-legacy-multi:t1', generation=None, conf=0.66),
        rec('fx-legacy-multi:t2', generation=None, conf=0.44),
        rec('fx-legacy-multi:t3', generation=None, conf=0.80),
    ])
    # a non-user turn must never enter the cohort
    non_user = rec('fx-canon-single:t9', generation=CANON, conf=0.80)
    non_user['turn_origin'] = 'background_review'
    write_jsonl(logs / 'shadow-2026-10-06.jsonl', [non_user])

    state_db = tmp / 'state.db'
    build_state_db(state_db, {
        'fx-canon-single': [('user', 1000.0, None), ('assistant', 1010.0, 'tool_calls'),
                            ('assistant', 1020.0, 'stop')],
        'fx-canon-multi': [('user', 2000.0, None), ('assistant', 2010.0, 'stop'),
                           ('user', 2100.0, None), ('assistant', 2110.0, 'incomplete')],
        'fx-legacy-single': [('user', 3000.0, None), ('assistant', 3009.0, 'stop')],
        'fx-legacy-multi': [('user', 4000.0, None), ('assistant', 4005.0, 'stop'),
                            ('user', 4100.0, None), ('assistant', 4104.0, 'stop'),
                            ('user', 4200.0, None), ('assistant', 4212.0, 'stop')],
    })
    cg = tmp / 'context-guard-telemetry.jsonl'
    cg.write_text(json.dumps({'provider': 'deepseek', 'model': 'deepseek-flash',
                              'estimated_context_tokens': 4096, 'call_site': 'preflight_request_tokens'}) + '\n'
                  + json.dumps({'provider': 'deepseek', 'model': 'deepseek-flash',
                                'estimate_error_unknown': 'YES'}) + '\n')

    # ---- sentinel deployment: a live deployment, from this suite's point of view ----
    write_jsonl(sentinel / 'logs' / 'shadow-2026-10-05.jsonl', [rec('sentinel:t1', generation='sentinel-generation')])
    (sentinel / 'logs' / 'skipped-non-user-turn.json').write_text('{"sentinel": true}\n')
    (sentinel / 'state' / 'session.json').write_text('{"sentinel": true}\n')
    (sentinel / 'runtime' / 'router' / 'shadow.py').write_text('# sentinel\n')
    build_state_db(sentinel / 'db' / 'state.db', {'sentinel-session': [('user', 1.0, None)]})
    usage_db = sentinel / 'db' / 'usage.db'
    usage_db.write_text('{"sentinel": true}\n')

    parent_env = {
        'JEV_LOG_DIR': str(sentinel / 'logs'),
        'JEV_STATE_DIR': str(sentinel / 'state'),
        'JEV_ROUTER_ROOT': str(sentinel / 'runtime'),
        'JEV_SKIP_COUNTER_PATH': str(sentinel / 'logs' / 'skipped-non-user-turn.json'),
        'JEV_DEPLOYMENT_GENERATION': 'sentinel-generation',
        'JEV_AVAILABLE_ROUTES': 'sentinel_route',
        'HERMES_HOME': str(sentinel),
        'HERMES_STATE_DB': str(sentinel / 'db' / 'state.db'),
        'HERMES_USAGE_DB': str(usage_db),
    }
    before = {name: fingerprint(p) for name, p in {
        'logs': sentinel / 'logs', 'state': sentinel / 'state', 'runtime': sentinel / 'runtime',
        'db': sentinel / 'db', 'counter': sentinel / 'logs' / 'skipped-non-user-turn.json',
        'usage_db': usage_db, 'state_db': sentinel / 'db' / 'state.db'}.items()}
    before_fixture_db = fingerprint(state_db)

    env = dict(parent_env, HERMES_HOME=str(tmp))
    proc = run_tool(env, TOOL, '--generation', 'all', '--log-dir', str(logs),
                    '--state-db', str(state_db), '--context-guard-telemetry', str(cg))
    chk('A0 audit runs cleanly', proc.returncode == 0, proc.stderr[-300:])
    if proc.returncode != 0:
        report()
        return 1
    out = json.loads(proc.stdout)
    cov = out['HISTORICAL_OUTCOME_JOIN_COVERAGE']
    canon, legacy = cov[CANON], cov[LEGACY]

    # ---------------- A: join coverage ----------------
    chk('A1 canonical human turns counted (non-user turn excluded)', canon['TOTAL_HUMAN_TURNS'] == 3,
        str(canon['TOTAL_HUMAN_TURNS']))
    chk('A2 canonical single-turn session detected', canon['SINGLE_TURN_SESSIONS'] == 1)
    chk('A3 canonical multi-turn session detected', canon['MULTI_TURN_SESSIONS'] == 1)
    chk('A4 legacy turns counted separately', legacy['TOTAL_HUMAN_TURNS'] == 4, str(legacy['TOTAL_HUMAN_TURNS']))
    chk('A5 turn start detectable from structure', out['TURN_BOUNDARY']['TURN_START_DETECTABLE'] == 'YES')
    chk('A6 turn end only partially detectable',
        out['TURN_BOUNDARY']['TURN_END_DETECTABLE'] == 'PARTIAL', out['TURN_BOUNDARY']['TURN_END_DETECTABLE'])
    chk('A7 next user message detectable', out['TURN_BOUNDARY']['NEXT_USER_DETECTABLE'] == 'YES')
    chk('A8 boundary reliability is MEDIUM, not overstated',
        out['TURN_BOUNDARY']['TURN_BOUNDARY_RELIABILITY'] == 'MEDIUM')
    ev = out['TURN_BOUNDARY']['TURN_BOUNDARY_EVIDENCE']
    # fixture: 7 user rows, 6 assistant rows carrying finish_reason='stop'
    chk('A9 completion marker ratio measured, not assumed',
        abs(ev['completion_per_user_row'] - round(6 / 7, 3)) < 1e-9, str(ev['completion_per_user_row']))
    chk('A10 no arbitrary follow-up threshold is applied',
        out['TURN_BOUNDARY']['FOLLOWUP_GAP_DISTRIBUTION_S']['no_threshold_is_applied'] is True)
    chk('A11 follow-up gap distribution is reported as EXPERIMENTAL',
        out['TURN_BOUNDARY']['FOLLOWUP_GAP_DISTRIBUTION_S']['status'] == 'EXPERIMENTAL')

    # ---------------- B: anti-replication ----------------
    per_signal = canon['PER_SIGNAL']
    tok = per_signal['per_turn_token_and_cache_values']
    chk('B1 token/cache aggregates exactly joinable only on single-turn sessions',
        tok['EXACTLY_JOINABLE_TURNS'] == canon['SINGLE_TURN_SESSION_TURNS'] == 1, str(tok))
    chk('B2 multi-turn siblings are unjoinable, not copied',
        tok['UNJOINABLE_TURNS'] == canon['MULTI_TURN_SESSION_TURNS'] == 2, str(tok))
    chk('B3 replication is forbidden by contract',
        out['STRUGGLE_STRUCTURE']['SESSION_AGGREGATE_REPLICATION_FORBIDDEN'] == 'YES')
    chk('B4 single-turn subset is flagged as biased',
        out['STRUGGLE_STRUCTURE']['SINGLE_TURN_SESSION_SUBSET']['selection_bias'] == 'YES')
    soft = {f['signal']: f for f in out['STRUGGLE_STRUCTURE']['SOFT_STRUGGLE_FEATURES']}
    chk('B5 session-grain features declare EXACT_PER_TURN NO',
        soft['input/output token volume']['exact_per_turn'] == 'NO'
        and soft['tool_call_count']['exact_per_turn'] == 'NO'
        and soft['cost']['exact_per_turn'] == 'NO')
    chk('B6 soft features are labelled features with ambiguity',
        all(f.get('ambiguity') for f in soft.values()))
    chk('B7 no soft feature claims to be a hard failure',
        out['STRUGGLE_STRUCTURE']['HARD_STRUGGLE_SIGNAL_AVAILABLE'] == 'PARTIAL')

    # ---------------- C: generation separation ----------------
    chk('C1 both generations reported without merging', set(cov) == {CANON, LEGACY})
    chk('C2 canonical figures do not absorb legacy turns',
        canon['TOTAL_HUMAN_TURNS'] + legacy['TOTAL_HUMAN_TURNS'] == 7)
    chk('C3 sentinel generation never analysed', 'sentinel-generation' not in json.dumps(out))
    chk('C4 parent JEV_AVAILABLE_ROUTES did not leak', 'sentinel_route' not in json.dumps(out))

    # ---------------- D: content and identifier safety ----------------
    blob = json.dumps(out)
    leaks = [s for s in ('fx-canon', 'fx-legacy', 'sentinel:t1', 'sentinel-session') if s in blob]
    chk('D1 no turn or session identifier reaches the output', not leaks, str(leaks))
    chk('D2 no scalar quality score is produced',
        not any(k in blob for k in ('quality_score', 'routing_correctness_label', 'SCORE')))
    chk('D3 ground-truth boundary is explicit',
        out['STRUGGLE_STRUCTURE']['ROUTING_CORRECTNESS_GROUND_TRUTH_AVAILABLE'] == 'NO'
        and out['STRUGGLE_STRUCTURE']['COUNTERFACTUAL_MIMO_OUTCOME_AVAILABLE'] == 'NO')
    chk('D4 per-turn exactness answered for every token family',
        all(out['PER_TURN_EXACTNESS'][k] == 'NO' for k in
            ('INPUT_TOKENS_PER_TURN_EXACT', 'OUTPUT_TOKENS_PER_TURN_EXACT',
             'CACHE_READ_PER_TURN_EXACT', 'CACHE_WRITE_PER_TURN_EXACT', 'COST_PER_TURN_EXACT',
             'PROMPT_CACHE_HIT_PER_TURN')))
    chk('D5 single-turn exception documented',
        'SINGLE_TURN_SESSION_EXACT_JOIN' in out['PER_TURN_EXACTNESS']['single_turn_session_exception'])
    chk('D6 content reading is denied explicitly', out['CONTENT_READ'] == 'NO')
    chk('D7 cache switch cost is not claimed measurable per turn',
        out['CACHE_SWITCH_COST_MEASURABLE_NOW'] == 'PARTIAL'
        and 'counterfactual' in out['CACHE_REUSE_CONTEXT']['not_usable_for'])
    chk('D8 retrospective struggle analysis is not overstated',
        out['RETROSPECTIVE_STRUGGLE_ANALYSIS'] in ('NOT_POSSIBLE', 'DESCRIPTIVE_ONLY', 'PROVISIONAL'))

    # ---------------- E: isolation ----------------
    after = {name: fingerprint(p) for name, p in {
        'logs': sentinel / 'logs', 'state': sentinel / 'state', 'runtime': sentinel / 'runtime',
        'db': sentinel / 'db', 'counter': sentinel / 'logs' / 'skipped-non-user-turn.json',
        'usage_db': usage_db, 'state_db': sentinel / 'db' / 'state.db'}.items()}
    for name in before:
        chk(f'E1.{name} sentinel path byte-identical', before[name] == after[name],
            f'{name} was touched by an offline run')
    chk('E2 fixture state database byte-identical after the audit',
        fingerprint(state_db) == before_fixture_db, 'the audit mutated the state database')
    # hostile parent environment that points at paths which do not exist: the run must fail loudly
    # rather than fall back to any live deployment, and the sentinel tree must stay inert
    void_env = dict(parent_env, HERMES_HOME=str(sentinel / 'absent'),
                    HERMES_STATE_DB=str(sentinel / 'absent' / 'state.db'),
                    JEV_LOG_DIR=str(sentinel / 'absent' / 'logs'))
    void = run_tool(void_env, TOOL, '--generation', 'all')
    chk('E3 unresolvable parent environment fails loudly instead of analysing something else',
        void.returncode != 0 and 'HISTORICAL_OUTCOME_JOIN_COVERAGE' not in void.stdout,
        f'rc={void.returncode}')
    chk('E4 sentinel tree still byte-identical after the hostile run',
        fingerprint(sentinel / 'logs') == before['logs']
        and fingerprint(sentinel / 'db') == before['db'])

    # ---------------- F: refuses to write inside a deployment ----------------
    dest = sentinel / 'logs' / 'aggregate.json'
    refused = run_tool(env, TOOL, '--generation', 'all', '--log-dir', str(logs),
                       '--state-db', str(state_db), '--json', str(dest))
    chk('F1 refuses to write an aggregate inside a deployment', refused.returncode == 4,
        f'rc={refused.returncode}')
    chk('F2 no aggregate file was created', not dest.exists())
    outside = pathlib.Path(tempfile.mkdtemp(prefix='jev-outcome-out-'))
    ok_dest = outside / 'aggregate.json'
    wrote = run_tool(env, TOOL, '--generation', 'all', '--log-dir', str(logs),
                     '--state-db', str(state_db), '--json', str(ok_dest))
    chk('F3 writes an aggregate outside the deployment', wrote.returncode == 0 and ok_dest.exists())

    # ---------------- G: routing-quality integration ----------------
    quality_env = dict(env, HERMES_HOME=str(tmp), HERMES_STATE_DB=str(state_db))
    base = run_tool(quality_env, QUALITY, '--generation', CANON, '--log-dir', str(logs),
                    '--runtime-root', str(ROOT), '--state-db', str(state_db))
    chk('G0 routing-quality runs on the fixture', base.returncode == 0, base.stderr[-200:])
    if base.returncode == 0:
        plain = json.loads(base.stdout)
        chk('G1 outcome correlation is absent without the flag', 'OUTCOME_CORRELATION' not in plain)
        joined = run_tool(quality_env, QUALITY, '--generation', CANON, '--log-dir', str(logs),
                          '--runtime-root', str(ROOT), '--state-db', str(state_db),
                          '--outcome-aggregate', str(ok_dest))
        chk('G2 correlation view is produced', joined.returncode == 0 and
            json.loads(joined.stdout).get('OUTCOME_CORRELATION', {}).get('CORRELATION_NOT_CAUSATION') == 'YES')
        cov_out = json.loads(joined.stdout)['OUTCOME_CORRELATION']
        chk('G3 the view states what it cannot conclude', bool(cov_out.get('what_this_cannot_say')))
        chk('G4 every existing aggregate key is unchanged by the flag',
            {k: v for k, v in json.loads(joined.stdout).items() if k != 'OUTCOME_CORRELATION'}
            == {k: v for k, v in plain.items() if k != 'OUTCOME_CORRELATION'},
            'the optional flag changed another result')
    unreadable = run_tool(quality_env, QUALITY, '--generation', CANON, '--log-dir', str(logs),
                          '--runtime-root', str(ROOT), '--state-db', str(state_db),
                          '--outcome-aggregate', str(tmp / 'missing.json'))
    chk('G5 a missing aggregate degrades instead of failing the run',
        unreadable.returncode in (0, 3), str(unreadable.returncode))

    # ---------------- H: hook audit and design honesty ----------------
    chk('G6 sentinel state database still byte-identical after the routing-quality runs',
        fingerprint(sentinel / 'db' / 'state.db') == before['state_db'])

    chk('H1 post-turn hook availability is PARTIAL, not claimed reliable',
        out['RELIABLE_POST_TURN_HOOK_AVAILABLE'] == 'PARTIAL')
    chk('H2 hook audit records when each hook fires and its risk',
        all(h.get('when_fires') and h.get('risk') for h in out['HOOK_AUDIT']['HOOKS']))
    chk('H3 an unconfirmed plugin hook is reported as documented, not verified',
        any(h['hook'] == 'on_session_end' and h['exists'] == 'DOCUMENTED'
            for h in out['HOOK_AUDIT']['HOOKS']))
    chk('H4 prospective telemetry is required', out['PROSPECTIVE_OUTCOME_TELEMETRY_REQUIRED'] == 'YES')
    chk('H5 struggle vector carries attribution quality',
        'attribution_quality' in out['STRUGGLE_STRUCTURE']['DEFAULT_MODEL_STRUGGLE_VECTOR_V1'])
    chk('H6 struggle vector forbids content fields',
        'prompt' in out['STRUGGLE_STRUCTURE']['DEFAULT_MODEL_STRUGGLE_VECTOR_V1']['note'])
    chk('H7 matrix covers the required minimum signals',
        {e['signal'] for e in out['SIGNAL_GRANULARITY_MATRIX']} >=
        {'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cost',
         'tool_call_count', 'rewind_count', 'end_reason', 'message timestamps', 'cancellation'})
    chk('H8 matrix reports a real grain per signal',
        all(e['grain'] in ('PER_REQUEST', 'PER_MESSAGE', 'PER_TURN', 'PER_SESSION', 'PER_SESSION_MODEL',
                           'GLOBAL', 'NOT_PER_TURN') or e['grain'].startswith('PER_')
            for e in out['SIGNAL_GRANULARITY_MATRIX']))
    chk('H9 no matrix entry invents a per-turn grain for a session aggregate',
        all(not (e['signal'].endswith('tokens') and e['grain'] == 'PER_TURN')
            for e in out['SIGNAL_GRANULARITY_MATRIX']))

    report()
    return 1 if FAILS else 0


def report():
    print(f'{"=" * 72}\nprivacy-safe outcome-signal audit — results\n{"=" * 72}')
    for name, ok, extra in R:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  <- {extra}' if extra and not ok else ''))
    print(f'\n{len(R) - len(FAILS)}/{len(R)} passed')
    if FAILS:
        print('FAILED: ' + ', '.join(FAILS))


if __name__ == '__main__':
    raise SystemExit(main())
