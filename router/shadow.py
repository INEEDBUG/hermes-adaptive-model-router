"""Shadow evaluation and telemetry.

Design constraints
------------------
* A shadow decision must never delay or modify a user turn: work is handed to a
  bounded queue and a daemon worker thread, and a full queue simply drops the
  sample.
* Only non-content telemetry is persisted: routing outcome, confidence,
  probabilities, latency, token usage and a fixed set of task-feature flags. The
  user's message, the dossier body, credentials, memory and tool output are never
  written to the log.
* ``MIMO_AVAILABLE`` records whether the alternative backend passed independent
  end-to-end validation. Until it does, the simulated decision can only fall back
  to the fast route.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone

from . import client, config

MIMO_AVAILABLE = True   # set True only after the alternate route passes M1-M5 validation

_q: "queue.Queue" = queue.Queue(maxsize=8)
_worker_started = False
_lock = threading.Lock()
_stats = {'turns': 0, 'deepseek_flash': 0, 'mimo_pro': 0, 'errors': 0,
          'privacy_fallback': 0, 'latencies': []}


def _ensure_dirs():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)


def simulate(decision: dict) -> str:
    """Map a JEV decision onto the route a future auto mode *would* execute.

    This is recorded only. Nothing here changes the executing model.
    """
    if not decision.get('ok'):
        # Fail-open policy: an unusable decision is never allowed to route work to
        # a weaker route silently, so it is attributed to the capable route.
        return 'mimo_pro' if MIMO_AVAILABLE else 'deepseek_flash'
    probs = decision.get('probabilities') or {}
    ranked = sorted(probs.values(), reverse=True)
    p1 = ranked[0] if ranked else 0.0
    p2 = ranked[1] if len(ranked) > 1 else 0.0
    target = decision.get('choice')
    if decision.get('confidence', 0.0) < config.min_confidence():
        target = 'mimo_pro'
    if (p1 - p2) < config.min_margin():
        target = 'mimo_pro'
    if target == 'mimo_pro' and not MIMO_AVAILABLE:
        return 'deepseek_flash'
    return target


def _write(record: dict):
    _ensure_dirs()
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    with open(config.log_path(day), 'a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    if record.get('success'):
        line = (f"[ROUTER-SHADOW] route={record.get('route')} "
                f"confidence={record.get('confidence')} "
                f"p_deepseek={record.get('p_deepseek')} p_mimo={record.get('p_mimo')} "
                f"latency_ms={record.get('latency_ms')} actual={record.get('actual_model')} "
                f"would_execute={record.get('would_execute')}")
    else:
        line = (f"[ROUTER-SHADOW] route={record.get('route')} "
                f"error={record.get('error')} actual={record.get('actual_model')} "
                f"would_execute={record.get('would_execute')}")
    with open(config.human_log_path(day), 'a') as f:
        f.write(line + '\n')


def _features(item: dict) -> dict:
    """Extract content-free task features from the dossier."""
    d = item.get('dossier') or {}
    task = d.get('task') or {}
    req = d.get('requirements') or {}
    risk = d.get('risk') or {}
    try:
        est = max(1, len(json.dumps(d, ensure_ascii=False)) // 4)
    except Exception:
        est = None
    return {
        'task_length': task.get('length'),
        'tool_use': req.get('tool_use'),
        'shell': req.get('shell'),
        'coding': req.get('coding'),
        'debugging': req.get('debugging'),
        'research': req.get('research'),
        'long_context': req.get('long_context'),
        'destructive_action': risk.get('destructive_action'),
        'production_change': risk.get('production_change'),
        'redaction_count': item.get('redaction_count'),
        'dossier_token_estimate': est,
    }


def _work(item: dict):
    t0 = time.time()
    decision = client.route(item['dossier'], timeout=config.timeout_seconds())
    would = simulate(decision)
    probs = decision.get('probabilities') or {}
    rec = {
        'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'turn_id': item.get('turn_id'),
        'jev_model': decision.get('model') or config.jev_model(),
        'route': decision.get('choice') or ('ERROR:' + str(decision.get('error'))),
        'confidence': decision.get('confidence'),
        'p_deepseek': probs.get('deepseek_flash'),
        'p_mimo': probs.get('mimo_pro'),
        'latency_ms': decision.get('latency_ms'),
        'input_tokens': decision.get('input_tokens'),
        'output_tokens': decision.get('output_tokens'),
        'success': bool(decision.get('ok')),
        'error': decision.get('error'),
        'actual_model': item.get('actual_model'),
        'would_execute': would,
        'mode': item.get('mode') or 'shadow',
        **_features(item),
    }
    with _lock:
        _stats['turns'] += 1
        if decision.get('ok'):
            if rec['route'] == 'mimo_pro':
                _stats['mimo_pro'] += 1
            else:
                _stats['deepseek_flash'] += 1
            if rec['latency_ms']:
                _stats['latencies'].append(rec['latency_ms'])
        else:
            _stats['errors'] += 1
    try:
        _write(rec)
    except Exception:
        pass


def _runner():
    while True:
        item = _q.get()
        try:
            _work(item)
        except Exception:
            pass
        finally:
            _q.task_done()


def submit(dossier: dict, *, turn_id=None, actual_model=None, redaction_count=None, mode='shadow'):
    """Enqueue a shadow evaluation without blocking the request path."""
    global _worker_started
    with _lock:
        if not _worker_started:
            t = threading.Thread(target=_runner, name='jev-shadow', daemon=True)
            t.start()
            _worker_started = True
    try:
        _q.put_nowait({'dossier': dossier, 'turn_id': turn_id, 'actual_model': actual_model,
                       'redaction_count': redaction_count, 'mode': mode})
        return True
    except queue.Full:
        return False


def log_privacy_fallback(*, turn_id=None, actual_model=None, reason='unsafe_redaction'):
    """Record a turn that was deliberately *not* sent for routing.

    Used when redaction reports content it cannot sanitise safely: the turn stays
    local and no external routing call is made.
    """
    rec = {'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'turn_id': turn_id, 'jev_model': None, 'route': 'LOCAL_PRIVACY_FALLBACK',
           'confidence': None, 'p_deepseek': None, 'p_mimo': None, 'latency_ms': 0,
           'input_tokens': None, 'output_tokens': None, 'success': True,
           'error': reason, 'actual_model': actual_model,
           'would_execute': 'deepseek_flash', 'mode': 'shadow'}
    with _lock:
        _stats['turns'] += 1
        _stats['privacy_fallback'] += 1
    try:
        _write(rec)
    except Exception:
        pass


def stats() -> dict:
    """In-process counters for the current worker lifetime."""
    with _lock:
        lat = _stats['latencies']
        return {**_stats, 'latencies': None,
                'avg_latency_ms': (sum(lat) / len(lat)) if lat else None,
                'samples': len(lat)}
