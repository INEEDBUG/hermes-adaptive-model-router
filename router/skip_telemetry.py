"""Content-free counters for turns the provenance boundary rejected.

The boundary must leave a trace (otherwise a silent misconfiguration is indistinguishable
from a busy deployment), but the trace may not contain content. What is written is a closed
set of enumeration labels and a count::

    {
      "2026-01-01": {
        "date": "2026-01-01",
        "counters": {
          "feishu|background_review|origin_not_user": {
            "platform": "feishu",
            "turn_origin": "background_review",
            "reason": "origin_not_user",
            "count": 1
          }
        }
      }
    }

Never written: prompt text, message bodies, Routing Dossiers, tool input or output, memory,
system prompts, session / turn / message ids, credentials.

Two properties enforce that structurally rather than by convention:

* :func:`bump` has no message parameter — the write path cannot receive turn content;
* every label is normalised to a short ``[a-z0-9_-]`` token and an unknown reason collapses
  to ``other``, so no free-form string can reach the file even if a caller passes one.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time

from router import config

ALLOWED_REASONS = (
    'allowlist_empty',        # JEV_ALLOWED_PLATFORMS is unset: nothing may be observed
    'missing_platform',       # the payload carried no platform label
    'platform_not_allowed',   # platform outside JEV_ALLOWED_PLATFORMS (subagent, cli, cron, ...)
    'missing_origin',         # no turn_origin in the payload (e.g. core integration patch absent)
    'origin_not_user',        # internal notification, background review, compaction, ...
    'invariant_violation',    # origin said user but the content was recognised as internal
    'other',
)

_MAX_LABEL = 40
_LABEL_RX = re.compile(r'[^a-z0-9_\-]+')
_lock = threading.Lock()


def _label(value) -> str:
    """Normalise a label to a bounded token; anything unusable becomes ``(missing)``."""
    token = _LABEL_RX.sub('_', str(value or '').strip().lower())[:_MAX_LABEL].strip('_')
    return token or '(missing)'


def _reason(value) -> str:
    token = _label(value)
    return token if token in ALLOWED_REASONS else 'other'


def path() -> pathlib.Path:
    return config.skip_counter_path()


def bump(platform, turn_origin, reason, *, path=None) -> dict:
    """Increment the counter for one rejected turn. Returns the stored entry.

    Call it once per turn, not once per API call: the caller owns that de-duplication.
    Failures are swallowed by the caller — telemetry never blocks the agent.
    """
    target = pathlib.Path(path) if path else config.skip_counter_path()
    day = time.strftime('%Y-%m-%d', time.gmtime())
    p, o, r = _label(platform), _label(turn_origin), _reason(reason)
    key = f'{p}|{o}|{r}'
    entry = {'platform': p, 'turn_origin': o, 'reason': r, 'count': 1}
    with _lock:
        data = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(errors='replace')) or {}
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        day_entry = data.get(day)
        if not isinstance(day_entry, dict) or not isinstance(day_entry.get('counters'), dict):
            day_entry = {'date': day, 'counters': {}}
            data[day] = day_entry
        counters = day_entry['counters']
        current = counters.get(key)
        if isinstance(current, dict):
            current['count'] = int(current.get('count', 0)) + 1
            entry = current
        else:
            counters[key] = entry
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            os.replace(tmp, target)
        except Exception:
            pass
    return entry


def read(path=None) -> dict:
    """Read the counters back (read-only; used by the tools and the tests)."""
    target = pathlib.Path(path) if path else config.skip_counter_path()
    try:
        data = json.loads(target.read_text(errors='replace'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def totals(data: dict | None = None) -> dict:
    """Flatten day buckets into ``{turn_origin: count}`` for reporting."""
    out: dict = {}
    for day_entry in (data if data is not None else read()).values():
        for counter in ((day_entry or {}).get('counters') or {}).values():
            origin = (counter or {}).get('turn_origin', '(missing)')
            out[origin] = out.get(origin, 0) + int((counter or {}).get('count', 0))
    return out
