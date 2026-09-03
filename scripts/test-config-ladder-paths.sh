#!/usr/bin/env bash
#
# test-config-ladder-paths.sh — check-config-ladder.py has to FIND the three
# repos before it can compare anything, and getting that wrong is invisible:
# it exits 75 with a SKIP nobody reads, and the nightly records "SKIP" next to
# six PASSes. This exercises the resolution alone, on throwaway trees, with no
# ssh and no real repo.
#
#   ./scripts/test-config-ladder-paths.sh
#
# The case that mattered (backlog 2.26): from a git worktree at
# ~/projects/otp-minneapolis-wt/<task> the sibling guess points at
# ~/projects/otp-minneapolis-wt/otprr/otp-react-redux, which does not exist.
#
# Exit 0 all pass, 1 a failure.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LADDER="$SCRIPT_DIR/check-config-ladder.py"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
FAILED=0
ok()  { printf '  %s✓%s %s\n' "$GREEN" "$NC" "$1"; }
bad() { printf '  %s✗%s %s\n' "$RED" "$NC" "$1"; FAILED=$((FAILED+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# A fake HOME whose ~/projects holds the two sibling repos, and a fake
# otp-minneapolis checkout that is NOT their sibling -- the worktree shape.
FAKE_HOME="$TMP/home"
mkdir -p "$FAKE_HOME/projects/otprr/otp-react-redux/lib/util"
mkdir -p "$FAKE_HOME/projects/transitnav"
cat > "$FAKE_HOME/projects/otprr/otp-react-redux/lib/util/debug-log.js" <<'JS'
const MAX_FULL_PAYLOAD_CHARS = 1000000
const MAX_BODY_BYTES = 1400000
JS
cat > "$FAKE_HOME/projects/transitnav/preferences_api.py" <<'PY'
DEBUG_LOG_MAX_LINE_CHARS = 1179648
PY

# The copy of check-config-ladder.py under test, planted in a tree with the
# worktree's shape: <root>/otp-minneapolis-wt/<task>/scripts/. Its sibling
# directory holds neither repo, exactly as on the real machine.
WT="$FAKE_HOME/projects/otp-minneapolis-wt/sometask"
mkdir -p "$WT/scripts" "$WT/deployment/nginx"
cp "$LADDER" "$WT/scripts/"
cat > "$WT/deployment/nginx/otp-common.conf.tmpl" <<'CONF'
location /api/debug-log {
    client_max_body_size 1536k;
    proxy_pass http://127.0.0.1:8092;
}
CONF

run_ladder() { env HOME="$FAKE_HOME" "$@" python3 "$WT/scripts/check-config-ladder.py" 2>&1; }

echo "=== from a worktree, the ~/projects fallback resolves both repos ==="
OUT="$(run_ladder)"; RC=$?
[ "$RC" -eq 0 ] && ok "exit 0 (was 75 before the fallback)" \
  || bad "exit $RC, expected 0 -- output: $OUT"
case "$OUT" in
  *"$FAKE_HOME/projects/otprr/otp-react-redux/lib/util/debug-log.js"*)
    ok "read debug-log.js from ~/projects, not from the worktree's sibling" ;;
  *) bad "did not name the ~/projects otprr path" ;;
esac
case "$OUT" in
  *"$FAKE_HOME/projects/transitnav/preferences_api.py"*)
    ok "read preferences_api.py from ~/projects" ;;
  *) bad "did not name the ~/projects transitnav path" ;;
esac
# The nginx rung is the one file that must still come from the tree the script
# lives in -- it is the copy being changed, and taking it from ~/projects would
# grade the wrong bytes.
case "$OUT" in
  *"$WT/deployment/nginx/otp-common.conf.tmpl"*)
    ok "nginx rung still read from the script's OWN tree" ;;
  *) bad "nginx rung did not come from the worktree" ;;
esac

echo "=== a real sibling layout still wins over ~/projects ==="
SIB="$FAKE_HOME/projects/otp-minneapolis"
mkdir -p "$SIB/scripts" "$SIB/deployment/nginx"
cp "$LADDER" "$SIB/scripts/"
cp "$WT/deployment/nginx/otp-common.conf.tmpl" "$SIB/deployment/nginx/"
OUT="$(env HOME="$FAKE_HOME" python3 "$SIB/scripts/check-config-ladder.py" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && ok "exit 0 from the sibling layout" || bad "exit $RC, expected 0"

echo "=== the env overrides still win outright ==="
mkdir -p "$TMP/elsewhere/lib/util"
cat > "$TMP/elsewhere/lib/util/debug-log.js" <<'JS'
const MAX_FULL_PAYLOAD_CHARS = 7
const MAX_BODY_BYTES = 9
JS
OUT="$(run_ladder OTPRR_DIR="$TMP/elsewhere")"
case "$OUT" in
  *"$TMP/elsewhere/lib/util/debug-log.js"*) ok "OTPRR_DIR beat both defaults" ;;
  *) bad "OTPRR_DIR was ignored -- output: $OUT" ;;
esac
# 7 < 1179648 < 9 is false, so the override must also be BELIEVED, not just read.
case "$OUT" in
  *"not strictly increasing"*|*"FAIL"*) ok "the overridden rungs were compared, not decorative" ;;
  *) bad "an override with a broken ladder did not fail" ;;
esac

echo "=== a genuinely missing repo still SKIPs, and says where it looked ==="
OUT="$(env HOME="$TMP/empty" python3 "$WT/scripts/check-config-ladder.py" 2>&1)"; RC=$?
[ "$RC" -eq 75 ] && ok "exit 75 (fails safe, does not invent a pass)" \
  || bad "exit $RC, expected 75"
case "$OUT" in
  *"also looked at"*) ok "the SKIP names the fallback it tried too" ;;
  *) bad "the SKIP named only one path" ;;
esac
# The transitnav sibling miss must NOT be reported here: ~/projects/transitnav
# is missing too under this HOME, so both are genuinely unresolved -- but a repo
# the fallback DID find must never appear in another repo's SKIP.
OUT2="$(run_ladder OTPRR_DIR=/nonexistent)"
case "$OUT2" in
  *"also looked at"*preferences_api.py*)
    bad "reported a transitnav miss the fallback had already recovered" ;;
  *) ok "a repo the fallback found is absent from the failing repo's SKIP" ;;
esac

echo
if [ "$FAILED" -eq 0 ]; then
  printf '%sAll checks passed.%s\n' "$GREEN" "$NC"; exit 0
fi
printf '%s%d check(s) FAILED.%s\n' "$RED" "$FAILED" "$NC"; exit 1
