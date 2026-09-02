#!/usr/bin/env bash
#
# server-env.sh — turn the DESKTOP's transitnav/.env into the LINODE's.
#
# Sourced by deploy-app.sh's `web` step, and exercised directly by
# deployment/test-server-env.sh. It lives in its own file for exactly that
# reason: the transform decides what production's Flask sidecar believes about
# itself, and a transform that cannot be run without an ssh cannot be tested.
#
# There is one .env, on the desktop, and `--only web` copies it up. That was
# fine while every key in it was host-independent. It stopped being fine the
# moment the house grew a staging lane of its own: two keys in that file now
# say *which box you are*, and shipping the house's answers to production
# points every phone at the wrong place.
#
#   APP_BUNDLE_DIR         where the OTA web-bundle zips live on disk
#   APP_BUNDLE_PUBLIC_BASE the absolute URL the phone is told to fetch them from
#
# The house-staging lane sets these to /home/rwt/app-bundles-house and
# https://rwtpc4.tail9d6464.ts.net:9966. Copied to production verbatim, the
# next launch check tells every phone -- including phones that have never been
# on the tailnet -- to pull its next web bundle from a desktop behind WiFi
# client isolation. The app would fail its update check silently and stay on
# whatever bundle it had.
#
# STRIPPED, NOT REWRITTEN. preferences_api.py already carries production's
# answers as its own defaults (~/app-bundles and https://api.transit-nav.com:9966,
# preferences_api.py:116 and :121), so removing the key IS setting it correctly.
# Writing the value here instead would make this the third copy of a URL that
# already exists twice, and the copy nothing tests.
#
# APP_BUNDLE_APP_ID is deliberately NOT in the list: the bundle id is the same
# app on both boxes.

# Keys that answer "which machine am I?" and must not cross from the desktop
# to the Linode. Add to this list, do not add a second sed.
HOST_LOCAL_ENV_KEYS="APP_BUNDLE_DIR APP_BUNDLE_PUBLIC_BASE"

# render_server_env <src-env> <domain> <app-port>
#   stdout: the .env to install on the server
#   stderr: one line per key stripped, so a deploy says what it removed
render_server_env() {
  local src="$1" domain="$2" app_port="$3"
  [ -f "$src" ] || { echo "render_server_env: no such file: $src" >&2; return 1; }
  # The bundled app calls cross-origin from capacitor://localhost; the web UI on
  # this host is same-origin. Both are covered, and the old tre.hopto.org entries
  # are kept so nothing that still points there breaks during the cutover week.
  local origin
  origin="https://$domain:$app_port,https://$domain,capacitor://localhost,https://tre.hopto.org,https://tre.hopto.org:$app_port"
  awk -v origin="$origin" -v strip="$HOST_LOCAL_ENV_KEYS" '
    BEGIN {
      n = split(strip, a, " ")
      for (i = 1; i <= n; i++) drop[a[i]] = 1
    }
    # Only real assignments are candidates. A comment that merely mentions the
    # key is documentation and stays.
    /^[A-Za-z_][A-Za-z0-9_]*=/ {
      key = $0
      sub(/=.*/, "", key)
      if (key == "PREFS_ALLOWED_ORIGIN") { print "PREFS_ALLOWED_ORIGIN=" origin; next }
      if (key in drop) {
        print "  stripped " key ": house-local, production uses the preferences_api.py default" > "/dev/stderr"
        next
      }
    }
    { print }
  ' "$src"
}
