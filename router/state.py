"""Runtime mode resolver: local, persistent, read once per turn, fail-safe = off.

Why not an environment variable?
--------------------------------
Hermes loads its ``.env`` with ``override=True`` and a running process does not
re-read its environment, so an env-var switch is effectively "you must restart to
turn it off". A state file plus a sentinel file can be flipped at any moment and
is honoured on the very next turn.

Resolution order (first match wins)
-----------------------------------
1. ``KILL`` sentinel present            -> ``off``
2. state file missing/unreadable/corrupt -> ``off``
3. ``tripped`` breaker flag set          -> ``off``
4. unknown mode value                    -> ``off``
5. ``auto`` without approval             -> ``shadow``
6. ``auto`` with expired ``auto_until``  -> ``shadow``
7. ``auto`` with stale ``heartbeat``     -> ``shadow``

The cost of a resolution is one or two ``stat`` calls plus at most one read of a
sub-1KB file when its mtime changed.

This resolver does **not** switch models and is not wired into any model-switching
path in this release.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

KILL_NAME = 'KILL'
MODE_NAME = 'mode.json'
HEARTBEAT_MAX_AGE_SECONDS = 120.0

VALID_MODES = ('off', 'shadow', 'auto')
_APPROVED_VALUES = {'1', 'true', 'yes', 'on'}

# Release policy flag.
#
# Automatic switching is NOT implemented in this release. A state file may record
# ``mode: "auto"`` to express operator intent, and :func:`resolve` reports that
# intent faithfully — but consumers must not act on it while this flag is False.
# The shadow plugin downgrades ``auto`` to ``shadow`` and records
# ``auto_not_implemented`` in the mode audit log, so neither telemetry nor an
# operator view can suggest that automatic routing happened.
AUTO_IMPLEMENTED = False


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get('HERMES_HOME') or '/opt/data')


def state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get('JEV_STATE_DIR') or (_home() / 'jev_router' / 'state'))


def audit_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get('JEV_AUDIT_LOG') or (_home() / 'logs' / 'router' / 'mode-audit.jsonl'))


def _approved() -> bool:
    """Explicit approval gate for auto mode: env var or .env entry only."""
    v = (os.environ.get('JEV_AUTO_APPROVED') or '').strip().lower()
    if not v:
        try:
            env_path = pathlib.Path(os.environ.get('HERMES_ENV_PATH') or (_home() / '.env'))
            for line in env_path.read_text(errors='replace').splitlines():
                if line.startswith('JEV_AUTO_APPROVED='):
                    v = line.split('=', 1)[1].strip().strip('"\' ').lower()
                    break
        except Exception:
            v = ''
    return v in _APPROVED_VALUES


def resolve(now: float | None = None) -> dict:
    """Return ``{'mode', 'source', 'reason'}`` with ``mode`` in ``off|shadow|auto``.

    Any error while resolving is treated as ``off``.
    """
    now = time.time() if now is None else float(now)
    d = state_dir()
    kill = d / KILL_NAME

    # 1) kill sentinel (including a failed stat) -> off
    try:
        if kill.exists():
            return {'mode': 'off', 'source': 'kill_file', 'reason': 'KILL sentinel present'}
    except OSError as exc:
        return {'mode': 'off', 'source': 'error', 'reason': f'KILL stat failed: {type(exc).__name__}'}

    # 2) state file missing / unreadable / corrupt -> off
    try:
        data = json.loads((d / MODE_NAME).read_text())
        if not isinstance(data, dict):
            raise ValueError('mode.json is not a JSON object')
    except FileNotFoundError:
        return {'mode': 'off', 'source': 'default', 'reason': 'mode.json missing'}
    except Exception as exc:
        return {'mode': 'off', 'source': 'default',
                'reason': f'mode.json unreadable/invalid: {type(exc).__name__}'}

    # 3) breaker flag -> off
    if data.get('tripped') is True:
        return {'mode': 'off', 'source': 'breaker', 'reason': 'tripped flag set'}

    # 4) mode value must be one of the known modes
    mode = str(data.get('mode') or '').strip().lower()
    if mode not in VALID_MODES:
        return {'mode': 'off', 'source': 'default', 'reason': f'invalid mode value {mode!r}'}

    # 5-7) auto requires approval + a live lease + a fresh heartbeat
    if mode == 'auto':
        if not _approved():
            return {'mode': 'shadow', 'source': 'approval', 'reason': 'auto not approved (JEV_AUTO_APPROVED unset)'}
        until = data.get('auto_until')
        if not isinstance(until, (int, float)) or isinstance(until, bool) or float(until) <= now:
            return {'mode': 'shadow', 'source': 'expiry', 'reason': 'auto_until missing or expired'}
        hb = data.get('heartbeat')
        if not isinstance(hb, (int, float)) or isinstance(hb, bool) or (now - float(hb)) > HEARTBEAT_MAX_AGE_SECONDS:
            return {'mode': 'shadow', 'source': 'heartbeat', 'reason': 'heartbeat stale'}
        return {'mode': 'auto', 'source': 'file', 'reason': 'ok'}

    return {'mode': mode, 'source': 'file', 'reason': 'ok'}


def audit(record: dict) -> None:
    """Append a mode-resolution/change entry. Never records credentials."""
    try:
        audit_path().parent.mkdir(parents=True, exist_ok=True)
        entry = dict(record)
        entry.setdefault('timestamp', time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()))
        with open(audit_path(), 'a') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass


def write_state(mode: str, **extra) -> pathlib.Path:
    """Write the state file atomically. Operational/test use only."""
    if mode not in VALID_MODES:
        raise ValueError(f'invalid mode {mode!r}')
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = {'mode': mode, 'updated_at': time.time(), **extra}
    tmp = d / (MODE_NAME + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(d / MODE_NAME)
    return d / MODE_NAME
