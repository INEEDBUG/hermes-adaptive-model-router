"""Prospective per-turn outcome telemetry.

Status: **deployed and canary-validated.** Production runs generation ``canonical-922bd11``
(runtime SHA ``922bd11aa3e019059acfc0195bf7dd1b24bc5e9c``) in ``shadow`` with Auto disabled, and a
real natural human turn produced one routing observation and one matching terminal record.
Per-turn exact figures start existing only from that generation onwards; historical records do not
become exact retroactively.

What this module records
------------------------
Structural execution facts for **admitted human turns only**, one terminal record per turn:

    request counts, request-error counts, retry counts observed,
    normalized usage buckets summed and maxed per request,
    API durations summed and maxed, finish reason, model attribution,
    terminal status, observed execution duration, attribution quality.

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

Human-turn boundary and cohort
------------------------------
Admission reuses the plugin's single boundary (``plugin._admit``): platform allow-list **and**
``turn_origin == 'user'``. This module never re-implements human-turn inference, and a non-user
turn never gets an accumulator. The rejection counters keep their existing behaviour.

The plugin opens the accumulator only *after* the turn's message was structurally usable, the
internal marker invariant held, a dossier was built and privacy permitted routing — and *before*
the routing submission. So the cohort is::

    OUTCOME_COHORT_CONTRACT: one accumulator == one turn that reached an actual canonical
    routing observation attempt. outcome_scope == 'routing_attempt'

A turn that never reached a routing attempt has no outcome record. Consequence: routing
denominators (admitted shadow turns) and outcome denominators share the same boundary, and
anything whose correlation does not match an admitted shadow turn is reported as UNMATCHED
instead of being silently mixed in.

Request identity semantics (read from the Hermes source, not assumed)
--------------------------------------------------------------------
``agent/conversation_loop.py`` assigns, once per outer-loop iteration and *before* the retry
loop::

    s.api_request_id = agent._current_api_request_id = f"{s.turn_id}:api:{s.api_call_count}"

``_run_api_retry_loop`` (``while retry_count < max_retries``) never reassigns it, while
``build_api_request`` fires ``pre_api_request`` on every pass. Hence::

    API_REQUEST_ID_SCOPE        = LOGICAL_CALL
    RETRY_REUSES_API_REQUEST_ID = YES
    PHYSICAL_ATTEMPT_IDENTITY   = (api_request_id, retry_count)  # observable on pre / error only

Therefore the id alone can never be the dedupe key for request counting: retries of one logical
call legitimately share it, and an error attempt followed by a successful retry would otherwise
swallow the success. Attempt-keyed counting is exact while ``retry_count`` stays present on the
attempt hooks; ``post_api_request`` carries no ``retry_count``, so a successful response is
counted as the event it is (one successful physical response) rather than being suppressed by a
shared id.

``REQUEST_COUNTING_MODEL = ATTEMPT_KEYED``, with two namespaces that must not interfere — an
error attempt may never swallow the success that follows it::

    attempt namespace  (api_request_id, retry_count)     shared by pre_api_request / api_request_error
    response namespace (api_request_id, api_call_count)  post_api_request

    api_logical_call_count = distinct api_request_id observed on any hook
    api_request_count      = distinct attempts observed, plus a successful response whose logical
                             call had no observed attempt hook
    api_error_count        = distinct failed attempts      (attempt namespace)
    api_success_count      = distinct successful responses (response namespace)
    retry_count_observed   = attempts observed with retry_count > 0

``DUPLICATE_HOOK_POLICY = SUPPRESS_DUPLICATE_AND_DEGRADE``: a repeated key inside a namespace is
suppressed as a duplicate delivery **and** the turn degrades to ``PARTIAL_PROSPECTIVE``. Bounded
memory follows the same rule: past capacity the key sets stop growing, novelty can no longer be
proven, and the turn degrades. Counting continues as structure in both cases, but a suppressed
event can never be presented as exact::

    EXACTNESS_NEVER_SILENTLY_FALSE = YES

Attribution quality (closed enum)
---------------------------------
``attribution_quality`` is exactly one of::

    EXACT_PROSPECTIVE     counts and usage buckets are complete for this turn
    PARTIAL_PROSPECTIVE   at least one enumerated degradation applied; structural facts are still
                          recorded, but analytics must not treat them as exact

Degradations are a closed list of tokens carried in ``attribution_quality_reasons``:
``duplicate_hook_event`` (a repeated key was suppressed as a duplicate delivery) and
``request_identity_saturated`` (key capacity reached, so novelty is no longer provable). No
free-form string ever enters this field.

Request-identity saturation policy
----------------------------------
``REQUEST_DEDUP_SATURATION_POLICY = SATURATE_THEN_DEGRADE``: past ``_MAX_SEEN_ATTEMPTS`` keys the
set stops growing, ``request_identity_saturated`` is set, and the record degrades to
``PARTIAL_PROSPECTIVE``. Later duplicate hooks can no longer be proven duplicates, so those counts
are lower bounds — which is precisely why the record must not keep claiming ``EXACT``.

Turn correlation
----------------
``TURN_CORRELATION_SCHEME = sha256(structural turn_id)``, full 64-hex digest, local join only. Raw
identifiers are never written. The digest contains no content and is not an identity; an analyzer
that wants to join it to routing telemetry re-hashes the shadow record's ``turn_id`` locally.

Duration semantics
------------------
``DURATION_SEMANTIC = observed_execution_duration_ms``: ``duration_ms`` runs from the moment this
module opened the accumulator for the turn (after admission and just before the routing
submission) to the observed terminal hook. It is **not** user-perceived latency, **not** the full
user-message-arrival → final-response wall time, and **not** model latency.

Terminal semantics
------------------
``post_llm_call`` is the normal completion seam and yields ``completed``. ``agent_loop_stopped``
yields ``interrupted``, but its payload carries only a session key — so a record is written only
when exactly one open accumulator matches that key; otherwise nothing is written. An
``api_request_error`` never declares a turn terminal by itself::

    MISSING_TERMINAL_IS_SUCCESS = NO
    TERMINAL_STATUS_ENUM = [completed, interrupted]

A turn whose terminal hook is never observed leaves **no record at all**. Missing is missing:
outcome analytics must compute ``ADMITTED_SHADOW_TURNS - TERMINAL_OUTCOMES`` and keep the gap
visible. An unfinished accumulator may be lost on a gateway restart or crash, and that is a
MISSING_TERMINAL outcome, never a completed one.

Day partition
-------------
``OUTCOME_DAY_PARTITION = UTC`` — the daily file name is computed with ``time.gmtime()`` and never
from the process timezone, so the partition is stable regardless of ``TZ``.

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

# Closed enum for attribution quality.
EXACT_PROSPECTIVE = 'EXACT_PROSPECTIVE'
PARTIAL_PROSPECTIVE = 'PARTIAL_PROSPECTIVE'
ATTRIBUTION_QUALITIES = (EXACT_PROSPECTIVE, PARTIAL_PROSPECTIVE)

# Closed enum of degradation tokens. Nothing outside this tuple may ever be recorded.
REASON_IDENTITY_SATURATED = 'request_identity_saturated'
REASON_DUPLICATE_HOOK_EVENT = 'duplicate_hook_event'
DEGRADATION_REASONS = (REASON_IDENTITY_SATURATED, REASON_DUPLICATE_HOOK_EVENT)

# One accumulator == one turn that reached a routing observation attempt.
OUTCOME_SCOPE = 'routing_attempt'

DURATION_SEMANTIC = 'observed_execution_duration_ms'
DAY_PARTITION_TZ = 'UTC'

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
_MAX_SEEN_ATTEMPTS = 512        # bounded attempt-key set per turn (saturation degrades attribution)
_MAX_SEEN_POSTS = 512           # bounded logical-call ids seen on successful responses
_MAX_MODELS = 8                 # bounded model inventory
_MAX_LABEL = 64
_LABEL_RX = re.compile(r'[^a-z0-9._:-]+')
_FINISH_RX = re.compile(r'[^a-z_]+')

_lock = threading.RLock()
_open: 'dict[str, dict]' = {}
_counts = {'started': 0, 'finalized': 0, 'evicted_unfinished': 0, 'duplicate_request': 0,
           'late_hook_ignored': 0, 'interrupt_unmatched': 0, 'write_failed': 0,
           'identity_saturated_turns': 0, 'repeat_logical_call_success': 0,
           'unpaired_success': 0}


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


def _attempt_key(request_id, retry_count) -> str:
    """Physical-attempt identity: the logical call id plus the attempt index.

    ``retry_count`` is present on ``pre_api_request`` and ``api_request_error`` — the two hooks
    that fire per attempt — and absent on ``post_api_request``, which is why a successful response
    is counted as an event instead of being keyed.
    """
    return f'{request_id or "?"}@{_int(retry_count)}'


def record_path(day: str | None = None, log_dir=None) -> pathlib.Path:
    """``<JEV_LOG_DIR>/outcomes/turn-outcome-YYYY-MM-DD.jsonl`` (day partition = UTC).

    A separate directory, not a new environment variable: the deployment already defines its
    telemetry root through ``JEV_LOG_DIR``, and mixing two schemas in ``shadow-*.jsonl`` would
    make either one unreadable by its own tooling.
    """
    root = pathlib.Path(log_dir) if log_dir else config.LOG_DIR
    stamp = day or time.strftime('%Y-%m-%d', time.gmtime())
    return root / 'outcomes' / f'turn-outcome-{stamp}.jsonl'


# --------------------------------------------------------------------------- lifecycle
def start(kwargs: dict, *, platform: str, turn_origin: str, outcome_scope: str = OUTCOME_SCOPE) -> bool:
    """Establish an accumulator for one admitted human turn that reached a routing attempt.

    The caller (``plugin._on_pre_api_request``) applies the boundary and calls this after the
    dossier was built and privacy permitted routing, immediately before ``shadow.submit``.
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
                'outcome_scope': _label(outcome_scope, '(missing)'),
                'session_key': str(picked.get('session_id') or ''),
                'platform': _label(platform, '(missing)'),
                'turn_origin': _label(turn_origin, '(missing)'),
                'deployment_generation': config.deployment_generation(),
                'first_seen_monotonic': time.monotonic(),
                'actual_model_first': _label(picked.get('model')),
                'actual_model_last': _label(picked.get('model')),
                'models_seen': [_label(picked.get('model'))],
                'model_switch_count': 0,
                'api_logical_call_count': 0,
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
                'seen_attempt_keys': set(),
                'seen_pre_events': set(),
                'seen_error_events': set(),
                'seen_logical_calls': set(),
                'seen_post_calls': set(),
                'request_identity_saturated': False,
                'degradations': [],
            }
            _counts['started'] += 1
        return True
    except Exception:
        return False


