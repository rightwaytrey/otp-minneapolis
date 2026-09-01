# config/nginx/

**The site config is no longer here.** It is one template with one value set per
environment, and it lives under `deployment/`:

    deployment/nginx/otp.conf.tmpl          the :9966 server blocks
    deployment/nginx/otp-common.conf.tmpl   every location block
    deployment/env/house.env                rwtpc4 (this desktop)
    deployment/env/prod.env                 the Linode
    deployment/render-nginx.py              the only thing that renders them

This directory used to hold the **house** copy and `deployment/nginx/` the
**server** copy, hand-maintained, 168 diff lines apart. A fix applied to one was
not deployed until somebody remembered the other, and
`scripts/check-nginx-parity.py` existed only to notice when nobody had. Parity is
now a property of the source — the shared text is literally the same bytes — and
`render-nginx.py --check` proves it. The parity checker is gone.

`conf.d/00-map-hash.conf` stays here: it is a global nginx tweak, not part of the
site, and `deploy-app.sh` writes it directly.

## To change the site

1. Edit the template.
2. `deployment/render-nginx.py --check`
3. `scripts/check-config-ladder.py` (repo) and `--deployed` (each host).
4. Install: `deployment/deploy-app.sh <ip> --only nginx` for the Linode,
   `deployment/install-house-nginx.sh` for this desktop.

**Never `cp` a repo file onto a live nginx path, in either direction.** The repo
holds `__PLACEHOLDER__` tokens and the live files hold substituted secrets. One
direction rolls production back to a stale config that still passes `nginx -t`;
the other is how the unlock secret reached a public repo and sat there from
2026-06-01 to 2026-09-01.
