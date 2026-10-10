#!/usr/bin/env bash
# The router's event stream (2026-10-09, self-hosted board): presses that name
# no cycle — START triage — land in ~/.claude/pipeline/board/inbox.log via
# pipeline-route.sh board, and the router follows that with Monitor, exactly
# as a cycle thread follows its own inbox. One kind of line on stdout:
#
#   INBOX #K key=… thread=- text=<path> :: <first line>
#
# Never exits on its own; the Monitor's timeout is the router's 15-minute scan tick.
set -u
DIR="$HOME/.claude/pipeline/board"
mkdir -p "$DIR"; touch "$DIR/inbox.log" "$DIR/handled"
trap 'pkill -P $$ 2>/dev/null' EXIT
trap 'exit 0' INT TERM
H=$(sort -n "$DIR/handled" | tail -n1); M=$(( ${H:-0} + 1 ))
tail --pid=$$ -n +"$M" -F "$DIR/inbox.log" 2>/dev/null | sed -u 's/^/INBOX /' &
while :; do sleep 3600 & wait $!; done
