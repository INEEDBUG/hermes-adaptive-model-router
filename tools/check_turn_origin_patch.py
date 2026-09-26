#!/usr/bin/env python3
"""Check the Hermes turn_origin integration this repository depends on.

    python3 tools/check_turn_origin_patch.py --hermes-root /path/to/hermes

Checks, in order:

1. the installed ``hermes_agent`` version still matches the version this integration was
   built and tested against;
2. every hunk of ``patches/hermes-v0.21.5-turn-origin.patch`` is present in the tree (each
   one is identified by a marker string it introduces, not by a line number);
3. the plugin still requires ``platform in JEV_ALLOWED_PLATFORMS`` **and**
   ``turn_origin == 'user'``;
4. behaviour, offline: a payload with no ``turn_origin``, and a payload whose origin is not
   ``user``, must both produce **zero** calls to the routing service, while an allow-listed
   human turn still produces exactly one.

Exit codes: ``0`` integration intact · ``3`` drift detected (stale patch, patched core gone,
or the plugin no longer fails closed).

On drift the router must stay fail-closed/off: the plugin already rejects any turn without a
``turn_origin`` label, so a Hermes upgrade that drops this integration cannot silently restore
unconditional routing — keep the plugin disabled (or the mode at ``off``) until the patch is
re-applied, and never "fix" it by defaulting a missing origin to ``user``.

Read-only: nothing here writes to the Hermes tree; the behavioural check uses a temporary
directory and stubs, so no routing service is contacted and no gateway is touched.
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET_VERSION = '0.21.5'
PATCHES = (ROOT / 'patches' / 'hermes-v0.21.5-turn-origin.patch',)

# (relative path, marker introduced by the patch)
CORE_MARKERS = (
    ('agent/turn_origin.py', 'def current(agent)'),
    ('agent/turn_origin.py', 'ORIGINS = ('),
    ('agent/conversation_loop.py', 'turn_origin'),
    ('agent/turn_api_request.py', 'turn_origin=_turn_origin.current(agent)'),
    ('gateway/run_turn_runner.py', '_turn_origin_hint'),
    ('hermes_cli/oneshot.py', '_turn_origin.stamp('),
)
PLUGIN_MARKERS = (
    ("kwargs.get('turn_origin')", 'reads the provenance label from the payload'),
    ("origin != config._HUMAN_ORIGIN", "requires turn_origin == 'user'"),
    ('allowed_platforms()', 'applies the platform allowlist'),
    ('skip_telemetry.bump', 'counts rejections content-free'),
)

problems: list[str] = []
notes: list[str] = []


def check_version(hermes_root: pathlib.Path) -> None:
    version = None
    try:
        version = md.version('hermes_agent')
    except Exception:
        for candidate in (hermes_root / 'hermes_constants.py', hermes_root / 'pyproject.toml'):
            if candidate.exists():
                text = candidate.read_text(errors='replace')
                for token in text.split('"')[1:]:
                    if token[:1].isdigit() and token.count('.') == 2:
                        version = token
                        break
            if version:
                break
    if not version:
        notes.append(f'hermes_agent version not detectable from {hermes_root} '
                     f'(expected target {TARGET_VERSION}); markers were still checked')
        return
    if version != TARGET_VERSION:
        problems.append(f'hermes_agent {version} != the version this integration targets '
                        f'({TARGET_VERSION}): re-verify the patch against the new upstream before '
                        f'raw use')
    else:
        notes.append(f'hermes_agent version {version} matches the target')


def check_core(hermes_root: pathlib.Path) -> None:
    for rel, marker in CORE_MARKERS:
        path = hermes_root / rel
        if not path.exists():
            problems.append(f'missing core file {rel}')
            continue
        if marker not in path.read_text(errors='replace'):
            problems.append(f'{rel}: marker {marker!r} absent (patch not applied, or drifted)')


def check_plugin() -> None:
    plugin = ROOT / 'plugin' / '__init__.py'
    if not plugin.exists():
        problems.append('plugin/__init__.py missing from this repository')
        return
    text = plugin.read_text(errors='replace')
    for marker, description in PLUGIN_MARKERS:
        if marker not in text:
            problems.append(f'plugin no longer {description} (marker {marker!r} absent)')


def check_behaviour() -> None:
    """Offline proof of the fail-closed contract, with the routing service replaced by a stub."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='turn-origin-check-'))
    state_dir = tmp / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'mode.json').write_text(json.dumps({'mode': 'shadow'}))
    os.environ.update({'JEV_LOG_DIR': str(tmp / 'logs'), 'JEV_STATE_DIR': str(state_dir),
                       'HERMES_HOME': str(tmp / 'home'), 'HERMES_ENV_PATH': str(tmp / 'home' / '.env'),
                       'ROUTER_MODE': 'shadow', 'JEV_ALLOWED_PLATFORMS': 'feishu'})
    os.environ.pop('JEV_AUTO_APPROVED', None)
    sys.path.insert(0, str(ROOT))

    from router import config
    config._cache['at'] = 0
    config._cache['env'] = {}

    calls = {'dossier': 0, 'jev': 0}
    import router.dossier as dossier_mod
    import router.shadow as shadow_mod

    dossier_mod.build = lambda message: (calls.__setitem__('dossier', calls['dossier'] + 1) or
                                        ({'task': {}}, {'unsafe': False, 'hits': 0}))
    shadow_mod.submit = lambda *a, **k: (calls.__setitem__('jev', calls['jev'] + 1) or True)

    spec = importlib.util.spec_from_file_location('router_plugin_check', ROOT / 'plugin' / '__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    base = {'turn_id': 'check', 'api_call_count': 1, 'retry_count': 0,
            'user_message': 'synthetic check message', 'model': 'synthetic-model'}

    def probe(**overrides):
        calls['dossier'] = calls['jev'] = 0
        plugin._on_pre_api_request(**{**base, **overrides, 'turn_id': f"check-{overrides.get('turn_origin', 'none')}-{calls['jev']}"})
        return calls['dossier'], calls['jev']

    cases = [
        ({'platform': 'feishu'}, (0, 0), 'no turn_origin at all (integration absent)'),
        ({'platform': 'feishu', 'turn_origin': ''}, (0, 0), 'empty turn_origin'),
        ({'platform': 'feishu', 'turn_origin': 'unknown'}, (0, 0), 'unknown turn_origin'),
        ({'platform': 'feishu', 'turn_origin': 'background_review'}, (0, 0), 'background review fork'),
        ({'platform': 'feishu', 'turn_origin': 'internal_notification'}, (0, 0), 'internal notification'),
        ({'platform': 'subagent', 'turn_origin': 'subagent'}, (0, 0), 'subagent'),
        ({'platform': 'feishu', 'turn_origin': 'user'}, (1, 1), 'allow-listed human turn'),
    ]
    for overrides, expected, label in cases:
        got = probe(**overrides)
        if got != expected:
            problems.append(f'behaviour: {label} produced dossier/JEV {got}, expected {expected}')
    notes.append('behaviour: fail-closed for missing/unknown/non-user origins, one observation '
                 'for an allow-listed human turn')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--hermes-root', default=os.environ.get('HERMES_ROOT') or str(pathlib.Path('~/hermes-agent').expanduser()),
                    help='path to the Hermes Agent checkout (default: $HERMES_ROOT or ~/hermes-agent)')
    args = ap.parse_args()
    hermes_root = pathlib.Path(args.hermes_root).expanduser()

    print('turn_origin integration check')
    print(f'  hermes root : {hermes_root}')
    print(f'  patch files : {", ".join(p.name for p in PATCHES if p.exists()) or "(none found)"}')
    if not hermes_root.exists():
        print('\n  RESULT: INCOMPLETE — the Hermes checkout was not found; pass --hermes-root, '
              'or set HERMES_ROOT.')
        print('  The router stays fail-closed while the integration cannot be verified.')
        return 3
    for patch in PATCHES:
        if not patch.exists():
            problems.append(f'missing patch file {patch.relative_to(ROOT)}')

    check_version(hermes_root)
    check_core(hermes_root)
    check_plugin()
    check_behaviour()

    for note in notes:
        print(f'  note        : {note}')
    if problems:
        print('\n  RESULT: DRIFT DETECTED')
        for problem in problems:
            print(f'    - {problem}')
        print('\n  Required action: keep the router fail-closed/off until this is resolved —\n'
              '  uninstall/disable the plugin (hermes config: plugins.enabled) or set the mode to\n'
              '  "off", re-apply patches/hermes-v0.21.5-turn-origin.patch, restart the gateway, and\n'
              '  run this check again. A missing turn_origin must never be treated as "user".')
        return 3
    print('\n  RESULT: INTEGRATION INTACT (router may observe human turns only)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
