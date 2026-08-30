#!/usr/bin/env bash
#
# deploy-app.sh — ship data, services and nginx to the TransitNav Linode.
#
# Idempotent: safe to re-run. Run configure-server.sh first.
#
#   ./deploy-app.sh <SERVER_IP>
#
# What deliberately does NOT go up:
#   - the OSM extract and GTFS zips  (build inputs; the graph is built on the
#     desktop, and router-config.json is the only config OTP needs to --load)
#   - the OpentripPlanner source tree (Dockerfile.runtime uses the prebuilt JAR)
#   - anything Pelias                 (geocoding is proxied to Stadia)
#   - graph.obj.backup-*, metro_transit_schedule.db, archive/
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TRANSITNAV="$HOME/projects/transitnav"

SERVER_IP="${1:-}"
[ -n "$SERVER_IP" ] || { echo "${RED}Usage: $0 <SERVER_IP>${NC}"; exit 1; }
[ -f "$SCRIPT_DIR/.env" ] || { echo "${RED}Error: .env not found${NC}"; exit 1; }
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"

for v in APP_USER DOMAIN APP_PORT STADIA_API_KEY HOME_TAILSCALE_IP UNLOCK_SECRET; do
  [ -n "${!v:-}" ] || { echo "${RED}Error: $v is empty in .env${NC}"; exit 1; }
done

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
RUSER="$APP_USER@$SERVER_IP"
RROOT="root@$SERVER_IP"
RREPO="/home/$APP_USER/projects/otp-minneapolis"
RTNAV="/home/$APP_USER/projects/transitnav"

JAR="$(find "$REPO_ROOT/OpentripPlanner/otp-shaded/target" -name 'otp-shaded-*.jar' -type f ! -name '*-sources.jar' 2>/dev/null | head -n1)"
[ -n "$JAR" ] || { echo "${RED}Error: OTP shaded JAR not found. Run scripts/build.sh${NC}"; exit 1; }
[ -f "$REPO_ROOT/data/graph.obj" ] || { echo "${RED}Error: data/graph.obj not found${NC}"; exit 1; }

echo "${GREEN}=== 1/7  Repo skeleton ===${NC}"
ssh "${SSH_OPTS[@]}" "$RUSER" "mkdir -p '$RREPO'/{data,config,docker,deployment/nginx,scripts} '$RTNAV'"

echo "${GREEN}=== 2/7  OTP graph + runtime config ===${NC}"
# router-config.json is the ONLY config OTP needs at runtime — it holds just the
# external Metro Transit and MVTA GTFS-RT updater URLs. build-config.json points
# at gtfs.zip and the OSM extract and is build-only, which is why neither ships.
rsync -az --info=progress2 "$REPO_ROOT/data/graph.obj" "$RUSER:$RREPO/data/graph.obj"
rsync -az "$REPO_ROOT/config/router-config.json" "$RUSER:$RREPO/config/"
rsync -az "$REPO_ROOT/config/build-config.json"  "$RUSER:$RREPO/config/"
# OTP is started as `--load /var/opentripplanner`, which is the DATA dir, and it
# reads router-config.json from there -- NOT from the /etc/otp mount. Miss this
# and OTP comes up healthy on defaults with all six GTFS-RT updaters silently
# absent: no trip updates, no vehicle positions, no alerts, for either feed.
# Go Mode is built on that realtime data, so the failure is severe and quiet.
# The desktop avoids it because run.sh and start-all.sh copy config/*.json into
# data/ on every start; this is that same step.
# Apply the server-side maxStopCount override before shipping, if set. Without
# this step every re-deploy would quietly restore the desktop's 20000 and the
# box would get ~3x slower with nothing in any log to say why.
RC_SRC="$REPO_ROOT/config/router-config.json"
if [ -n "${SERVER_MAX_STOP_COUNT:-}" ]; then
  RC_TUNED="$(mktemp)"; trap 'rm -f "$RC_TUNED"' EXIT
  python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
d['routingDefaults']['accessEgress']['maxStopCount']=int(sys.argv[2])
json.dump(d,open(sys.argv[3],'w'),indent=2)
" "$RC_SRC" "$SERVER_MAX_STOP_COUNT" "$RC_TUNED"
  echo "    maxStopCount override: $SERVER_MAX_STOP_COUNT (desktop keeps its own value)"
  RC_SRC="$RC_TUNED"
