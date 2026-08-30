#!/usr/bin/env bash
#
# verify-server.sh — prove the Linode is serving correctly BEFORE any DNS change.
#
# Every public check uses --resolve, so it exercises the real hostname, the real
# certificate and the real nginx config while api.transit-nav.com still points at
# the house. Nothing here mutates anything.
#
#   ./verify-server.sh <SERVER_IP>
#
# Exit non-zero if any REQUIRED check fails.
set -uo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"
IP="${1:-}"
[ -n "$IP" ] || { echo "Usage: $0 <SERVER_IP>"; exit 1; }

BASE="https://$DOMAIN:$APP_PORT"
RES=(--resolve "$DOMAIN:$APP_PORT:$IP")
FAILED=0
SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes)

ok()   { printf '  %s✓%s %s\n' "$GREEN" "$NC" "$1"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$NC" "$1"; FAILED=$((FAILED+1)); }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$NC" "$1"; }

echo "=== 1. Backends on the box (loopback) ==="
code=$("${SSH[@]}" "rwt@$IP" "curl -s -o /dev/null -w '%{http_code}' --max-time 20 http://127.0.0.1:8090/otp/" 2>/dev/null)
[ "$code" = "200" ] && ok "OTP :8090 -> 200" || bad "OTP :8090 -> ${code:-unreachable}"
h=$("${SSH[@]}" "rwt@$IP" "curl -s --max-time 10 http://127.0.0.1:8092/api/health" 2>/dev/null)
[ -n "$h" ] && ok "prefs-api :8092 -> $h" || bad "prefs-api :8092 unreachable"

echo
echo "=== 2. Realtime feeds actually attached ==="
# OTP answers 200 with no updaters at all, so this is checked explicitly: a graph
# serving stale schedule with no GTFS-RT is the quiet failure mode that matters.
n=$("${SSH[@]}" "rwt@$IP" "docker logs otp-minneapolis 2>&1 | grep -ciE 'stop-time-updater|vehicle-positions|real-time-alerts|GtfsRealtime'" 2>/dev/null)
[ "${n:-0}" -gt 0 ] && ok "updater activity in OTP log ($n lines)" || bad "NO GTFS-RT updater activity — check data/router-config.json exists on the box"
"${SSH[@]}" "rwt@$IP" "test -f /home/rwt/projects/otp-minneapolis/data/router-config.json" 2>/dev/null \
  && ok "data/router-config.json present (the file OTP --load actually reads)" \
  || bad "data/router-config.json MISSING — OTP is running on defaults"

echo
echo "=== 3. Graph covers today ==="
today=$(date +%Y-%m-%d)
str=$("${SSH[@]}" "rwt@$IP" "curl -s --max-time 20 -H 'content-type: application/json' -d '{\"query\":\"{serviceTimeRange{start end}}\"}' http://127.0.0.1:8090/otp/gtfs/v1" 2>/dev/null)
python3 - "$str" "$today" <<'PY' && ok "serviceTimeRange covers $today" || bad "serviceTimeRange does NOT cover $today"
import json,sys,datetime
try:
    d=json.loads(sys.argv[1])["data"]["serviceTimeRange"]
except Exception:
    sys.exit(1)
s=datetime.datetime.fromtimestamp(d["start"]).date()
e=datetime.datetime.fromtimestamp(d["end"]).date()
t=datetime.date.fromisoformat(sys.argv[2])
print(f"      {s} .. {e}")
sys.exit(0 if s <= t <= e else 1)
PY

echo
echo "=== 4. Through nginx on the real hostname (DNS still points home) ==="
code=$(curl -s "${RES[@]}" -o /dev/null -w '%{http_code}' --max-time 25 -H 'content-type: application/json' \
  -d '{"query":"{serviceTimeRange{start end}}"}' "$BASE/otp/gtfs/v1")
[ "$code" = "200" ] && ok "/otp/gtfs/v1 -> 200" || bad "/otp/gtfs/v1 -> $code"

lbl=$(curl -s "${RES[@]}" --max-time 25 "$BASE/pelias/v1/autocomplete?text=target%20field&focus.point.lat=44.98&focus.point.lon=-93.27" \
  | python3 -c "import sys,json;f=json.load(sys.stdin).get('features');print(f[0]['properties']['label'] if f else '')" 2>/dev/null)
