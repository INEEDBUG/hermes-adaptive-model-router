"""Hermes Agent plugin: shadow-only routing observer.

The plugin registers exactly one hook, ``pre_api_request``, which fires before an
LLM call. On the first call of a turn it builds a minimal Routing Dossier from the
current user message, hands it to a background worker and returns.

Guarantees
----------
* the hook always returns ``None``: no context is injected and no request content
  is modified;
* every exception is swallowed: the agent turn can never fail because of routing;
* the executing model is decided by the gateway's own configuration, never here;
* with the resolved mode ``off`` the plugin returns immediately, so its behaviour
  is identical to not being installed.
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
    """True only for the first non-retry API call of a turn."""
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


def _on_pre_api_request(**kwargs):
    try:
        from router import dossier, shadow

        mode = _resolve_mode()
        if mode == 'off':
            return None
        if not _first_call_of_turn(kwargs):
            return None

        user_message = kwargs.get('user_message')
        if not isinstance(user_message, str) or not user_message.strip():
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
                      redaction_count=meta.get('hits'), mode=mode)
    except Exception:
        pass
    return None


def register(ctx) -> None:
    ctx.register_hook('pre_api_request', _on_pre_api_request)
    try:
        import logging

        logging.getLogger('jev-shadow-router').info(
            'jev-shadow-router loaded: pre_api_request hook registered; runtime_mode=%s '
            '(fail-safe off; no provider/model switching)', _resolve_mode())
    except Exception:
        pass
