#!/usr/bin/env bash
# Offline tests for tools/install_plugin.sh.
#
# They run against throwaway HERMES_HOME directories and a stand-in `hermes` CLI on PATH,
# so no real installation, gateway or configuration is touched. The stand-in records the
# HERMES_HOME it actually received and writes into that home, so a call that forgets to
# name the home is visible as a changed decoy rather than passing silently on arguments.
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
CALLS="$TMP/hermes-calls.log"          # "<hermes-home>|<args>" per invocation
SET_LOG="$TMP/plugins_enabled.json"    # value passed to `config set`
SET_HOME="$TMP/set-home.log"           # which home that write targeted
export CALLS_LOG="$CALLS" SET_LOG="$SET_LOG" SET_HOME_LOG="$SET_HOME"
cat > "$BIN/hermes" <<'CLI'
#!/usr/bin/env bash
# A real `hermes` writes into the Hermes Home it was given (or its process default when
# none was given). Model both, and log the home that was actually used.
CLI_HOME="${HERMES_HOME:-${FAKE_DEFAULT_HOME:-$PWD}}"
printf '%s|%s\n' "$CLI_HOME" "$*" >> "$CALLS_LOG"
case "$1 $2" in
  "config get")
    printf -- '- shared-memory\n- tavily-search\n'
    ;;
  "config set")
    printf '%s\n' "$4" > "$SET_LOG"
    printf '%s\n' "$CLI_HOME" > "$SET_HOME_LOG"
    printf '\n# written by the stand-in CLI\nplugins_enabled_json: %s\n' "$4" >> "$CLI_HOME/config.yaml"
    ;;
  *) exit 0 ;;
esac
CLI
chmod +x "$BIN/hermes"

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
chk "A7 the CLI was called exactly once to set plugins.enabled" \
  "$([ "$(grep -c 'config set plugins.enabled' "$CALLS")" = 1 ] && echo 0 || echo 1)"

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

echo "=== E. the write targets --hermes-home, never the process default ==="
# The decoy stands in for the CLI's own default home. HERMES_HOME is deliberately *unset*
# in the environment: with it exported, the installer would pass its own value along by
# accident and the un-scoped call below could never be observed. Only --hermes-home may
# decide, and the backup must land in the same home as the write.
HOME_E="$(new_home e)"
DECOY="$TMP/decoy"; mkdir -p "$DECOY"
printf 'plugins:\n  enabled:\n    - shared-memory\n' > "$DECOY/config.yaml"
printf 'SOME_EXISTING_KEY=1\n' > "$DECOY/.env"
DECOY_CFG_BEFORE="$(cat "$DECOY/config.yaml")"
DECOY_FILES_BEFORE="$(find "$DECOY" -type f | sort)"
rm -f "$CALLS" "$SET_LOG" "$SET_HOME"
env -u HERMES_HOME FAKE_DEFAULT_HOME="$DECOY" PATH="$BIN:$PATH" JEV_STATE_DIR="$TMP/state-e" \
  bash "$INSTALLER" --hermes-home "$HOME_E" > "$TMP/out-e.txt" 2>&1
RC_E=$?
chk "E1 installer exits 0" "$([ "$RC_E" = 0 ] && echo 0 || echo 1)"
chk "E2 the target home got the plugin files" \
  "$([ -f "$HOME_E/plugins/jev-shadow-router/plugin.yaml" ] && echo 0 || echo 1)"
chk "E3 the backup was made in the target home" \
  "$([ -n "$(ls "$HOME_E"/config.yaml.bak.* 2>/dev/null)" ] && echo 0 || echo 1)"
chk "E4 the decoy config.yaml is byte-identical" \
  "$([ "$DECOY_CFG_BEFORE" = "$(cat "$DECOY/config.yaml")" ] && echo 0 || echo 1)"
chk "E5 the decoy home gained no file at all" \
  "$([ "$DECOY_FILES_BEFORE" = "$(find "$DECOY" -type f | sort)" ] && echo 0 || echo 1)"
