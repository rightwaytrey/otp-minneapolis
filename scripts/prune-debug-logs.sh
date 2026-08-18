#!/usr/bin/env bash
#
# prune-debug-logs.sh — compress and expire the Go Mode debug-log sink.
#
# Why: preferences_api.py's /api/debug-log writes one JSONL file per UTC day to
# ~/otp-debug-logs and never removes anything. It caps entries per request, but
# nothing caps the file, the directory, or the retention. By 2026-08-16 a single
# rider had accumulated 147 MB across 29 files (one 60 MB day) on a root
# filesystem sitting at 86% full with 31 GB free — and telemetry defaults ON in
# the native build, so every extra tester multiplies that. A full disk takes
# nginx, OTP and MariaDB down with it, so this is the cheapest insurance there
# is.
#
# The logs are JSONL and compress ~11x (the 60 MB day gzips to 5.3 MB), so
# compressing is nearly free and keeps the history browsable with zcat/zgrep.
#
# Deliberately NOT touched: ride-watch/, which holds per-ride findings, replies
# and current-ride.md. It is small (176 KB) and is read by the ride console and
# the post-ride triage notes.
#
# Cron:  30 4 * * *  /home/rwt/projects/otp-minneapolis/scripts/prune-debug-logs.sh
# (04:30 — after the 03:00 GTFS refresh, before the 05:00 verify suite.)
set -uo pipefail

LOG_DIR=${DEBUG_LOG_DIR:-/home/rwt/otp-debug-logs}
COMPRESS_AFTER_DAYS=${COMPRESS_AFTER_DAYS:-3}
# 90, not 30. Compression already took the directory from 147MB to 13MB, so
# deletion is no longer about space — it is only about bounding growth. The
# rider reads these back weeks later when triaging a ride (the 7/13, 7/29 and
# 8/02 post-mortems all did), and 30 days would have destroyed that material
# before it was used. 90 days deletes nothing that exists today (the oldest
# file is 63 days old) and still caps the directory. Tighten it here if other
# people's location traces start landing in it — a shorter window is the right
# answer once the logs stop being only the owner's own rides.
DELETE_AFTER_DAYS=${DELETE_AFTER_DAYS:-90}

[ -d "$LOG_DIR" ] || { echo "$(date -Is) no such dir: $LOG_DIR"; exit 0; }

before=$(du -sm "$LOG_DIR" 2>/dev/null | cut -f1)

# Compress closed days only. -mtime +N is "last modified more than N days ago",
# so today's file (still being appended to by gunicorn) is never touched.
# maxdepth 1 keeps ride-watch/ out of it.
compressed=$(find "$LOG_DIR" -maxdepth 1 -type f -name 'debug-*.jsonl' \
  -mtime +"$COMPRESS_AFTER_DAYS" -print -exec gzip -9 {} \; 2>/dev/null | wc -l)

deleted=$(find "$LOG_DIR" -maxdepth 1 -type f -name 'debug-*.jsonl.gz' \
  -mtime +"$DELETE_AFTER_DAYS" -print -delete 2>/dev/null | wc -l)

after=$(du -sm "$LOG_DIR" 2>/dev/null | cut -f1)
echo "$(date -Is) prune-debug-logs: compressed=$compressed deleted=$deleted ${before}MB -> ${after}MB"
