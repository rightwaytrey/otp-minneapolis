#!/usr/bin/env bash
#
# nightly-verify.sh — run the full Go Mode verify-*.js GPS-replay suite against
# the :9967 dev server and write a pass/fail report to the Obsidian vault.
#
# Why: the pre-push discipline runs only the verify scripts relevant to a
# change; this catches the regressions those targeted runs miss. It can't run
# in GitHub Actions (needs the real OTP backend + the dev server's test hooks),
# so it runs here, nightly, after the 03:00 GTFS refresh window.
#
# CAVEAT the report must surface: :9967 serves the SHARED otprr working tree,
# which parallel sessions may leave dirty — every report records exactly what
# tree was tested (git describe --dirty + status).
#
# Cron:  0 5 * * *  /home/rwt/projects/otp-minneapolis/scripts/nightly-verify.sh
set -uo pipefail

WEB_REPO=/home/rwt/projects/otprr/otp-react-redux
APP_URL=${APP_URL:-http://localhost:9967/}
VAULT_DIR=/home/rwt/obsidian-vault/Claude/verify-nightly
LOG_ROOT=/home/rwt/projects/otp-minneapolis/data/nightly-verify
PER_SCRIPT_TIMEOUT=300 # seconds — the default; see script_timeout() for the exceptions
KEEP_DAYS=14

# Two scripts replay a whole recorded ride at 25x and legitimately need longer
# than the default. Both were reported TIMEOUT every night from 2026-08-01 to
# 2026-08-16 while dying at ~92% of their replay (3311/3657 and 3322/3560) — the
# 300s cap cut them off just short of their assertions, so the suite's two
# longest end-to-end checks went 16 days without once completing. The cap is
# per-script rather than raised globally so a genuinely hung script still dies
# quickly.
script_timeout() {
  case "$1" in
    verify-transit-trust | verify-onboard-loop-0802) echo 600 ;;
    *) echo "$PER_SCRIPT_TIMEOUT" ;;
  esac
}

DATE=$(date +%F)
RUN_DIR="$LOG_ROOT/$DATE"
REPORT="$VAULT_DIR/$DATE.md"
mkdir -p "$RUN_DIR" "$VAULT_DIR"

# Rotate raw logs; vault reports are small and kept forever.
find "$LOG_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" \
  -exec rm -rf {} + 2>/dev/null

tree_desc=$(git -C "$WEB_REPO" describe --always --dirty 2>/dev/null)
tree_status=$(git -C "$WEB_REPO" status --short 2>/dev/null)
tree_branch=$(git -C "$WEB_REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)

{
  echo "# Nightly verify — $DATE"
  echo
  echo "- App: \`$APP_URL\`"
  echo "- Tree: \`$tree_branch\` @ \`$tree_desc\`"
  if [ -n "$tree_status" ]; then
    echo "- **Working tree is DIRTY** — results reflect uncommitted edits:"
    echo '```'
    echo "$tree_status"
    echo '```'
  else
    echo "- Working tree clean."
  fi
  echo
} > "$REPORT"

# A script may exit 75 to say "the thing under test could not be exercised, and
# that is not a defect" -- e.g. verify-onboard-options at 05:00, when no bus is
# out of the garage to board. That is not a pass and it is not a failure; either
# label is a lie, and a suite that cries wolf gets discounted.
EXIT_SKIP=75

pass=0 fail=0 skip=0
results=""
failures=""
skips=""

# --- Static config checks --------------------------------------------------
# These need no dev server and no network, so they run BEFORE the :9967 gate --
# a config that has drifted is worth reporting on a night the dev server is
# down, which is exactly when nobody is looking.
#
# Both guard invariants that span files no single repo's test suite can see, and
# both are here because the same failure shape has now bitten three times: a
# change lands in one of several places that must agree, every health check
# stays green, and the symptom shows up days later in a ride nobody can replay.
for check in check-config-ladder.py check-nginx-parity.py; do
  name="${check%.py}"
  log="$RUN_DIR/$name.log"
  start=$(date +%s)
  if python3 "$(dirname "$0")/$check" > "$log" 2>&1; then
    status=PASS; pass=$((pass + 1))
  else
    rc=$?
    if [ "$rc" -eq "$EXIT_SKIP" ]; then
      status=SKIP
      skip=$((skip + 1))
      skips+=$'\n'"## $name — SKIP"$'\n\n```\n'"$(tail -25 "$log")"$'\n```\n'
    else
      status=FAIL
      fail=$((fail + 1))
      failures+=$'\n'"## $name — FAIL"$'\n\n```\n'"$(tail -25 "$log")"$'\n```\n'
    fi
  fi
  dur=$(( $(date +%s) - start ))
  results+="| $name | $status | ${dur}s |"$'\n'
done

# Dev server must be up (docker restart otp-frontend-dev fixes a clobbered
# config; see the dev-server notes).
if ! curl -sf -o /dev/null --max-time 15 "$APP_URL"; then
  {
    echo "**ABORTED: $APP_URL is not responding — no verify-*.js scripts were run.**"
    echo
    echo "The static config checks above do not need it, and their results stand:"
    echo
    echo "| check | result | duration |"
    echo "| --- | --- | --- |"
    printf '%s' "$results"
    [ -n "$failures" ] && { echo; echo "$failures"; }
    [ -n "$skips" ] && { echo; echo "$skips"; }
  } >> "$REPORT"
  [ "$fail" -eq 0 ] || exit 1
  exit 1
fi

for script in "$WEB_REPO"/scripts/verify-*.js; do
  name=$(basename "$script" .js)
  log="$RUN_DIR/$name.log"
  limit=$(script_timeout "$name")
  start=$(date +%s)
  if (cd "$WEB_REPO" && APP_URL="$APP_URL" OUT_DIR="$RUN_DIR" \
      PUPPETEER_EXECUTABLE_PATH=/opt/google/chrome/chrome \
      timeout "$limit" node "$script") > "$log" 2>&1; then
    status=PASS; pass=$((pass + 1))
  else
    rc=$?
    if [ "$rc" -eq "$EXIT_SKIP" ]; then
      status=SKIP
      skip=$((skip + 1))
      skips+=$'\n'"## $name — SKIP"$'\n\n```\n'"$(tail -25 "$log")"$'\n```\n'
    else
      if [ "$rc" -eq 124 ]; then status="TIMEOUT (${limit}s cap)"; else status=FAIL; fi
      fail=$((fail + 1))
      failures+=$'\n'"## $name — $status"$'\n\n```\n'"$(tail -25 "$log")"$'\n```\n'
    fi
  fi
  dur=$(( $(date +%s) - start ))
  results+="| $name | $status | ${dur}s |"$'\n'
done

{
  if [ "$skip" -gt 0 ]; then
    echo "**${pass} passed, ${fail} failed, ${skip} skipped.**"
  else
    echo "**${pass} passed, ${fail} failed.**"
  fi
  echo
  echo "| script | result | duration |"
  echo "| --- | --- | --- |"
  printf '%s' "$results"
  if [ -n "$failures" ]; then
    echo
    echo "$failures"
  fi
  if [ -n "$skips" ]; then
    echo
    echo "$skips"
  fi
  if [ -n "$failures" ] || [ -n "$skips" ]; then
    echo
    echo "Raw logs: \`$RUN_DIR/\`"
  fi
} >> "$REPORT"

[ "$fail" -eq 0 ]
