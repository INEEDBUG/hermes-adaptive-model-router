#!/usr/bin/env python3
"""Offline suite: routing-quality aggregates and tool isolation (v0.2.0).

Run from the repository root::

    python3 tests/test_routing_quality.py

Everything happens inside a private temporary tree. The suite additionally *emulates a live
deployment* by exporting JEV_ROUTER_ROOT / JEV_SKIP_COUNTER_PATH / JEV_LOG_DIR / JEV_STATE_DIR /
JEV_DEPLOYMENT_GENERATION / JEV_AVAILABLE_ROUTES at sentinel paths, and then proves that neither
this suite nor the tool it drives reads or writes them: a real deployment's telemetry must never
be an input to, or an output of, an offline test run.

Groups
    A  aggregate correctness on a synthetic canonical cohort
    B  generation separation (legacy must never be merged into canonical figures)
    C  cohort boundary (non-user turns and non-real sources stay out)
    D  counter contamination sidecar handling (never a subtractive correction)
    E  deployment isolation: sentinel parent paths untouched, poison telemetry not ingested
    F  the tool refuses to write inside a deployment
"""
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools' / 'routing_quality.py'
CANON = 'canonical-dff8b11'
LEDGER_MARKER = 'offline_test_environment_leak'

R = []
FAILS = []


def chk(name, cond, extra=''):
    R.append((name, bool(cond), extra))
    if not cond:
        FAILS.append(name)


def rec(turn_id, *, route, conf, p_ds, p_mimo, ok=True, would=None, error=None,
        platform='feishu', origin='user', generation: str | None = CANON,
        timestamp='2026-10-05T10:00:00Z'):
    r = {'turn_id': turn_id, 'platform': platform, 'turn_origin': origin, 'route': route,
         'confidence': conf, 'success': ok, 'error': error, 'would_execute': would,
         'mode': 'shadow', 'actual_model': 'deepseek-flash', 'timestamp': timestamp,
         'latency_ms': 1000, 'jev_model': 'jev-test'}
    if p_ds is not None:
        r['p_deepseek'] = p_ds
    if p_mimo is not None:
        r['p_mimo'] = p_mimo
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


