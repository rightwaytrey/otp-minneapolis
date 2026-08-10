#!/bin/bash

# Tailscale TLS Certificate Refresh
# Keeps nginx's cert for the tailnet hostname current.
#
# Why this exists: the public hostname's Let's Encrypt cert renews over HTTP-01,
# which needs inbound :80 from the internet. When the router's port-forward went
# away (2026-07-12), certbot failed 58 runs straight and the cert expired on
# 2026-08-09 — taking address search and trip planning down with it, since the
# phone refuses an expired cert.
#
# "tailscale cert" has no such dependency: Tailscale proves the hostname itself,
# so there is no challenge to reach, no port to forward, no ISP to cooperate.
# Tailscale caches the cert and only fetches a new one when it nears expiry, so
# running this daily is cheap; nginx is reloaded ONLY when the bytes change.

set -e  # Exit on error

DOMAIN="rwtpc4.tail9d6464.ts.net"
CERT_DIR="/etc/nginx/certs"
CRT="$CERT_DIR/$DOMAIN.crt"
KEY="$CERT_DIR/$DOMAIN.key"

echo "======================================"
echo "Tailscale cert refresh: $DOMAIN"
echo "======================================"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (writes $CERT_DIR, reloads nginx)" >&2
    exit 1
fi

mkdir -p "$CERT_DIR"
chmod 755 "$CERT_DIR"

# Checksum before, so we can tell whether anything actually rotated.
BEFORE=""
[ -f "$CRT" ] && BEFORE="$(sha256sum "$CRT" | cut -d' ' -f1)"

# Write to temp files first: a failed fetch must never truncate a working cert.
TMP_CRT="$(mktemp)"
TMP_KEY="$(mktemp)"
trap 'rm -f "$TMP_CRT" "$TMP_KEY"' EXIT

if ! tailscale cert --cert-file "$TMP_CRT" --key-file "$TMP_KEY" "$DOMAIN"; then
    echo "ERROR: tailscale cert failed; leaving the existing cert in place" >&2
    exit 1
fi

# Refuse to install anything that isn't a usable cert/key pair.
if ! openssl x509 -in "$TMP_CRT" -noout >/dev/null 2>&1; then
    echo "ERROR: fetched file is not a valid certificate; not installing" >&2
    exit 1
fi

install -m 644 "$TMP_CRT" "$CRT"
install -m 600 "$TMP_KEY" "$KEY"

AFTER="$(sha256sum "$CRT" | cut -d' ' -f1)"
echo "Valid until: $(openssl x509 -in "$CRT" -noout -enddate | cut -d= -f2)"

if [ "$BEFORE" = "$AFTER" ]; then
    echo "Certificate unchanged — skipping nginx reload."
    exit 0
fi

echo "Certificate rotated — reloading nginx."
nginx -t
systemctl reload nginx
echo "Done."
