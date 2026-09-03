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
#     desktop, and router-config.json + otp-config.json are the only configs
#     OTP needs to --load)
#   - the OpentripPlanner source tree (Dockerfile.runtime uses the prebuilt JAR)
#   - anything Pelias                 (geocoding is proxied to Stadia)
#   - graph.obj.backup-*, metro_transit_schedule.db, archive/
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TRANSITNAV="$HOME/projects/transitnav"

SERVER_IP="${1:-}"
[ -n "$SERVER_IP" ] || {
  echo "${RED}Usage: $0 <SERVER_IP> [--only step,step] [--skip step,step] [--yes-www]${NC}"
  echo "  steps: repo graph jar web otp prefs nginx   (default: all)"
  echo "  e.g.:  $0 1.2.3.4 --only nginx      # install the snippet, touch nothing else"
  exit 1
}
shift

# Step selection. This script used to be all-or-nothing, and that was a trap:
# on 2026-08-31 a session needed only two new nginx locations and would have
# shipped the desktop's entire /var/www build to production to get them --
# 34 deletions and 215 changed files, including unreleased place-editor work.
# Adding an nginx location must not be able to publish a frontend.
ALL_STEPS="repo graph jar web otp prefs nginx"
ONLY=""; SKIP=""; YES_WWW=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="${2//,/ }"; shift 2 ;;
    --skip) SKIP="${2//,/ }"; shift 2 ;;
    --yes-www) YES_WWW=1; shift ;;
    *) echo "${RED}Unknown argument: $1${NC}"; exit 1 ;;
  esac
done
for s in $ONLY $SKIP; do
  case " $ALL_STEPS " in *" $s "*) ;; *) echo "${RED}Unknown step: $s${NC}"; exit 1 ;; esac
done
want() {
  [ -z "$ONLY" ] || case " $ONLY " in *" $1 "*) ;; *) return 1 ;; esac
  case " $SKIP " in *" $1 "*) return 1 ;; esac
  return 0
}
[ -n "$ONLY$SKIP" ] && echo "${YELLOW}Steps: ${ONLY:-$ALL_STEPS}${SKIP:+ (minus: $SKIP)}${NC}"
[ -f "$SCRIPT_DIR/.env" ] || { echo "${RED}Error: .env not found${NC}"; exit 1; }
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"

# RIDE_UPSTREAM is in this list because the nginx render consumes it below.
# deployment/.env defines it by reference to HOME_TAILSCALE_IP -- /api/ride-note
# and /api/ride-status proxy to the DESKTOP, where ride-watch actually runs --
# but it is a separate knob, because the house render points it at 127.0.0.1 and
# a future staging box may point it somewhere else again. Empty here means the
# renderer fails closed on a leftover placeholder, so catch it before the ssh.
for v in APP_USER DOMAIN APP_PORT STADIA_API_KEY HOME_TAILSCALE_IP UNLOCK_SECRET RIDE_UPSTREAM; do
  [ -n "${!v:-}" ] || { echo "${RED}Error: $v is empty in .env${NC}"; exit 1; }
done

# One EXIT trap for the whole script, over a list of paths. bash keeps only the
# LAST `trap ... EXIT` that is installed, so the two handlers this script used to
# set -- the tuned router-config.json in the graph step and the nginx render dir
# in the nginx step -- clobbered each other: the nginx trap replaced the graph
# trap, and the tuned config leaked in /tmp on every full deploy with
# SERVER_MAX_STOP_COUNT set. Steps now register their temp paths instead of
# installing traps of their own. (backlog 2.23)
CLEANUP_PATHS=()
cleanup() { [ "${#CLEANUP_PATHS[@]}" -eq 0 ] || rm -rf "${CLEANUP_PATHS[@]}"; }
cleanup_add() { CLEANUP_PATHS+=("$@"); }
trap cleanup EXIT

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
RUSER="$APP_USER@$SERVER_IP"
RROOT="root@$SERVER_IP"
RREPO="/home/$APP_USER/projects/otp-minneapolis"
RTNAV="/home/$APP_USER/projects/transitnav"

