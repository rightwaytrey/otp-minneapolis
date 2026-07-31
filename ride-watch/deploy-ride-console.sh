#!/usr/bin/env bash
#
# deploy-ride-console.sh — publish the rider's mid-ride console to the web root
# so nginx serves it at https://tre.hopto.org:9966/ride.
#
# It is a single self-contained file, so "deploy" is a copy. The only subtlety
# is where it lands: the frontend's deploy-prod.sh rsyncs the built app into
# /var/www/transitnav with --delete, which would sweep this page away on the
# next frontend deploy. That script therefore calls this one at the end, and
# this script is safe to run any number of times.
#
# Usage:  ride-watch/deploy-ride-console.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ride-console.html"
WEBROOT=/var/www/transitnav
DEST="$WEBROOT/ride.html"

[ -f "$SRC" ] || { echo "ERROR: $SRC not found"; exit 1; }
[ -d "$WEBROOT" ] || { echo "ERROR: web root $WEBROOT not found"; exit 1; }

install -m 644 "$SRC" "$DEST"
echo "==> Deployed $(wc -c <"$DEST") bytes -> $DEST"
echo "    Live at https://tre.hopto.org:9966/ride"