fi
rsync -az "$RC_SRC" "$RUSER:$RREPO/data/router-config.json"
rsync -az "$RC_SRC" "$RUSER:$RREPO/config/router-config.json"
rsync -az "$REPO_ROOT/config/build-config.json"  "$RUSER:$RREPO/data/"

echo "${GREEN}=== 3/7  OTP JAR + runtime image definition ===${NC}"
ssh "${SSH_OPTS[@]}" "$RUSER" "mkdir -p '$RREPO/OpentripPlanner/otp-shaded/target'"
rsync -az --info=progress2 "$JAR" "$RUSER:$RREPO/OpentripPlanner/otp-shaded/target/"
rsync -az "$REPO_ROOT/docker/Dockerfile.runtime" "$RUSER:$RREPO/docker/"
rsync -az "$SCRIPT_DIR/docker-compose.server.yml" "$RUSER:$RREPO/deployment/"

echo "${GREEN}=== 4/7  Flask sidecar + static web root ===${NC}"
rsync -az \
  "$TRANSITNAV/preferences_api.py" "$TRANSITNAV/onboard_api.py" \
  "$TRANSITNAV/build_gtfs_shapes.py" "$TRANSITNAV/gtfs_shapes.db" \
  "$TRANSITNAV/requirements-prefs-api.txt" \
  "$RUSER:$RTNAV/"
# The bundled app calls cross-origin from capacitor://localhost; the web UI on
# this host is same-origin. Both are covered, and the old tre.hopto.org entries
# are kept so nothing that still points there breaks during the cutover week.
sed -e "s#^PREFS_ALLOWED_ORIGIN=.*#PREFS_ALLOWED_ORIGIN=https://$DOMAIN:$APP_PORT,https://$DOMAIN,capacitor://localhost,https://tre.hopto.org,https://tre.hopto.org:$APP_PORT#" \
    "$TRANSITNAV/.env" | ssh "${SSH_OPTS[@]}" "$RUSER" "cat > '$RTNAV/.env' && chmod 600 '$RTNAV/.env'"
# The site file sets auth_basic for the web UI and names /etc/nginx/.htpasswd.
# If that file is absent nginx does not fall back to open -- it returns 403 for
# every gated request, which reads like a permissions bug rather than a missing
# file. Root-owned on the desktop, so it is read through a container (docker is
# passwordless here) and written straight to the server as root.
sudo -n docker run --rm -v /etc/nginx:/m:ro alpine:latest cat /m/.htpasswd 2>/dev/null \
  | ssh "${SSH_OPTS[@]}" "$RROOT" 'cat > /etc/nginx/.htpasswd && chmod 640 /etc/nginx/.htpasswd && chown root:www-data /etc/nginx/.htpasswd'
ssh "${SSH_OPTS[@]}" "$RROOT" 'test -s /etc/nginx/.htpasswd' \
  || { echo "${RED}Error: .htpasswd did not transfer${NC}"; exit 1; }

rsync -az --delete /var/www/transitnav/ "$RUSER:/tmp/transitnav-www/"
ssh "${SSH_OPTS[@]}" "$RROOT" "rsync -a --delete /tmp/transitnav-www/ /var/www/transitnav/ && chown -R $APP_USER:www-data /var/www/transitnav && rm -rf /tmp/transitnav-www"

echo "${GREEN}=== 5/7  Build and start OTP ===${NC}"
ssh "${SSH_OPTS[@]}" "$RUSER" bash -s <<REMOTE
set -euo pipefail
cd '$RREPO'
docker build -q -f docker/Dockerfile.runtime -t docker-otp . >/dev/null
docker compose -f deployment/docker-compose.server.yml up -d
REMOTE