# awk, not `| head -n1`: under `set -euo pipefail` head closes the pipe on the
# second match, find takes SIGPIPE, and the whole `$(...)` assignment aborts the
# script at 141. One shaded JAR hides it; a version bump leaves two. `sort` also
# makes the pick deterministic instead of directory order. (backlog 2.21/2.15)
JAR="$(find "$REPO_ROOT/OpentripPlanner/otp-shaded/target" -name 'otp-shaded-*.jar' -type f ! -name '*-sources.jar' 2>/dev/null | sort | awk 'NR==1')"
[ -n "$JAR" ] || { echo "${RED}Error: OTP shaded JAR not found. Run scripts/build.sh${NC}"; exit 1; }
[ -f "$REPO_ROOT/data/graph.obj" ] || { echo "${RED}Error: data/graph.obj not found${NC}"; exit 1; }

if want repo; then
echo "${GREEN}=== 1/7  Repo skeleton ===${NC}"
ssh "${SSH_OPTS[@]}" "$RUSER" "mkdir -p '$RREPO'/{data,config,docker,deployment/nginx,scripts} '$RTNAV'"
# The manifest is written ON the target (see scripts/deploy-manifest.py for why
# a git checkout there cannot answer "what is deployed?"), so the recorder has
# to live there too.
rsync -az "$REPO_ROOT/scripts/deploy-manifest.py" "$RUSER:$RREPO/scripts/"
fi

if want graph; then
echo "${GREEN}=== 2/7  OTP graph + runtime config ===${NC}"
# router-config.json and otp-config.json are the configs OTP needs at runtime —
# the first holds the external Metro Transit and MVTA GTFS-RT updater URLs plus
# the vector-tile layers, the second turns the sandbox APIs on.
# build-config.json points at gtfs.zip and the OSM extract and is build-only,
# which is why neither of those ships.
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
  RC_TUNED="$(mktemp)"; cleanup_add "$RC_TUNED"
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
# otp-config.json turns SANDBOX features on. It is a second runtime config, and
# it goes to the same DATA dir for the same reason router-config.json does:
# `--load /var/opentripplanner` is where OTP looks. Without it OTP logs
# "'/var/opentripplanner/otp-config.json' is not present. Using default
# configuration." and every sandbox feature stays off -- including the vector
# tile API that serves the stop layer, whose endpoint then 404s while
# router-config.json's `vectorTiles` block sits there parsed and unused.
# The desktop gets this free: run.sh/start-all.sh/build-graph.sh already
# `cp config/*.json data/`.
rsync -az "$REPO_ROOT/config/otp-config.json"    "$RUSER:$RREPO/data/"
rsync -az "$REPO_ROOT/config/otp-config.json"    "$RUSER:$RREPO/config/"
fi

if want jar; then
echo "${GREEN}=== 3/7  OTP JAR + runtime image definition ===${NC}"
ssh "${SSH_OPTS[@]}" "$RUSER" "mkdir -p '$RREPO/OpentripPlanner/otp-shaded/target'"
rsync -az --info=progress2 "$JAR" "$RUSER:$RREPO/OpentripPlanner/otp-shaded/target/"
rsync -az "$REPO_ROOT/docker/Dockerfile.runtime" "$RUSER:$RREPO/docker/"
rsync -az "$SCRIPT_DIR/docker-compose.server.yml" "$RUSER:$RREPO/deployment/"
fi

if want web; then
echo "${GREEN}=== 4/7  Flask sidecar + static web root ===${NC}"
rsync -az \
  "$TRANSITNAV/preferences_api.py" "$TRANSITNAV/onboard_api.py" \
  "$TRANSITNAV/build_gtfs_shapes.py" "$TRANSITNAV/gtfs_shapes.db" \
  "$TRANSITNAV/requirements-prefs-api.txt" \
  "$RUSER:$RTNAV/"
