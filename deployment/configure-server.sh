#!/usr/bin/env bash
#
# configure-server.sh — base OS setup for the TransitNav Linode.
#
# Idempotent: safe to re-run. Installs docker, nginx, certbot (DNS-01 via
# Cloudflare), tailscale and a swapfile; creates the app user; opens the
# firewall. Deliberately installs NO Java and NO Maven — OTP runs from a
# prebuilt JAR inside a container, and the graph is built on the desktop.
#
#   ./configure-server.sh <SERVER_IP>
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVER_IP="${1:-}"
[ -n "$SERVER_IP" ] || { echo "${RED}Usage: $0 <SERVER_IP>${NC}"; exit 1; }
[ -f "$SCRIPT_DIR/.env" ] || { echo "${RED}Error: .env not found. cp env.example .env${NC}"; exit 1; }
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"

for v in APP_USER DOMAIN APP_PORT; do
  [ -n "${!v:-}" ] || { echo "${RED}Error: $v is empty in .env${NC}"; exit 1; }
done
# CF_TOKEN and TAILSCALE_AUTHKEY are optional HERE so the base OS can be built
# before the secrets exist. Each gates exactly one step, both steps are
# idempotent, and re-running this script once the values are in .env completes
# them. deploy-app.sh will refuse to install nginx without a certificate, so a
# half-configured box cannot be mistaken for a finished one.
: "${CF_TOKEN:=}"
: "${TAILSCALE_AUTHKEY:=}"
[ -n "$CF_TOKEN" ] || echo "${YELLOW}  note: CF_TOKEN empty -> skipping the TLS certificate step${NC}"
[ -n "$TAILSCALE_AUTHKEY" ] || echo "${YELLOW}  note: TAILSCALE_AUTHKEY empty -> skipping the tailnet join${NC}"

KEY_PATH="${SSH_PUBLIC_KEY_PATH/#\~/$HOME}"
SSH_PUBLIC_KEY="$(cat "$KEY_PATH")"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

echo "${GREEN}=== Configuring $SERVER_IP ===${NC}"
ssh "${SSH_OPTS[@]}" "root@$SERVER_IP" true || { echo "${RED}Cannot SSH as root${NC}"; exit 1; }

# The remote script is fed on stdin with the few values it needs exported, so
# no secret is ever written to a file on the server or shown in `ps`.
ssh "${SSH_OPTS[@]}" "root@$SERVER_IP" \
  "APP_USER='$APP_USER' DOMAIN='$DOMAIN' APP_PORT='$APP_PORT' \
   CF_TOKEN='$CF_TOKEN' TAILSCALE_AUTHKEY='$TAILSCALE_AUTHKEY' \
   SSH_PUBLIC_KEY='$SSH_PUBLIC_KEY' bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "=== Waiting for cloud-init / unattended-upgrades to release apt ==="
for _ in $(seq 120); do
  fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
  fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || break
  sleep 5
done

echo "=== System update ==="
apt-get update -qq
apt-get upgrade -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold"

echo "=== Packages ==="
# No openjdk, no maven: OTP ships as a prebuilt JAR in a JRE container and the
# graph is built on the desktop. Adding a JDK here invites someone to rebuild
# on a box that has no room for it.
apt-get install -y -qq \
  ca-certificates curl gnupg lsb-release \
  nginx rsync ufw jq unzip \
  python3 python3-venv python3-pip \
  certbot python3-certbot-dns-cloudflare

echo "=== Swapfile (2G) ==="
# Steady state is ~3.2 GB of 4 GB. Swap is insurance against an OTP GC spike,
# not a place to run anything from.
if ! swapon --show=NAME --noheadings | grep -q '^/swapfile$'; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -qw vm.swappiness=10
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf

echo "=== Docker ==="
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker

echo "=== Tailscale ==="
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled
if tailscale status >/dev/null 2>&1; then
  echo "  already on the tailnet"
elif [ -n "$TAILSCALE_AUTHKEY" ]; then
  tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname=transitnav --ssh=false
else
  echo "  SKIPPED: no auth key. tailscaled is installed and enabled; re-run to join."
fi
echo "  tailnet IP: $(tailscale ip -4 2>/dev/null || echo '(not joined)')"

echo "=== App user: $APP_USER ==="
if ! id "$APP_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$APP_USER"
fi
usermod -aG docker "$APP_USER"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"
printf '%s\n' "$SSH_PUBLIC_KEY" > "/home/$APP_USER/.ssh/authorized_keys"
chown "$APP_USER:$APP_USER" "/home/$APP_USER/.ssh/authorized_keys"
chmod 600 "/home/$APP_USER/.ssh/authorized_keys"
# prefs-api is a systemd USER unit (deliberately: no sudo needed to restart it,
# and a forgotten restart has silently shipped stale code more than once).
# On a headless box that only starts at boot with lingering enabled.
loginctl enable-linger "$APP_USER"

echo "=== SSH hardening ==="
# Port 22 is reachable from the internet until the tailnet gate goes up, so
# keys-only is not optional. Ubuntu's cloud image ships
# /etc/ssh/sshd_config.d/50-cloud-init.conf with PasswordAuthentication yes,
# which overrides the main config -- hence a drop-in that sorts AFTER it.
# Safe to apply while connected: this session is already key-authenticated.
cat > /etc/ssh/sshd_config.d/99-transitnav-hardening.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
SSHD
chmod 644 /etc/ssh/sshd_config.d/99-transitnav-hardening.conf
sshd -t && systemctl reload ssh
sshd -T | grep -iE '^(passwordauthentication|permitrootlogin)' | sed 's/^/  /'

echo "=== Firewall ==="
# :9966 is not a choice — every shipped TestFlight build calls that port on
# this hostname. :80 is kept only for a redirect; renewal is DNS-01.
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp   >/dev/null
ufw allow 80/tcp   >/dev/null
ufw allow 443/tcp  >/dev/null
ufw allow "$APP_PORT"/tcp >/dev/null
ufw allow in on tailscale0 >/dev/null
ufw --force enable >/dev/null
ufw status numbered | sed 's/^/  /'

echo "=== TLS certificate (DNS-01 via Cloudflare) ==="
if [ -z "$CF_TOKEN" ]; then
  echo "  SKIPPED: no Cloudflare token. certbot and the dns plugin are installed;"
  echo "  re-run this script with CF_TOKEN set in .env to issue the certificate."
else
# DNS-01 is why this hostname exists: renewal writes a TXT record, so there is
# no inbound :80 to be forwarded, blocked, or silently dropped by a router.
install -d -m 700 /etc/letsencrypt
umask 077
cat > /etc/letsencrypt/cloudflare.ini <<CFINI
dns_cloudflare_api_token = $CF_TOKEN
CFINI
chmod 600 /etc/letsencrypt/cloudflare.ini
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 -d "$DOMAIN"
else
  echo "  certificate already present"
fi
certbot certificates 2>/dev/null | grep -E 'Certificate Name|Expiry' | sed 's/^/  /'
fi

echo "=== Data directories ==="
install -d -o "$APP_USER" -g "$APP_USER" \
  "/home/$APP_USER/projects" "/home/$APP_USER/otp-debug-logs" /var/www/transitnav
chown -R "$APP_USER:www-data" /var/www/transitnav

echo
echo "Base configuration complete."
free -h | sed 's/^/  /'
df -h / | sed 's/^/  /'
REMOTE

echo
echo "${GREEN}Done.${NC}  Next: ./deploy-app.sh $SERVER_IP"
echo "${YELLOW}The A record for $DOMAIN still points at the house.${NC}"
