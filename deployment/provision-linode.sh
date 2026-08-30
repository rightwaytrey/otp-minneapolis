#!/usr/bin/env bash
#
# provision-linode.sh — create the TransitNav Linode.
#
# Adapted from tradingbot/local/deployment/provision-linode.sh, which is the
# working pattern on this box. Differences that matter: us-ord instead of
# ap-southeast, a 4 GB plan sized from measured RSS rather than a guess, and a
# refusal to create a second instance with the same label.
#
# Does NOT configure anything — run configure-server.sh next.
#
#   cp env.example .env && $EDITOR .env
#   ./provision-linode.sh [--yes]
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AUTO_YES=false
for arg in "$@"; do [[ "$arg" == "--yes" || "$arg" == "-y" ]] && AUTO_YES=true; done

[ -f "$SCRIPT_DIR/.env" ] || { echo "${RED}Error: .env not found. cp env.example .env${NC}"; exit 1; }
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"

command -v linode-cli >/dev/null || { echo "${RED}Error: linode-cli not installed${NC}"; exit 1; }
linode-cli account view --text --no-headers >/dev/null 2>&1 || {
  echo "${RED}Error: linode-cli not configured. Run: linode-cli configure${NC}"; exit 1; }

KEY_PATH="${SSH_PUBLIC_KEY_PATH/#\~/$HOME}"
[ -f "$KEY_PATH" ] || { echo "${RED}Error: SSH public key not found at $KEY_PATH${NC}"; exit 1; }
SSH_PUBLIC_KEY="$(cat "$KEY_PATH")"

# Creating a second instance with the same label is silently expensive: it
# boots, bills hourly, and is easy not to notice next to the tradingbot fleet.
EXISTING="$(linode-cli linodes list --label "$LINODE_LABEL" --text --no-headers --format 'id,ipv4' 2>/dev/null || true)"
if [ -n "$EXISTING" ]; then
  echo "${YELLOW}A Linode labelled '$LINODE_LABEL' already exists:${NC}"
  echo "  $EXISTING"
  echo "Nothing to do. Delete it first if you truly want a fresh one."
  exit 0
fi

: "${LINODE_ROOT_PASSWORD:=$(openssl rand -base64 24)}"

echo "${GREEN}=== Provision TransitNav Linode ===${NC}"
printf '  region %s\n  type   %s\n  image  %s\n  label  %s\n  auth   SSH keys only\n\n' \
  "$LINODE_REGION" "$LINODE_TYPE" "$LINODE_IMAGE" "$LINODE_LABEL"

if [ "$AUTO_YES" != true ]; then
  read -rp "This creates a billable instance. Proceed? (y/N) " -n 1 REPLY; echo
  [[ $REPLY =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

LINODE_ID="$(linode-cli linodes create \
  --label "$LINODE_LABEL" --region "$LINODE_REGION" --type "$LINODE_TYPE" \
  --image "$LINODE_IMAGE" --root_pass "$LINODE_ROOT_PASSWORD" \
  --authorized_keys "$SSH_PUBLIC_KEY" --booted true \
  --text --no-headers --format id | tr -d '\n')"
[ -n "$LINODE_ID" ] || { echo "${RED}Error: create failed${NC}"; exit 1; }
echo "${GREEN}Created (id $LINODE_ID)${NC}"

echo -n "Waiting for boot"
for _ in $(seq 60); do
  STATUS="$(linode-cli linodes view "$LINODE_ID" --text --no-headers --format status | tr -d '\n')"
  [ "$STATUS" = "running" ] && break
  echo -n "."; sleep 5
done; echo

SERVER_IP="$(linode-cli linodes view "$LINODE_ID" --text --no-headers --format ipv4 | tr -d '[],"' | awk '{print $1}')"
[ -n "$SERVER_IP" ] || { echo "${RED}Error: no IP returned${NC}"; exit 1; }

cat > "$SCRIPT_DIR/server-info.txt" <<INFO
# Written by provision-linode.sh — do not commit (gitignored).
LINODE_ID=$LINODE_ID
SERVER_IP=$SERVER_IP
LABEL=$LINODE_LABEL
REGION=$LINODE_REGION
TYPE=$LINODE_TYPE
CREATED=$(date -Is)
INFO

echo
echo "${GREEN}Ready.${NC}  IP: $SERVER_IP  (also saved to server-info.txt)"
echo
echo "The A record for $DOMAIN still points at the house. Do NOT flip it yet."
echo "Next:  ./configure-server.sh $SERVER_IP"
