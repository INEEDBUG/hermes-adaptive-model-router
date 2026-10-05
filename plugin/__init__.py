"""Hermes Agent plugin: shadow-only routing observer.

The plugin registers the routing hook ``pre_api_request`` (which fires before an LLM call) plus
four content-free prospective outcome-telemetry hooks — ``post_api_request``,
``api_request_error``, ``post_llm_call`` and ``agent_loop_stopped``. On the first call of a turn,
``pre_api_request`` builds a minimal Routing Dossier from the current user message, hands it to a
background worker, opens a per-turn outcome accumulator for an admitted human turn, and returns.
Everything the outcome hooks record is structural execution fact; see :mod:`router.outcome`.

Guarantees
----------
* the hook always returns ``None``: no context is injected and no request content
  is modified;
* every exception is swallowed: the agent turn can never fail because of routing;
* the executing model is decided by the gateway's own configuration, never here;
* with the resolved mode ``off`` the plugin returns immediately, so its behaviour
  is identical to not being installed.

Human-turn provenance boundary (v0.2.0)
--------------------------------------
A routing observation is a privacy event: it hands a sanitised, truncated copy of the
current turn to a third-party decision service. Only a **human-origin** turn may cause one,
and the two conditions are evaluated structurally, before any dossier work::

    platform     in JEV_ALLOWED_PLATFORMS     (gate 1)
    turn_origin  == "user"                    (gate 2, authoritative)

``turn_origin`` is supplied by the gateway (see
``patches/hermes-v0.21.5-turn-origin.patch``); it is never inferred from message text here.
An internal notification, a background review fork, a compaction continuation, a subagent,
a cron or a CLI turn is rejected — as is a payload with no ``turn_origin`` at all, which is
what happens when the integration patch is absent (after a Hermes upgrade, for instance).
Missing / empty / unknown origin resolves to a skip, never to ``user``.

Gate 1 alone is not a boundary: internal and background turns can inherit the platform
label of the session that spawned them, which is exactly why gate 2 exists.

Rejected turns are counted content-free (see :mod:`router.skip_telemetry`) and nothing
about them is transmitted anywhere.
"""
from __future__ import annotations

import pathlib
import sys
import threading

# Repo root (parent of this plugin directory) must be importable so that the
# sibling `router` package can be loaded by the gateway process.
_ROOT = pathlib.Path(__import__('os').environ.get('JEV_ROUTER_ROOT') or pathlib.Path(__file__).resolve().parents[1])
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_seen_turns = set()
_seen_lock = threading.Lock()
_MAX_SEEN = 256


def _first_call_of_turn(kwargs: dict) -> bool:
    """True only for the first non-retry API call of a turn.

    Evaluated before the boundary gates so that an observation and a rejection are each
    counted once per *turn*, not once per API call: a background fork that issues three
    calls is one rejection, not three.
    """
    if kwargs.get('retry_count'):
        return False
    acc = kwargs.get('api_call_count')
    if acc is None or acc == 1:
        turn_id = kwargs.get('turn_id')
        if turn_id is None:
            return True
        with _seen_lock:
            if turn_id in _seen_turns:
                return False
            _seen_turns.add(turn_id)
            if len(_seen_turns) > _MAX_SEEN:
                _seen_turns.clear()
        return True
    return False


def _effective_mode(res: dict) -> str:
    """Map a resolution result onto the mode this release can actually honour.

    ``auto`` is not implemented here (``router.state.AUTO_IMPLEMENTED`` is False):
    it is downgraded to ``shadow`` and audited, so no telemetry record or operator
    view can suggest that automatic model switching took place.
    """
    mode = res.get('mode')
    if mode not in ('off', 'shadow', 'auto'):
        return 'off'
    if mode == 'auto':
        try:
            from router import state as router_state

            return 'shadow' if not router_state.AUTO_IMPLEMENTED else 'auto'
        except Exception:
            return 'shadow'          # fail-safe: never honour auto without evidence
    return mode


def _resolve_mode() -> str:
    """Resolve the effective runtime mode once per turn; any problem means ``off``.

    The state file (``mode.json``) is authoritative; this is the shadow switch only
    and never touches provider or model selection.
    """
    try:
        from router import state as router_state

        res = router_state.resolve()
        eff = _effective_mode(res)
        if res.get('source') != 'file' or eff != res.get('mode'):
            router_state.audit({'event': 'mode_resolution', 'recorded_mode': res.get('mode'),
                                'effective_mode': eff, 'source': res.get('source'),
                                'reason': res.get('reason'),
                                'note': None if eff == res.get('mode') else 'auto_not_implemented'})
        return eff
    except Exception:
        return 'off'


def _provenance(kwargs: dict) -> tuple:
    """Return ``(platform, turn_origin)`` as normalised enumeration labels.

    Absent, non-string or empty values are returned as empty strings so the caller can
    reject them explicitly; nothing here looks at message content.
    """
    platform = str(kwargs.get('platform') or '').strip().lower()
    raw_origin = kwargs.get('turn_origin')
    origin = str(raw_origin).strip().lower() if isinstance(raw_origin, str) else ''
    return platform, origin


