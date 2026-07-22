#!/bin/bash
#
# cron-gtfs-refresh.sh
#
# Unattended refresh of the Metro Transit + MVTA GTFS feeds, OTP graph rebuild,
# and container restart. Runs NIGHTLY from cron, but does real work only when a
# publisher has released a new feed_version — see step 2. Nightly matters
# because Metro Transit republishes about weekly and regenerates its trip_ids
# each time, which silently breaks GTFS-RT matching until the graph catches up.
#
# Steps:
#   1. Download both feeds to temp files and sanity-check the zips
#   2. Compare feed_version against what the current graph was built from;
#      exit early (nothing touched) if neither publisher has changed
#   3. Back up gtfs.zip / mvta-gtfs.zip / graph.obj, then swap the feeds in
#   4. Sync config/*.json into data/
#   5. Rebuild the graph (java --build --save) with the locally built shaded JAR
#   6. docker restart otp-minneapolis
#   7. Verify the backend's serviceTimeRange covers today
#   8. Prune old backups
#
# FORCE_REBUILD=1 skips the step-2 check (use after editing build-config.json,
# which the version check cannot see).
#
# All output goes to stdout/stderr; the cron entry redirects it to a log file.

set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
CONFIG_DIR="$REPO_ROOT/config"
GTFS_URL="https://svc.metrotransit.org/mtgtfs/gtfs.zip"
MVTA_GTFS_URL="https://srv.mvta.com/InfoPoint/gtfs-zip.ashx"
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

# Read one named column out of a feed's feed_info.txt. The two publishers order
# their columns differently (MVTA has no default_lang), so look the field up by
# header name rather than by position.
feed_field() {
  unzip -p "$1" feed_info.txt 2>/dev/null | awk -F, -v want="$2" '
    { gsub(/\r/, "") }
    NR == 1 { for (i = 1; i <= NF; i++) if ($i == want) col = i; next }
    NR == 2 && col { print $col; exit }
  '
}

# 1. Download both feeds to temp files and sanity-check them. Nothing existing
#    is touched until we know the downloads are good AND worth rebuilding for.
TMP_GTFS="$DATA_DIR/gtfs.zip.download-$TS"
log "Downloading fresh GTFS from $GTFS_URL ..."
curl -fsSL --retry 3 --retry-delay 5 "$GTFS_URL" -o "$TMP_GTFS"
if ! unzip -tqq "$TMP_GTFS" >/dev/null 2>&1; then
  log "ERROR: downloaded GTFS is not a valid zip. Aborting (existing data untouched)."
  rm -f "$TMP_GTFS"
  exit 1
fi
FEED_END="$(feed_field "$TMP_GTFS" feed_end_date)"
log "Downloaded GTFS OK ($(du -h "$TMP_GTFS" | cut -f1)); feed_end_date=${FEED_END:-unknown}"

# 1b. Download fresh MVTA GTFS (second transit feed) the same way
TMP_MVTA="$DATA_DIR/mvta-gtfs.zip.download-$TS"
log "Downloading fresh MVTA GTFS from $MVTA_GTFS_URL ..."
curl -fsSL --retry 3 --retry-delay 5 "$MVTA_GTFS_URL" -o "$TMP_MVTA"
if ! unzip -tqq "$TMP_MVTA" >/dev/null 2>&1; then
  log "ERROR: downloaded MVTA GTFS is not a valid zip. Aborting (existing data untouched)."
  rm -f "$TMP_GTFS" "$TMP_MVTA"
  exit 1
fi
MVTA_FEED_END="$(feed_field "$TMP_MVTA" feed_end_date)"
log "Downloaded MVTA GTFS OK ($(du -h "$TMP_MVTA" | cut -f1)); feed_end_date=${MVTA_FEED_END:-unknown}"