# The desktop's transitnav/.env is the source for the server's, but it is no
# longer a straight copy with one CORS line rewritten: since the house grew a
# staging lane of its own it also carries keys that say WHICH BOX YOU ARE, and
# those must not travel. render_server_env owns that transform and prints what
# it removed; deployment/test-server-env.sh tests it without needing an ssh.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/server-env.sh"
render_server_env "$TRANSITNAV/.env" "$DOMAIN" "$APP_PORT" \
  | ssh "${SSH_OPTS[@]}" "$RUSER" "cat > '$RTNAV/.env' && chmod 600 '$RTNAV/.env'"
# The site file sets auth_basic for the web UI and names /etc/nginx/.htpasswd.
# If that file is absent nginx does not fall back to open -- it returns 403 for
# every gated request, which reads like a permissions bug rather than a missing
# file. Root-owned on the desktop, so it is read through a container (docker is
# passwordless here) and written straight to the server as root.
# Seed it only when the server has none. This used to overwrite unconditionally
# from the desktop's copy, which silently reverts any credential added on the
# server since the last deploy.
if ssh "${SSH_OPTS[@]}" "$RROOT" 'test -s /etc/nginx/.htpasswd'; then
  echo "  .htpasswd already present on the server, left alone"
else
sudo -n docker run --rm -v /etc/nginx:/m:ro alpine:latest cat /m/.htpasswd 2>/dev/null \
  | ssh "${SSH_OPTS[@]}" "$RROOT" 'cat > /etc/nginx/.htpasswd && chmod 640 /etc/nginx/.htpasswd && chown root:www-data /etc/nginx/.htpasswd'
fi
ssh "${SSH_OPTS[@]}" "$RROOT" 'test -s /etc/nginx/.htpasswd' \
  || { echo "${RED}Error: .htpasswd did not transfer${NC}"; exit 1; }

# PUBLISHING THE FRONTEND. /var/www/transitnav on this desktop is a build
# output directory, not a release: whatever the last local build left there is
# what --delete makes production match, unreleased work included. So say what
# the mirror would do and make the destructive case opt in.
#
# The count comes from the real rsync in dry-run against the server's own tree,
# because the desktop cannot otherwise know what production is currently
# serving.
WWW_PLAN="$(rsync -az --delete --dry-run --itemize-changes \
  -e "ssh ${SSH_OPTS[*]}" /var/www/transitnav/ "$RUSER:/var/www/transitnav/" 2>/dev/null || true)"
WWW_DELETES="$(printf '%s\n' "$WWW_PLAN" | grep -c '^\*deleting' || true)"
WWW_CHANGES="$(printf '%s\n' "$WWW_PLAN" | grep -cv '^\*deleting' || true)"
echo "  frontend mirror: ${WWW_CHANGES} new/changed, ${WWW_DELETES} deleted"
# Gate on ANY difference, not just deletions. The first version of this guard
# only refused when files would be removed, which left the case that actually
# happened wide open: a desktop build with 215 new/changed files and nothing to
# delete publishes an entire unreleased UI in silence. Deletions are the loud
# symptom of divergence, not the definition of it -- writes publish just as
# much. Zero and zero still proceeds without ceremony, because a mirror that
# changes nothing IS nothing.
if [ "$((WWW_DELETES + WWW_CHANGES))" -gt 0 ] && [ "$YES_WWW" -ne 1 ]; then
  echo "${RED}Refusing to publish the frontend: ${WWW_CHANGES} file(s) would be written and ${WWW_DELETES} DELETED on production.${NC}"
  echo "${YELLOW}The desktop build differs from what is live, so this would publish whatever"
  echo "the last local build left in /var/www/transitnav. Check you are shipping what you"
  echo "think you are, then re-run with --yes-www, or --skip web to leave it alone.${NC}"
  # awk, not `| head -5`: under `set -euo pipefail` head closes the pipe, the
  # upstream grep takes SIGPIPE, and the script dies at 141 having printed one
  # list instead of two. Same defect as backlog 2.15 in install-house-nginx.sh.
  printf '%s\n' "$WWW_PLAN" | grep '^\*deleting' | awk 'NR<=5' || true
  printf '%s\n' "$WWW_PLAN" | grep -v '^\*deleting' | awk 'NR<=5' || true
  exit 1
