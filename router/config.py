"""Configuration and mode resolution for the shadow router.

Only ``off`` and ``shadow`` are reachable in this release; ``auto`` additionally
requires an explicit approval flag (see :func:`mode`), so editing a single
environment variable can never enable automatic model switching by accident.
"""
from __future__ import annotations

import os
import pathlib
import re
import time

# Hermes stores its state under HERMES_HOME (default: /opt/data in the container).
HERMES_HOME = pathlib.Path(os.environ.get('HERMES_HOME') or '/opt/data')

# Credentials are read from the environment first, then from this file.
ENV_PATH = pathlib.Path(os.environ.get('HERMES_ENV_PATH') or (HERMES_HOME / '.env'))

# Shadow telemetry (JSONL + human-readable log) is written here.
LOG_DIR = pathlib.Path(os.environ.get('JEV_LOG_DIR') or (HERMES_HOME / 'logs' / 'router'))

DEFAULTS = {
    'ROUTER_MODE': 'shadow',
    'JEV_MODEL': 'jev-latest',
    'JEV_MIN_CONFIDENCE': '0.65',
    'JEV_MIN_MARGIN': '0.15',
    'JEV_TIMEOUT_SECONDS': '3',
    # Route availability is configured, never assumed: empty means "no route has been
    # validated for this deployment" (see available_routes()).
    'JEV_AVAILABLE_ROUTES': '',
    # Gate 1 of the human-turn boundary: platforms whose *human* turns may be observed.
    # Empty means "none" — routing stays inert until an operator names them (fail-closed).
    'JEV_ALLOWED_PLATFORMS': '',
    # Optional defense-in-depth markers, separated by '||'. This is an anomaly detector,
    # never the boundary: the authoritative condition is turn_origin == 'user'. Empty
    # disables it.
    'JEV_INTERNAL_MARKERS': '',
}

# Canonical route names the decision service can emit. A future route is listed here and
# named in JEV_AVAILABLE_ROUTES, which makes deployment availability explicit and
# prepares this configuration surface for it — but adding a route for real still means
# extending its criteria, telemetry, simulation and tests.
KNOWN_ROUTES = ('deepseek_flash', 'mimo_pro')

# ---------------------------------------------------------------------------
# Human-turn provenance (v0.2.0)
# ---------------------------------------------------------------------------
# A turn's origin is a *structural* label supplied by the gateway (see
# patches/hermes-v0.21.5-turn-origin.patch). It is never inferred from message text, and a
# missing label is never treated as `user`.
TURN_ORIGINS = ('user', 'internal_notification', 'compaction_continuation', 'background_review',
                'subagent', 'oneshot', 'cron', 'curator', 'api_server', 'unknown')
UNKNOWN_ORIGIN = 'unknown'
_HUMAN_ORIGIN = 'user'

_AUTO_APPROVAL_KEYS = {'JEV_AUTO_APPROVED'}
_APPROVED_VALUES = {'1', 'true', 'yes', 'on'}

_cache: dict = {'at': 0.0, 'env': {}, 'mode_warning': None}


def _read_env_file() -> dict:
    out = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(errors='replace').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def raw(name: str, default: str = '') -> str:
    """Resolve ``name`` as environment -> .env file -> default.

    The .env file is re-read at most once every 30 seconds, so a threshold can be
    adjusted without restarting the gateway.
    """
    now = time.time()
    if now - _cache['at'] > 30:
        _cache['env'] = _read_env_file()
        _cache['at'] = now
    v = os.environ.get(name)
    if v is None or v == '':
        v = _cache['env'].get(name)
    if v is None or v == '':
        v = DEFAULTS.get(name, default)
    return str(v)


def mode() -> str:
    """Return ``off`` or ``shadow``.

    ``auto`` is only honoured when ``JEV_AUTO_APPROVED`` is explicitly truthy;
    otherwise the request is downgraded to ``shadow`` in code, not just in docs.
    """
    m = (raw('ROUTER_MODE') or 'shadow').strip().lower()
    if m not in {'off', 'shadow', 'auto'}:
        return 'shadow'
    if m == 'auto':
        approved = (os.environ.get('JEV_AUTO_APPROVED') or _cache['env'].get('JEV_AUTO_APPROVED') or '').strip().lower()
        if approved in _APPROVED_VALUES:
            return 'auto'
        # Hard gate: never enter auto mode without explicit approval.
        _cache['mode_warning'] = 'auto requested but JEV_AUTO_APPROVED not set -> downgraded to shadow'
        return 'shadow'
    return m


def auto_requested_but_blocked() -> bool:
    return bool(_cache['mode_warning']) and (raw('ROUTER_MODE').strip().lower() == 'auto')


def available_routes() -> tuple:
    """Routes this deployment has configured **and** independently validated.

    Resolved from ``JEV_AVAILABLE_ROUTES`` (comma or space separated). The public
    default is **empty**: this repository never assumes an alternative provider works,
    so a route only counts as executable once an operator names it here after
    validation. Unknown names are ignored rather than trusted.
    """
    value = str(raw('JEV_AVAILABLE_ROUTES', '') or '')
    out = []
    for part in re.split(r'[,\s]+', value.strip()):
        name = part.strip().lower()
        if name and name in KNOWN_ROUTES and name not in out:
            out.append(name)
    return tuple(out)


def allowed_platforms() -> tuple:
    """Gate 1: platforms whose human turns may be observed (``JEV_ALLOWED_PLATFORMS``).

    The public default is **empty**, i.e. no platform is allowed and the router stays inert
    until an operator names one (``feishu``, ``telegram``, ...).

    This gate alone is *not* a privacy boundary: an internal notification, a background
    review fork or a subagent turn can carry the same platform label as a human message
    (a fork inherits ``platform`` from the agent that spawned it). Only together with
    ``turn_origin == 'user'`` does it describe human traffic — see ``plugin/__init__.py``.
    """
    value = str(raw('JEV_ALLOWED_PLATFORMS', '') or '')
    out = []
    for part in re.split(r'[,\s]+', value.strip()):
        name = part.strip().lower()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def internal_markers() -> tuple:
    """Optional anomaly-detector markers (``JEV_INTERNAL_MARKERS``, ``'||'`` separated).

    Used only as defense in depth: if a turn claims ``turn_origin == 'user'`` but its text
    carries a configured internal marker, the turn is skipped and counted as an invariant
    violation. The authoritative condition remains ``turn_origin``; content is never used
    to *grant* eligibility.
    """
    value = str(raw('JEV_INTERNAL_MARKERS', '') or '')
    return tuple(p.strip() for p in value.split('||') if p.strip())


def skip_counter_path() -> pathlib.Path:
    """Content-free rejection counters (see :mod:`router.skip_telemetry`)."""
    return LOG_DIR / 'skipped-turn-counters.json'


def min_confidence() -> float:
    try:
        return float(raw('JEV_MIN_CONFIDENCE'))
    except ValueError:
        return 0.65


def min_margin() -> float:
    try:
        return float(raw('JEV_MIN_MARGIN'))
    except ValueError:
        return 0.15


def timeout_seconds() -> float:
    try:
        return max(0.5, min(10.0, float(raw('JEV_TIMEOUT_SECONDS'))))
    except ValueError:
        return 3.0


def jev_model() -> str:
    return raw('JEV_MODEL') or 'jev-latest'


def log_path(day: str) -> pathlib.Path:
    return LOG_DIR / f'shadow-{day}.jsonl'


def human_log_path(day: str) -> pathlib.Path:
    return LOG_DIR / f'shadow-{day}.log'
