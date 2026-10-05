"""Prospective per-turn outcome telemetry — repository implementation candidate.

Status: **implemented here, NOT deployed.** Production still runs ``canonical-dff8b11`` in
``shadow`` with Auto disabled. Per-turn exact figures start existing only after a future
deployment; historical records do not become exact retroactively.

What this module records
------------------------
Structural execution facts for **admitted human turns only**, one terminal record per turn:

    request counts, request-error counts, retry counts observed,
    normalized usage buckets summed and maxed per request,
    API durations summed and maxed, finish reason, model attribution,
    terminal status, wall duration, attribution quality.

It deliberately records **no content**: no prompt, no response, no message body, no tool name,
argument or result, no memory, no dossier, no error message or string, no raw session / task /
turn identifier. A later phase must not be able to mistake a struggle proxy for routing
correctness, so nothing here can express "the other model would have been better"::

    DEFAULT_MODEL_STRUGGLE != ROUTING_CORRECTNESS
    DEFAULT_MODEL_STRUGGLE != MIMO_WOULD_BE_BETTER

Content blindness is structural, not a convention
-------------------------------------------------
* every handler copies only the keys named in a frozen allow-list out of the hook payload
  (``_PRE_KEYS`` / ``_POST_KEYS`` / ...), so the content-bearing keys that Hermes does pass
  (``user_message``, ``assistant_response``, ``conversation_history``, ``response``,
  ``assistant_message``, ``request``, ``error``) are never read, copied, hashed, inspected or
  serialized, even though they are present in ``kwargs``;
* ``turn_correlation`` is derived **only** from the structural ``turn_id``; no content is ever
  hashed;
* every value written is a closed enum, an int, a bool or null.

Human-turn boundary
-------------------
Admission reuses the plugin's single boundary (``plugin._admit``): platform allow-list **and**
``turn_origin == 'user'``. This module never re-implements human-turn inference, and a non-user
turn never gets an accumulator. The rejection counters keep their existing behaviour.

Terminal semantics
------------------
``post_llm_call`` is the normal completion seam and yields ``completed``. ``agent_loop_stopped``
yields ``interrupted``, but its payload carries only a session key — so a record is written only
when exactly one open accumulator matches that key; otherwise nothing is written. An
``api_request_error`` never declares a turn terminal by itself.

    MISSING_TERMINAL_IS_SUCCESS = NO

A turn whose terminal hook is never observed leaves **no record at all**. Missing is missing:
outcome analytics must compute ``ADMITTED_SHADOW_TURNS - TERMINAL_OUTCOMES`` and keep the gap
visible. An unfinished accumulator may be lost on a gateway restart or crash, and that is a
MISSING_TERMINAL outcome, never a completed one.

Failure policy
--------------
``OUTCOME_TELEMETRY_FAILURE_POLICY = FAIL_OPEN_FOR_HERMES_EXECUTION + FAIL_CLOSED_FOR_ANALYTICS``:
recording can never raise into, block, or alter a Hermes turn, and analysis can never read a
missing record as success.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import time

from router import config

SCHEMA_VERSION = 'turn-outcome-v1'

# Closed enum. ``failed`` and ``cancelled`` are deliberately absent: no hook proves that a
# request error or an interruption ended the turn as a failure, and a wrong classification is
# worse than no record.
TERMINAL_STATUSES = ('completed', 'interrupted')

ATTRIBUTION_QUALITY = 'EXACT_PROSPECTIVE'

# Keys this module is allowed to read out of each hook payload. Anything else — in particular
# user_message / assistant_response / conversation_history / response / assistant_message /
# request / error — is never touched.
_PRE_KEYS = frozenset({
    'turn_id', 'task_id', 'session_id', 'platform', 'turn_origin', 'model', 'provider',
    'api_mode', 'api_call_count', 'retry_count', 'api_request_id',
})
_POST_KEYS = frozenset({
    'turn_id', 'task_id', 'session_id', 'platform', 'model', 'provider', 'api_mode',
    'api_call_count', 'api_duration', 'api_request_id', 'finish_reason', 'response_model', 'usage',
})
_ERROR_KEYS = frozenset({
    'turn_id', 'task_id', 'session_id', 'platform', 'model', 'api_call_count', 'api_duration',
    'api_request_id', 'retry_count', 'max_retries', 'retryable', 'status_code',
})
_FINAL_KEYS = frozenset({'turn_id', 'task_id', 'session_id', 'model', 'platform'})
_STOPPED_KEYS = frozenset({'session_key', 'platform'})

# Normalized usage buckets that may be summed. ``prompt_tokens`` / ``total_tokens`` are
# provider-independent *derived* properties in Hermes (``agent.usage_pricing.CanonicalUsage``),
# so they are recomputed here rather than trusted as separate buckets.
_USAGE_INT_KEYS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens',
                   'reasoning_tokens')

_MAX_OPEN = 64                 # bounded in-memory accumulators (oldest unfinished are evicted)
_MAX_SEEN_REQUESTS = 512        # bounded duplicate suppression per turn
_MAX_MODELS = 8                 # bounded model attribution list
_MAX_LABEL = 64
_LABEL_RX = re.compile(r'[^a-z0-9._:-]+')
_FINISH_RX = re.compile(r'[^a-z_]+')

_lock = threading.RLock()
_open: 'dict[str, dict]' = {}
_counts = {'started': 0, 'finalized': 0, 'evicted_unfinished': 0, 'duplicate_request': 0,
           'late_hook_ignored': 0, 'interrupt_unmatched': 0, 'write_failed': 0}


# --------------------------------------------------------------------------- helpers
def _label(value, default='(unknown)') -> str:
    token = _LABEL_RX.sub('-', str(value or '').strip().lower())[:_MAX_LABEL].strip('-')
    return token or default


def _int(value) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _correlation(turn_id) -> str:
    """Locally joinable digest of the structural turn id. No content is ever hashed here."""
    raw = str(turn_id or '')
    if not raw:
        return ''
    return hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()


def _pick(kwargs: dict, allowed: frozenset) -> dict:
    """Copy only allow-listed keys. A content-bearing key is never even read."""
    return {k: kwargs[k] for k in allowed if k in kwargs}


def record_path(day: str | None = None, log_dir=None) -> pathlib.Path:
    """``<JEV_LOG_DIR>/outcomes/turn-outcome-YYYY-MM-DD.jsonl``.

    A separate directory, not a new environment variable: the deployment already defines its
    telemetry root through ``JEV_LOG_DIR``, and mixing two schemas in ``shadow-*.jsonl`` would
    make either one unreadable by its own tooling.
    """
    root = pathlib.Path(log_dir) if log_dir else config.LOG_DIR
    stamp = day or time.strftime('%Y-%m-%d', time.gmtime())
    return root / 'outcomes' / f'turn-outcome-{stamp}.jsonl'


# --------------------------------------------------------------------------- lifecycle
def start(kwargs: dict, *, platform: str, turn_origin: str) -> bool:
    """Establish an accumulator for one admitted human turn. Caller applies the boundary.

    Must be called on the **first** physical request of the turn (the plugin already de-dupes
    that); subsequent requests of the same tool loop must not create a second accumulator.
    """
    try:
        picked = _pick(kwargs, _PRE_KEYS)
        corr = _correlation(picked.get('turn_id'))
        if not corr:
            return False
        with _lock:
            if corr in _open:
                return False
            if len(_open) >= _MAX_OPEN:
                oldest = min(_open, key=lambda c: _open[c]['first_seen_monotonic'])
                _open.pop(oldest, None)          # eviction never fabricates a terminal record
                _counts['evicted_unfinished'] += 1
            _open[corr] = {
                'turn_correlation': corr,
                'session_key': str(picked.get('session_id') or ''),
                'platform': _label(platform, '(missing)'),
                'turn_origin': _label(turn_origin, '(missing)'),
                'deployment_generation': config.deployment_generation(),
                'first_seen_monotonic': time.monotonic(),
                'actual_model_first': _label(picked.get('model')),
                'actual_model_last': _label(picked.get('model')),
                'models_seen': [_label(picked.get('model'))],
                'model_switch_count': 0,
                'api_request_count': 0,
                'api_success_count': 0,
                'api_error_count': 0,
                'retry_count_observed': 0,
                'api_input_tokens_sum': 0,
                'api_output_tokens_sum': 0,
                'api_cache_read_tokens_sum': 0,
                'api_cache_write_tokens_sum': 0,
                'api_reasoning_tokens_sum': 0,
                'api_input_tokens_max_request': 0,
                'api_output_tokens_max_request': 0,
                'api_cache_read_tokens_max_request': 0,
                'api_cache_write_tokens_max_request': 0,
                'api_prompt_tokens_max_request': 0,
                'api_duration_ms_sum': 0,
                'api_duration_ms_max': 0,
                'finish_reason_last': None,
                'runtime_error_observed': False,
                'interrupted_observed': False,
                'seen_api_request_ids': set(),
            }
            _counts['started'] += 1
        return True
    except Exception:
        return False


def _note_model(acc: dict, model) -> None:
    token = _label(model)
    if acc['models_seen'] and acc['models_seen'][-1] == token:
        return
    if token in acc['models_seen']:
        acc['actual_model_last'] = token
        return
    if len(acc['models_seen']) < _MAX_MODELS:
        acc['models_seen'].append(token)
    acc['model_switch_count'] += 1
    acc['actual_model_last'] = token


def observe_response(kwargs: dict) -> bool:
    """Aggregate one successful physical request into its turn's accumulator."""
    try:
        picked = _pick(kwargs, _POST_KEYS)
        corr = _correlation(picked.get('turn_id'))
        with _lock:
            acc = _open.get(corr)
            if acc is None:
                _counts['late_hook_ignored'] += 1
                return False
            request_id = str(picked.get('api_request_id') or '')
            if request_id:
                if request_id in acc['seen_api_request_ids']:
                    _counts['duplicate_request'] += 1      # duplicate delivery, not a new request
                    return False
                if len(acc['seen_api_request_ids']) < _MAX_SEEN_REQUESTS:
                    acc['seen_api_request_ids'].add(request_id)

            acc['api_request_count'] += 1
            acc['api_success_count'] += 1

            usage = picked.get('usage')
            usage = usage if isinstance(usage, dict) else {}
            buckets = {k: _int(usage.get(k)) for k in _USAGE_INT_KEYS}
            acc['api_input_tokens_sum'] += buckets['input_tokens']
            acc['api_output_tokens_sum'] += buckets['output_tokens']
            acc['api_cache_read_tokens_sum'] += buckets['cache_read_tokens']
            acc['api_cache_write_tokens_sum'] += buckets['cache_write_tokens']
            acc['api_reasoning_tokens_sum'] += buckets['reasoning_tokens']
            acc['api_input_tokens_max_request'] = max(acc['api_input_tokens_max_request'],
                                                      buckets['input_tokens'])
            acc['api_output_tokens_max_request'] = max(acc['api_output_tokens_max_request'],
                                                       buckets['output_tokens'])
            acc['api_cache_read_tokens_max_request'] = max(
                acc['api_cache_read_tokens_max_request'], buckets['cache_read_tokens'])
            acc['api_cache_write_tokens_max_request'] = max(
                acc['api_cache_write_tokens_max_request'], buckets['cache_write_tokens'])
            # prompt_tokens = input + cache_read + cache_write in Hermes's canonical shape, i.e.
            # the full consumed context of that request. Recomputed, never trusted as a bucket.
            prompt_total = (buckets['input_tokens'] + buckets['cache_read_tokens']
                            + buckets['cache_write_tokens'])
            acc['api_prompt_tokens_max_request'] = max(acc['api_prompt_tokens_max_request'],
                                                       prompt_total)

            duration_ms = _int(float(picked.get('api_duration') or 0) * 1000)
            acc['api_duration_ms_sum'] += duration_ms
            acc['api_duration_ms_max'] = max(acc['api_duration_ms_max'], duration_ms)

            finish = picked.get('finish_reason')
            acc['finish_reason_last'] = (_FINISH_RX.sub('', str(finish).strip().lower())[:32]
                                        or None) if finish is not None else None
            _note_model(acc, picked.get('response_model') or picked.get('model'))
        return True
    except Exception:
        return False


