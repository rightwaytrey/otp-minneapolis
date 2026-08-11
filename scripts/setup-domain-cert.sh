#!/bin/bash

# One-shot setup: real domain + DNS-01 certificate that renews without ports.
#
# This is the permanent fix for the 2026-08-09 outage. tre.hopto.org's cert
# expired and could not be renewed because Let's Encrypt validates only on :80
# or :443 and neither reaches this box (measured from four continents; :9966 on
# the same IP answers fine, so it is those two ports specifically). A free
# No-IP hostname also cannot hold the TXT record DNS-01 needs.
#
# DNS-01 against a real zone removes the whole class of problem: renewal proves
# ownership by writing a TXT record through the Cloudflare API. No inbound port,
# no router rule, nothing that can silently disappear for four weeks.
#
# Usage:  sudo ./setup-domain-cert.sh api.transitnav.app <cloudflare-token> [email]
#
# The token needs exactly one permission: Zone -> DNS -> Edit, on this zone.

set -e  # Exit on error

DOMAIN="$1"
CF_TOKEN="$2"
EMAIL="${3:-rightwaytrey@gmail.com}"

if [ -z "$DOMAIN" ] || [ -z "$CF_TOKEN" ]; then
    echo "Usage: sudo $0 <fqdn> <cloudflare-token> [email]" >&2
    echo "   eg: sudo $0 api.transitnav.app AbCd1234... " >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (installs snap plugins, writes /etc)" >&2
    exit 1
fi

# The registrable zone is the last two labels: api.transitnav.app -> transitnav.app
ZONE="$(echo "$DOMAIN" | awk -F. '{print $(NF-1)"."$NF}')"

echo "======================================"
echo "Domain: $DOMAIN   (zone: $ZONE)"
echo "======================================"

echo "==> Verifying the token can see the zone..."
ZONE_ID="$(curl -s --max-time 20 -H "Authorization: Bearer $CF_TOKEN" \
    "https://api.cloudflare.com/client/v4/zones?name=$ZONE" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['result'][0]['id'] if d.get('result') else '')" 2>/dev/null)"
if [ -z "$ZONE_ID" ]; then
    echo "ERROR: token cannot see zone '$ZONE'." >&2
    echo "       Check the domain's nameservers point at Cloudflare and that" >&2
    echo "       the token is scoped Zone:DNS:Edit for this zone." >&2
    exit 1
fi
echo "    zone id: $ZONE_ID"

echo "==> Installing the certbot Cloudflare DNS plugin..."
# Snap certbot refuses root-trusted plugins until told to; without this the
# plugin installs but certbot will not load it.
snap set certbot trust-plugin-with-root: ok
snap install certbot-dns-cloudflare 2>/dev/null || snap refresh certbot-dns-cloudflare

echo "==> Writing credentials..."
install -d -m 700 /etc/letsencrypt
CREDS=/etc/letsencrypt/cloudflare.ini
printf 'dns_cloudflare_api_token = %s\n' "$CF_TOKEN" > "$CREDS"
chmod 600 "$CREDS"   # certbot warns loudly, and rightly, if this is readable

install -d -m 755 /etc/transitnav
cat > /etc/transitnav/domain.env <<EOF
# Written by setup-domain-cert.sh — consumed by cloudflare-ddns.sh
DOMAIN=$DOMAIN
ZONE=$ZONE
CF_TOKEN=$CF_TOKEN
EOF
chmod 600 /etc/transitnav/domain.env

echo "==> Pointing $DOMAIN at this box's public IP..."
"$(cd "$(dirname "$0")" && pwd)/cloudflare-ddns.sh"

echo "==> Requesting the certificate over DNS-01..."
# --deploy-hook, not a cron reload: nginx keeps the old cert in memory until
# reloaded, which is exactly how a renewed cert can still serve as expired.
certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials "$CREDS" \
    --dns-cloudflare-propagation-seconds 30 \
    -d "$DOMAIN" \
    --non-interactive --agree-tos -m "$EMAIL" \
    --deploy-hook "systemctl reload nginx"

echo
echo "======================================"
echo "Certificate installed:"
certbot certificates --cert-name "$DOMAIN" 2>/dev/null | grep -E "Certificate Name|Domains|Expiry" || true
echo "======================================"
echo
echo "Next: add the nginx server block for $DOMAIN and point the app at it."
echo "Renewal is handled by certbot's existing snap timer — DNS-01, so it no"
echo "longer depends on any port being open."