# 2. Rebuild only when a publisher has actually released a new feed_version.
#    Metro Transit republishes roughly WEEKLY and regenerates its trip_ids every
#    time, so a graph even a few days stale stops matching GTFS-RT: on
#    2026-07-22 a graph built from the 2026-07-11 feed resolved only 756/924
#    live vehicles (82%), while the then-current feed resolved 924/924. That is
#    why this now runs nightly — this check makes the ~6 nights a week with no
#    new feed cost two downloads instead of a 4-minute rebuild and a restart.
#    Set FORCE_REBUILD=1 to rebuild anyway (e.g. after changing build-config).
NEW_VERSION="$(feed_field "$TMP_GTFS" feed_version)"
OLD_VERSION="$(feed_field "$DATA_DIR/gtfs.zip" feed_version)"
NEW_MVTA_VERSION="$(feed_field "$TMP_MVTA" feed_version)"
OLD_MVTA_VERSION="$(feed_field "$DATA_DIR/mvta-gtfs.zip" feed_version)"
log "Metro Transit feed_version: have=${OLD_VERSION:-none} new=${NEW_VERSION:-none}"
log "MVTA feed_version:          have=${OLD_MVTA_VERSION:-none} new=${NEW_MVTA_VERSION:-none}"
if [ "${FORCE_REBUILD:-0}" != "1" ] &&
   [ -f "$DATA_DIR/graph.obj" ] &&
   [ -n "$NEW_VERSION" ] && [ "$NEW_VERSION" = "$OLD_VERSION" ] &&
   [ -n "$NEW_MVTA_VERSION" ] && [ "$NEW_MVTA_VERSION" = "$OLD_MVTA_VERSION" ]; then
  rm -f "$TMP_GTFS" "$TMP_MVTA"
  log "Both feeds unchanged since the current graph was built — nothing to do."
  log "===== OTP GTFS refresh complete (no rebuild needed) ====="
  exit 0
fi

# 3. Back up what we are about to replace, then swap the new feeds in.
if [ -f "$DATA_DIR/gtfs.zip" ]; then
  cp -p "$DATA_DIR/gtfs.zip" "$DATA_DIR/gtfs.zip.backup-$TS"
  log "Backed up gtfs.zip -> gtfs.zip.backup-$TS"
fi
if [ -f "$DATA_DIR/mvta-gtfs.zip" ]; then
  cp -p "$DATA_DIR/mvta-gtfs.zip" "$DATA_DIR/mvta-gtfs.zip.backup-$TS"
  log "Backed up mvta-gtfs.zip -> mvta-gtfs.zip.backup-$TS"
fi
if [ -f "$DATA_DIR/graph.obj" ]; then
  cp -p "$DATA_DIR/graph.obj" "$DATA_DIR/graph.obj.backup-$TS"
  log "Backed up graph.obj -> graph.obj.backup-$TS"
fi
mv -f "$TMP_GTFS" "$DATA_DIR/gtfs.zip"
mv -f "$TMP_MVTA" "$DATA_DIR/mvta-gtfs.zip"

# 4. Sync configs into data dir
if [ -d "$CONFIG_DIR" ]; then
  cp -f "$CONFIG_DIR/"*.json "$DATA_DIR/" 2>/dev/null || true
  log "Synced config/*.json into data/"
fi

# 5. Rebuild graph (overwrites data/graph.obj)
log "Rebuilding OTP graph (java -Xmx4G --build --save) ... this takes a few minutes"
java -Xmx4G -jar "$JAR" --build --save "$DATA_DIR"
log "Graph rebuilt: $(du -h "$DATA_DIR/graph.obj" | cut -f1)"

# 6. Restart the container so it loads the new graph
log "Restarting container $CONTAINER ..."
docker restart "$CONTAINER"

# 7. Wait for OTP to come back, then verify the loaded service range covers today
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

# 8. Prune old backups, keeping the newest $KEEP_BACKUPS of each kind
for pat in 'gtfs.zip.backup-*' 'mvta-gtfs.zip.backup-*' 'graph.obj.backup-*'; do
  # shellcheck disable=SC2012
  ls -1t "$DATA_DIR"/$pat 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | while read -r f; do
    rm -f "$f" && log "Pruned old backup: $(basename "$f")"
  done
done

log "===== OTP GTFS refresh complete ====="