def run_tool(env, *args):
    base = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONDONTWRITEBYTECODE': '1'}
    e = dict(base, **env)
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=e)


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='jev-routing-quality-'))
    logs = tmp / 'logs'
    state = tmp
    sentinel = tmp / 'sentinel'
    (sentinel / 'logs').mkdir(parents=True)
    (sentinel / 'state').mkdir(parents=True)
    (sentinel / 'runtime' / 'router').mkdir(parents=True)

    # ---- fixture: canonical cohort (7 human turns across 2 days, 3 sessions) ----
    write_jsonl(logs / 'shadow-2026-10-05.jsonl', [
        rec('fx-canon-1:t1', route='deepseek_flash', conf=0.58, p_ds=0.79, p_mimo=0.21,
            would='mimo_pro', timestamp='2026-10-05T10:00:00Z'),
        rec('fx-canon-1:t2', route='mimo_pro', conf=0.90, p_ds=0.05, p_mimo=0.95,
            would='mimo_pro', timestamp='2026-10-05T10:05:00Z'),
        rec('fx-canon-1:t3', route='deepseek_flash', conf=0.80, p_ds=0.55, p_mimo=0.45,
            would='mimo_pro', timestamp='2026-10-05T10:10:00Z'),
        rec('fx-canon-1:t4', route='deepseek_flash', conf=0.90, p_ds=0.90, p_mimo=0.10,
            would='deepseek_flash', timestamp='2026-10-05T10:15:00Z'),
        rec('fx-canon-2:t1', route='mimo_pro', conf=0.92, p_ds=0.04, p_mimo=0.96,
            would='mimo_pro', timestamp='2026-10-05T11:00:00Z'),
    ])
    write_jsonl(logs / 'shadow-2026-10-06.jsonl', [
        rec('fx-canon-3:t1', route='deepseek_flash', conf=0.62, p_ds=0.52, p_mimo=0.48,
            would='mimo_pro', timestamp='2026-10-06T09:00:00Z'),
        rec('fx-canon-3:t2', route='ERROR:timeout', conf=None, p_ds=None, p_mimo=None, ok=False,
            error='timeout', would='mimo_pro', timestamp='2026-10-06T09:05:00Z'),
        rec('fx-canon-4:t1', route='deepseek_flash', conf=0.99, p_ds=0.99, p_mimo=0.01,
            would='deepseek_flash', origin='background_review', timestamp='2026-10-06T09:10:00Z'),
    ])
    # legacy file: no generation key, and would_execute under legacy semantics
    write_jsonl(logs / 'shadow-2026-09-20.jsonl', [
        rec('fx-legacy-1:t1', route='deepseek_flash', conf=0.90, p_ds=0.90, p_mimo=0.10,
            would='mimo_pro', generation=None, timestamp='2026-09-20T10:00:00Z'),
        rec('fx-legacy-1:t2', route='deepseek_flash', conf=0.30, p_ds=0.60, p_mimo=0.40,
            would='mimo_pro', generation=None, timestamp='2026-09-20T10:05:00Z'),
    ])
    # poison telemetry under the sentinel deployment path
    write_jsonl(sentinel / 'logs' / 'shadow-2026-10-05.jsonl', [
        rec('poison-1:t1', route='mimo_pro', conf=0.99, p_ds=0.01, p_mimo=0.99, would='mimo_pro',
            generation='poison-generation', timestamp='2026-10-05T00:00:00Z')])
    (sentinel / 'logs' / 'skipped-non-user-turn.json').write_text('{"sentinel": true}\n')
    (sentinel / 'env').write_text('SENTINEL=1\n')

    con = sqlite3.connect(state / 'state.db')
    con.execute('CREATE TABLE sessions (id TEXT, source TEXT)')
    con.executemany('INSERT INTO sessions VALUES (?,?)',
                    [('fx-canon-1', 'feishu'), ('fx-canon-2', 'feishu'), ('fx-canon-3', 'feishu'),
                     ('fx-canon-4', 'feishu'), ('fx-legacy-1', 'feishu'), ('poison-1', 'feishu')])
    con.commit()
    con.close()

    # sentinel parent environment: a live deployment, from the suite's point of view
    parent_env = {
        'JEV_LOG_DIR': str(sentinel / 'logs'),
        'JEV_STATE_DIR': str(sentinel / 'state'),
        'JEV_ROUTER_ROOT': str(sentinel / 'runtime'),
        'JEV_SKIP_COUNTER_PATH': str(sentinel / 'logs' / 'skipped-non-user-turn.json'),
        'JEV_DEPLOYMENT_GENERATION': 'sentinel-generation',
        'JEV_AVAILABLE_ROUTES': 'sentinel_route',
        'HERMES_HOME': str(sentinel),
    }
    before_logs, before_counter, before_runtime = (fingerprint(sentinel / 'logs'),
                                                   fingerprint(sentinel / 'logs' / 'skipped-non-user-turn.json'),
                                                   fingerprint(sentinel / 'runtime'))
    ledger = tmp / 'ledger.json'
    ledger.write_text(json.dumps({'event_type': LEDGER_MARKER, 'affected_source': 'skip counter only',
                                  'shadow_telemetry_affected': 'NO', 'confidence': 'PARTIAL',
                                  'window_classification': 'UNUSABLE_FOR_REAL_REJECTION_RATE',
                                  'reconstruction': {'synthetic_delta_per_run': 46}}))

    tool_env = dict(parent_env, HERMES_HOME=str(tmp), JEV_LOG_DIR=str(logs))
    common = ['--log-dir', str(logs), '--state-db', str(state / 'state.db'),
              '--runtime-root', str(ROOT), '--available-routes', 'deepseek_flash,mimo_pro',
              '--confidence-threshold', '0.65', '--margin-threshold', '0.15']

    # ---------------- A: aggregate correctness ----------------
    p = run_tool(tool_env, '--generation', CANON, *common, '--features', '--longitudinal', '--sensitivity')
    chk('A0 tool exits 0 on the canonical cohort', p.returncode == 0, p.stderr.strip()[:160])
    out = json.loads(p.stdout) if p.returncode == 0 else {}
    c = out.get('COHORT', {})
    chk('A1 human turns counted', c.get('HUMAN_TURNS') == 7, str(c.get('HUMAN_TURNS')))
    chk('A2 sample days counted', c.get('SAMPLE_DAYS') == 2, str(c.get('SAMPLE_DAYS')))
    chk('A3 day concentration', abs((c.get('DAY_CONCENTRATION') or 0) - 5 / 7) < 1e-4, str(c.get('DAY_CONCENTRATION')))
    chk('A4 successful decisions', c.get('SUCCESSFUL_JEV_DECISIONS') == 6, str(c.get('SUCCESSFUL_JEV_DECISIONS')))
    chk('A5 timeouts separated from errors',
        c.get('JEV_TIMEOUTS') == 1 and c.get('JEV_ERRORS') == 0,
        f"to={c.get('JEV_TIMEOUTS')} err={c.get('JEV_ERRORS')}")
    chk('A6 direct route distribution', c.get('DIRECT_ROUTE_DISTRIBUTION') == {'deepseek_flash': 4, 'mimo_pro': 2},
        json.dumps(c.get('DIRECT_ROUTE_DISTRIBUTION')))
    chk('A7 policy route distribution', c.get('POLICY_ROUTE_DISTRIBUTION') == {'mimo_pro': 5, 'deepseek_flash': 1},
        json.dumps(c.get('POLICY_ROUTE_DISTRIBUTION')))
    chk('A8 override count and rate',
        c.get('POLICY_OVERRIDE_COUNT') == 3 and abs((c.get('POLICY_OVERRIDE_RATE') or 0) - 0.5) < 1e-9,
        f"{c.get('POLICY_OVERRIDE_COUNT')} {c.get('POLICY_OVERRIDE_RATE')}")
    chk('A9 override reasons classified from the code path',
        c.get('OVERRIDE_REASON_COUNTS') == {'LOW_CONFIDENCE_ESCALATION': 1, 'LOW_MARGIN_ESCALATION': 1,
                                            'CONFIDENCE_AND_MARGIN_ESCALATION': 1},
        json.dumps(c.get('OVERRIDE_REASON_COUNTS')))
    chk('A10 deepseek->mimo escalation breakdown',
        c.get('DEEPSEEK_TO_MIMO_OVERRIDE') == {'total': 3, 'breakdown': {'LOW_CONFIDENCE_ONLY': 1,
                                                                        'LOW_MARGIN_ONLY': 1, 'BOTH': 1}},
        json.dumps(c.get('DEEPSEEK_TO_MIMO_OVERRIDE')))
    chk('A11 replay fidelity is 1.0 (taxonomy matches production semantics)',
        (c.get('POLICY_REPLAY_FIDELITY') or {}).get('fidelity') == 1.0,
        json.dumps(c.get('POLICY_REPLAY_FIDELITY')))
    chk('A12 confidence bins', c.get('CONFIDENCE_BINS') == {'<0.50': 0, '0.50-<0.60': 1, '0.60-<0.65': 1,
                                                            '0.65-<0.75': 0, '0.75-<0.90': 1, '>=0.90': 3},
        json.dumps(c.get('CONFIDENCE_BINS')))
    chk('A13 margin bins', c.get('MARGIN_BINS') == {'<0.10': 1, '0.10-<0.15': 1, '0.15-<0.30': 0,
                                                    '0.30-<0.50': 0, '>=0.50': 4},
        json.dumps(c.get('MARGIN_BINS')))
    chk('A14 near-threshold count', c.get('NEAR_THRESHOLD_COUNT') == 1, str(c.get('NEAR_THRESHOLD_COUNT')))
    chk('A15 longitudinal sessions and flip rate',
        c.get('SESSIONS_OBSERVED') == 3 and abs((c.get('POLICY_ROUTE_FLIP_RATE') or 0) - 0.25) < 1e-9,
        f"sessions={c.get('SESSIONS_OBSERVED')} flip={c.get('POLICY_ROUTE_FLIP_RATE')}")
    chk('A16 longitudinal marked insufficient on a small cohort',
        c.get('LONGITUDINAL_SAMPLE') == 'INSUFFICIENT', str(c.get('LONGITUDINAL_SAMPLE')))
    chk('A17 sensitivity grid complete',
        len(c.get('POLICY_SENSITIVITY_TABLE') or []) == 20, str(len(c.get('POLICY_SENSITIVITY_TABLE') or [])))
    chk('A18 loosening the confidence gate reduces escalation',
        [r for r in c['POLICY_SENSITIVITY_TABLE']
         if r['confidence_threshold'] == 0.55][0]['deepseek_to_mimo_escalation_rate']
        < [r for r in c['POLICY_SENSITIVITY_TABLE']
           if r['confidence_threshold'] == 0.75][0]['deepseek_to_mimo_escalation_rate'],
        'expected monotone escalation behaviour')
    chk('A19 feature aggregate is multi-label and complete',
        set(c.get('TASK_FEATURE_BREAKDOWN', {})) == {'coding', 'debugging', 'research', 'tool_use', 'shell',
                                                     'long_context', 'destructive_action', 'production_change'}
        and c.get('TASK_FEATURE_MULTILABEL') is True, json.dumps(list(c.get('TASK_FEATURE_BREAKDOWN', {}))))
    chk('A20 quality ground truth reported absent', c.get('QUALITY_GROUND_TRUTH_AVAILABLE') == 'NO',
        str(c.get('QUALITY_GROUND_TRUTH_AVAILABLE')))

    # ---------------- B: generation separation ----------------
    pb = run_tool(tool_env, '--generation', 'all', *common)
    ob = json.loads(pb.stdout) if pb.returncode == 0 else {}
    chk('B1 --generation all refuses routing figures',
        ob.get('GENERATION_SEPARATION_REQUIRED') is True and 'COHORT' not in ob, json.dumps(sorted(ob))[:120])
    chk('B2 record counts still reported per generation',
        set(ob.get('GENERATION_DISTRIBUTION', {})) >= {CANON, 'legacy_unversioned'},
        json.dumps(ob.get('GENERATION_DISTRIBUTION')))
    legacy_run = run_tool(tool_env, '--generation', 'legacy_unversioned', *common)
    ol = json.loads(legacy_run.stdout) if legacy_run.returncode == 0 else {}
    chk('B3 legacy cohort analysed separately',
        (ol.get('COHORT', {}).get('HUMAN_TURNS') == 2), str(ol.get('COHORT', {}).get('HUMAN_TURNS')))
    chk('B4 canonical figures contain no legacy turn',
        c.get('HUMAN_TURNS') == 7 and 'poison-generation' not in json.dumps(out),
        'legacy/sentinel leakage into the canonical cohort')

    # ---------------- C: cohort boundary ----------------
    chk('C1 non-user turn excluded from the cohort', c.get('HUMAN_TURNS') == 7,
        'a background_review turn must not be admitted')
    chk('C2 timeout recorded as an attempt but not as a decision',
        c.get('SUCCESSFUL_JEV_DECISIONS') == 6 and c.get('HUMAN_TURNS') == 7, '')

    # ---------------- D: contamination sidecar ----------------
    pd_ = run_tool(tool_env, '--generation', CANON, *common, '--contamination-ledger', str(ledger))
    od = json.loads(pd_.stdout) if pd_.returncode == 0 else {}
    cc = od.get('COUNTER_CONTAMINATION', {})
    chk('D1 sidecar reported as known contamination', cc.get('status') == 'KNOWN_SYNTHETIC_COUNTER_CONTAMINATION',
        json.dumps(cc)[:120])
    chk('D2 reconstructed synthetic delta surfaced', cc.get('RECONSTRUCTED_SYNTHETIC_COUNTER_DELTA_PER_RUN') == 46,
        str(cc.get('RECONSTRUCTED_SYNTHETIC_COUNTER_DELTA_PER_RUN')))
    chk('D3 no subtractive real-rejection total is produced',
        'NOT_DERIVABLE' in str(cc.get('REAL_REJECTION_TOTAL')), str(cc.get('REAL_REJECTION_TOTAL')))
    chk('D4 window classified unusable', cc.get('window_classification') == 'UNUSABLE_FOR_REAL_REJECTION_RATE',
        str(cc.get('window_classification')))

    # ---------------- E: deployment isolation ----------------
    chk('E1 sentinel log tree byte-identical after the run', fingerprint(sentinel / 'logs') == before_logs,
        'offline suite touched a deployment telemetry path')
    chk('E2 sentinel counter untouched', fingerprint(sentinel / 'logs' / 'skipped-non-user-turn.json') == before_counter,
        'offline suite touched a deployment counter')
    chk('E3 sentinel runtime root untouched', fingerprint(sentinel / 'runtime') == before_runtime,
        'offline suite touched a deployment runtime root')
    chk('E4 inherited deployment generation never becomes the analysed cohort',
        c.get('GENERATION') == CANON and 'sentinel-generation' not in json.dumps(out),
        'parent JEV_DEPLOYMENT_GENERATION leaked into the analysis')
    chk('E5 inherited availability never replaces the requested routes',
        'sentinel_route' not in json.dumps(out), 'parent JEV_AVAILABLE_ROUTES leaked into the analysis')
    chk('E6 inherited state dir did not become the session source',
        c.get('HUMAN_TURNS') == 7, 'parent JEV_STATE_DIR / HERMES_HOME leaked into the cohort')
    chk('E7 default paths from a sentinel parent do not reach the fixture',
        run_tool(parent_env, '--generation', CANON).returncode != 0,
        'a pure-deployment environment should not produce a fixture-based result')

    # ---------------- F: no writes into a deployment ----------------
    refused = run_tool(tool_env, '--generation', CANON, *common, '--json',
                       str(sentinel / 'logs' / 'aggregate.json'))
    chk('F1 refuses to write an aggregate inside the deployment', refused.returncode == 4,
        f"exit={refused.returncode} {refused.stderr.strip()[:80]}")
    chk('F2 nothing was written into the deployment anyway',
        not (sentinel / 'logs' / 'aggregate.json').exists(), 'aggregate leaked into a deployment path')
    private_out = pathlib.Path(tempfile.mkdtemp(prefix='jev-rq-out-')) / 'aggregate.json'
    allowed = run_tool(tool_env, '--generation', CANON, *common, '--json', str(private_out))
    chk('F3 writes when the destination is a private temp path',
        allowed.returncode == 0 and private_out.exists(), f"exit={allowed.returncode}")
    inside_home = run_tool(tool_env, '--generation', CANON, *common, '--json', str(tmp / 'aggregate.json'))
    chk('F4 refuses a destination inside the configured deployment home', inside_home.returncode == 4,
        f"exit={inside_home.returncode}")
    shutil.rmtree(private_out.parent, ignore_errors=True)

    # ---------------- G: no raw identifiers in any output ----------------
    blob = json.dumps(out)
    leaked = [t for t in ('fx-canon-1:t1', 'fx-canon-1:t4', 'fx-canon-3:t2', 'fx-legacy-1:t1', 'poison-1:t1',
                          'fx-canon-1') if t in blob]
    chk('G1 no turn or session identifier in the aggregate', not leaked, f'leaked={leaked}')
    chk('G2 no raw telemetry record is echoed', 'p_deepseek' not in blob and 'dossier' not in blob, '')

    shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok, _ in R if not ok]
    print(f'total {len(R)} checks, failed {len(failed)}: {failed}')
    for n, ok, extra in R:
        if not ok:
            print(f'  [FAIL] {n} {extra}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
