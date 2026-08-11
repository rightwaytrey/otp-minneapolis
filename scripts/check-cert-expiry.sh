#!/bin/bash

# TLS Certificate Expiry Watch
# Pages Pushover before a certificate can expire out from under the app.
#
# Why this exists: on 2026-08-09 tre.hopto.org's cert expired and took address
# search AND trip planning down together — an expired cert is refused before any
# HTTP response exists, so every API call the phone makes fails at once. It was
# not sudden. certbot's HTTP-01 challenge needs inbound :80, the router's forward
# disappeared on 2026-07-12, and certbot then failed 58 consecutive runs over
# four weeks. Nothing was watching, so the first symptom was the rider standing
# at a stop unable to search for an address.
#
# This checks what nginx ACTUALLY SERVES, not the file on disk — that also
# catches the classic "certbot renewed it but nobody reloaded nginx", where the
# disk looks healthy and the socket still hands out the expired one.

set -e  # Exit on error

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUSHOVER_CREDS="${RIDE_WATCH_PUSHOVER_CREDS:-$HOME/.config/pushover/credentials}"

# host:port pairs to check. The tailnet name is served by the same nginx and
# renews by a different mechanism, so checking both tells you WHICH path broke.
ENDPOINTS=(
    "tre.hopto.org:9966"
    "rwtpc4.tail9d6464.ts.net:9966"
)

WARN_DAYS=21    # certbot renews at 30 days out; 21 means it has already missed twice
URGENT_DAYS=7

notify() {
    local title="$1" message="$2" priority="$3"
    local user token
    user="$(grep -iE '^(USER_KEY|USER|PUSHOVER_USER_KEY)=' "$PUSHOVER_CREDS" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' ')"
    token="$(grep -iE '^(API_TOKEN|TOKEN|PUSHOVER_API_TOKEN)=' "$PUSHOVER_CREDS" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' ')"
    # Fall back to the bare two-line form ride_watch.py also accepts.
    if [ -z "$user" ] || [ -z "$token" ]; then
        user="$(sed -n '1p' "$PUSHOVER_CREDS" 2>/dev/null | tr -d ' ')"
        token="$(sed -n '2p' "$PUSHOVER_CREDS" 2>/dev/null | tr -d ' ')"
    fi
    if [ -z "$user" ] || [ -z "$token" ]; then
        echo "ERROR: could not read Pushover credentials at $PUSHOVER_CREDS" >&2
        return 1
    fi
    curl -s --max-time 20 \
        --form-string "token=$token" \
        --form-string "user=$user" \
        --form-string "title=$title" \
        --form-string "message=$message" \
        --form-string "priority=$priority" \
        https://api.pushover.net/1/messages.json >/dev/null
}

EXIT=0

for ep in "${ENDPOINTS[@]}"; do
    host="${ep%%:*}"
    port="${ep##*:}"

    # -servername so SNI picks the right server block; without it nginx hands
    # back the default block's cert and the check silently tests the wrong name.
    enddate="$(echo | timeout 25 openssl s_client -connect "$ep" -servername "$host" 2>/dev/null \
               | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"

    if [ -z "$enddate" ]; then
        echo "$host: UNREACHABLE (could not read a certificate)"
        notify "TLS check failed" "$host:$port served no certificate — nginx down, or the port is closed." 1
        EXIT=1
        continue
    fi

    end_epoch="$(date -d "$enddate" +%s 2>/dev/null)" || { echo "$host: unparseable date '$enddate'" >&2; EXIT=1; continue; }
    now_epoch="$(date +%s)"
    days=$(( (end_epoch - now_epoch) / 86400 ))

    printf '%-32s %4d days left (%s)\n' "$host" "$days" "$enddate"

    if [ "$days" -lt 0 ]; then
        notify "TLS cert EXPIRED" "$host expired $(( -days )) days ago. Address search and trip planning are down until it is renewed." 1
        EXIT=1
    elif [ "$days" -le "$URGENT_DAYS" ]; then
        notify "TLS cert expires in $days days" "$host has not renewed. Check that inbound :80 still reaches this box — that is what broke on 2026-07-12." 1
        EXIT=1
    elif [ "$days" -le "$WARN_DAYS" ]; then
        notify "TLS cert expires in $days days" "$host should have auto-renewed by now (certbot renews at 30 days out) and has not." 0
        EXIT=1
    fi
done

exit $EXIT
