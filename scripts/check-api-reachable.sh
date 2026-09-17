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
#
# 2026-09-17 — TWO THINGS ABOVE ARE NO LONGER TRUE, and this script paged the
# rider 34 times before anyone noticed:
#
#   1. THE ROUTER IS NOT IN THE PATH ANY MORE. api.transit-nav.com resolves to
#      172.238.175.38, which is the Linode's own public IP — no home forward, no
#      hairpin NAT problem. The page text told the rider to "check the :9966
#      forward first", which is now advice about a machine that cannot be the
#      cause. (rwtpc4 still has an /etc/hosts line pointing the name at the
#      Tailscale address, which is why a local curl proves nothing — that part
#      of the premise survives.)
#
#   2. A FREE CORS RELAY IS A BAD TELESCOPE FOR A NON-STANDARD PORT. Both relays
#      sit behind Cloudflare, and Cloudflare answers their fetch of :9966 with
#      `error code: 520` / `522` / `Oops... Request Timeout` — errors about the
#      RELAY's fetch, not about our origin. The old code read exactly that as
#      "the relay is fine and could not reach us. That is a real answer." It is
#      not: on 2026-09-17 the endpoint returned 200 with `"features"` in 2.0 s
#      over the public internet throughout, the phone's own check-ins were
#      landing, and a bundle had just been verified over the same public URL.
#
# So the rule now is: a relay error is never evidence about us (the body is
# classified, not just the control URL); two different relays must agree before
# we call it unreachable; and before paging we ask the ORIGIN, from outside the
# house, whether it is answering on its public IP. Only when that fails too is
# this "the server is down" — and only then does it page at priority 1.

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
# corsproxy.io was here and is not any more: it now answers server-side requests
# with {"error":"Server-side requests are not allowed on your plan"}, which is a
# 200-shaped refusal. The control check below catches that correctly (it fails
# for example.com too, so the run is INCONCLUSIVE rather than a false alarm) but
# a relay that can never succeed is dead weight.
#
# These are free services and they are flaky by nature — during one testing
# session allorigins began returning 520 for a URL it had served correctly two
# minutes earlier. That is exactly why there is a control URL and a two-strike
# rule: a relay having a bad day must never read as "the app is down".
RELAYS=(
    "https://api.allorigins.win/raw?url="
    "https://api.codetabs.com/v1/proxy?quest="
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
ours_failed=0     # how many DIFFERENT relays fetched the control fine but not us

# A relay answering with its own error page tells us nothing about our origin.
# Cloudflare 520/522 ("Web server is returning an unknown error" / "connection
# timed out") is what both relays return for a TLS fetch on :9966 when their
# own fetcher gives up; codetabs answers "Oops... Request Timeout". Matching the
# body is deliberate: these come back with a 200-shaped response, so curl's exit
# status and the HTTP code are both useless here.
looks_like_relay_error() {
    printf '%s' "$1" | grep -qiE 'error code: 5[0-9][0-9]|Request Timeout|cloudflare|<title>5[0-9][0-9]|Server-side requests are not allowed'
}

for relay in "${RELAYS[@]}"; do
    encoded="$(urlencode "$TARGET_URL")"
    body="$(curl -s --max-time 25 "${relay}${encoded}" 2>/dev/null || true)"
    if [ -n "$body" ] && printf '%s' "$body" | grep -q -- "$EXPECT"; then
        reachable="yes"; via="$relay"; break
    fi

    if [ -z "$body" ] || looks_like_relay_error "$body"; then
        # The telescope is broken, not the sky. Try the next one.
        reachable="unknown"; via="$relay"
        detail="$(printf '%s' "$body" | head -c 200)"
        continue
    fi

    # Our fetch came back with something that is neither our data nor a known
    # relay failure. Is the relay itself alive?
    control="$(curl -s --max-time 20 "${relay}$(urlencode "$CONTROL_URL")" 2>/dev/null || true)"
    if [ -n "$control" ] && printf '%s' "$control" | grep -q -- "$CONTROL_EXPECT"; then
        # This relay is fine and could not reach us. ONE relay saying so is a
        # suspicion, not a verdict (2026-09-17: allorigins served example.com
        # while 520-ing us for hours). Keep looking.
        ours_failed=$(( ours_failed + 1 ))
        via="$relay"
        detail="$(printf '%s' "$body" | head -c 200)"
        continue
    fi
    reachable="unknown"; via="$relay"
done

# Two independent relays fetched the control and not us: that is corroborated.
if [ "$reachable" != "yes" ] && [ "$ours_failed" -ge 2 ]; then
    reachable="no"
fi

# Does the ORIGIN answer on its public IP, asked from outside the house? This is
# the check that distinguishes "the server is down" (page hard) from "the path
# from some networks is unhappy" (say so quietly). It runs ON the Linode and
# dials the public A record with --resolve, so it exercises nginx, TLS and the
# public address — everything except the rider's own ISP. No ssh, no answer, no
# veto: an unreachable Linode is itself the outage.
ORIGIN_SSH="${API_REACHABLE_ORIGIN_SSH:-rwt@100.126.171.72}"
origin_answers() {
    local ip
    ip="$(dig +short "${TARGET_HOST%%:*}" @8.8.8.8 2>/dev/null | head -1)"
    [ -n "$ip" ] || return 1
    timeout 45 ssh -o BatchMode=yes -o ConnectTimeout=10 "$ORIGIN_SSH" \
        "curl -s --max-time 20 --resolve '${TARGET_HOST}:${ip}' 'https://${TARGET_HOST}${TARGET_PATH}' | grep -q -- '${EXPECT}'" \
        >/dev/null 2>&1
}

# Has the rider's phone itself reached this host recently? The app reports its
# bundle on every launch, from whatever network it is on, which is the only
# truly end-to-end evidence available. `ship-web-check` is our own ship script
# and does not count.
phone_reached_recently() {
    local age
    age="$(timeout 45 ssh -o BatchMode=yes -o ConnectTimeout=10 "$ORIGIN_SSH" \
        "grep -v ship-web-check app-bundles-dev/checks.jsonl 2>/dev/null | tail -1" 2>/dev/null \
        | python3 -c 'import sys,json,time
line=sys.stdin.read().strip()
print(int(time.time()-json.loads(line)["t"]) if line else 10**9)' 2>/dev/null || echo 1000000000)"
    case "$age" in ''|*[!0-9]*) return 1 ;; esac
    [ "$age" -lt 7200 ]
}

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
        echo "FAIL (${streak}): ${TARGET_HOST} not reachable via ${ours_failed} relays"
        [ -n "$detail" ] && echo "  relay said: $detail"
        if [ "$streak" -eq "$FAIL_THRESHOLD" ]; then
            if origin_answers; then
                # The origin is serving its own public IP. Whatever the relays
                # cannot do, the server is not down — so this is a quiet note,
                # never a priority-1 page.
                if phone_reached_recently; then
                    echo "  SUSPECT relays only: origin answers on its public IP and the phone checked in within 2h — not paging"
                else
                    echo "  SUSPECT network path: origin answers on its public IP but no phone check-in in 2h — quiet note"
                    notify "Server up, path suspect" \
"${TARGET_HOST} answers on its own public IP from outside the house, but ${ours_failed} relays could not reach it and the phone has not checked in for 2h. Nothing to fix on the box; this is a routing or ISP suspicion." 0
                fi
            else
                notify "App cannot reach the server" \
"${TARGET_HOST} is not answering from the public internet, and the origin does not answer on its own public IP either. api.transit-nav.com is the LINODE (no home router in this path since the migration) — check nginx :9966 and the cert on the Linode, then its firewall. Relay detail: ${detail:-none}." 1
            fi
        fi
        exit 1
        ;;
    *)
        # Every relay unusable. Say so in the cron mail and change nothing:
        # a check that cannot see is not a check that found a problem.
        echo "INCONCLUSIVE: no relay could be reached; reachability unknown this run"
        [ -n "$detail" ] && echo "  last relay said: $detail"
        ;;
esac
