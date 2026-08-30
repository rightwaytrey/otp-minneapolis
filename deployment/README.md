# deployment/ — moving the stack to a Linode

Implements the migration plan: a 4 GB Linode in `us-ord` serving OTP, nginx,
prefs-api and the static UI, with geocoding proxied to Stadia's hosted Pelias.
The graph build and `ride-watch` stay on the desktop.

## Order — step 0 is a gate, not a formality

```bash
# 0. GATE. Prove Stadia matches local Pelias before spending anything.
#    Fails loudly if it does not; then the answer is an 8 GB box with
#    local Pelias, and none of the steps below apply as written.
STADIA_API_KEY=... python3 ../scripts/geocoder-parity.py --n 25

cp env.example .env && $EDITOR .env     # 1. fill in the three secrets
./provision-linode.sh                   # 2. create the instance
./configure-server.sh <SERVER_IP>       # 3. base OS, docker, java, tailscale, TLS
./deploy-app.sh <SERVER_IP>             # 4. ship data + services + nginx
```

Only after all four pass, and after verifying through nginx with `--resolve`,
flip the Cloudflare A record. See the plan's Verification section — in
particular that `POST /api/preferences` with an empty body must answer **400,
not 401**. A 401 means the config re-gated a route the bundled app calls
cross-origin and cannot authenticate.

## Why the app never changes

Same hostname, same `:9966`, same `/pelias/v1` path. `docs/API-COMPAT.md`
rules 1-3 require `/pelias/v1/*` to keep working for every already-shipped
build, and swapping the proxy target behind that path honours it.

## Rollback

Two, both live for at least a week after cutover:

1. Point the Cloudflare A record back at home — restores the entire old stack.
2. Repoint `/pelias/` at `http://$HOME_TAILSCALE_IP:4000` — restores local
   Pelias alone, without touching anything else. This is why the Pelias
   containers and the 68 GB corpus stay on the desktop rather than being
   deleted.
