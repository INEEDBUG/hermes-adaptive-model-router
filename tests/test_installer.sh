#!/usr/bin/env bash
# Offline tests for tools/install_plugin.sh.
#
# They run against a throwaway HERMES_HOME and a stand-in `hermes` CLI on PATH, so no
# real installation, gateway or configuration is touched.
#
# Run from the repository root:  bash tests/test_installer.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/tools/install_plugin.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/jev-installer-test-XXXXXX")"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

PASS=0
FAIL=0
chk() { # name, condition-as-exit-status
  if [ "$2" = "0" ]; then printf '  [PASS] %s\n' "$1"; PASS=$((PASS + 1))
  else printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL + 1)); fi
}

# --- stand-in hermes CLI ---------------------------------------------------------
BIN="$TMP/bin"; mkdir -p "$BIN"
CALLS="$TMP/hermes-calls.log"
cat > "$BIN/hermes" <<'CLI'
#!/usr/bin/env bash
echo "$*" >> "$CALLS_LOG"
case "$1 $2" in
  "config get")
    printf -- '- shared-memory\n- tavily-search\n'
    ;;
  "config set")
    printf '%s\n' "$4" > "$SET_LOG"
    ;;
  *) exit 0 ;;
esac
CLI
chmod +x "$BIN/hermes"
export CALLS_LOG="$CALLS" SET_LOG="$TMP/plugins_enabled.json"

new_home() { # $1 = name
  local h="$TMP/$1"
  mkdir -p "$h"
  printf 'SOME_EXISTING_KEY=1\n' > "$h/.env"
  printf 'plugins:\n  enabled:\n    - shared-memory\n' > "$h/config.yaml"
  printf '%s' "$h"
}

echo "=== A. real run: backups, merge, persistence ==="
HOME_A="$(new_home a)"
CONFIG_BEFORE="$(cat "$HOME_A/config.yaml")"
ENV_BEFORE="$(cat "$HOME_A/.env")"
PATH="$BIN:$PATH" HERMES_HOME="$HOME_A" JEV_STATE_DIR="$TMP/state-a" \
  bash "$INSTALLER" --hermes-home "$HOME_A" > "$TMP/out-a.txt" 2>&1
RC_A=$?
chk "A1 installer exits 0" "$([ "$RC_A" = 0 ] && echo 0 || echo 1)"

# The backup must exist and be byte-identical to the pre-run file.
CONFIG_BAK="$(ls "$HOME_A"/config.yaml.bak.* 2>/dev/null | head -1)"
chk "A2 config.yaml is backed up before it is modified" "$([ -n "$CONFIG_BAK" ] && echo 0 || echo 1)"
[ -n "$CONFIG_BAK" ] || CONFIG_BAK=/nonexistent
chk "A3 the backup matches the pre-run content" \
  "$(diff <(printf '%s\n' "$CONFIG_BEFORE") "$CONFIG_BAK" >/dev/null && echo 0 || echo 1)"
ENV_BAK="$(ls "$HOME_A"/.env.bak.* 2>/dev/null | head -1)"
chk "A4 .env is backed up too" "$([ -n "$ENV_BAK" ] && echo 0 || echo 1)"

# plugins.enabled must be the merge, never a replacement.
MERGED="$(cat "$TMP/plugins_enabled.json" 2>/dev/null || echo '')"
chk "A5 plugins.enabled includes the existing entries" \
  "$(printf '%s' "$MERGED" | grep -q 'shared-memory' && printf '%s' "$MERGED" | grep -q 'tavily-search' && echo 0 || echo 1)"
chk "A6 plugins.enabled includes the new plugin" \
  "$(printf '%s' "$MERGED" | grep -q 'jev-shadow-router' && echo 0 || echo 1)"
chk "A7 the CLI was called with the installer's HERMES_HOME" \
  "$(grep -c 'config set plugins.enabled' "$CALLS" | grep -qx 1 && echo 0 || echo 1)"

