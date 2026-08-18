# Handoff — open work on TransitNav

Written 2026-08-18 for an agent with no prior context. Everything urgent is done;
what follows is the tail. Read the orientation and the traps before touching
anything — several of the traps below cost real hours to discover.

---

## Orientation

Four places, one product:

| Repo | Path | Trunk | What it is |
| --- | --- | --- | --- |
| Frontend (Go Mode, ~11k LOC) | `~/projects/otprr/otp-react-redux` | **`main`** | the app UI; a fork of otp-react-redux |
| Infra / OTP / verify / ride-watch | `~/projects/otp-minneapolis` | `master` | nginx, OTP, cron, ride-watch daemon |
| Flask sidecar (`:8092`) | `~/projects/transitnav` | `master` | `/api/preferences`, `/api/onboard/`, `/api/debug-log`, `/api/ride-*` |
| iOS shell | `~/projects/transitnav-ios` | `main` | Capacitor wrapper, TestFlight CI |

**"The app" always means the iOS app on the user's phone** — never the dev server,
never a browser tab. It is a *bundled* build: the WKWebView serves a frozen copy
of the frontend from `capacitor://localhost`. Uncommitted or unpushed frontend
work is definitionally not in the app. Shipping means: push to `main`, then
dispatch `testflight.yml`, then report the build number.

```
echo '{"ref":"main","inputs":{"ref":"main"}}' | gh api --method POST \
  repos/rightwaytrey/transitnav-ios/actions/workflows/testflight.yml/dispatches --input -
```

Running services: `prefs-api` (:8092), `nginx` (:9966), `ride-watch` (user unit),
OTP in Docker (:8090), frontend dev server (:9967). Cron: GTFS refresh 03:00,
debug-log prune 04:30, verify suite 05:00, cert watch 09:15.

---

## Traps

Each of these has already bitten someone.

1. **`prefs-api` serves stale code.** gunicorn runs with no `--reload`, so editing
   `preferences_api.py` changes nothing until restart. It has silently no-op'd a
   shipped feature twice. Check `systemctl show prefs-api -p ActiveEnterTimestamp`
   against the file mtime; the restart needs the user's sudo (no passwordless sudo
   here — hand them the command as a plain block).
2. **Never merge the frontend into `master`.** `node-ci.yml` runs `semantic-release`
   with `NPM_TOKEN` — an npm publish of the upstream package name. The trunk was
   renamed from `feature/go-mode` to `main` on 2026-08-18; `dev` is archived as
   `upstream-stale-2025-11`. `go-mode-ci.yml` watches `main` only.
3. **Basic Auth does not protect the APIs.** nginx gates the web UI at `/`; every
   route the bundled app calls is `auth_basic off` + rate-limited, deliberately —
   the app is cross-origin and can carry no credential. `/api/preferences` spends a
   metered Anthropic key from the open internet.
4. **The verify scripts are flaky when no live vehicle is mid-trip.** A `FAIL: no
   live in-progress vehicles found`, or a search returning nothing, is usually the
   feed and not your change. Re-run 2–3 times before concluding anything. To
   attribute a failure, stash your change and re-run — and then re-run *again*,
   because a single pass can pass by luck.
5. **Itinerary times are `number | string`.** Use `new Date(x).getTime()`, not
   `Number(x)` — `Number('2026-01-28T10:00:00')` is `NaN` and fails silently.
6. **jest needs `NODE_ENV=test TZ=America/Chicago`** or it dies in global-setup.
7. **`lib/actions/go-mode.ts` has a scoped `sort-imports` disable** — order its
   imports by hand; the autofixer is non-convergent on that file.
8. **Pre-existing noise, do not treat as regressions:** ~60 TypeScript errors
   (`typecheck` is `continue-on-error` in CI) and 198 missing `fr` translations.
   New English-only message ids go in `i18n/i18n-exceptions.json` → `ignoredIds`.
9. **Never deploy to, or suggest, the `:9966` browser build.** It is legacy; the
   phone doesn't use it.
10. **Measure a proposed gate against the recorded rides before adding it.** A
    previous "obvious" guard would have suppressed two genuine detections and
    prevented no false positive. The replay fixtures are the spec.