[ -n "$lbl" ] && ok "/pelias/v1/autocomplete -> $lbl" || bad "/pelias/v1/autocomplete returned nothing"

code=$(curl -s "${RES[@]}" -o /dev/null -w '%{http_code}' --max-time 25 "$BASE/pelias/v1/reverse?point.lat=44.98&point.lon=-93.27&size=1")
[ "$code" = "200" ] && ok "/pelias/v1/reverse -> 200" || bad "/pelias/v1/reverse -> $code"

code=$(curl -s "${RES[@]}" -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/api/onboard/health")
[ "$code" = "200" ] && ok "/api/onboard/health -> 200" || bad "/api/onboard/health -> $code"

# 400 not 401. A 401 means the config re-gated a route the bundled app calls
# cross-origin and cannot authenticate; a 404 means a location block is missing.
# Empty text is rejected before any Anthropic call, so this costs nothing.
code=$(curl -s "${RES[@]}" -o /dev/null -w '%{http_code}' --max-time 20 -X POST \
  -H 'content-type: application/json' -d '{"text":""}' "$BASE/api/preferences")
case "$code" in
  400) ok "/api/preferences -> 400 (public, as required)" ;;
  401) bad "/api/preferences -> 401 — route got re-gated; the app CANNOT authenticate" ;;
  404) bad "/api/preferences -> 404 — nginx snippet is missing the location block" ;;
  *)   bad "/api/preferences -> $code (expected 400)" ;;
esac

code=$(curl -s "${RES[@]}" -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/api/debug-log" -X POST -H 'content-type: application/json' -d '{}')
[ "$code" != "401" ] && [ "$code" != "404" ] && ok "/api/debug-log reachable ($code)" || bad "/api/debug-log -> $code"

echo
echo "=== 5. CORS for the bundled app (capacitor://localhost) ==="
acao=$(curl -s "${RES[@]}" -D - -o /dev/null --max-time 20 -H 'Origin: capacitor://localhost' \
  "$BASE/pelias/v1/autocomplete?text=lake%20st" | grep -ci '^access-control-allow-origin')
[ "$acao" = "1" ] && ok "exactly one ACAO header on /pelias (duplicates are rejected by browsers)" \
                  || bad "ACAO header count = $acao (must be exactly 1)"

echo
echo "=== 6. Tailnet-gated routes ==="
code=$(curl -s "${RES[@]}" -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/api/ride-status")
[ "$code" = "403" ] && ok "/api/ride-status -> 403 from a public source IP (correct)" \
                    || warn "/api/ride-status -> $code from public (expected 403)"
TS_IP=$("${SSH[@]}" "root@$IP" 'tailscale ip -4' 2>/dev/null)
if [ -n "$TS_IP" ]; then
  code=$(curl -s --resolve "$DOMAIN:$APP_PORT:$TS_IP" -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/api/ride-status")
  [ "$code" = "200" ] && ok "/api/ride-status -> 200 over the tailnet (proxied to rwtpc4)" \
                      || bad "/api/ride-status -> $code over the tailnet"
else
  warn "server not on the tailnet; skipped the positive ride-status check"
fi

echo
echo "=== 7. Static UI ==="
code=$(curl -s "${RES[@]}" -o /dev/null -w '%{http_code}' --max-time 20 -u "${WEB_USER:-trey}:${WEB_PASS:-}" "$BASE/index.html")
[ "$code" = "200" ] || [ "$code" = "401" ] && ok "/index.html -> $code (401 = Basic Auth gate intact)" || bad "/index.html -> $code"

echo
echo "=== 8. Headroom ==="
"${SSH[@]}" "rwt@$IP" 'free -h | awk "/Mem:|Swap:/{printf \"      %-6s %s total, %s available\n\",\$1,\$2,\$NF}"; df -h / | awk "NR==2{printf \"      disk   %s total, %s free\n\",\$2,\$4}"' 2>/dev/null

echo
if [ "$FAILED" -eq 0 ]; then
  echo "${GREEN}All required checks passed.${NC}"
  echo "Cutover is now a single DNS change: point $DOMAIN at $IP (TTL 120)."
  echo "Then verify ON THE PHONE — a passing curl is not a passing app."
else
  echo "${RED}$FAILED required check(s) failed. Do not flip DNS.${NC}"
fi
exit $FAILED