chk "A8 JEV_ROUTER_ROOT was persisted in .env" \
  "$(grep -q "^JEV_ROUTER_ROOT=$ROOT$" "$HOME_A/.env" && echo 0 || echo 1)"
chk "A9 the existing .env content is untouched" \
  "$(grep -q '^SOME_EXISTING_KEY=1$' "$HOME_A/.env" && echo 0 || echo 1)"
chk "A10 the plugin files were copied" \
  "$([ -f "$HOME_A/plugins/jev-shadow-router/plugin.yaml" ] && [ -f "$HOME_A/plugins/jev-shadow-router/__init__.py" ] && echo 0 || echo 1)"
chk "A11 the state file was initialised" \
  "$([ -f "$TMP/state-a/mode.json" ] && grep -q '"shadow"' "$TMP/state-a/mode.json" && echo 0 || echo 1)"

echo "=== B. second run is idempotent ==="
PATH="$BIN:$PATH" HERMES_HOME="$HOME_A" JEV_STATE_DIR="$TMP/state-a" \
  bash "$INSTALLER" --hermes-home "$HOME_A" > "$TMP/out-b.txt" 2>&1
chk "B1 JEV_ROUTER_ROOT appears exactly once" \
  "$([ "$(grep -c '^JEV_ROUTER_ROOT=' "$HOME_A/.env")" = 1 ] && echo 0 || echo 1)"
chk "B2 the merge does not duplicate the plugin" \
  "$([ "$(cat "$TMP/plugins_enabled.json" | tr ',' '\n' | grep -c 'jev-shadow-router')" = 1 ] && echo 0 || echo 1)"

echo "=== C. dry run changes nothing ==="
HOME_C="$(new_home c)"
BEFORE_C="$(cat "$HOME_C/.env")"; CFG_C="$(cat "$HOME_C/config.yaml")"
rm -f "$CALLS" "$SET_LOG"
PATH="$BIN:$PATH" HERMES_HOME="$HOME_C" JEV_STATE_DIR="$TMP/state-c" \
  bash "$INSTALLER" --hermes-home "$HOME_C" --dry-run > "$TMP/out-c.txt" 2>&1
RC_C=$?
chk "C1 dry run exits 0" "$([ "$RC_C" = 0 ] && echo 0 || echo 1)"
chk "C2 no backup file is created" \
  "$([ -z "$(ls "$HOME_C"/*.bak.* 2>/dev/null)" ] && echo 0 || echo 1)"
chk "C3 .env is unchanged" \
  "$([ "$BEFORE_C" = "$(cat "$HOME_C/.env")" ] && echo 0 || echo 1)"
chk "C4 config.yaml is unchanged" \
  "$([ "$CFG_C" = "$(cat "$HOME_C/config.yaml")" ] && echo 0 || echo 1)"
chk "C5 the CLI is never asked to set anything" \
  "$([ ! -s "$SET_LOG" ] && echo 0 || echo 1)"
chk "C6 the dry run says what it would back up" \
  "$(grep -q 'would back up' "$TMP/out-c.txt" && echo 0 || echo 1)"

echo "=== D. no config.yaml yet (nothing to back up) ==="
HOME_D="$TMP/d"; mkdir -p "$HOME_D"; printf 'SOME_EXISTING_KEY=1\n' > "$HOME_D/.env"
PATH="$BIN:$PATH" HERMES_HOME="$HOME_D" JEV_STATE_DIR="$TMP/state-d" \
  bash "$INSTALLER" --hermes-home "$HOME_D" > "$TMP/out-d.txt" 2>&1
RC_D=$?
chk "D1 still exits 0" "$([ "$RC_D" = 0 ] && echo 0 || echo 1)"
chk "D2 it says there was nothing to back up" \
  "$(grep -q 'no .*config.yaml yet' "$TMP/out-d.txt" && echo 0 || echo 1)"

echo
echo "total $((PASS + FAIL)) checks, failed $FAIL"
[ "$FAIL" = 0 ] || exit 1
exit 0
