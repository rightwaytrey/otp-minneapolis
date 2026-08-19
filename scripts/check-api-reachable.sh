#!/bin/bash

# Outside-In API Reachability Watch
# Pages Pushover when the phone can no longer reach this box.
#
# Why this exists: on 2026-08-14 at 17:46 the router's :9966 forward stopped
# passing traffic. Nothing on this machine noticed, because from this machine
# nothing was wrong — nginx up, OTP planning in 10ms, Pelias answering in 12ms,
# certificate valid. Every check that ran locally said healthy for four days
# while the app on the phone could not load at all. It surfaced when the rider
# opened it and got a spinner that never resolved.
#
# The lesson is that localhost cannot answer the only question that matters —
# "can my phone reach this?" — so this check does not run locally. It asks a
# public relay to fetch our endpoint FROM the internet, which is the same path
# the phone takes.
#
# Hairpin NAT is off on this router (80, 443 and 9966 all refuse when dialled
# via the public IP from inside), so probing our own public address from here
# proves nothing either. The relay is not a workaround; it is the only vantage
# point available.
#
# This is the second time a forward has vanished silently: :80 disappeared on
# 2026-07-12, certbot then failed 58 consecutive runs, and tre.hopto.org's cert
# expired on 2026-08-09 taking address search and trip planning with it. That one
# was caught eventually by check-cert-expiry.sh — but only weeks later, and only
# as a symptom. This watches the cause.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUSHOVER_CREDS="${RIDE_WATCH_PUSHOVER_CREDS:-$HOME/.config/pushover/credentials}"
STATE_FILE="${API_REACHABLE_STATE:-$HOME/.cache/otp-api-reachable.state}"

# What we ask for. A GET, public (auth_basic off), cheap, and it exercises the
# whole chain the phone depends on: router forward -> nginx -> TLS -> upstream.
# A query with a known-stable answer, so a 200 carrying junk still fails.
TARGET_HOST="${API_REACHABLE_HOST:-api.transit-nav.com:9966}"
TARGET_PATH="/pelias/v1/autocomplete?text=lake%20street"
EXPECT='"features"'

# A URL that is not ours, fetched through the same relay in the same run. If
# THIS fails too, the relay is having a bad day and we know nothing about our
# own reachability — so we stay quiet rather than page the rider at 09:15 about
# somebody else's outage. Distinguishing "we are down" from "the telescope is
# broken" is the whole reason this control exists.
CONTROL_URL="https://example.com/"
CONTROL_EXPECT="Example Domain"

# Relays, tried in order. Each takes a URL-encoded target and returns the body
# verbatim. More than one because depending on a single free service to tell you
# your app is down is its own single point of failure.
RELAYS=(
    "https://api.allorigins.win/raw?url="
    "https://corsproxy.io/?"
)

# Consecutive failures before paging. One failure is a flaky relay or a dropped
# packet; two in a row, twenty minutes apart, is the forward being gone.
FAIL_THRESHOLD="${API_REACHABLE_FAIL_THRESHOLD:-2}"

urlencode() {
    python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1"
}

notify() {
    local title="$1" message="$2" priority="$3"
    local user token
    user="$(grep -iE '^(USER_KEY|USER|PUSHOVER_USER_KEY)=' "$PUSHOVER_CREDS" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' ')"
    token="$(grep -iE '^(API_TOKEN|TOKEN|PUSHOVER_API_TOKEN)=' "$PUSHOVER_CREDS" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' ')"
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

# Cache-buster: a relay that served us a cached 200 from before the outage would
# report health that no longer exists.
TARGET_URL="https://${TARGET_HOST}${TARGET_PATH}&_cb=$(date +%s)"

reachable=""      # yes | no | unknown
via=""
detail=""

for relay in "${RELAYS[@]}"; do
    encoded="$(urlencode "$TARGET_URL")"
    body="$(curl -s --max-time 25 "${relay}${encoded}" 2>/dev/null || true)"
    if [ -n "$body" ] && printf '%s' "$body" | grep -q -- "$EXPECT"; then
        reachable="yes"; via="$relay"; break
    fi

    # Our fetch failed through this relay. Is the relay itself alive?
    control="$(curl -s --max-time 20 "${relay}$(urlencode "$CONTROL_URL")" 2>/dev/null || true)"
    if [ -n "$control" ] && printf '%s' "$control" | grep -q -- "$CONTROL_EXPECT"; then
        # Relay is fine and could not reach us. That is a real answer.
        reachable="no"; via="$relay"
        detail="$(printf '%s' "$body" | head -c 200)"
        break
    fi
    # Relay is broken or blocked; try the next one before concluding anything.
    reachable="unknown"; via="$relay"
done

mkdir -p "$(dirname "$STATE_FILE")"
streak="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
case "$streak" in ''|*[!0-9]*) streak=0 ;; esac

case "$reachable" in
    yes)
        if [ "$streak" -ge "$FAIL_THRESHOLD" ]; then
            notify "API reachable again" \
"The phone can reach ${TARGET_HOST} again after ${streak} failed checks." 0
        fi
        echo 0 > "$STATE_FILE"
        echo "OK: ${TARGET_HOST} reachable from the internet (via ${via})"
        ;;
    no)
        streak=$(( streak + 1 ))
        echo "$streak" > "$STATE_FILE"
        echo "FAIL (${streak}): ${TARGET_HOST} not reachable from the internet"
        [ -n "$detail" ] && echo "  relay said: $detail"
        if [ "$streak" -eq "$FAIL_THRESHOLD" ]; then
            notify "App cannot reach the server" \
"${TARGET_HOST} is not answering from the public internet, though it is healthy on the box itself. That pattern is the router's port forward: it is what broke on 2026-08-14 (silent for four days) and on 2026-07-12. Check the :9966 forward first." 1
        fi
        exit 1
        ;;
    *)
        # Every relay unusable. Say so in the cron mail and change nothing:
        # a check that cannot see is not a check that found a problem.
        echo "INCONCLUSIVE: no relay could be reached; reachability unknown this run"
        ;;
esac