def _note_model(acc: dict, model) -> None:
    """Record a model observation.

    ``model_switch_count`` counts **transitions**: it advances exactly when the newly observed
    model differs from ``actual_model_last``. ``models_seen`` is only a bounded unique-model
    inventory and never decides the switch count, so A -> B -> A is 2 transitions and
    A -> B -> B is 1.
    """
    token = _label(model)
    if token != acc['actual_model_last']:
        acc['model_switch_count'] += 1
        acc['actual_model_last'] = token
    if token not in acc['models_seen'] and len(acc['models_seen']) < _MAX_MODELS:
        acc['models_seen'].append(token)


def _degrade(acc: dict, reason: str) -> None:
    if reason in DEGRADATION_REASONS and reason not in acc['degradations']:
        acc['degradations'].append(reason)


def _saturate(acc: dict) -> None:
    """Mark identity saturation once per turn and degrade it, never silently."""
    if not acc['request_identity_saturated']:
        acc['request_identity_saturated'] = True
        _counts['identity_saturated_turns'] += 1
    _degrade(acc, REASON_IDENTITY_SATURATED)


def _note_attempt(acc: dict, request_id, retry_count, kind: str) -> bool:
    """Register an attempt observation. Returns True when the *attempt* is newly observed.

    One attempt can legitimately be observed twice — ``pre_api_request`` before it runs and
    ``api_request_error`` when it fails — so duplicate delivery is detected **within a hook kind**,
    not across kinds. Three paths must never be silent:

    * a repeated key inside one kind is a duplicate delivery: suppressed *and* the turn degrades;
    * past capacity the key sets stop growing: no degradation-free novelty can be claimed, so the
      turn degrades;
    * a cross-kind hit is an expected second sighting of the same attempt: neither double counted
      nor degraded.

    Suppression can therefore never be presented as ``EXACT``, because ``EXACT`` and
    ``EXACTNESS_NEVER_SILENTLY_FALSE`` have to hold together.
    """
    key = _attempt_key(request_id, retry_count)
    kind_set = acc['seen_pre_events' if kind == 'pre' else 'seen_error_events']
    if key in kind_set:
        _counts['duplicate_request'] += 1
        _degrade(acc, REASON_DUPLICATE_HOOK_EVENT)
        return False
    if len(kind_set) < _MAX_SEEN_ATTEMPTS:
        kind_set.add(key)
    else:
        _saturate(acc)
    if key in acc['seen_attempt_keys']:
        return False                       # same attempt seen through the other hook: not new
    if len(acc['seen_attempt_keys']) < _MAX_SEEN_ATTEMPTS:
        acc['seen_attempt_keys'].add(key)
    else:
        _saturate(acc)
    return True


