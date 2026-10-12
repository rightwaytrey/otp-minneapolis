#!/usr/bin/env bash
# Promote a cycle's ride-verified web sha to the PRODUCTION app — the whole
# ship-prod publish in one command, so it is one thing to allow (2026-10-12).
#
#   pipeline-ship-prod.sh --check <cycle> <sha>          read-only: is it already in prod?
#   pipeline-ship-prod.sh <cycle> <sha> "<one line>"     ship it
#
# Why it exists: cycle threads run in auto mode, and production publishes are
# the one thing the auto-mode guard is right to refuse when they are improvised.
# On 10-12 cycle 13's agent had to promote a sha older than otprr main; nothing
# sanctioned could build it, so it built by hand, rsync'd into www/ and was
# refused [Production Deploy]. This script is the sanctioned path — it is named
# in ~/.claude/settings.json (permissions.allow and the autoMode allow rule) —
# and the skill's ship-prod section tells agents to run it and nothing else.
#
# What it does, in order: refuses unless <sha> is a commit that contains the
# current production web sha (never a rollback); exits 0 with ALREADY if prod
# already contains <sha>; takes ~/.claude/pipeline/ship.lock (stale after 2h);
# runs transitnav-ios scripts/ship_web.sh with WEB_SHA=<sha> (build-web.sh
# builds that commit in a detached worktree — see its WEB_SHA note), which
# publishes, then checks the checksum over the public URL; restarts
# otp-frontend-dev (the build clobbers its config); releases the lock.
# Exit: 0 shipped or ALREADY, 1 refused/failed, 2 usage.
set -euo pipefail

CHECK=0
if [ "${1:-}" = "--check" ]; then CHECK=1; shift; fi
N="${1:-}"; SHA="${2:-}"; NOTE="${3:-}"; N="${N#c}"
if [ -z "$N" ] || [ -z "$SHA" ] || { [ "$CHECK" = 0 ] && [ -z "$NOTE" ]; }; then
  sed -n '5,6p' "$0" >&2; exit 2
fi

OTPRR=/home/rwt/projects/otprr/otp-react-redux
IOS=/home/rwt/projects/transitnav-ios
LINODE=rwt@100.126.171.72
LOCK=/home/rwt/.claude/pipeline/ship.lock
export PATH="/home/rwt/.nvm/versions/node/v22.16.0/bin:$PATH"   # ship_web.sh needs node 22

FULL="$(git -C "$OTPRR" rev-parse --verify "$SHA^{commit}" 2>/dev/null)" || {
  echo "REFUSED: $SHA is not a commit in $OTPRR" >&2; exit 1; }
PROD="$(ssh -o BatchMode=yes -o ConnectTimeout=15 "$LINODE" cat app-bundles/manifest.json |
  python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["version"],d.get("web_sha",""))')"
PROD_VER="${PROD%% *}"; PROD_SHA="${PROD#* }"
echo "prod: $PROD_VER web ${PROD_SHA:0:9} · cycle $N wants ${FULL:0:9}"

if [ -n "$PROD_SHA" ] && git -C "$OTPRR" merge-base --is-ancestor "$FULL" "$PROD_SHA"; then
  echo "ALREADY: production $PROD_VER contains ${FULL:0:9} — nothing to ship."; exit 0
fi
if [ -n "$PROD_SHA" ] && ! git -C "$OTPRR" merge-base --is-ancestor "$PROD_SHA" "$FULL"; then
  echo "REFUSED: ${FULL:0:9} does not contain production's ${PROD_SHA:0:9};" >&2
  echo "shipping it would roll changes back. The rider decides that, by hand." >&2
  exit 1
fi
if [ "$CHECK" = 1 ]; then echo "WOULD SHIP: ${FULL:0:9} is ahead of production."; exit 0; fi

if ! mkdir "$LOCK" 2>/dev/null; then
  AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK") ))
  if [ "$AGE" -lt 7200 ]; then
    echo "REFUSED: ship lock held ($(cat "$LOCK/owner" 2>/dev/null)), ${AGE}s old — retry later." >&2; exit 1
  fi
  echo "ship lock is ${AGE}s old — stale, taking it"; rm -rf "$LOCK"; mkdir "$LOCK"
fi
echo "c$N prod $(date +%s)" > "$LOCK/owner"
trap 'rm -rf "$LOCK"; docker restart otp-frontend-dev >/dev/null 2>&1 || true' EXIT

cd "$IOS"
TRANSITNAV_TARGET=production WEB_SHA="$FULL" scripts/ship_web.sh "$NOTE"