def _admit(kwargs: dict) -> tuple:
    """Apply the two gates. Returns ``(admitted, platform, origin)``.

    Fail-closed: every path that is not *both* an allow-listed platform and a ``user``
    origin returns ``False``. Each rejection is counted content-free; nothing is sent.
    """
    from router import config, skip_telemetry

    platform, origin = _provenance(kwargs)
    allowed = config.allowed_platforms()
    if not platform:
        skip_telemetry.bump('', origin, 'missing_platform')
        return False, platform, origin
    if not allowed:
        # No platform was ever authorised for observation: observe nothing.
        skip_telemetry.bump(platform, origin, 'allowlist_empty')
        return False, platform, origin
    if platform not in allowed:
        skip_telemetry.bump(platform, origin, 'platform_not_allowed')
        return False, platform, origin
    if not origin:
        skip_telemetry.bump(platform, '', 'missing_origin')
        return False, platform, origin
    if origin != config._HUMAN_ORIGIN:
        skip_telemetry.bump(platform, origin, 'origin_not_user')
        return False, platform, origin
    return True, platform, origin


def _on_pre_api_request(**kwargs):
    try:
        from router import config, dossier, outcome, shadow, skip_telemetry

        mode = _resolve_mode()
        if mode == 'off':
            return None
        if not _first_call_of_turn(kwargs):
            return None

        # --- human-turn provenance boundary: both gates, before any dossier work -------
        admitted, platform, origin = _admit(kwargs)
        if not admitted:
            return None
        # --- end boundary -------------------------------------------------------------

        # Prospective per-turn outcome telemetry: an accumulator exists only for an admitted
        # human turn, and it is created after the same boundary the routing observation uses —
        # never by a second, divergent notion of "human turn".
        outcome.start(kwargs, platform=platform, turn_origin=origin)

        user_message = kwargs.get('user_message')
        if not isinstance(user_message, str) or not user_message.strip():
            return None

        # Defense in depth only. ``turn_origin`` already said "human turn", so a configured
        # internal marker here is an anomaly worth counting — but content never *grants*
        # eligibility, it can only withhold it.
        markers = config.internal_markers()
        if markers and any(marker in user_message for marker in markers):
            skip_telemetry.bump(platform, origin, 'invariant_violation')
            return None

        built = dossier.build(user_message)
        if not isinstance(built, tuple) or len(built) != 2:
            return None
        d, meta = built
        actual_model = kwargs.get('model')

        if meta.get('unsafe'):
            # Content we cannot sanitise safely is never sent out for routing.
            shadow.log_privacy_fallback(turn_id=kwargs.get('turn_id'), actual_model=actual_model)
            return None

        shadow.submit(d, turn_id=kwargs.get('turn_id'), actual_model=actual_model,
                      redaction_count=meta.get('hits'), mode=mode,
                      platform=platform, turn_origin=origin)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------------------
# Prospective outcome telemetry handlers.
#
# Each one is deliberately tiny, content-blind, and wrapped so that a failure can never reach
# the agent turn: OUTCOME_TELEMETRY_FAILURE_POLICY is FAIL_OPEN_FOR_HERMES_EXECUTION and
# FAIL_CLOSED_FOR_ANALYTICS. Response, error and terminal hooks carry no turn_origin, so they
# never re-admit a turn; they resolve it through the accumulator the boundary already created.
# ---------------------------------------------------------------------------------------
def _on_post_api_request(**kwargs):
    try:
        from router import outcome

        outcome.observe_response(kwargs)
    except Exception:
        pass
    return None


def _on_api_request_error(**kwargs):
    try:
        from router import outcome

        outcome.observe_error(kwargs)
    except Exception:
        pass
    return None


def _on_post_llm_call(**kwargs):
    try:
        from router import outcome

        outcome.finalize(kwargs, 'completed')
    except Exception:
        pass
    return None


def _on_agent_loop_stopped(**kwargs):
    try:
        from router import outcome

        outcome.interrupted(kwargs)
    except Exception:
        pass
    return None


def register(ctx) -> None:
    ctx.register_hook('pre_api_request', _on_pre_api_request)
    ctx.register_hook('post_api_request', _on_post_api_request)
    ctx.register_hook('api_request_error', _on_api_request_error)
    ctx.register_hook('post_llm_call', _on_post_llm_call)
    ctx.register_hook('agent_loop_stopped', _on_agent_loop_stopped)
    try:
        import logging

        logging.getLogger('jev-shadow-router').info(
            'jev-shadow-router loaded: pre_api_request hook registered; runtime_mode=%s; '
            'allowed_platforms=%s (human turns only; fail-closed without turn_origin)',
            _resolve_mode(), _allowed_platforms_for_log())
    except Exception:
        pass


def _allowed_platforms_for_log() -> str:
    try:
        from router import config

        platforms = config.allowed_platforms()
        return ','.join(platforms) if platforms else '(none: routing inert)'
    except Exception:
        return '(unresolved)'
