#!/usr/bin/env bash
#
# pull-debug-logs.sh — mirror the server's Go Mode debug logs down to this box.
#
# Why this exists: /api/debug-log now lands on the Linode, but ride-watch still
# runs here (it drives tmux and the claude CLI under the user's login) and works
# by tailing ~/otp-debug-logs/debug-<UTC-date>.jsonl. Without this the daemon
# sits on a file that stops growing and silently watches nothing.
#
# Runs as a user service, looping rather than as a 5s timer: systemd timers are
# awkward below ~10s and a loop keeps one long-lived ssh-free rsync cadence.
#
# SAFETY: only debug-*.jsonl* is pulled, and --delete is NEVER used.
# ~/otp-debug-logs/ride-watch/ is written HERE by the daemon and does not exist
# on the server; a mirroring sync would wipe it.
set -uo pipefail

REMOTE="${REMOTE:-rwt@100.126.171.72}"
REMOTE_DIR="${REMOTE_DIR:-/home/rwt/otp-debug-logs/}"
LOCAL_DIR="${LOCAL_DIR:-$HOME/otp-debug-logs/}"
INTERVAL="${INTERVAL:-5}"
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes"

mkdir -p "$LOCAL_DIR"
fails=0
while true; do
  # --append-verify suits append-only JSONL: it ships only the new tail and
  # re-checks the overlap, so a rotated or rewritten file still transfers whole.
  if rsync -az --append-verify --timeout=30 -e "ssh $SSH_OPTS" \
       --include='debug-*.jsonl' --include='debug-*.jsonl.gz' --exclude='*' \
       "$REMOTE:$REMOTE_DIR" "$LOCAL_DIR" 2>/dev/null; then
    fails=0
  else
    fails=$((fails + 1))
    # Quiet about blips; loud once it is clearly not transient. A dead sync
    # means ride-watch is blind, which is worth a log line.
    [ "$fails" = 12 ] && echo "debug-log sync failing for ~1min ($REMOTE)" >&2
  fi
  sleep "$INTERVAL"
done
