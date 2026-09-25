#!/usr/bin/env python3
"""Safely initialize the runtime state file (``mode.json``).

Why this tool exists
--------------------
The **runtime authority is the state file**, not the ``ROUTER_MODE`` variable:

* ``router.state.resolve()`` reads ``$JEV_STATE_DIR/mode.json`` once per turn;
* a missing, unreadable or corrupt state file resolves to ``off`` (fail-safe);
* setting ``ROUTER_MODE=shadow`` alone therefore does **not** make the plugin
  collect anything — the state file must exist first.

This tool creates that file explicitly and atomically. It refuses to write
``auto`` (not implemented in this release) and refuses to touch anything while a
``KILL`` sentinel is present, so an operator cannot accidentally clear a stop.

Usage::

    python3 tools/init_state.py                  # initialise mode=shadow
    python3 tools/init_state.py --mode off       # initialise disabled
    python3 tools/init_state.py --dry-run        # show what would happen
    HERMES_HOME=/opt/data python3 tools/init_state.py
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from router import state  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description='Initialise the router runtime state file.')
    ap.add_argument('--mode', choices=('shadow', 'off'), default='shadow',
                    help='mode to record (default: shadow; auto is never written)')
    ap.add_argument('--state-dir', default=None,
                    help='override the state directory (default: router.state.state_dir())')
    ap.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    ap.add_argument('--force', action='store_true',
                    help='proceed even if a KILL sentinel is present (do NOT use to resume '
                         'incident handling)')
    args = ap.parse_args()

    if args.state_dir:
        import os
        os.environ['JEV_STATE_DIR'] = args.state_dir

    d = state.state_dir()
    kill = d / state.KILL_NAME
    mode_file = d / state.MODE_NAME

    print(f'state directory : {d}')
    print(f'state file      : {mode_file}')
    print(f'kill sentinel   : {kill} ({"PRESENT" if kill.exists() else "absent"})')

    if kill.exists() and not args.force:
        print()
        print('REFUSING TO WRITE: a KILL sentinel is present, which resolves to mode=off.')
        print('Remove the sentinel deliberately first, then re-run. Nothing was changed.')
        return 2

    if args.dry_run:
        print()
        print(f'DRY RUN: would write mode={args.mode!r}. Nothing was changed.')
        return 0

    path = state.write_state(args.mode, by='tools/init_state.py')
    res = state.resolve()
    print()
    print(f'wrote           : {path}')
    print(f'resolved mode   : {res["mode"]} (source={res["source"]}, reason={res["reason"]})')
    if res['mode'] != args.mode:
        print('WARNING: the resolved mode differs from the requested mode — inspect the state file.')
        return 1
    print()
    print('Next: the plugin will collect decisions on the next turn. Verify with:')
    print('  python3 tools/shadow_stats.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
