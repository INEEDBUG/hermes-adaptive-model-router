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
}

# Canonical route names the decision service can emit. Adding a future route means
# adding its name here and to JEV_AVAILABLE_ROUTES — no other code change.
KNOWN_ROUTES = ('deepseek_flash', 'mimo_pro')

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