def observe_error(kwargs: dict) -> bool:
    """Count a failed physical request. Recorded structurally: the error body is never read."""
    try:
        picked = _pick(kwargs, _ERROR_KEYS)
        corr = _correlation(picked.get('turn_id'))
        with _lock:
            acc = _open.get(corr)
            if acc is None:
                _counts['late_hook_ignored'] += 1
                return False
            request_id = str(picked.get('api_request_id') or '')
            if request_id:
                if request_id in acc['seen_api_request_ids']:
                    _counts['duplicate_request'] += 1
                    return False
                if len(acc['seen_api_request_ids']) < _MAX_SEEN_REQUESTS:
                    acc['seen_api_request_ids'].add(request_id)
            acc['api_request_count'] += 1
            acc['api_error_count'] += 1
            acc['runtime_error_observed'] = True
            acc['retry_count_observed'] = max(acc['retry_count_observed'],
                                              _int(picked.get('retry_count')))
            acc['api_duration_ms_sum'] += _int(float(picked.get('api_duration') or 0) * 1000)
            _note_model(acc, picked.get('model'))
        return True
    except Exception:
        return False


def finalize(kwargs: dict, status: str = 'completed') -> bool:
    """Write the single terminal record for a turn, then drop its accumulator."""
    try:
        if status not in TERMINAL_STATUSES:
            return False
        picked = _pick(kwargs, _FINAL_KEYS)
        corr = _correlation(picked.get('turn_id'))
        with _lock:
            acc = _open.get(corr)
            if acc is None:
                # Unknown turn, or a duplicate terminal: at most one record per turn.
                _counts['late_hook_ignored'] += 1
                return False
            acc['actual_model_last'] = _label(picked.get('model')) if picked.get('model') else acc['actual_model_last']
            record = _build_record(acc, status)
            _open.pop(corr, None)                     # cleanup happens whichever way writing goes
        written = _append(record)
        with _lock:
            _counts['finalized' if written else 'write_failed'] += 1
        return written
    except Exception:
        return False