def _note_logical_call(acc: dict, request_id) -> None:
    if not request_id:
        return
    if request_id in acc['seen_logical_calls']:
        return
    if len(acc['seen_logical_calls']) < _MAX_SEEN_POSTS:
        acc['seen_logical_calls'].add(request_id)
    else:
        _saturate(acc)
    acc['api_logical_call_count'] += 1


def observe_request(kwargs: dict) -> bool:
    """Register one physical attempt from ``pre_api_request`` (fires once per attempt)."""
    try:
        picked = _pick(kwargs, _PRE_KEYS)
        corr = _correlation(picked.get('turn_id'))
        with _lock:
            acc = _open.get(corr)
            if acc is None:
                _counts['late_hook_ignored'] += 1
                return False
            request_id = str(picked.get('api_request_id') or '')
            if _note_attempt(acc, request_id, picked.get('retry_count'), 'pre'):
                acc['api_request_count'] += 1
                if _int(picked.get('retry_count')) > 0:
                    acc['retry_count_observed'] += 1
            _note_logical_call(acc, request_id)
            _note_model(acc, picked.get('model'))
        return True
    except Exception:
        return False


def observe_response(kwargs: dict) -> bool:
    """Aggregate one successful physical response into its turn's accumulator."""
    try:
        picked = _pick(kwargs, _POST_KEYS)
        corr = _correlation(picked.get('turn_id'))
        with _lock:
            acc = _open.get(corr)
            if acc is None:
                _counts['late_hook_ignored'] += 1
                return False
            request_id = str(picked.get('api_request_id') or '')

            # ``api_success_count`` counts successful physical responses. The response namespace is
            # kept apart from the attempt namespace (``api_request_id``, ``retry_count``) and keyed
            # by (``api_request_id``, ``api_call_count``), so an error attempt can never swallow the
            # success that follows it. A repeat in *this* namespace is a duplicate delivery: it is
            # suppressed and the turn degrades, never silently.
            post_key = f'{request_id or "?"}#{_int(picked.get("api_call_count"))}'
            if post_key in acc['seen_post_calls']:
                _counts['duplicate_request'] += 1
                _degrade(acc, REASON_DUPLICATE_HOOK_EVENT)
                return False
            if len(acc['seen_post_calls']) < _MAX_SEEN_POSTS:
                acc['seen_post_calls'].add(post_key)
            else:
                _saturate(acc)
            acc['api_success_count'] += 1
            if request_id and request_id not in acc['seen_logical_calls']:
                # A success whose attempt hook was never observed still counts as one request.
                _counts['unpaired_success'] += 1
                acc['api_request_count'] += 1
            _note_logical_call(acc, request_id)

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
    """Count a failed physical attempt. Recorded structurally: the error body is never read."""
    try:
        picked = _pick(kwargs, _ERROR_KEYS)
        corr = _correlation(picked.get('turn_id'))
        with _lock:
            acc = _open.get(corr)
            if acc is None:
                _counts['late_hook_ignored'] += 1
                return False
            request_id = str(picked.get('api_request_id') or '')
            retry_count = picked.get('retry_count')
            if _note_attempt(acc, request_id, retry_count, 'error'):
                acc['api_request_count'] += 1
                if _int(retry_count) > 0:
                    acc['retry_count_observed'] += 1
            _note_logical_call(acc, request_id)
            # The failed attempt is its own attempt key, so a following retry that reuses the
            # logical call id is a different attempt and is never swallowed by this one.
            acc['api_error_count'] += 1
            acc['runtime_error_observed'] = True
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
            if picked.get('model'):
                # Same transition logic as every other observation: a model change first exposed
                # on the terminal hook must advance the switch count, never bypass it.
                _note_model(acc, picked.get('model'))
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
def _attribution_quality(acc: dict) -> str:
    return PARTIAL_PROSPECTIVE if acc['degradations'] else EXACT_PROSPECTIVE