**Verification loop:** `NODE_ENV=test TZ=America/Chicago npx jest __tests__/`
(697 passing), then the relevant `scripts/verify-*.js` against `:9967`, then the
05:00 nightly report in `~/obsidian-vault/Claude/verify-nightly/`. Every new
behaviour should get a test that is **checked to fail against the old code** before
it is kept — otherwise it proves nothing.

---

## The work, in the order I'd take it

### 1. App architecture review — *the user said this is what they most want next*
Not a code change; a read and a written opinion. `lib/actions/go-mode.ts` is 4,840
lines and `lib/reducers/go-mode.ts` 860; Go Mode is ~11k LOC across 38 modules.
Known structural smells to start from: `buildLiveItinerary` runs only in
`TripSheet`, so other consumers silently read plan-time values (this caused two
separate bugs); `goMode.reRoute` reducer state is written and read by nothing;
`GoModeHeader.tsx` is dead code. Deliver an assessment, not a refactor — agree the
shape with the user first.

### 2. The app icon — blocked, needs a decision
Still the stock Capacitor logo at
`transitnav-ios/ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png`.
The user picked a **Stone Arch Bridge** concept. Five hand-drawn attempts failed
(they read as battlements or a flag); the honest answer was to hand it to an image
model. The `OPENAI_API_KEY` in the environment has **no credits**. Options: the user
tops that up, uses another tool and hands over a file, or supplies a reference photo
to trace proportions from. Requirements: 1024×1024, **RGB with no alpha** (Apple
rejects transparency), legible at 29px, plus three 2732×2732 splash copies. Blocks
nothing — internal TestFlight has no icon requirement.

### 3. Response caching on `/api/preferences`
The only spend control still missing. `X-Real-IP`, a file-locked daily cap, and
usage logging landed on 2026-08-17 (`57c9695`); identical text still costs a fresh
model call every time. A hash-keyed memo is small and safe. Note the per-IP window
is process-local and gunicorn runs `-w 2`, so that limit is 2× what the constant
says — which is why the daily cap is file-backed.

### 4. Ride-console multi-rider safety — *before a second person uses `/ride`*
`preferences_api.py` `_recent_findings()` / `_recent_replies()` glob and take
`files[-1]` — the newest file **globally**, not the caller's — and `current-ride.md`
is one file for the whole server. Two riders would cross-serve each other's live
location and notes. Safe today only because those routes are Tailscale-gated. Fix
before opening them, not after.

### 5. Departure-drift rule for ride-watch
The app detects a bus moving while the rider rides toward it; the `ride-watch`
daemon has no counterpart, so the watcher can't see drift. Plan it separately; the
daemon lives in `otp-minneapolis/ride-watch/`.

### 6. TypeScript debt
~60 errors, all implicit-any and missing `redux-actions` decls, mostly in the
go-mode modules. Paying them down lets `go-mode-ci.yml` flip `typecheck` to
blocking. Large, mechanical, no rider-visible gain — only worth it as deliberate
hygiene.

### 7. Android
Feasible: only two native plugins (background-geolocation, local-notifications),
both Android-capable. No `android/` platform exists yet. Staged plan was
signed-APK CI on ubuntu for direct installs first, Play Store ($25) later.

### 8. External-beta paperwork — *only if going beyond people they know*
Internal TestFlight (≤100, must be App Store Connect team users) needs none of
this. External (≤10,000) requires Beta App Review, a hosted privacy policy that
discloses the GPS/debug-log streaming, and the App Privacy label. Location purpose
strings and export compliance are already done. See `transitnav-ios/TESTFLIGHT-SETUP.md` §7.

---

## Loose ends, not work

- `scripts/check-cert-expiry.sh` in this repo is **modified and uncommitted — it is
  the user's own work**, adding `tre.hopto.org:443` back under watch now the `:80`
  forward is restored. Do not stage it without asking.
- `transitnav/trip_options.txt` is untracked scratch from the retired Python planner
  (Oct 2025). Offered for deletion twice, never confirmed.
- `tre.hopto.org`'s cert was renewed 2026-08-17 and runs to 2026-11-15. Older notes
  calling it dead are stale. `api.transit-nav.com` is still the host to build
  against — DNS-01, no inbound-port dependency.
- Watch the first 04:30 run of `prune-debug-logs.sh`. It compresses after 3 days and
  deletes after 90; the 90 is deliberate (the user reads rides back weeks later).
  Tighten it once other people's traces start landing there.
