#!/usr/bin/env python3
"""Read-only source-compatibility gate for the prospective outcome hooks.

    python3 tools/check_outcome_hook_contract.py [--hermes-root /opt/hermes] [--json PATH]

What this is, and what it is not
--------------------------------
It is a **source compatibility gate**: it reads the Hermes tree and asserts that the hooks this
candidate needs still exist and still carry the fields its logic depends on. It is *not* a runtime
validation: it proves nothing about a live process, it sends no request, it reads no credential,
it touches no deployment path and it never imports the Hermes runtime.

Failing closed is the point. A hook that disappeared, a fire site that stopped passing
``turn_origin``, or a manifest whose hook list no longer matches ``register()`` must stop a future
deployment decision *before* anything is copied or restarted::

    exit 0  PASS
    exit 3  FAIL  (a required hook or a load-bearing field is missing / manifest drift)

Required vs optional
--------------------
``REQUIRED`` fields are the ones the module's counting logic cannot be correct without. ``OPTIONAL``
fields are recorded when present and tolerated when absent — the gate reports them but never fails
on them, so a Hermes build that drops an observability extra does not block the candidate while a
build that drops ``retry_count`` does.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

MODE = 'SOURCE_COMPATIBILITY_GATE'
LIVE_REQUEST = 'NO'
CREDENTIALS_USED = 'NO'

# hook -> {required: [...], optional: [...], content_bearing: [...]}
HOOK_CONTRACT = {
    'pre_api_request': {
        'required': ['turn_id', 'api_request_id', 'api_call_count', 'retry_count', 'platform',
                     'turn_origin', 'model'],
        'optional': ['session_id', 'task_id', 'provider', 'api_mode', 'message_count', 'tool_count',
                     'approx_input_tokens', 'request_char_count', 'started_at', 'max_tokens',
                     'middleware_trace', 'user_message', 'conversation_history', 'request_messages',
                     'system_prompt', 'request'],
        'content_bearing': ['user_message', 'conversation_history', 'request_messages',
                            'system_prompt', 'request'],
        'why': 'fires once per physical attempt, and it is the only hook that carries turn_origin, '
               'so it both admits the turn and keys the attempt (api_request_id, retry_count)',
        'critical_fields': ['turn_origin', 'retry_count', 'api_request_id'],
    },
    'post_api_request': {
        'required': ['turn_id', 'api_request_id', 'api_call_count', 'usage', 'finish_reason',
                     'api_duration', 'model'],
        'optional': ['session_id', 'task_id', 'provider', 'api_mode', 'response_model',
                     'started_at', 'ended_at', 'first_chunk_at', 'message_count',
                     'assistant_content_chars', 'assistant_tool_call_count', 'moa_references',
                     'response', 'assistant_message'],
        'content_bearing': ['response', 'assistant_message'],
        'why': 'the only hook that carries normalized usage buckets, finish reason and request '
               'duration for a successful response',
        'critical_fields': ['usage', 'finish_reason', 'api_duration'],
    },
    'api_request_error': {
        'required': ['turn_id', 'api_request_id', 'api_call_count', 'retry_count', 'status_code',
                     'api_duration'],
        'optional': ['max_retries', 'retryable', 'reason', 'session_id', 'task_id', 'provider',
                     'api_mode', 'started_at', 'ended_at', 'error', 'request'],
        'content_bearing': ['error', 'request'],
        'why': 'keys the failed attempt so a retry that reuses the logical call id is never '
               'swallowed by it',
        'critical_fields': ['retry_count', 'status_code'],
    },
    'post_llm_call': {
        'required': ['turn_id', 'model', 'platform'],
        'optional': ['session_id', 'task_id', 'user_message', 'assistant_response',
                     'conversation_history'],
        'content_bearing': ['user_message', 'assistant_response', 'conversation_history'],
        'why': 'the normal logical-turn completion seam: the terminal record and its model '
               'attribution depend on it',
        'critical_fields': ['turn_id'],
    },
    'agent_loop_stopped': {
        'required': ['session_key'],
        'optional': ['platform', 'reason', 'invalidation_reason'],
        'content_bearing': [],
        'why': 'the only hook that observes an interrupted turn; it carries no turn id, so an '
               'interruption is attributable only when exactly one open accumulator matches',
        'critical_fields': ['session_key'],
    },
}

REQUIRED_HOOKS = tuple(HOOK_CONTRACT)

_FIRE_CALL_RX = re.compile(
    r'(?:invoke_hook|_invoke_hook|_invoke_hook_safely|_lifecycle\.invoke_hook|_invoke_api_request_error_hook)'
    r'\s*\(\s*[\'"](?P<hook>[a-z_]+)[\'"]')
_HOOK_LIST_RX = re.compile(r'^\s{0,4}[\'"](?P<hook>[a-z_]+)[\'"],?\s*$')
_REGISTER_RX = re.compile(r'register_hook\(\s*[\'"](?P<hook>[a-z_]+)[\'"]')


def _balanced_kwargs(text: str, open_idx: int) -> tuple[list, str]:
    """Return (top-level ``name=`` keywords, call text) for the call whose '(' is at open_idx."""
    depth, i, end = 0, open_idx, min(len(text), open_idx + 40000)
    while i < end:
        ch = text[i]
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    call = text[open_idx:i]
    names, depth2 = [], 0
    for m in re.finditer(r'[()\[\]{}]|\b([a-zA-Z_][a-zA-Z0-9_]*)\s*=(?!=)', call):
        if m.group(0) in '([{':
            depth2 += 1
        elif m.group(0) in ')]}':
            depth2 -= 1
        elif m.group(1) and depth2 == 1:
            names.append(m.group(1))
    return names, call


def _fire_sites(root: pathlib.Path, hook: str) -> tuple[list, list, list]:
    """Return (sites, files, kwargs) for one hook: every ``invoke_hook``-style fire site."""
    sites, files, kwargs = [], [], []
    for path in sorted(root.rglob('*.py')):
        rel = str(path.relative_to(root))
        if '/test' in rel or rel.startswith('test'):
            continue
        try:
            text = path.read_text(errors='replace')
        except Exception:
            continue
        if hook not in text:
            continue
        for m in _FIRE_CALL_RX.finditer(text):
            if m.group('hook') != hook:
                continue
            open_idx = text.index('(', m.start())
            names, _call = _balanced_kwargs(text, open_idx)
            line = text.count('\n', 0, m.start()) + 1
            sites.append({'file': rel, 'line': line})
            files.append(rel)
            kwargs.extend(names)
    return sites, sorted(set(files)), sorted(set(kwargs))


def _manifest_hooks(repo: pathlib.Path) -> list:
    """Parse the ``hooks:`` list out of plugin/plugin.yaml without a YAML dependency."""
    path = repo / 'plugin' / 'plugin.yaml'
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return []
    hooks, inside = [], False
    for line in lines:
        if re.match(r'^hooks:\s*$', line):
            inside = True
            continue
        if inside:
            if re.match(r'^\s*[a-z_]+:\s', line):      # next top-level key ends the list
                break
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if stripped.startswith('- '):
                hooks.append(stripped[2:].strip().strip('"\''))
    return hooks


def _registered_hooks(repo: pathlib.Path) -> list:
    try:
        text = (repo / 'plugin' / '__init__.py').read_text(errors='replace')
    except Exception:
        return []
    return sorted(set(_REGISTER_RX.findall(text)))


def _version_of_record(repo: pathlib.Path) -> dict:
    out = {}
    try:
        for line in (repo / 'plugin' / 'plugin.yaml').read_text().splitlines():
            m = re.match(r'^version:\s*["\']?([0-9][^"\'\s]*)', line)
            if m:
                out['plugin_manifest'] = m.group(1)
                break
    except Exception:
        pass
    try:
        text = (repo / 'router' / '__init__.py').read_text(errors='replace')
        m = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)', text)
        if m:
            out['router_module'] = m.group(1)
    except Exception:
        pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--hermes-root', default='/opt/hermes',
                    help='read-only Hermes source tree to check against')
    ap.add_argument('--repo-root', default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument('--json', default=None, help='write the evidence JSON here (never inside a deployment)')
    args = ap.parse_args(argv)

    hermes = pathlib.Path(args.hermes_root)
    repo = pathlib.Path(args.repo_root)
    if not hermes.is_dir():
        print(f'Hermes source root not found: {hermes}', file=sys.stderr)
        return 2

    hooks, missing_hooks, missing_required, hooks_needing_optional = {}, [], [], []
    for hook in REQUIRED_HOOKS:
        spec = HOOK_CONTRACT[hook]
        sites, files, found = _fire_sites(hermes, hook)
        required_missing = [f for f in spec['required'] if f not in found]
        optional_missing = [f for f in spec['optional'] if f not in found]
        hooks[hook] = {
            'FIRE_SITE_COUNT': len(sites),
            'FILES': files,
            'REQUIRED_FIELDS': spec['required'],
            'REQUIRED_PRESENT': sorted(set(spec['required']) & set(found)),
            'REQUIRED_MISSING': required_missing,
            'OPTIONAL_MISSING': optional_missing,
            'CONTENT_BEARING_KEYS_TO_IGNORE': spec['content_bearing'],
            'WHY_IT_IS_LOAD_BEARING': spec['why'],
            'RESULT': 'FAIL' if (not sites or required_missing) else 'PASS',
        }
        if not sites:
            missing_hooks.append(hook)
        if required_missing:
            missing_required.append({'hook': hook, 'fields': required_missing,
                                     'critical': [f for f in spec['critical_fields']
                                                  if f in required_missing]})
        if optional_missing:
            hooks_needing_optional.append({'hook': hook, 'fields': optional_missing})

    manifest = _manifest_hooks(repo)
    registered = _registered_hooks(repo)
    versions = _version_of_record(repo)
    version_consistent = (len(set(versions.values())) == 1) if len(versions) == 2 else False

    # Order-insensitive: the manifest list and the register() calls need not agree on order, only
    # on membership — a hook declared but never registered (or the reverse) is the drift that
    # matters.
    manifest_matches = (sorted(set(manifest)) == sorted(set(registered)))
    unregistered_required = [h for h in REQUIRED_HOOKS if h not in registered]
    undeclared_required = [h for h in REQUIRED_HOOKS if h not in manifest]

    ok = (not missing_hooks and not missing_required and manifest_matches
          and not unregistered_required and not undeclared_required and version_consistent)

    report = {
        'CHECKER': 'check_outcome_hook_contract.py',
        'MODE': MODE,
        'NOT_RUNTIME_VALIDATION': 'YES — this proves source compatibility only, never a live process',
        'LIVE_REQUEST': LIVE_REQUEST,
        'CREDENTIALS_USED': CREDENTIALS_USED,
        'DEPLOYMENT_PATHS_TOUCHED': 'NO',
        'HERMES_ROOT': str(hermes),
        'REQUIRED_HOOKS': list(REQUIRED_HOOKS),
        'HOOKS': hooks,
        'MISSING_HOOKS': missing_hooks,
        'MISSING_LOAD_BEARING_FIELDS': missing_required,
        'OPTIONAL_FIELDS_ABSENT_IN_THIS_BUILD': hooks_needing_optional,
        'REQUIRED_HOOKS_PRESENT': 'NO' if missing_hooks else 'YES',
        'LOAD_BEARING_FIELDS_PRESENT': 'NO' if missing_required else 'YES',
        'MANIFEST_HOOKS': manifest,
        'REGISTERED_HOOKS': registered,
        'MANIFEST_HOOKS_MATCH_REGISTERED': 'YES' if manifest_matches else 'NO',
        'REQUIRED_HOOKS_UNREGISTERED': unregistered_required,
        'REQUIRED_HOOKS_UNDECLARED_IN_MANIFEST': undeclared_required,
        'VERSIONS_OF_RECORD': versions,
        'VERSION_CONSISTENCY': 'PASS' if version_consistent else 'FAIL',
        'RESULT': 'PASS' if ok else 'FAIL',
        'NEXT_ON_FAILURE': ('do not authorise a deployment: re-pin the payload contract from the new '
                            'Hermes source, adapt router/outcome.py and re-run this gate'),
    }
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        dest = pathlib.Path(args.json)
        if dest.exists():
            print(f'refusing to overwrite {dest}', file=sys.stderr)
            return 4
        dest.write_text(payload + '\n')
        print(f'contract report written to {dest}')
    else:
        print(payload)
    return 0 if ok else 3


if __name__ == '__main__':
    raise SystemExit(main())