def interrupted(kwargs: dict) -> bool:
    """Finalize as ``interrupted`` from ``agent_loop_stopped``.

    That hook carries a session key and no turn id, so a turn is only claimable when exactly one
    open accumulator matches the key. Anything else — no match, or two open turns in the same
    session — writes nothing, because a wrong terminal status is worse than a missing record.
    """
    try:
        picked = _pick(kwargs, _STOPPED_KEYS)
        key = str(picked.get('session_key') or '')
        if not key:
            return False
        with _lock:
            matches = [c for c, a in _open.items() if a['session_key'] == key]
            if len(matches) != 1:
                _counts['interrupt_unmatched'] += 1
                return False
            acc = _open[matches[0]]
            acc['interrupted_observed'] = True
            record = _build_record(acc, 'interrupted')
            _open.pop(matches[0], None)
        written = _append(record)
        with _lock:
            _counts['finalized' if written else 'write_failed'] += 1
        return written
    except Exception:
        return False


# --------------------------------------------------------------------------- record
def _build_record(acc: dict, status: str) -> dict:
    duration_ms = max(0, int((time.monotonic() - acc['first_seen_monotonic']) * 1000))
    return {
        'schema_version': SCHEMA_VERSION,
        'deployment_generation': acc['deployment_generation'],
        'platform': acc['platform'],
        'turn_origin': acc['turn_origin'],
        'turn_correlation': acc['turn_correlation'],
        'actual_model_first': acc['actual_model_first'],
        'actual_model_last': acc['actual_model_last'],
        'model_switch_count': acc['model_switch_count'],
        'terminal_status': status,
        'duration_ms': duration_ms,
        'api_request_count': acc['api_request_count'],
        'api_success_count': acc['api_success_count'],
        'api_error_count': acc['api_error_count'],
        'retry_count_observed': acc['retry_count_observed'],
        'api_input_tokens_sum': acc['api_input_tokens_sum'],
        'api_output_tokens_sum': acc['api_output_tokens_sum'],
        'api_cache_read_tokens_sum': acc['api_cache_read_tokens_sum'],
        'api_cache_write_tokens_sum': acc['api_cache_write_tokens_sum'],
        'api_input_tokens_max_request': acc['api_input_tokens_max_request'],
        'api_output_tokens_max_request': acc['api_output_tokens_max_request'],
        'api_cache_read_tokens_max_request': acc['api_cache_read_tokens_max_request'],
        'api_cache_write_tokens_max_request': acc['api_cache_write_tokens_max_request'],
        'api_prompt_tokens_max_request': acc['api_prompt_tokens_max_request'],
        'api_duration_ms_sum': acc['api_duration_ms_sum'],
        'api_duration_ms_max': acc['api_duration_ms_max'],
        'finish_reason_last': acc['finish_reason_last'],
        'runtime_error_observed': bool(acc['runtime_error_observed']),
        'interrupted_observed': bool(acc['interrupted_observed']),
        'attribution_quality': ATTRIBUTION_QUALITY,
    }


def _append(record: dict) -> bool:
    """Append one JSON line. No truncation, no rewrite, never raises into the caller."""
    try:
        target = record_path()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _lock:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(record, sort_keys=True) + '\n').encode('utf-8'))
            finally:
                os.close(fd)
        return True
    except Exception:
        return False


def stats() -> dict:
    """Content-free internal counters (diagnostics and tests only)."""
    with _lock:
        out = dict(_counts)
        out['open_accumulators'] = len(_open)
        return out


def read_records(paths=None, log_dir=None) -> list:
    """Read written records back, oldest day first (read-only helper for tools and tests)."""
    if paths is None:
        root = pathlib.Path(log_dir) if log_dir else config.LOG_DIR
        paths = sorted((root / 'outcomes').glob('turn-outcome-*.jsonl'))
    out = []
    for p in paths:
        try:
            for line in pathlib.Path(p).read_text(errors='replace').splitlines():
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            continue
    return out


def _reset_for_tests() -> None:
    with _lock:
        _open.clear()
        for k in _counts:
            _counts[k] = 0
