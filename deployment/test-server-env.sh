#!/usr/bin/env bash
#
# test-server-env.sh — exercise render_server_env without an ssh or a server.
#
# The transform it covers decides what production's Flask sidecar believes
# about itself, and its failure mode is silent: a wrong APP_BUNDLE_PUBLIC_BASE
# does not error, it just tells every phone to fetch its next web bundle from
# somewhere it cannot reach, and the phone stays on the bundle it has.
#
#   ./deployment/test-server-env.sh
#
# Exit 0 all pass, 1 a failure.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/server-env.sh"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
FAILED=0
ok()  { printf '  %s✓%s %s\n' "$GREEN" "$NC" "$1"; }
bad() { printf '  %s✗%s %s\n' "$RED" "$NC" "$1"; FAILED=$((FAILED+1)); }

check_has()    { case "$2" in *"$1"*) ok "$3" ;; *) bad "$3" ;; esac; }
check_lacks()  { case "$2" in *"$1"*) bad "$3" ;; *) ok "$3" ;; esac; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
SRC="$TMP/house.env"

# A house .env as backlog 2.17's staging lane leaves it: the two host-local
# keys, a key that only LOOKS host-local, an unrelated secret, and a comment
# that mentions one of the stripped names.
cat > "$SRC" <<'ENV'
# transitnav sidecar config (desktop)
PREFS_ALLOWED_ORIGIN=http://localhost:9967,https://rwtpc4.tail9d6464.ts.net:9966
PREFS_MODEL=claude-sonnet-4-5
PREFS_PORT=8092
ANTHROPIC_API_KEY=sk-ant-house-key-do-not-ship-elsewhere
# APP_BUNDLE_DIR below is the house staging lane, NOT production.
APP_BUNDLE_DIR=/home/rwt/app-bundles-house
APP_BUNDLE_PUBLIC_BASE=https://rwtpc4.tail9d6464.ts.net:9966
APP_BUNDLE_APP_ID=org.rightwaytrey.transitnav
ENV

echo "=== render_server_env: house .env -> server .env ==="
OUT="$(render_server_env "$SRC" api.transit-nav.com 9966 2>"$TMP/err")"
ERR="$(cat "$TMP/err")"

check_lacks "APP_BUNDLE_DIR=" "$OUT" \
  "APP_BUNDLE_DIR assignment does not reach the server"
check_lacks "APP_BUNDLE_PUBLIC_BASE=" "$OUT" \
  "APP_BUNDLE_PUBLIC_BASE assignment does not reach the server"
check_lacks "app-bundles-house" "$OUT" \
  "the house bundle directory appears nowhere in the output"
check_lacks "rwtpc4.tail9d6464.ts.net" "$OUT" \
  "the house tailnet host appears nowhere in the output"

check_has "APP_BUNDLE_APP_ID=org.rightwaytrey.transitnav" "$OUT" \
  "APP_BUNDLE_APP_ID survives (same app on both boxes, not host-local)"
check_has "ANTHROPIC_API_KEY=sk-ant-house-key-do-not-ship-elsewhere" "$OUT" \
  "unrelated keys are passed through untouched"
check_has "PREFS_PORT=8092" "$OUT" "PREFS_PORT is passed through untouched"
check_has "# APP_BUNDLE_DIR below is the house staging lane" "$OUT" \
  "a COMMENT mentioning a stripped key is documentation and stays"

check_has "PREFS_ALLOWED_ORIGIN=https://api.transit-nav.com:9966,https://api.transit-nav.com,capacitor://localhost," \
  "$OUT" "PREFS_ALLOWED_ORIGIN is rewritten for the server"
check_lacks "PREFS_ALLOWED_ORIGIN=http://localhost:9967" "$OUT" \
  "the desktop's own origins are gone"

echo "=== it says what it removed ==="
check_has "stripped APP_BUNDLE_DIR" "$ERR" "reports APP_BUNDLE_DIR on stderr"
check_has "stripped APP_BUNDLE_PUBLIC_BASE" "$ERR" \
  "reports APP_BUNDLE_PUBLIC_BASE on stderr"

echo "=== a .env with none of those keys is unchanged apart from the origin ==="
cat > "$TMP/plain.env" <<'ENV'
PREFS_ALLOWED_ORIGIN=http://localhost:9967
PREFS_PORT=8092
ENV
OUT2="$(render_server_env "$TMP/plain.env" api.transit-nav.com 9966 2>"$TMP/err2")"
[ "$(wc -l < "$TMP/plain.env")" = "$(printf '%s\n' "$OUT2" | wc -l)" ] \
  && ok "line count preserved" || bad "line count changed"
[ -s "$TMP/err2" ] && bad "reported a strip that did not happen" \
  || ok "says nothing when nothing was stripped"

echo "=== a missing source is an error, not an empty .env ==="
if render_server_env "$TMP/nope.env" api.transit-nav.com 9966 >/dev/null 2>&1; then
  bad "a missing source file returned success"
else
  ok "a missing source file is a non-zero return (never truncates the server's .env)"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  printf '%sAll checks passed.%s\n' "$GREEN" "$NC"; exit 0
fi
printf '%s%d check(s) FAILED.%s\n' "$RED" "$FAILED" "$NC"; exit 1