def _build_record(acc: dict, status: str) -> dict:
    duration_ms = max(0, int((time.monotonic() - acc['first_seen_monotonic']) * 1000))
    return {
        'schema_version': SCHEMA_VERSION,
        'outcome_scope': acc['outcome_scope'],
        'deployment_generation': acc['deployment_generation'],
        'platform': acc['platform'],
        'turn_origin': acc['turn_origin'],
        'turn_correlation': acc['turn_correlation'],
        'actual_model_first': acc['actual_model_first'],
        'actual_model_last': acc['actual_model_last'],
        'models_seen_count': len(acc['models_seen']),
        'model_switch_count': acc['model_switch_count'],
        'terminal_status': status,
        'duration_ms': duration_ms,
        'duration_semantics': DURATION_SEMANTIC,
        'api_logical_call_count': acc['api_logical_call_count'],
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
        'request_identity_saturated': bool(acc['request_identity_saturated']),
        'attribution_quality': _attribution_quality(acc),
        'attribution_quality_reasons': list(acc['degradations']),
    }


RECORD_KEYS = frozenset({
    'schema_version', 'outcome_scope', 'deployment_generation', 'platform', 'turn_origin',
    'turn_correlation', 'actual_model_first', 'actual_model_last', 'models_seen_count',
    'model_switch_count', 'terminal_status', 'duration_ms', 'duration_semantics',
    'api_logical_call_count', 'api_request_count', 'api_success_count', 'api_error_count',
    'retry_count_observed', 'api_input_tokens_sum', 'api_output_tokens_sum',
    'api_cache_read_tokens_sum', 'api_cache_write_tokens_sum',
    'api_input_tokens_max_request', 'api_output_tokens_max_request',
    'api_cache_read_tokens_max_request', 'api_cache_write_tokens_max_request',
    'api_prompt_tokens_max_request', 'api_duration_ms_sum', 'api_duration_ms_max',
    'finish_reason_last', 'runtime_error_observed', 'interrupted_observed',
    'request_identity_saturated', 'attribution_quality', 'attribution_quality_reasons',
})
_INT_FIELDS = frozenset({
    'models_seen_count', 'model_switch_count', 'duration_ms', 'api_logical_call_count',
    'api_request_count', 'api_success_count', 'api_error_count', 'retry_count_observed',
    'api_input_tokens_sum', 'api_output_tokens_sum', 'api_cache_read_tokens_sum',
    'api_cache_write_tokens_sum', 'api_input_tokens_max_request', 'api_output_tokens_max_request',
    'api_cache_read_tokens_max_request', 'api_cache_write_tokens_max_request',
    'api_prompt_tokens_max_request', 'api_duration_ms_sum', 'api_duration_ms_max',
})
_BOOL_FIELDS = frozenset({'runtime_error_observed', 'interrupted_observed',
                          'request_identity_saturated'})

