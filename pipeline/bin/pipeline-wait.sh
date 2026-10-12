#!/usr/bin/env bash
# A pipeline thread's ONE-SHOT waiter (2026-10-10; replaces the Monitor on
# pipeline-cycle-watch.sh / pipeline-board-watch.sh). The thread runs it with
# Bash run_in_background; it blocks — silently, for as long as it takes — until
# there is real work, prints that work as a batch of lines, and EXITS. The
# thread handles the batch and starts it again.
#
# Why (the rider, 2026-10-10: "such a needless waste of context"): a Monitor
# lasts at most 30 minutes, so an idle thread was woken 48x a day by "Monitor
# expired with no events", ran a self-check and re-armed. Cycle 15's thread:
# 33 wake-ups, 30 of them empty. Here an idle hour costs the thread nothing.
#
#   pipeline-wait.sh <N>      cycle N's thread
#   pipeline-wait.sh board    the router
#
# Lines it prints (one batch, then exit 0):
#   INBOX #K key=… thread=… text=<path> :: <first line>   a press
#   RIDE <session> start=<ms> end=<ms> req=<request path|->  a trip ended (cycles)
#   CHECK <key> :: <what>                                    a self-check rule fired
#                                                            (pipeline-check.py; once per key)
# The router's 15-minute ledger scan runs HERE (pipeline-scan-apply.py, logged
# to board/scan.log) and never wakes the router.
#
# Inbox cursor: `delivered` (K per line, written here) and `handled` (written
# by the thread). A press is printed when its K is above both. The thread
# wrappers empty `delivered` at every launch, so a relaunched session gets the
# presses its predecessor never marked handled.
#
# Rides: every closed trip — one per report-request-<session>-<HHMM>.json in
# ~/otp-debug-logs/ride-watch (written only for a ride with findings,
# ride_watch.py `_write_report_request(trip) if n > 0`), plus state.json's
# lastTrip (every ended trip, clean ones included) — keyed "<session> <startMs>"
# in cN/trips.seen. On the very first run every trip already there is history:
# recorded, never printed. req=- is the clean-ride path.
set -u
N="${1:?usage: pipeline-wait.sh <N|board>}"; N="${N#c}"
BIN="$(dirname "$(readlink -f "$0")")"
if [ "$N" = "board" ]; then DIR="$HOME/.claude/pipeline/board"; else DIR="$HOME/.claude/pipeline/c$N"; fi
mkdir -p "$DIR"; touch "$DIR/inbox.log" "$DIR/handled" "$DIR/delivered"
REQ="$HOME/otp-debug-logs/ride-watch"
TRIPS="$DIR/trips.seen"
SCAN_EVERY=900

trips() {  # "<session> <startMs> <endMs> <request path|->", one line per closed trip
  python3 -I - "$REQ" <<'EOF' 2>/dev/null
import glob, json, os, sys
d = sys.argv[1]; out = {}
for p in sorted(glob.glob(os.path.join(d, "report-request-*.json"))):
    try:
        j = json.load(open(p))
    except (OSError, ValueError):
        continue
    if isinstance(j, dict) and j.get("session") and isinstance(j.get("startMs"), (int, float)):
        out[(j["session"], int(j["startMs"]))] = (int(j.get("endMs") or 0), p)
try:
    t = json.load(open(os.path.join(d, "state.json"))).get("lastTrip") or {}
    k = (t.get("session"), int(t.get("startMs")))
    if k[0] and k not in out:
        out[k] = (int(t.get("endMs") or 0), "-")
except (OSError, ValueError, TypeError, AttributeError):
    pass
for (s, st), (en, p) in sorted(out.items(), key=lambda kv: kv[1][0]):
    print(s, st, en, p)
EOF
}
[ "$N" = "board" ] || [ -f "$TRIPS" ] || trips | awk '{print $1" "$2}' > "$TRIPS"

inbox() {  # unprinted, unhandled presses; records them in `delivered`
  local h k line
  h=$(cat "$DIR/handled" "$DIR/delivered" | sort -n | tail -n1); h=${h:-0}
  while IFS= read -r line; do
    k=${line%% *}; k=${k#\#}
    case "$k" in ''|*[!0-9]*) continue ;; esac
    if [ "$k" -gt "$h" ]; then echo "INBOX $line"; echo "$k" >> "$DIR/delivered"; fi
  done < "$DIR/inbox.log"
}

rides() {
  trips | while read -r sess st en req; do
    grep -qxF "$sess $st" "$TRIPS" || { echo "RIDE $sess start=$st end=$en req=$req"; echo "$sess $st" >> "$TRIPS"; }
  done
}

scan() {  # router only: the silent ledger scan, every 15 min
  local stamp="$DIR/scan.stamp"
  if [ ! -f "$stamp" ] || [ $(( $(date +%s) - $(stat -c %Y "$stamp") )) -ge $SCAN_EVERY ]; then
    touch "$stamp"
    { date '+%F %T'; python3 -I "$BIN/pipeline-scan-apply.py" 2>&1; } >> "$DIR/scan.log"
  fi
}

while :; do
  [ "$N" = "board" ] && scan
  OUT="$(inbox; [ "$N" = "board" ] || rides; python3 -I "$BIN/pipeline-check.py" "$N" 2>/dev/null)"
  if [ -n "$OUT" ]; then printf '%s\n' "$OUT"; exit 0; fi
  sleep 60
done