chk "E6 every CLI call carried HERMES_HOME=<target>" \
  "$([ -s "$CALLS" ] && [ -z "$(grep -v "^$HOME_E|" "$CALLS")" ] && echo 0 || echo 1)"
chk "E7 exactly one write, and it went to the target home" \
  "$([ "$(grep -c 'config set' "$CALLS")" = 1 ] && grep -q "^$HOME_E|config set plugins.enabled" "$CALLS" \
     && [ "$(cat "$SET_HOME" 2>/dev/null)" = "$HOME_E" ] && echo 0 || echo 1)"

echo "=== F. an existing runtime state is preserved (no reset on reinstall) ==="
HOME_F="$(new_home f)"
STATE_F="$TMP/state-f"; mkdir -p "$STATE_F"
run_f() { PATH="$BIN:$PATH" HERMES_HOME="$HOME_F" JEV_STATE_DIR="$STATE_F" \
            bash "$INSTALLER" --hermes-home "$HOME_F" > "$1" 2>&1; }

printf '{"mode": "off", "updated_at": 1}\n' > "$STATE_F/mode.json"
BEFORE_OFF="$(cat "$STATE_F/mode.json")"
run_f "$TMP/out-f1.txt"; RC_F=$?
chk "F1 a reinstall with an existing state exits 0" "$([ "$RC_F" = 0 ] && echo 0 || echo 1)"
chk "F2 existing mode=off is preserved byte-for-byte" \
  "$([ "$BEFORE_OFF" = "$(cat "$STATE_F/mode.json")" ] && echo 0 || echo 1)"
chk "F3 the run reports that it preserved the state" \
  "$(grep -qi 'existing state preserved' "$TMP/out-f1.txt" && echo 0 || echo 1)"

printf '{"mode": "shadow", "tripped": true, "updated_at": 1}\n' > "$STATE_F/mode.json"
BEFORE_TRIP="$(cat "$STATE_F/mode.json")"
run_f "$TMP/out-f2.txt"
chk "F4 a tripped breaker flag survives a reinstall" \
  "$([ "$BEFORE_TRIP" = "$(cat "$STATE_F/mode.json")" ] && echo 0 || echo 1)"

printf '{"mode": "shadow", "updated_at": 1}\n' > "$STATE_F/mode.json"
BEFORE_SHADOW="$(cat "$STATE_F/mode.json")"
run_f "$TMP/out-f3.txt"
chk "F5 an existing shadow state is unchanged" \
  "$([ "$BEFORE_SHADOW" = "$(cat "$STATE_F/mode.json")" ] && echo 0 || echo 1)"

touch "$STATE_F/KILL"
run_f "$TMP/out-f4.txt"; RC_F4=$?
chk "F6 a KILL sentinel is preserved and never bypassed" \
  "$([ -f "$STATE_F/KILL" ] && [ "$BEFORE_SHADOW" = "$(cat "$STATE_F/mode.json")" ] \
     && [ "$RC_F4" = 0 ] && echo 0 || echo 1)"
chk "F7 the run reports the sentinel instead of writing" \
  "$(grep -q 'sentinel' "$TMP/out-f4.txt" && echo 0 || echo 1)"
rm -f "$STATE_F/KILL"

STATE_G="$TMP/state-g"
PATH="$BIN:$PATH" HERMES_HOME="$HOME_F" JEV_STATE_DIR="$STATE_G" \
  bash "$INSTALLER" --hermes-home "$HOME_F" > "$TMP/out-g.txt" 2>&1
chk "F8 a fresh state directory is still initialised (mode=shadow)" \
  "$([ -f "$STATE_G/mode.json" ] && grep -q '"shadow"' "$STATE_G/mode.json" && echo 0 || echo 1)"

echo
echo "total $((PASS + FAIL)) checks, failed $FAIL"
[ "$FAIL" = 0 ] || exit 1
exit 0