fi
rsync -az --delete /var/www/transitnav/ "$RUSER:/tmp/transitnav-www/"
ssh "${SSH_OPTS[@]}" "$RROOT" "rsync -a --delete /tmp/transitnav-www/ /var/www/transitnav/ && chown -R $APP_USER:www-data /var/www/transitnav && rm -rf /tmp/transitnav-www"
fi

if want otp; then
echo "${GREEN}=== 5/7  Build and start OTP ===${NC}"
# --force-recreate, and then an assertion that it actually took.
#
# 2026-09-02 17:25 CDT, `--only jar,otp,graph`: the new shaded JAR and
# router-config.json both landed, `docker build -t docker-otp` produced a new
# image, and `docker compose up -d` printed nothing and left otp-minneapolis at
# `Up 10 hours` on the PREVIOUS image. The deploy reported success; OTP was
# serving the old JAR from a container created ten hours earlier, so the new
# itinerary-filter key was on disk and ignored. Nothing in any log said so.
# (backlog 2.27)
#
# Why plain `up -d` is not enough here: compose only recreates a container it
# considers changed, and on this box it did not consider a same-tag rebuild a
# change. It is NOT reproducible on the desktop -- measured 2026-09-02, Docker
# 28.3 / overlay2 / Compose 2.29 does recreate on a same-tag rebuild. The Linode
# is Docker 29.7 with the containerd image store and Compose v5.5, where the
# container's `com.docker.compose.image` label and its `.Image` are two
# different digests of the same image; which of them a given compose version
# compares is not something this script should have to know. So force it:
# this service is one JVM reading a bind-mounted graph, the recreate costs about
# 40 s of downtime, and it only happens when the `otp` step was asked for.
#
# The assertion is the part that must never be dropped. A deploy that ships a
# JAR the running process is not executing is worse than a deploy that fails,
# because the next session reads "deployed" and debugs the wrong thing.
ssh "${SSH_OPTS[@]}" "$RUSER" bash -s <<REMOTE
set -euo pipefail
cd '$RREPO'
docker build -q -f docker/Dockerfile.runtime -t docker-otp . >/dev/null
BUILT="\$(docker image inspect docker-otp --format '{{.Id}}')"
docker compose -f deployment/docker-compose.server.yml up -d --force-recreate otp
RUNNING="\$(docker inspect --format '{{.Image}}' otp-minneapolis)"
echo "  image built  : \${BUILT#sha256:}"
echo "  image running: \${RUNNING#sha256:}"
if [ "\$BUILT" != "\$RUNNING" ]; then
  echo "ERROR: otp-minneapolis is NOT running the image this deploy just built." >&2
  echo "       The JAR and router-config.json on the box are not what OTP loaded." >&2
  echo "       Recreate it by hand and find out why compose declined:" >&2
  echo "         docker compose -f deployment/docker-compose.server.yml up -d --force-recreate otp" >&2
  exit 1
fi
REMOTE
fi

if want prefs; then
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
fi

if want nginx; then
echo "${GREEN}=== 7/7  nginx ===${NC}"
# RENDER, NEVER COPY. There is one template per file and one value set per
# environment (deployment/nginx/*.tmpl + deployment/env/prod.env); render-nginx.py
# substitutes the secrets out of .env and fails closed on any leftover `__`
# placeholder. This script is the ONE owner of nginx on the server: nothing else
# may put a file under /etc/nginx, in either direction. The repo copies hold
# placeholders and the live files hold real secrets, so a `cp` either way is a
# breakage -- and one direction is how the unlock secret reached a public repo.
TMP="$(mktemp -d)"; cleanup_add "$TMP"
# Parity between the two environments before anything is installed: shared
# locations must be byte-identical (this replaced scripts/check-nginx-parity.py).
# --check has two halves: shared-location parity, and the guard that refuses to
# let a real credential live in a committed file. The second half needs
# deployment/.env, and when that is absent it exits 75 SKIP rather than 0 --
# because "no secret leaked" and "nothing to compare against" are not the same
# green line (backlog 2.20). A deploy host always has .env, so a SKIP *here*
# means the file has gone missing, and that must stop the deploy, not pass it.
NGINX_CHECK_RC=0
NGINX_CHECK_OUT="$(python3 "$SCRIPT_DIR/render-nginx.py" --check 2>&1)" || NGINX_CHECK_RC=$?
if [ "$NGINX_CHECK_RC" -eq 75 ]; then
  printf '%s\n' "$NGINX_CHECK_OUT"
  echo "${RED}Error: render-nginx.py --check skipped its committed-secret guard (exit 75).${NC}"
  echo "${YELLOW}deployment/.env is missing on this machine, so the guard had no values to"
  echo "compare. Restore it and re-run; do not deploy nginx unverified.${NC}"
  exit 1
