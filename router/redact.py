"""Deterministic, local redaction.

No external service and no model is involved: a fixed set of regular expressions
is applied to the raw turn before anything leaves the host. Redaction runs first,
so the routing service never receives credential-like content in the first place.
"""
from __future__ import annotations

import re

PATTERNS = [
    ('private_key', re.compile(r'-----BEGIN[^-]*PRIVATE KEY-----.*?(?:-----END[^-]*PRIVATE KEY-----)?', re.S)),
    ('api_key', re.compile(r'\b(?:sk|tp|pk|rk)-[A-Za-z0-9_\-]{8,}\b')),
    ('api_key', re.compile(r'\bapikey_[A-Za-z0-9_]{16,}\b', re.I)),
    ('bearer', re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}')),
    ('kv_secret', re.compile(
        r'(?i)\b(api[_\-\\s]?key|access[_\-\\s]?token|refresh[_\-\\s]?token|auth[_\-\\s]?token|token|'
        r'password|passwd|pwd|secret|client[_\-\\s]?secret|credential)\b\s*[:=]\s*["\']?([^\s"\',;]{4,})')),
    ('email', re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')),
    ('ipv4', re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')),
    ('ipv6', re.compile(r'\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b')),
    ('ssh_target', re.compile(r'\b[a-z_][a-z0-9_\-]{0,31}@[A-Za-z0-9.\-_]+')),
    ('long_token', re.compile(r'\b[A-Za-z0-9+/=_\-]{40,}\b')),
]

REPLACEMENTS = {
    'private_key': '[REDACTED_PRIVATE_KEY]',
    'api_key': '[REDACTED_KEY]',
    'bearer': 'Bearer [REDACTED]',
    'kv_secret': None,   # special case: keep the key name, drop the value
    'email': '[REDACTED_EMAIL]',
    'ipv4': '[REDACTED_IP]',
    'ipv6': '[REDACTED_IP]',
    'ssh_target': '[REDACTED_CREDENTIAL]',
    'long_token': '[REDACTED_TOKEN]',
}


def redact(text: str) -> tuple:
    """Return ``(redacted_text, metadata)``.

    ``metadata`` carries ``hits`` (int), ``kinds`` (set) and ``unsafe`` (bool).
    Private key material is treated as *unsafe*: the caller must not send that
    turn to a third-party service at all and records a local privacy fallback.
    """
    if not isinstance(text, str):
        return '', {'hits': 0, 'kinds': set(), 'unsafe': False}
    out = text
    kinds = set()
    hits = 0
    for kind, rx in PATTERNS:
        if kind == 'kv_secret':
            # Keep the key name, drop the value. The match is counted once by the
            # shared ``hits += n`` below; the substitution callback must not count,
            # otherwise ``kv_secret`` matches would be counted twice.
            out, n = rx.subn(lambda m: f'{m.group(1)}=[REDACTED]', out)
        else:
            out, n = rx.subn(REPLACEMENTS[kind], out)
        if n:
            hits += n
            kinds.add(kind)
    unsafe = 'private_key' in kinds
    return out, {'hits': hits, 'kinds': kinds, 'unsafe': unsafe}


def truncate(text: str, limit: int = 1200) -> str:
    text = re.sub(r'\s+', ' ', text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit] + ' …[truncated]'
