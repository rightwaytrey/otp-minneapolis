#!/usr/bin/env bash
# The one event stream a TransitNav cycle thread follows with Monitor
# (2026-10-08, self-driving cycles). Two kinds of line on stdout:
#
#   INBOX #K key=… thread=… text=<path> :: <first line>   a press the router forwarded
#   RIDE <session> start=<ms> end=<ms> req=<request path|->   a trip has ended
#
# It never exits on its own: the Monitor's 30-minute timeout is the thread's
# self-check tick (see the transitnav-pipeline skill, "Cycle threads").
#
# Inbox: resumes one past the highest K in `handled`, as before.
# Rides (since 2026-10-09 the TRIP is the trigger, not the vault report): every
# 60 s it lists the trips the ride-watch daemon has closed — one per
# report-request-<session>-<HHMM>.json in ~/otp-debug-logs/ride-watch (written at
# trip end, but ONLY for a ride with findings: ride_watch.py
# `_write_report_request(trip) if n > 0`), plus state.json's lastTrip, which is
# written for every ended trip, clean ones included. An app check-in with no
# trip writes neither. Each trip is keyed "<session> <startMs>" in
# cN/trips.seen, so a ride with findings (both sources) is emitted once and a
# re-arm never repeats one. req=- means the clean-ride path (no request file).
# On the very first run every trip already there is history: recorded, never
# emitted. (cN/rides.seen is the old report-name list, kept, no longer read.)
#
#   pipeline-cycle-watch.sh <N>
set -u
N="${1:?usage: pipeline-cycle-watch.sh <N>}"; N="${N#c}"
DIR="$HOME/.claude/pipeline/c$N"
mkdir -p "$DIR"; touch "$DIR/inbox.log" "$DIR/handled"
# The background tail must die with us even on SIGKILL (a Monitor timeout):
# `tail --pid=$$` exits within a second of this script, and sed follows on EOF.
# Never `kill 0` here — it could reach the Claude session running the Monitor.
trap 'pkill -P $$ 2>/dev/null' EXIT
trap 'exit 0' INT TERM

H=$(sort -n "$DIR/handled" | tail -n1); M=$(( ${H:-0} + 1 ))
REQ="$HOME/otp-debug-logs/ride-watch"
TRIPS="$DIR/trips.seen"
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
[ -f "$TRIPS" ] || trips | awk '{print $1" "$2}' > "$TRIPS"


tail --pid=$$ -n +"$M" -F "$DIR/inbox.log" 2>/dev/null | sed -u 's/^/INBOX /' &

while :; do
  trips | while read -r sess st en req; do
    grep -qxF "$sess $st" "$TRIPS" || { echo "RIDE $sess start=$st end=$en req=$req"; echo "$sess $st" >> "$TRIPS"; }
  done
  sleep 60 & wait $!
done