elif [ "$NGINX_CHECK_RC" -ne 0 ]; then
  printf '%s\n' "$NGINX_CHECK_OUT"
  echo "${RED}Error: rendered house/prod configs are not in parity; run deployment/render-nginx.py --check${NC}"
  exit 1
fi
RIDE_UPSTREAM="$RIDE_UPSTREAM" STADIA_API_KEY="$STADIA_API_KEY" UNLOCK_SECRET="$UNLOCK_SECRET" \
  python3 "$SCRIPT_DIR/render-nginx.py" --env prod --out "$TMP" >/dev/null \
  || { echo "${RED}Error: rendering the prod nginx config failed${NC}"; exit 1; }

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
echo "${YELLOW}Not yet live.${NC} $DOMAIN still resolves to the house."
echo "Verify through nginx on this box first:"
echo "  curl --resolve $DOMAIN:$APP_PORT:$SERVER_IP https://$DOMAIN:$APP_PORT/pelias/v1/autocomplete?text=target%20field"
echo "  curl -o /dev/null -w '%{http_code}\\n' -X POST -H 'content-type: application/json' -d '{\"text\":\"\"}' \\"
echo "       --resolve $DOMAIN:$APP_PORT:$SERVER_IP https://$DOMAIN:$APP_PORT/api/preferences   # expect 400, NOT 401"
fi

# --- Health ---------------------------------------------------------------
# OUTSIDE `if want nginx`, deliberately. This block used to sit inside it, so
# `--only prefs`, `--only web` and `--skip nginx` deployed and then printed
# nothing at all -- including the prefs-api and GTFS-RT-updater probes, which
# have nothing to do with nginx. A step that reports must not be welded to an
# unrelated step (backlog 2.14; same shape as 2.9).
#
# It probes what is on the box, not what this run shipped, so it is honest
# after a partial deploy: `--only prefs` still tells you whether OTP is up and
# whether its updaters attached, which is exactly what you want to know after
# restarting a sidecar next to them.
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
# Say, on EVERY run, whether the container is the image the docker-otp tag now
# names -- not just on the runs that rebuilt it. `--only prefs` on a box whose
# OTP is a rebuild behind is exactly the state backlog 2.27 hid for hours, and
# it is invisible in "Up 10 hours (healthy)": a stale container is healthy.
# This prints and does not exit non-zero. The `otp` step is where a mismatch
# fails the deploy; making the health ssh exit 1 would kill the script under
# `set -e` before the manifest is recorded, which is backlog 2.19 again.
echo -n "  otp image  : "
IMG_TAG="$(docker image inspect docker-otp --format '{{.Id}}' 2>/dev/null || echo none)"
IMG_RUN="$(docker inspect --format '{{.Image}}' otp-minneapolis 2>/dev/null || echo none)"
if [ "$IMG_TAG" = "$IMG_RUN" ]; then
  echo "${IMG_RUN#sha256:} (container matches the docker-otp tag)"
else
  echo "MISMATCH  tag=${IMG_TAG#sha256:}  container=${IMG_RUN#sha256:}"
  echo "               the running container was not created from the current"
  echo "               image -- re-run deploy-app.sh <host> --only otp"
fi
echo -n "  prefs-api  : "; curl -s --max-time 10 http://127.0.0.1:8092/api/health || echo unreachable
echo
free -h | sed 's/^/  /'
REMOTE

