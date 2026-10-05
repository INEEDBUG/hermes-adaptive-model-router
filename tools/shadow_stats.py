#!/usr/bin/env python3
"""Production shadow statistics — read only.

Separates real production turns from everything else
----------------------------------------------------
A record counts as a *real* turn only when the session embedded in ``turn_id``
exists in the Hermes session database with a real messaging platform as its
source. Manual tests (``OFFLINE-TEST-*``, ``KILLTEST-*``), CLI one-shot sessions
and fault-injection runs are grouped into ``excluded`` buckets and listed by
``turn_id``, so they can never contaminate production statistics.

The reader never writes: telemetry files are read as text and the session
database is opened read-only.

Usage::

    HERMES_HOME=/opt/data python3 tools/shadow_stats.py [--json]
                                     [--group-by-generation] [--generation NAME]
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from collections import Counter, defaultdict

HERMES_HOME = pathlib.Path(os.environ.get('HERMES_HOME') or '/opt/data')
LOGDIR = pathlib.Path(os.environ.get('JEV_LOG_DIR') or (HERMES_HOME / 'logs' / 'router'))
DB = os.environ.get('HERMES_STATE_DB') or str(HERMES_HOME / 'state.db')

# Platform sources that represent a genuine end-user turn.
REAL_SOURCES = {'feishu', 'lark', 'telegram', 'discord', 'slack', 'signal',
                'whatsapp', 'imessage', 'api_server'}
TEST_TURN_PREFIXES = ('OFFLINE-TEST', 'KILLTEST', 'MANUAL-TEST')

# Mirrors router.config.GENERATION_LEGACY. Records written before the deployment
# generation key existed carry no value and are reported under this label instead of being
# silently merged into a later generation's figures.
GENERATION_LEGACY = 'legacy_unversioned'


def load_records() -> list:
    recs = []
    for p in sorted(LOGDIR.glob('shadow-*.jsonl')):
        for line in p.read_text(errors='replace').splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            r['_file'] = p.name
            recs.append(r)
    return recs


def session_sources() -> dict:
    import sqlite3
    try:
        c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
        return {str(i): (s or '') for i, s in c.execute('SELECT id, source FROM sessions')}
    except Exception:
        return {}


def classify(rec: dict, sources: dict) -> str:
    tid = str(rec.get('turn_id') or '')
    if not tid:
        return 'excluded_no_turn_id'
    if tid.startswith(TEST_TURN_PREFIXES):
        return 'excluded_manual_test'
    sid = tid.split(':')[0]
    src = sources.get(sid)
    if src is None:
        return 'excluded_unknown_session'
    if src in REAL_SOURCES:
        return 'real'
    return f'excluded_source:{src}'


def pct(vals: list, q: float):
    if not vals:
        return None
    xs = sorted(vals)
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[k]


def mean(vals: list):
    return round(sum(vals) / len(vals), 4) if vals else None


def dist(vals: list, edges: list) -> dict:
    out = {f'<{edges[0]}': 0}
    for a, b in zip(edges, edges[1:]):
        out[f'{a}-{b}'] = 0
    out[f'>={edges[-1]}'] = 0
    for v in vals:
        if v < edges[0]:
            out[f'<{edges[0]}'] += 1
            continue
        placed = False
        for a, b in zip(edges, edges[1:]):
            if a <= v < b:
                out[f'{a}-{b}'] += 1
                placed = True
                break
        if not placed:
            out[f'>={edges[-1]}'] += 1
    return out


def dossier_size_bucket(v) -> str:
    """Bucket the Routing Dossier token estimate.

    This measures the *routing input* size only. It is not the agent's conversation
    context size and not a prompt-cache size: those are runtime signals a future
    auto release would have to collect separately.
    """
    if v < 100:
        return '<100'
    if v < 150:
        return '100-150'
    if v < 250:
        return '150-250'
    if v < 400:
        return '250-400'
    return '>=400'


def generation_of(rec: dict) -> str:
    """Deployment generation of one record (content-free label written by the router).

    A record without the key predates the migration metadata, so it is reported as
    ``legacy_unversioned``: guessing a generation would silently mix policy semantics.
    """
    value = rec.get('deployment_generation')
    if isinstance(value, str) and value.strip():
        return value.strip()
    return GENERATION_LEGACY


def _summarize(rows: list) -> dict:
    """Per-generation figures. ``would_execute`` is only meaningful inside one generation:
    the legacy deployment decided executability from a hard-coded route list, the canonical
    line decides it from configured availability."""
    ok = [r for r in rows if r.get('success')]
    return {
        'real_turns': len(rows),
        'real_success': len(ok),
        'routes': dict(Counter((r.get('route') or 'ERROR') for r in rows)),
        'route_pct': ({k: round(100.0 * v / len(ok), 1)
                       for k, v in Counter(r.get('route') for r in ok).items()} if ok else {}),
        'would_execute': dict(Counter(str(r.get('would_execute')) for r in rows)),
        'confidence_median': pct([float(r['confidence']) for r in ok
                                  if isinstance(r.get('confidence'), (int, float))], 0.5),
        'latency_p50': pct([int(r['latency_ms']) for r in rows
                            if isinstance(r.get('latency_ms'), (int, float))], 0.5),
    }


def build(recs: list, sources: dict, generation: str | None = None) -> dict:
    if generation:
        # GROUP_BY_DEPLOYMENT_GENERATION: restrict the whole analysis to one generation so
        # that routing-policy figures are never computed across migration boundaries.
        recs = [r for r in recs if generation_of(r) == generation]
    buckets = defaultdict(list)
    for r in recs:
        buckets[classify(r, sources)].append(r)

    real = buckets.get('real', [])
    ok = [r for r in real if r.get('success')]
    conf = [float(r['confidence']) for r in ok if isinstance(r.get('confidence'), (int, float))]
    margins = [abs(float(r['p_deepseek']) - float(r['p_mimo'])) for r in ok
               if isinstance(r.get('p_deepseek'), (int, float)) and isinstance(r.get('p_mimo'), (int, float))]
    lat = [int(r['latency_ms']) for r in real if isinstance(r.get('latency_ms'), (int, float))]
    routes = Counter((r.get('route') or 'ERROR') for r in real)
    routes_ok = Counter(r.get('route') for r in ok)

    cats = ('tool_use', 'shell', 'coding', 'debugging', 'research', 'long_context',
            'destructive_action', 'production_change')
    cat_route = {c: Counter(r.get('route') for r in ok if r.get(c) is True) for c in cats}
    tl_route: dict = {}
    for r in ok:
        tl_route.setdefault(str(r.get('task_length')), Counter())[r.get('route')] += 1
    ctx_route = defaultdict(Counter)
    for r in ok:
        v = r.get('dossier_token_estimate')
        if isinstance(v, (int, float)):
            ctx_route[dossier_size_bucket(v)][r.get('route')] += 1

    errors = Counter(str(r.get('error')) for r in real if not r.get('success') and r.get('error'))
    gens = sorted({generation_of(r) for r in real})
    by_gen = {g: _summarize([r for r in real if generation_of(r) == g]) for g in gens}
    mixed = len(gens) > 1
    return {
        # Deployment-generation split. ``would_execute`` below is only populated when every
        # real turn belongs to one generation; otherwise the caller must read the
        # per-generation blocks, because legacy and canonical are different policies.
        'generations': gens,
        'mixed_generations': mixed,
        'by_generation': by_gen,
        'would_execute_by_generation': {g: by_gen[g]['would_execute'] for g in gens},
        'would_execute': by_gen[gens[0]]['would_execute'] if len(gens) == 1 else None,
        'would_execute_note': (
            'single deployment generation' if len(gens) == 1 else
            'mixed deployment generations: would_execute is not comparable across them, '
            'read would_execute_by_generation (use --generation NAME for one policy)'),
        'real_turns': len(real),
        'real_success': len(ok),
        'routes': dict(routes),
        'route_counts_success': dict(routes_ok),
        'route_pct': {k: round(100.0 * v / len(ok), 1) for k, v in routes_ok.items()} if ok else {},
        'error_pct': round(100.0 * (len(real) - len(ok)) / len(real), 1) if real else 0.0,
        'confidence': {'n': len(conf), 'mean': mean(conf), 'median': pct(conf, 0.5),
                       'p10': pct(conf, 0.10), 'p90': pct(conf, 0.90)},
        'margin': {'n': len(margins), 'mean': mean(margins), 'median': pct(margins, 0.5),
                   'distribution': dist(margins, [0.05, 0.15, 0.3, 0.5])},
        'latency': {'n': len(lat), 'mean': mean(lat), 'p50': pct(lat, 0.5),
                    'p95': pct(lat, 0.95), 'max': max(lat) if lat else None},
        'error_count': sum(errors.values()),
        'errors': dict(errors),
        'privacy_fallback': routes.get('LOCAL_PRIVACY_FALLBACK', 0),
        'category_route': {k: dict(v) for k, v in cat_route.items()},
        'task_length_route': {k: dict(v) for k, v in tl_route.items()},
        'dossier_size_bucket_route': {k: dict(v) for k, v in ctx_route.items()},
        'excluded': {k: len(v) for k, v in buckets.items() if k != 'real'},
        'excluded_turn_ids': {k: [r.get('turn_id') for r in v][:12]
                              for k, v in buckets.items() if k != 'real'},
        'total_records': len(recs),
    }


def render(s: dict) -> str:
    L = []
    L.append('=== JEV production shadow statistics (real turns kept separate from tests) ===')
    L.append(f"records: {s['total_records']}  |  real turns: {s['real_turns']}")
    L.append(f"excluded: {s['excluded']}")
    for k, ids in s['excluded_turn_ids'].items():
        if ids:
            L.append(f"  [{k}] e.g. {ids[:6]}")
    L.append('')
    L.append(f"routes (successful): {s['route_counts_success']} -> {s['route_pct']} (share of successful turns)")
    L.append(f"all real turns:      {s['routes']}")
    c = s['confidence']
    L.append(f"confidence: n={c['n']} mean={c['mean']} median={c['median']} p10={c['p10']} p90={c['p90']}")
    m = s['margin']
    L.append(f"margin:     n={m['n']} mean={m['mean']} median={m['median']}")
    L.append(f"margin distribution: {m['distribution']}")
    la = s['latency']
    L.append(f"latency: n={la['n']} mean={la['mean']} p50={la['p50']} p95={la['p95']} max={la['max']}")
    L.append(f"timeout/error: {s['error_count']} {s['errors']}  |  privacy fallback: {s['privacy_fallback']}")
    L.append('')
    L.append(f"deployment generations (real turns): {s['generations']}")
    if s['mixed_generations']:
        L.append('  WARNING: more than one deployment generation is present. would_execute /')
        L.append('  routing-policy figures are NOT comparable across generations and are')
        L.append('  therefore reported per generation; the mixed raw totals above are counts')
        L.append('  only and must not be quoted as a canonical routing figure.')
    for g, b in s['by_generation'].items():
        L.append(f"  [{g}] real={b['real_turns']} success={b['real_success']} "
                 f"routes={b['routes']} would_execute={b['would_execute']} "
                 f"conf_median={b['confidence_median']} latency_p50={b['latency_p50']}")
    if not s['mixed_generations']:
        L.append(f"  would_execute (single generation): {s['would_execute']}")
    L.append('')
    L.append('task category x route:')
    for k, v in s['category_route'].items():
        if v:
            L.append(f'  {k}: {v}')
    L.append(f"task_length x route: {s['task_length_route']}")
    L.append('routing-input size proxy — Routing Dossier token-estimate bucket x route')
    L.append('(this is the sanitised dossier size, NOT the agent conversation context'
             ' or prompt-cache size)')
    for k, v in s['dossier_size_bucket_route'].items():
        L.append(f'  {k}: {v}')
    return '\n'.join(L)


def main() -> int:
    recs = load_records()
    src = session_sources()
    generation = None
    if '--generation' in sys.argv:
        i = sys.argv.index('--generation')
        if i + 1 < len(sys.argv):
            generation = sys.argv[i + 1]
        else:
            print('--generation needs a value', file=sys.stderr)
            return 2
    # Per-generation grouping is always computed, so --group-by-generation is accepted as
    # an explicit statement of intent rather than a switch that changes the maths.
    s = build(recs, src, generation=generation)
    if '--json' in sys.argv:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        print(render(s))
    return 0


if __name__ == '__main__':
    sys.exit(main())
