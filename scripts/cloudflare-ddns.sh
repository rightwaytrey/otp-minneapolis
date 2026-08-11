#!/bin/bash

# Cloudflare Dynamic DNS
# Keeps the app's A record pointed at this box's current public IP.
#
# Replaces what ddns-noip does for tre.hopto.org. The IP here is dynamic, so a
# static A record goes stale the moment the ISP hands out a new lease and the
# app stops resolving — a harder failure than the cert outage, since nothing
# resolves at all.
#
# Only writes when the IP actually differs: Cloudflare rate-limits, and an
# unchanged PUT every five minutes is noise in the audit log for no benefit.
#
# Config: /etc/transitnav/domain.env  (see scripts/setup-domain-cert.sh)
#   DOMAIN=api.transitnav.app
#   ZONE=transitnav.app
#   CF_TOKEN=<Zone:DNS:Edit token>

set -e  # Exit on error

CONFIG="${TRANSITNAV_DOMAIN_ENV:-/etc/transitnav/domain.env}"

if [ ! -r "$CONFIG" ]; then
    echo "ERROR: cannot read $CONFIG — run setup-domain-cert.sh first" >&2
    exit 1
fi
# shellcheck disable=SC1090
. "$CONFIG"

for var in DOMAIN ZONE CF_TOKEN; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: $var is not set in $CONFIG" >&2
        exit 1
    fi
done

API="https://api.cloudflare.com/client/v4"
AUTH=(-H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json")

# The true public IP, not a LAN or tailnet address — same reason ddns-noip uses
# an external echo service (100.x would be catastrophic to publish here).
CURRENT_IP="$(curl -s --max-time 20 https://api.ipify.org)"
if ! [[ "$CURRENT_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: could not determine public IP (got '$CURRENT_IP')" >&2
    exit 1
fi
if [[ "$CURRENT_IP" == 100.* ]]; then
    echo "ERROR: refusing to publish a tailnet address ($CURRENT_IP)" >&2
    exit 1
fi

json_get() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null; }

ZONE_ID="$(curl -s --max-time 20 "${AUTH[@]}" "$API/zones?name=$ZONE" \
    | json_get "d['result'][0]['id'] if d.get('result') else ''")"
if [ -z "$ZONE_ID" ]; then
    echo "ERROR: zone '$ZONE' not found — is the token scoped to it?" >&2
    exit 1
fi

REC="$(curl -s --max-time 20 "${AUTH[@]}" "$API/zones/$ZONE_ID/dns_records?type=A&name=$DOMAIN")"
REC_ID="$(echo "$REC" | json_get "d['result'][0]['id'] if d.get('result') else ''")"
REC_IP="$(echo "$REC" | json_get "d['result'][0]['content'] if d.get('result') else ''")"

# proxied=false is REQUIRED, not a preference: the app talks to :9966, and
# Cloudflare's proxy only forwards a fixed set of ports (9966 is not among
# them). Proxying would also hide the real IP the app must reach directly.
BODY="{\"type\":\"A\",\"name\":\"$DOMAIN\",\"content\":\"$CURRENT_IP\",\"ttl\":120,\"proxied\":false}"

if [ -z "$REC_ID" ]; then
    echo "Creating $DOMAIN -> $CURRENT_IP"
    RESULT="$(curl -s --max-time 20 -X POST "${AUTH[@]}" -d "$BODY" "$API/zones/$ZONE_ID/dns_records")"
elif [ "$REC_IP" = "$CURRENT_IP" ]; then
    echo "$DOMAIN already -> $CURRENT_IP (no change)"
    exit 0
else
    echo "Updating $DOMAIN: $REC_IP -> $CURRENT_IP"
    RESULT="$(curl -s --max-time 20 -X PUT "${AUTH[@]}" -d "$BODY" "$API/zones/$ZONE_ID/dns_records/$REC_ID")"
fi

if [ "$(echo "$RESULT" | json_get "d.get('success')")" != "True" ]; then
    echo "ERROR: Cloudflare rejected the update:" >&2
    echo "$RESULT" | head -c 500 >&2
    echo >&2
    exit 1
fi
echo "OK."
