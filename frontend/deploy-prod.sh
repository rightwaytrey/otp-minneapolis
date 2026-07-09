#!/usr/bin/env bash
#
# deploy-prod.sh — build + publish the TransitNav / Go Mode frontend as a
# STATIC production bundle served by nginx at https://tre.hopto.org:9966.
#
# Why this exists: the phone (native shell) loads that URL. Serving a live Vite
# dev server there means HMR can reload — or freeze — the app mid-trip. This
# builds a static bundle instead: nothing to hot-reload, nothing to reconnect.
#
# Safe to run anytime. The running phone only picks up a new build on its next
# app launch / manual refresh, so it never yanks a page out from under a trip.
#
# Usage:  ./deploy-prod.sh
# Requires: docker access (build runs in the otp-frontend-dev container) and
# write access to the web root (set up once — owned by you:www-data).
set -euo pipefail

REPO=/home/rwt/projects/otprr/otp-react-redux
WEBROOT=/var/www/transitnav

echo "==> Building production bundle in the frontend container..."
docker exec -w /app -e YAML_CONFIG=/app/port-config.yml -e NODE_ENV=production \
  otp-frontend-dev yarn build

echo "==> Publishing dist/ -> $WEBROOT ..."
# --no-owner/--no-group: dist is built as root inside the container; we don't
# preserve that on the host. Content + timestamps only; --delete prunes stale
# hashed assets from previous builds.
rsync -rlptD --delete --no-owner --no-group "$REPO/dist/" "$WEBROOT/"
chmod -R u=rwX,go=rX "$WEBROOT"

echo "==> Deployed $(find "$WEBROOT" -type f | wc -l) files."
echo "    Live at https://tre.hopto.org:9966 — the app loads it on next launch/refresh."
