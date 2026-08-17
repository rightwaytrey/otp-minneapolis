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

# Dev server must be up (docker restart otp-frontend-dev fixes a clobbered
# config; see the dev-server notes).
if ! curl -sf -o /dev/null --max-time 15 "$APP_URL"; then
  echo "**ABORTED: $APP_URL is not responding — no scripts were run.**" >> "$REPORT"
  exit 1
fi

pass=0 fail=0
results=""
failures=""

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
    if [ "$rc" -eq 124 ]; then status="TIMEOUT (${limit}s cap)"; else status=FAIL; fi
    fail=$((fail + 1))
    failures+=$'\n'"## $name — $status"$'\n\n```\n'"$(tail -25 "$log")"$'\n```\n'
  fi
  dur=$(( $(date +%s) - start ))
  results+="| $name | $status | ${dur}s |"$'\n'
done

{
  echo "**${pass} passed, ${fail} failed.**"
  echo
  echo "| script | result | duration |"
  echo "| --- | --- | --- |"
  printf '%s' "$results"
  if [ -n "$failures" ]; then
    echo
    echo "$failures"
    echo
    echo "Raw logs: \`$RUN_DIR/\`"
  fi
} >> "$REPORT"

[ "$fail" -eq 0 ]
