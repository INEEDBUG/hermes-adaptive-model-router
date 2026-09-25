"""TypeSafe JEV client, written strictly against the official OpenAPI schema.

Contract notes (verified against ``GET /openapi.json``):
  * ``POST /v1/systemone`` takes ``{state, model, questions}`` and returns
    ``{model, answers, usage}``;
  * a ``ChoiceQuestion`` uses the literal type ``"choice"`` and is answered by a
    ``ChoiceAnswer`` carrying ``choice``, ``confidence`` and ``probabilities``;
  * the response ``model`` field is the *concrete* model revision behind the
    requested alias (e.g. requesting ``jev-latest`` resolves to a pinned
    version), which is recorded in telemetry so a decision can be reproduced.

:func:`route` never raises. Every failure mode is classified into a string that
the caller can log and reason about, because a routing failure must never break
the agent turn that triggered it.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.request

from . import config

BASE_URL = os.environ.get('TYPESAFE_BASE_URL', 'https://api.typesafe.ai')
QUESTION_NAME = 'route'

CRITERIA = {
    'deepseek_flash': (
        'Choose for clear or well-specified tasks, routine tool execution, '
        'shell/status checks, known procedures, ordinary troubleshooting, '
        'straightforward agent workflows, and work where low latency is valuable.'
    ),
    'mimo_pro': (
        'Choose for ambiguous or complex tasks, deep reasoning, programming, '
        'difficult debugging, architecture, multi-file work, long-horizon planning, '
        'novel problems, or tasks where quality matters more than latency.'
    ),
}
INSTRUCTIONS = ('Select the single best backend model to execute this agent turn. '
                'Answer with exactly one choice from the criteria.')


def api_key() -> str:
    """Read the credential from the environment, falling back to the .env file."""
    k = (os.environ.get('TYPESAFE_API_KEY') or '').strip()
    if k:
        return k
    p = config.ENV_PATH
    if p.exists():
        for line in p.read_text(errors='replace').splitlines():
            if line.startswith('TYPESAFE_API_KEY='):
                v = line.split('=', 1)[1].strip()
                if v:
                    return v
    raise RuntimeError('TYPESAFE_API_KEY is missing')


def route(state, timeout: float = 3.0, model: str = None) -> dict:
    """Ask JEV for a route decision. Never raises; ``ok=False`` carries ``error``."""
    out = {'ok': False, 'model': None, 'choice': None, 'confidence': None,
           'probabilities': {}, 'latency_ms': None, 'input_tokens': None,
           'output_tokens': None, 'error': None}
    body = {'state': state, 'model': model or config.jev_model(),
            'questions': {QUESTION_NAME: {'type': 'choice', 'instructions': INSTRUCTIONS,
                                          'criteria': CRITERIA}}}
    t0 = time.time()
    try:
        req = urllib.request.Request(BASE_URL + '/v1/systemone',
                                     data=json.dumps(body).encode(), method='POST')
        req.add_header('Authorization', 'Bearer ' + api_key())
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'application/json')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode('utf-8', 'replace'))
        out['latency_ms'] = int((time.time() - t0) * 1000)
        if not isinstance(payload, dict):
            out['error'] = 'malformed_response'
            return out
        out['model'] = payload.get('model')
        usage = payload.get('usage') or {}
        out['input_tokens'] = usage.get('input_tokens')
        out['output_tokens'] = usage.get('output_tokens')
        ans = (payload.get('answers') or {}).get(QUESTION_NAME) or {}
        if ans.get('type') != 'choice':
            out['error'] = 'malformed_answer_type'
            return out
        choice = ans.get('choice')
        probs = ans.get('probabilities') or {}
        conf = ans.get('confidence')
        if choice not in CRITERIA:
            out['error'] = 'malformed_unknown_choice'
            return out
        if not isinstance(probs, dict) or not probs:
            out['error'] = 'malformed_missing_probabilities'
            return out
        if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0.0 <= float(conf) <= 1.0:
            out['error'] = 'malformed_confidence'
            return out
        out.update(ok=True, choice=choice, confidence=float(conf),
                   probabilities={k: float(v) for k, v in probs.items()})
        return out
    except urllib.error.HTTPError as e:
        out['latency_ms'] = int((time.time() - t0) * 1000)
        out['error'] = f'http_{e.code}'
        return out
    except urllib.error.URLError as e:
        out['latency_ms'] = int((time.time() - t0) * 1000)
        out['error'] = 'timeout' if 'timed out' in str(e).lower() else f'urlerror_{type(e.reason).__name__}'
        return out
    except Exception as e:
        out['latency_ms'] = int((time.time() - t0) * 1000)
        out['error'] = type(e).__name__
        return out


def models() -> list:
    """List the aliases advertised by the routing service (used for probing)."""
    req = urllib.request.Request(BASE_URL + '/v1/models')
    req.add_header('Authorization', 'Bearer ' + api_key())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode()).get('models', [])
