#!/usr/bin/env bash
# Persistent installer for the JEV Shadow Router plugin.
#
# Why an installer
# ----------------
# Copying the plugin and exporting a variable in an interactive shell is not
# persistent: a gateway restart comes from a different environment and the import
# of the `router` package would fail (the plugin then silently disables itself).
# This script makes the installation durable and refuses to destroy existing state:
#
#   1. copies plugin/ into  $HERMES_HOME/plugins/jev-shadow-router
#   2. persists JEV_ROUTER_ROOT in $HERMES_HOME/.env  (append-only; never overwrites)
#   3. MERGES 'jev-shadow-router' into plugins.enabled  (never replaces the list)
#   4. initialises the runtime state file (mode.json) — mode.json is authoritative,
#      and a missing file means the plugin stays off
#
# Usage:
#   ./tools/install_plugin.sh [--hermes-home /opt/data] [--dry-run]
#                             [--no-state] [--state-mode shadow|off]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-/opt/data}"
PLUGIN_NAME="jev-shadow-router"
DRY_RUN=0
INIT_STATE=1
STATE_MODE="shadow"

while [ $# -gt 0 ]; do
  case "$1" in
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=1; shift ;;
    --no-state)    INIT_STATE=0; shift ;;
    --state-mode)  STATE_MODE="$2"; shift 2 ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if [ "$DRY_RUN" = "1" ]; then say "[dry-run] $*"; else eval "$@"; fi; }

step "Pre-flight"
say "repository        : $REPO_ROOT"
say "HERMES_HOME       : $HERMES_HOME"
say "target plugin dir : $HERMES_HOME/plugins/$PLUGIN_NAME"
[ -f "$REPO_ROOT/plugin/plugin.yaml" ] || { echo "plugin/plugin.yaml not found — run from the repository" >&2; exit 66; }
if [ ! -d "$HERMES_HOME" ]; then
  echo "HERMES_HOME ($HERMES_HOME) does not exist. Pass --hermes-home <path> for a different layout." >&2
  exit 66
fi

step "1/4 Copy the plugin"
run "mkdir -p '$HERMES_HOME/plugins/$PLUGIN_NAME'"
run "cp -f '$REPO_ROOT/plugin/plugin.yaml' '$REPO_ROOT/plugin/__init__.py' '$HERMES_HOME/plugins/$PLUGIN_NAME/'"
say "copied plugin.yaml + __init__.py"

step "2/4 Persist JEV_ROUTER_ROOT in $HERMES_HOME/.env"
ENV_FILE="$HERMES_HOME/.env"
if [ -f "$ENV_FILE" ] && grep -qE '^[[:space:]]*JEV_ROUTER_ROOT=' "$ENV_FILE"; then
  CURRENT="$(grep -E '^[[:space:]]*JEV_ROUTER_ROOT=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
  say "already present: JEV_ROUTER_ROOT=$CURRENT (left untouched)"
  if [ "$CURRENT" != "$REPO_ROOT" ]; then
    say "NOTE: it points somewhere else. Remove that line and re-run to point at $REPO_ROOT."
  fi
else
  run "cp -n '$ENV_FILE' '$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)' 2>/dev/null || true"
  if [ "$DRY_RUN" = "1" ]; then
    say "[dry-run] would append: JEV_ROUTER_ROOT=$REPO_ROOT"
  else
    {
      printf '\n# added by hermes-adaptive-model-router installer (persistent plugin import path)\n'
      printf 'JEV_ROUTER_ROOT=%s\n' "$REPO_ROOT"
    } >> "$ENV_FILE"
    say "appended JEV_ROUTER_ROOT=$REPO_ROOT"
  fi
fi
say "Hermes loads $ENV_FILE with override=True, so the gateway process receives this on restart."

step "3/4 Enable the plugin (merge, never replace)"
CONFIG_FILE="$HERMES_HOME/config.yaml"
if command -v hermes >/dev/null 2>&1; then
  CURRENT_JSON="$(hermes config get plugins.enabled 2>/dev/null || true)"
  if [ "$DRY_RUN" = "1" ]; then
    say "[dry-run] current plugins.enabled: ${CURRENT_JSON:-<unset>}"
    say "[dry-run] would merge '$PLUGIN_NAME' into the existing list and set it back"
  else
    MERGED="$(PLUGIN_NAME="$PLUGIN_NAME" CURRENT_JSON="$CURRENT_JSON" python3 - <<'PY'
import json, os, re, sys
name = os.environ['PLUGIN_NAME']
raw = (os.environ.get('CURRENT_JSON') or '').strip()
items = []
if raw:
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, list):
            items = [str(x) for x in loaded]
    except Exception:
        # `hermes config get` may print a YAML block list ("- name" per line)
        items = [m.group(1).strip().strip('"\'') for m in re.finditer(r'^\s*-\s*(\S+)', raw, re.M)]
items = [i for i in dict.fromkeys(items) if i and i != 'None']
if name not in items:
    items.append(name)
print(json.dumps(items))
PY
)"
    hermes config set plugins.enabled "$MERGED" >/dev/null
    say "plugins.enabled = $MERGED (existing entries preserved)"
  fi
else
  say "hermes CLI not found on PATH — enable it manually, preserving the existing list:"
  say "  in $CONFIG_FILE:  plugins:"
  say "    enabled:"
  say "      - <your existing plugins...>"
  say "      - $PLUGIN_NAME"
fi

step "4/4 Initialise the runtime state (authoritative mode.json)"
if [ "$INIT_STATE" = "0" ]; then
  say "skipped (--no-state). Without mode.json the plugin resolves to 'off'."
else
  if [ "$DRY_RUN" = "1" ]; then
    say "[dry-run] would run: python3 tools/init_state.py --mode $STATE_MODE"
  else
    python3 "$REPO_ROOT/tools/init_state.py" --mode "$STATE_MODE"
  fi
fi

step "Verify"
say "python3 $REPO_ROOT/tests/test_state.py      # kill-switch resolver, offline"
say "python3 $REPO_ROOT/tests/test_router.py     # router suite, offline"
say "python3 $REPO_ROOT/tools/shadow_stats.py    # real turns vs tests, after traffic"
say "Restart the gateway so the plugin is loaded and the environment is re-read."
echo
echo "Done. Nothing existing was overwritten; .env and config.yaml edits are additive."
