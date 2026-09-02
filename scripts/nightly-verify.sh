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
# These need no dev server, so they run BEFORE the :9967 gate -- a config that
# has drifted is worth reporting on a night the dev server is down, which is
# exactly when nobody is looking. Most of them DO reach the network (--deployed
# reads a box over ssh, and each payload probe POSTs ~900 KB through that box's
# nginx); all of those exit 75 SKIP rather than FAIL when the host is
# unreachable, so an off Linode is not a red night.
#
# They guard invariants that span files no single repo's test suite can see, and
# they are here because the same failure shape has now bitten three times: a
# change lands in one of several places that must agree, every health check
# stays green, and the symptom shows up days later in a ride nobody can replay.
#
# check-nginx-parity.py is GONE (2026-09-01): the two nginx copies it compared
# were collapsed into one template, so parity is a property of the source rather
# than something to notice afterwards. render-nginx.py --check proves it, and
# check-config-ladder.py --deployed is the one that reads production instead of
# the repo -- the repo-only form printed OK on 2026-09-01 while the box was two
# rungs behind.
# ONE ENTRY PER HOST, not one per check. There are two deployments of this
# stack -- the Linode and rwtpc4 (the house) -- and they answer to the same
# name: /etc/hosts here maps api.transit-nav.com to the Linode's tailnet
# address, so a probe sent by name from this box grades the Linode no matter
# which machine you meant. That is not hypothetical: on 2026-09-01 the payload
# probe printed OK while the house was two rungs behind and 413ing every real
# ride's telemetry (backlog 2.16). A check that can silently grade the wrong
# machine has to be told which machine, so `--target house|prod` names the box
# and the result line repeats the address it actually reached.
#
# The house probe writes a ~900 KB `config-probe` line into ~/otp-debug-logs,
# which ride-watch tails. It is inert there: ride_watch.py only opens a trip on
# START_GO_MODE, so a LADDER_PROBE on session `config-probe` matches no trip and
# falls through _process without touching any rule.
#
# Each entry is "<display name>|<command>". The command inherits cron's cwd
# ($HOME), NOT scripts/ -- every entry must name its paths off $(dirname "$0")
# and every check it calls must resolve its own repo paths, or the 05:00 run
# grades something other than what a hand-run in scripts/ grades.
STATIC_CHECKS=(
  "config-ladder-repo|python3 $(dirname "$0")/check-config-ladder.py"
  # The host is spelled out rather than left to check-config-ladder.py's
  # default: the entry NAME claims which box it graded, and a name that is
  # only true while a default holds is the failure this block warns about.
  "config-ladder-deployed-prod|python3 $(dirname "$0")/check-config-ladder.py --deployed --ssh rwt@100.126.171.72"
  "config-ladder-deployed-house|python3 $(dirname "$0")/check-config-ladder.py --deployed --ssh local"
  "nginx-render-parity|python3 $(dirname "$0")/../deployment/render-nginx.py --check"
  # The desktop's transitnav/.env is the source for the Linode's, and two of its
  # keys now say which box you are (backlog 2.22). deploy-app.sh strips them;
  # this proves the transform still does, without needing a deploy to find out.
  "server-env-transform|bash $(dirname "$0")/../deployment/test-server-env.sh"
  "debug-log-payload-prod|python3 $(dirname "$0")/check-debug-log-payload.py --target prod"
  "debug-log-payload-house|python3 $(dirname "$0")/check-debug-log-payload.py --target house"
  # Asks the running OTP a real routing question, because the answer depends on
  # two artefacts that ship by different steps: router-config.json (--only
  # graph) and the jar built from the patched fork (--only jar otp). Either one
  # arriving alone puts the Southdale search back to a lone walk card, and
  # nothing else in this suite would notice.
  "transit-not-hidden-prod|python3 $(dirname "$0")/check-transit-not-hidden.py --target prod"
)
for entry in "${STATIC_CHECKS[@]}"; do
  name="${entry%%|*}"
  cmd="${entry#*|}"
  log="$RUN_DIR/$name.log"
  start=$(date +%s)
  if $cmd > "$log" 2>&1; then
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
  # Always non-zero: a night with no dev server is a night the suite did not
  # run, whatever the static checks said. (The guarded `exit 1` that stood
  # here first was unreachable and read as if a clean static pass could exit 0.)
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
