#!/usr/bin/env bash
#
# install-house-nginx.sh — render and install the :9966 site on THIS desktop.
#
# The house half of what deploy-app.sh does for the Linode, calling the same
# renderer with the same templates and the house value set. There is no second
# copy of the config any more; parity between the two hosts is a property of
# deployment/nginx/*.tmpl, and `render-nginx.py --check` proves it.
#
# Nothing here `cp`s a repo file onto a live nginx path. It renders, diffs, and
# only then installs. README.md used to tell you to
# `sudo cp config/nginx/otp.conf /etc/nginx/sites-available/otp`, which is how
# the live config both rots and leaks: the repo file carries __PLACEHOLDER__
# tokens, the live file carries substituted secrets, and copying either
# direction is a breakage.
#
#   ./install-house-nginx.sh              # render + show the diff, change nothing
#   ./install-house-nginx.sh --install    # ... and install it (needs sudo)
#
# UNLOCK_SECRET must be in the environment (deployment/.env has it; it is
# gitignored). Rotating it is backlog 2.10 and needs a human.
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1
[ $# -gt 1 ] && { echo "${RED}Usage: $0 [--install]${NC}"; exit 1; }

if [ -z "${UNLOCK_SECRET:-}" ] && [ -f "$SCRIPT_DIR/.env" ]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
fi
[ -n "${UNLOCK_SECRET:-}" ] || {
  echo "${RED}Error: UNLOCK_SECRET is not set and deployment/.env did not supply it.${NC}"
  echo "The renderer fails closed rather than installing a config with a live"
  echo "__UNLOCK_SECRET__ placeholder, which would lock every cookie holder out."
  exit 1
}

echo "${GREEN}=== Parity ===${NC}"
python3 "$SCRIPT_DIR/render-nginx.py" --check

echo
echo "${GREEN}=== Render (house) ===${NC}"
OUT="$SCRIPT_DIR/rendered/house"   # gitignored
rm -rf "$OUT"
UNLOCK_SECRET="$UNLOCK_SECRET" python3 "$SCRIPT_DIR/render-nginx.py" --env house --out "$OUT"

echo
echo "${GREEN}=== What would change ===${NC}"
CHANGED=0
diff_one() {
  local rendered="$1" live="$2"
  if sudo -n test -r "$live" 2>/dev/null || [ -r "$live" ]; then
    if sudo -n diff -u "$live" "$rendered" >/dev/null 2>&1 || diff -u "$live" "$rendered" >/dev/null 2>&1; then
      echo "  $live: unchanged"
      return
    fi
    echo "${YELLOW}  $live: DIFFERS${NC}"
    { sudo -n diff -u "$live" "$rendered" 2>/dev/null || diff -u "$live" "$rendered" 2>/dev/null || true; } \
      | grep -E '^[+-]' | grep -vE '^[+-][+-]' | head -40 | sed 's/^/    /'
    CHANGED=1
  else
    echo "${YELLOW}  $live: not present (or unreadable) — would be created${NC}"
    CHANGED=1
  fi
}
diff_one "$OUT/otp-common.conf" /etc/nginx/snippets/otp-common.conf
diff_one "$OUT/otp.conf"        /etc/nginx/sites-available/otp

if [ "$INSTALL" -ne 1 ]; then
  echo
  echo "${YELLOW}Dry run. Nothing was installed.${NC} Re-run with --install to apply."
  echo "The rendered files are in $OUT (mode 600 — they hold the real secret)."
  exit 0
fi

if [ "$CHANGED" -eq 0 ]; then
  echo
  echo "Nothing to do."
  exit 0
fi

echo
echo "${GREEN}=== Install ===${NC}"
sudo install -D -m 644 -o root -g root "$OUT/otp-common.conf" /etc/nginx/snippets/otp-common.conf
sudo install -D -m 644 -o root -g root "$OUT/otp.conf"        /etc/nginx/sites-available/otp
sudo ln -sfn /etc/nginx/sites-available/otp /etc/nginx/sites-enabled/otp
sudo install -D -m 644 -o root -g root "$REPO_ROOT/config/nginx/conf.d/00-map-hash.conf" \
  /etc/nginx/conf.d/00-map-hash.conf
sudo nginx -t
sudo systemctl reload nginx
echo "${GREEN}nginx reloaded.${NC}"

echo
echo "${GREEN}=== Deploy manifest ===${NC}"
PROVENANCE="$(python3 "$REPO_ROOT/scripts/deploy-manifest.py" provenance \
  --repo "otp-minneapolis=$REPO_ROOT" \
  --repo "transitnav=$HOME/projects/transitnav" \
  --repo "otprr=$HOME/projects/otprr/otp-react-redux")"
python3 "$REPO_ROOT/scripts/deploy-manifest.py" record \
  --target house --steps nginx --provenance "$PROVENANCE" \
  --file /etc/nginx/snippets/otp-common.conf \
  --file /etc/nginx/sites-available/otp \
  --file /etc/nginx/conf.d/00-map-hash.conf
echo
echo "Check the ladder against what is now live:"
echo "  scripts/check-config-ladder.py --deployed --ssh localhost"
echo "  scripts/check-debug-log-payload.py --resolve 127.0.0.1 --ssh localhost"