# Closed reject reasons for the parser. Counts are reported; offending values are never echoed.
REJECT_UNKNOWN_SCHEMA = 'unknown_schema'
REJECT_UNEXPECTED_FIELD = 'unexpected_field'
REJECT_INVALID_TERMINAL_STATUS = 'invalid_terminal_status'
REJECT_INVALID_ATTRIBUTION_QUALITY = 'invalid_attribution_quality'
REJECT_INVALID_REASON = 'invalid_attribution_reason'
REJECT_MALFORMED_NUMERIC = 'malformed_numeric'
REJECT_MISSING_FIELD = 'missing_field'
REJECT_INVALID_TYPE = 'invalid_type'


def validate_record(record) -> str | None:
    """Strict, fail-closed validation. Returns a closed reject reason, or ``None`` when usable.

    The parser never coerces: an unknown schema version, an unexpected key, an out-of-enum
    terminal status or attribution quality, a non-enum degradation token, a malformed numeric or a
    wrong type all reject the whole record. Callers must report counts only — never the offending
    value, which could carry content.
    """
    if not isinstance(record, dict):
        return REJECT_UNKNOWN_SCHEMA
    if record.get('schema_version') != SCHEMA_VERSION:
        return REJECT_UNKNOWN_SCHEMA
    if set(record) - RECORD_KEYS:
        return REJECT_UNEXPECTED_FIELD
    if RECORD_KEYS - set(record):
        return REJECT_MISSING_FIELD
    if record.get('terminal_status') not in TERMINAL_STATUSES:
        return REJECT_INVALID_TERMINAL_STATUS
    if record.get('attribution_quality') not in ATTRIBUTION_QUALITIES:
        return REJECT_INVALID_ATTRIBUTION_QUALITY
    reasons = record.get('attribution_quality_reasons')
    if not isinstance(reasons, list) or any(r not in DEGRADATION_REASONS for r in reasons):
        return REJECT_INVALID_REASON
    for name in _INT_FIELDS:
        value = record.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return REJECT_MALFORMED_NUMERIC
    for name in _BOOL_FIELDS:
        if not isinstance(record.get(name), bool):
            return REJECT_INVALID_TYPE
    finish = record.get('finish_reason_last')
    if finish is not None and not isinstance(finish, str):
        return REJECT_INVALID_TYPE
    for name in ('outcome_scope', 'deployment_generation', 'platform', 'turn_origin',
                 'turn_correlation', 'actual_model_first', 'actual_model_last',
                 'duration_semantics'):
        if not isinstance(record.get(name), str) or not record.get(name):
            return REJECT_INVALID_TYPE
    if len(record['turn_correlation']) != 64 or any(c not in '0123456789abcdef'
                                                    for c in record['turn_correlation']):
        return REJECT_INVALID_TYPE
    return None


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
    good, _rejects = read_records_strict(paths=paths, log_dir=log_dir)
    return good


def read_records_strict(paths=None, log_dir=None):
    """Read and validate. Returns ``(records, reject_counts)``; nothing is coerced."""
    if paths is None:
        root = pathlib.Path(log_dir) if log_dir else config.LOG_DIR
        paths = sorted((root / 'outcomes').glob('turn-outcome-*.jsonl'))
    good, rejects, lines = [], {}, 0
    for p in paths:
        try:
            text = pathlib.Path(p).read_text(errors='replace')
        except Exception:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            lines += 1
            try:
                parsed = json.loads(line)
            except Exception:
                rejects[REJECT_UNKNOWN_SCHEMA] = rejects.get(REJECT_UNKNOWN_SCHEMA, 0) + 1
                continue
            reason = validate_record(parsed)
            if reason:
                rejects[reason] = rejects.get(reason, 0) + 1
            else:
                good.append(parsed)
    return good, {'lines': lines, 'accepted': len(good), 'rejected': rejects}


def _reset_for_tests() -> None:
    with _lock:
        _open.clear()
        for k in _counts:
            _counts[k] = 0
