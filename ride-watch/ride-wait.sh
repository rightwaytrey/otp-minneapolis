#!/usr/bin/env bash
# A ride thread's ONE-SHOT waiter (2026-10-10; replaces the Monitor on
# `tail -F <events file>`). The thread runs it with Bash run_in_background; it
# blocks until the events file holds line M or later, prints every line from M
# on, and EXITS. The thread handles them and starts it again with M one past
# the highest #N it has seen — nothing is replayed or skipped.
#
# Why (the rider, 2026-10-10: "such a needless waste of context"): a Monitor
# lasts at most 30 minutes, so a thread on a long or quiet ride was woken by
# "Monitor expired … no events" and had to re-arm. This one costs nothing
# while nothing happens.
#
#   ride-wait.sh <events file> <M>
set -u
F="${1:?usage: ride-wait.sh <events file> <M>}"; M="${2:?usage: ride-wait.sh <events file> <M>}"
case "$M" in ''|*[!0-9]*|0) echo "M must be a line number >= 1" >&2; exit 2 ;; esac
while :; do
  if [ -f "$F" ] && [ "$(wc -l < "$F")" -ge "$M" ]; then
    sleep 0.3   # a milestone and the next one often land together: one batch
    tail -n +"$M" "$F"; exit 0
  fi
  sleep 1
done
