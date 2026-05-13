#!/bin/bash
#
# cron-gtfs-refresh.sh
#
# Unattended refresh of the Metro Transit GTFS feed + OTP graph rebuild + container
# restart. Intended to be run from cron (the Metro Transit GTFS feed expires roughly
# monthly, so run this on the 1st and 15th).
#
# Steps:
#   1. Back up data/gtfs.zip and data/graph.obj (timestamped, keep last few)
#   2. Download fresh GTFS from Metro Transit
#   3. Sync config/*.json into data/
#   4. Rebuild the graph (java --build --save) with the locally built shaded JAR
#   5. docker restart otp-minneapolis
#   6. Verify the backend's serviceTimeRange covers today
#
# All output goes to stdout/stderr; the cron entry redirects it to a log file.

set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
CONFIG_DIR="$REPO_ROOT/config"
GTFS_URL="https://svc.metrotransit.org/mtgtfs/gtfs.zip"
CONTAINER="otp-minneapolis"
OTP_URL="http://127.0.0.1:8090/otp/gtfs/v1"
KEEP_BACKUPS=3
TS="$(date +%Y%m%d-%H%M%S)"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "===== OTP GTFS refresh starting (run id $TS) ====="

JAR="$(find "$REPO_ROOT/OpentripPlanner/otp-shaded/target" -name 'otp-shaded-*.jar' -type f ! -name '*-sources.jar' 2>/dev/null | head -n1)"
if [ -z "$JAR" ]; then
  log "ERROR: OTP shaded JAR not found under OpentripPlanner/otp-shaded/target. Run scripts/build.sh first."
  exit 1
fi
log "Using JAR: $JAR"

# 1. Backup current data
if [ -f "$DATA_DIR/gtfs.zip" ]; then
  cp -p "$DATA_DIR/gtfs.zip" "$DATA_DIR/gtfs.zip.backup-$TS"
  log "Backed up gtfs.zip -> gtfs.zip.backup-$TS"
fi
if [ -f "$DATA_DIR/graph.obj" ]; then
  cp -p "$DATA_DIR/graph.obj" "$DATA_DIR/graph.obj.backup-$TS"
  log "Backed up graph.obj -> graph.obj.backup-$TS"
fi

# 2. Download fresh GTFS to a temp file, sanity-check, then swap in
TMP_GTFS="$DATA_DIR/gtfs.zip.download-$TS"
log "Downloading fresh GTFS from $GTFS_URL ..."
curl -fsSL --retry 3 --retry-delay 5 "$GTFS_URL" -o "$TMP_GTFS"
if ! unzip -tqq "$TMP_GTFS" >/dev/null 2>&1; then
  log "ERROR: downloaded GTFS is not a valid zip. Aborting (existing data untouched)."
  rm -f "$TMP_GTFS"
  exit 1
fi
FEED_END="$(unzip -p "$TMP_GTFS" feed_info.txt 2>/dev/null | awk -F, 'NR==2{print $6}')"
log "Downloaded GTFS OK ($(du -h "$TMP_GTFS" | cut -f1)); feed_end_date=${FEED_END:-unknown}"
mv -f "$TMP_GTFS" "$DATA_DIR/gtfs.zip"

# 3. Sync configs into data dir
if [ -d "$CONFIG_DIR" ]; then
  cp -f "$CONFIG_DIR/"*.json "$DATA_DIR/" 2>/dev/null || true
  log "Synced config/*.json into data/"
fi

# 4. Rebuild graph (overwrites data/graph.obj)
log "Rebuilding OTP graph (java -Xmx4G --build --save) ... this takes a few minutes"
java -Xmx4G -jar "$JAR" --build --save "$DATA_DIR"
log "Graph rebuilt: $(du -h "$DATA_DIR/graph.obj" | cut -f1)"

# 5. Restart the container so it loads the new graph
log "Restarting container $CONTAINER ..."
docker restart "$CONTAINER"

# 6. Wait for OTP to come back, then verify the loaded service range covers today
log "Waiting for OTP backend to come up ..."
up=0
for i in $(seq 1 60); do
  if curl -fsS -m 3 -o /dev/null "http://127.0.0.1:8090/otp/" 2>/dev/null; then up=1; break; fi
  sleep 5
done
if [ "$up" != "1" ]; then
  log "ERROR: OTP backend did not come up within ~5 minutes. Check 'docker logs $CONTAINER'."
  exit 1
fi

RANGE_JSON="$(curl -fsS -m 10 -X POST "$OTP_URL" -H 'Content-Type: application/json' \
  -d '{"query":"{serviceTimeRange{start end}}"}' || true)"
log "serviceTimeRange response: $RANGE_JSON"
START_TS="$(echo "$RANGE_JSON" | grep -o '"start":[0-9]*' | grep -o '[0-9]*' || true)"
END_TS="$(echo "$RANGE_JSON" | grep -o '"end":[0-9]*' | grep -o '[0-9]*' || true)"
NOW_TS="$(date +%s)"
if [ -n "$START_TS" ] && [ -n "$END_TS" ] && [ "$NOW_TS" -ge "$START_TS" ] && [ "$NOW_TS" -le "$END_TS" ]; then
  log "OK: loaded transit service range covers today ($(date -d @"$START_TS" +%F) .. $(date -d @"$END_TS" +%F))."
else
  log "WARNING: loaded service range does not appear to cover today. start=$START_TS end=$END_TS now=$NOW_TS"
fi

# 7. Prune old backups, keeping the newest $KEEP_BACKUPS of each kind
for pat in 'gtfs.zip.backup-*' 'graph.obj.backup-*'; do
  # shellcheck disable=SC2012
  ls -1t "$DATA_DIR"/$pat 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | while read -r f; do
    rm -f "$f" && log "Pruned old backup: $(basename "$f")"
  done
done

log "===== OTP GTFS refresh complete ====="