echo "${GREEN}=== 6/7  prefs-api (systemd user unit) ===${NC}"
ssh "${SSH_OPTS[@]}" "$RUSER" bash -s <<REMOTE
set -euo pipefail
cd '$RTNAV'
[ -d venv-prefs ] || python3 -m venv venv-prefs
./venv-prefs/bin/pip install --quiet --upgrade pip
./venv-prefs/bin/pip install --quiet -r requirements-prefs-api.txt
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/prefs-api.service <<'UNIT'
[Unit]
Description=NL routing-preferences API (otp-react-redux)
# A user unit, matching the desktop deliberately: nothing it does needs root,
# and as a system unit every code change needed a sudo to take effect. gunicorn
# has no --reload, so a restart that did not happen silently ships nothing.
# Boot start depends on lingering, which configure-server.sh enables.

[Service]
Type=simple
WorkingDirectory=$RTNAV
EnvironmentFile=$RTNAV/.env
ExecStart=$RTNAV/venv-prefs/bin/gunicorn -w 2 --threads 8 -b 127.0.0.1:8092 preferences_api:app
Restart=on-failure
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable prefs-api >/dev/null 2>&1 || true
systemctl --user restart prefs-api
REMOTE

echo "${GREEN}=== 7/7  nginx ===${NC}"
# Substitute the deploy-time placeholders. The Stadia key lives only in .env and
# on the server; it must never be committed into config/nginx.
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
for f in otp-common.conf otp.conf; do
  sed -e "s#__STADIA_API_KEY__#$STADIA_API_KEY#g" \
      -e "s#__HOME_TAILSCALE_IP__#$HOME_TAILSCALE_IP#g" \
      -e "s#__UNLOCK_SECRET__#$UNLOCK_SECRET#g" \
      "$SCRIPT_DIR/nginx/$f" > "$TMP/$f"
  grep -q '__' "$TMP/$f" && { echo "${RED}Error: unsubstituted placeholder remains in $f${NC}"; exit 1; }
done

rsync -az "$TMP/otp.conf" "$TMP/otp-common.conf" "$RROOT:/tmp/"
ssh "${SSH_OPTS[@]}" "$RROOT" bash -s <<'REMOTE'
set -euo pipefail
# map_hash_bucket_size must be parsed before any map block.
echo 'map_hash_bucket_size 128;' > /etc/nginx/conf.d/00-map-hash.conf
install -D -m 644 /tmp/otp-common.conf /etc/nginx/snippets/otp-common.conf
install -D -m 644 /tmp/otp.conf /etc/nginx/sites-available/otp
ln -sfn /etc/nginx/sites-available/otp /etc/nginx/sites-enabled/otp
rm -f /etc/nginx/sites-enabled/default
rm -f /tmp/otp.conf /tmp/otp-common.conf
nginx -t
systemctl reload nginx
REMOTE

echo
echo "${GREEN}=== Health ===${NC}"
ssh "${SSH_OPTS[@]}" "$RUSER" bash -s <<'REMOTE'
echo -n "  OTP        : "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 20 http://127.0.0.1:8090/otp/ || echo unreachable
# "healthy" is not enough: OTP answers happily with no realtime feeds attached.
echo -n "  GTFS-RT    : "
curl -s --max-time 20 -H 'content-type: application/json' \
  -d '{"query":"{feeds{feedId realtimeVehicles: agencies{id}}}"}' \
  http://127.0.0.1:8090/otp/gtfs/v1 >/dev/null 2>&1 && echo "graph queryable" || echo "QUERY FAILED"
echo -n "  updaters   : "
docker logs otp-minneapolis 2>&1 | grep -ciE "GtfsRealtime|stop-time-updater|vehicle-positions" || echo 0
echo -n "  prefs-api  : "; curl -s --max-time 10 http://127.0.0.1:8092/api/health || echo unreachable
echo
free -h | sed 's/^/  /'
REMOTE

echo
echo "${YELLOW}Not yet live.${NC} $DOMAIN still resolves to the house."
echo "Verify through nginx on this box first:"
echo "  curl --resolve $DOMAIN:$APP_PORT:$SERVER_IP https://$DOMAIN:$APP_PORT/pelias/v1/autocomplete?text=target%20field"
echo "  curl -o /dev/null -w '%{http_code}\\n' -X POST -H 'content-type: application/json' -d '{\"text\":\"\"}' \\"
echo "       --resolve $DOMAIN:$APP_PORT:$SERVER_IP https://$DOMAIN:$APP_PORT/api/preferences   # expect 400, NOT 401"
