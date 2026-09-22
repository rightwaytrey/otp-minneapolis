#!/bin/sh
# May ride-watch be restarted right now?  Exit 0 = yes.
#
# Used as the ExecCondition of ride-watch-restart.service, which the path unit
# fires on any change to ride_watch.py.  It exists because that condition used
# to ask one question — "is there an active trip?" — and on 2026-09-21 16:43:53
# the honest answer was "no" while the 16:05 ride's wrap-up was one minute into
# its ten-minute window.  The restart took the rider's console away mid-write,
# re-opened the finished 16:31 sub-ride out of the stream, filed four findings
# about a rider who was already home, and paged them "Ride ended — 5 findings.
# Report pending" twenty-five seconds before the real report landed (24.1).
#
# A ride is not over when the trip ends.  It is over when its write-up has
# settled, and the daemon says so in current-ride.md:
#
#   No active trip. Last: ...
#   Wrap-up pending until 16:52:53 (epoch 1790026373) — report for session ...
#
# Both conditions must hold.  If the status file is missing or unreadable the
# answer is no, which is the right default: do not act on a state you cannot
# observe.  The epoch is what keeps a daemon that died mid-window from blocking
# every future restart — the marker ages out by itself.
#
# Run it by hand to see what it thinks:  ride-watch/restart-ok.sh; echo $?
# Point it somewhere else for a test:    RIDE_WATCH_STATUS=/tmp/x.md ...
# Ask it about another moment:           RIDE_WATCH_NOW=$(date +%s) ...
set -u

STATUS="${RIDE_WATCH_STATUS:-$HOME/otp-debug-logs/ride-watch/current-ride.md}"

[ -r "$STATUS" ] || exit 1
grep -q '^No active trip' "$STATUS" || exit 1

# The furthest-out wrap-up deadline in the file, if any.
latest=$(sed -n 's/^Wrap-up pending until [^(]*(epoch \([0-9][0-9]*\)).*$/\1/p' \
         "$STATUS" | sort -rn | head -n 1)
[ -n "$latest" ] || exit 0

now="${RIDE_WATCH_NOW:-$(date +%s)}"
[ "$now" -gt "$latest" ] || exit 1
exit 0