# --- What did this actually put on the box? --------------------------------
# Deploy here is file copying, not a checkout: `git rev-parse` fails in both
# ~/projects/transitnav and ~/projects/otp-minneapolis on the server, the nginx
# config that runs is RENDERED (it holds substituted secrets and can never be a
# tracked file), only five files of the transitnav repo ship at all, and
# router-config.json is rewritten in flight by SERVER_MAX_STOP_COUNT. So the
# only honest answer to "what runs?" is the bytes on the box plus the shas they
# were built from. Record both, on the target, every time.
#
# Read it back later with:
#   scripts/deploy-manifest.py show   --ssh $APP_USER@<tailnet-ip>
#   scripts/deploy-manifest.py verify --ssh $APP_USER@<tailnet-ip>
# `verify` is the one that earns its keep: it catches a file hand-edited on the
# box after the deploy that recorded it.
echo
echo "${GREEN}=== Deploy manifest ===${NC}"
PROVENANCE="$(python3 "$REPO_ROOT/scripts/deploy-manifest.py" provenance \
  --repo "otp-minneapolis=$REPO_ROOT" \
  --repo "transitnav=$TRANSITNAV" \
  --repo "otprr=$HOME/projects/otprr/otp-react-redux")"
# --only nginx skips the repo step, so make sure the recorder is present.
rsync -az "$REPO_ROOT/scripts/deploy-manifest.py" "$RUSER:$RREPO/scripts/"
# ssh does NOT pass argv through: it joins its arguments with single spaces and
# hands one string to the REMOTE shell, which re-parses it. $PROVENANCE is JSON
# -- spaces, double quotes, braces -- so the local quoting was stripped here and
# the remote shell word-split it, argparse died with "unrecognized arguments",
# and `set -e` killed the deploy at the very last step. That is why NEITHER box
# had a manifest after real deploys on 2026-09-02 (11:58 and 12:39 both reached
# the rsync one line above and died on the line below). Quote the command for
# the remote shell explicitly. (backlog 2.19)
RECORD_ARGV=(
  python3 "$RREPO/scripts/deploy-manifest.py" record
  --target prod
  --steps "$(echo "${ONLY:-$ALL_STEPS}" | tr ' ' ',')"
  --provenance "$PROVENANCE"
  --file /etc/nginx/snippets/otp-common.conf
  --file /etc/nginx/sites-available/otp
  --file /etc/nginx/conf.d/00-map-hash.conf
  --file "$RTNAV/preferences_api.py"
  --file "$RTNAV/onboard_api.py"
  --file "$RREPO/data/router-config.json"
  --file "$RREPO/data/otp-config.json"
  --file "$RREPO/data/graph.obj"
  # The shaded JAR. It was the one artefact the manifest did not record, and it
  # is half of what decides how OTP routes -- backlog 2.27 is precisely the case
  # where the JAR on the box was right and every recorded file agreed, while the
  # process serving traffic was running a different one. Too large to digest, so
  # this is size+mtime; rsync -a preserves the desktop's mtime, which is what
  # identifies the build. The name comes from the local $JAR because the remote
  # path is the same file under the same name.
  --file "$RREPO/OpentripPlanner/otp-shaded/target/$(basename "$JAR")"
  # The two image ids, recorded beside the files. Every recorded FILE agreed with
  # the manifest on 2026-09-02 while the process serving traffic ran a different
  # image, because compose had declined to recreate the container (backlog 2.27);
  # files can never show that, and these two ids can. `verify` prints them and
  # does not grade them -- the container is recreated by ordinary operations, and
  # scripts/check-otp-image.py is the thing that asks whether the ids still
  # agree, nightly, on both boxes (backlog 2.28).
  --docker-image docker-otp
  --docker-container otp-minneapolis
)
printf -v RECORD_CMD '%q ' "${RECORD_ARGV[@]}"
ssh "${SSH_OPTS[@]}" "$RUSER" "$RECORD_CMD"
ssh "${SSH_OPTS[@]}" "$RUSER" "cat '$RREPO/deployment/deploy-manifest.json'" | sed 's/^/  /'
