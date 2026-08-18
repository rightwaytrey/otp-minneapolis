# Server API compatibility contract

The iOS app is BUNDLED: each build carries its own frozen frontend JS and
calls this server's APIs cross-origin from `capacitor://localhost`. Once a
build is in external testers' (or App Store users') hands, those users run old
JS against the *live* server — the server cannot assume clients update in
lockstep. From the first external promotion onward, the endpoints below are a
public compatibility contract.

## Covered endpoints (everything the bundled app calls)

| Endpoint | Backing service |
| --- | --- |
| `/otp/...` (GraphQL trip planning, GTFS, vehicle positions) | OpenTripPlanner |
| `/geocoder/...` (autocomplete, search, reverse) | Pelias |
| `/api/onboard/...` (onboard-discovery preferences) | prefs-api sidecar |
| `/api/debug-log` (diagnostics sink) | Flask sidecar :8092 |

All served through nginx at `https://api.transit-nav.com:9966` (public,
rate-limited) — that is the name the bundled app is built against, and the
contract is against that name.

`tre.hopto.org:9966` serves the same config and the same routes and is working
again (its `:80` forward was restored 2026-08-17 and the cert renews through
2026-11-15). Prefer `api.transit-nav.com` anyway: its HTTP-01 challenge needs
that inbound port to keep existing, and when it went missing on 2026-07-12
certbot failed 58 straight runs and the cert expired unnoticed on 2026-08-09,
taking address search and trip planning down together. DNS-01 has no such
dependency.

## Rules

1. **Additive changes only.** New fields, new endpoints, new optional
   parameters are always fine. Removing/renaming fields, changing types or
   semantics, tightening validation, or changing a route's path is a breaking
   change.
2. **Breaking changes get a new path** (e.g. `/api/onboard/v2/`) and the old
   one keeps working alongside it.
3. **Retire old paths only when the fleet has moved.** Debug-log batches carry
   a `build` stamp and nginx access logs show which paths are still hit; an old
   path can be dropped once no active build uses it (TestFlight builds also
   expire after 90 days, which bounds how long stragglers can exist).
4. **OTP/Pelias upgrades count.** A new OpenTripPlanner version can change
   GraphQL schema/behavior — before upgrading the container, check the queries
   the app actually makes (they're in the bundled frontend) still work, e.g. by
   pointing a promoted-tag build (`app-1.0.<n>` in the fork) at the staging
   instance or replaying the verify suite against the upgraded backend.

## What is NOT covered

- `:9967` (dev server) and anything behind the auth gate — internal only.
- The static web build at `:9966` — it redeploys with the server
  (`frontend/deploy-prod.sh`), so it never runs stale JS.
