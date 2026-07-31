# ride-watch

A daemon that watches the transit-navigation app's live telemetry while the
rider is actually on a trip, flags anomalies as they happen, and asks Claude
for a written post-mortem once the ride ends.

The problem it solves: the app streams a lot of telemetry, and the
interesting failures (the stop counter collapsing, the itinerary being
swapped out from under a rider who is already aboard) are buried in ~1 Hz
noise and only noticed hours later, if at all. ride-watch reads the stream
continuously so that a bad ride is understood the same evening.

## How it works

The app batches JSONL telemetry to the Flask sidecar
(`/api/debug-log` in `transitnav/preferences_api.py`), which appends it to
`~/otp-debug-logs/debug-<UTC-date>.jsonl`. ride-watch tails that file.

Each line is a redux action (`{type, payload, t, session, recv}`), a console
line (`{kind:"console", level, args}`), or a session marker.

**State machine**, per session:

- `idle` → `active` on `START_GO_MODE` (the itinerary is captured then)
- `active` → `ended` on `STOP_GO_MODE`, or after 15 minutes of silence
- A *second* `START_GO_MODE` during an active trip is an **itinerary swap**,
  not a new trip. This distinction is what makes the `aboard-swap` rule
  possible.
- If the daemon starts mid-ride it scans the last 5 minutes of the file and
  **adopts** an in-progress trip rather than missing it.

**Rules.** Each produces a finding
`{tsMs, session, rule, severity, summary, context}` at `info`, `warn`, or
`page` severity:

| rule | fires when | severity |
| --- | --- | --- |
| `stop-count-collapse` | `stopsRemaining` drops to 1 below 60% of a transit leg | page |
| `stop-count-increase` | `stopsRemaining` rises with no itinerary swap | warn |
| `aboard-swap` | itinerary replaced while `SET_RIDING` is held, no rider action nearby | page |
| `riding-flip` | `SET_RIDING` tripId changes on the same transit leg | page |
| `missed-bus-while-riding` | `MISSED_BUS` notification while riding is held | page |
| `deviated-streak` | `status='deviated'` continuously >90s | warn (page on a transit leg) |
| `gps-gap` | no `UPDATE_POSITION` for >60s mid-trip | warn |
| `reroute-storm` | more than 3 `START_REROUTE` in 5 minutes | warn |
| `console-error` | a `console.error` line (deduped by message) | info |
| `distance-spike` | `distanceFromRoute` >2000m one tick after <200m | warn |

## The three surfaces

**1. Pushover — the interrupt.** Only `page`-severity findings, at most
**2 per ride**, with a **120s minimum gap**. Everything beyond the cap is
logged, not sent. Message copy follows the rider's rule: only the numbers
they can act on, under 120 characters, no coaching phrases, no exclamation
marks. Credentials come from `~/.config/pushover/credentials`
(`USER_KEY=` / `API_TOKEN=`, one per line).

Pages are **coalesced and ranked** rather than sent first-come-first-served
— see below.

## Which page the rider actually gets

Failures arrive in cascades. On 2026-07-29 the riding tripId flipped at
17:28:45 and the stop counter collapsed to "1 left" at 17:28:53. Under
first-wins paging the rider was told about the trip id — a diagnostic detail
— and the 120s rate limit swallowed the stop count, which is the one that
would have put them off at the wrong stop.

So a page is not sent when it fires. It goes into a small per-trip buffer for
`PAGE_COALESCE_MS` (**15s**); when that window closes, the **highest-ranked**
page in it is sent and the rest are dropped and logged as
`superseded by <rule>`. Ties go to the earlier finding. The window is opened
by the first page and later pages do **not** extend it, so a continuing storm
cannot defer paging indefinitely; subsequent pages simply open the next
window. The 2-per-trip cap and the 120s global rate limit still apply on top.

`PAGE_RANK`, highest first — the question is "how much does this change what
the rider does in the next minute?", not "how broken is the app" (the
post-ride report covers that):

| rank | rule | why |
| --- | --- | --- |
| 50 | `stop-count-collapse` | the banner is lying about when to get off; acted on immediately |
| 40 | `missed-bus-while-riding` | a wrong alert telling a seated rider to move |
| 30 | `aboard-swap` | the on-screen route no longer matches their bus |
| 20 | `riding-flip` | board state suspect, but they are on the right vehicle |
| 10 | `deviated-streak` | tracking looks off; nothing to do about it |

Rules absent from `PAGE_RANK` get `PAGE_RANK_DEFAULT` (25, mid-pack) so a new
page rule is neither silently starved nor able to outrank the stop counter
before anyone has decided where it belongs — **add your rule to `PAGE_RANK`
when you add it.** Non-page severities never push at all.

A buffered page is flushed by the main loop's 5s tick, so it goes out even if
the log falls silent right after the finding, and immediately on trip end, so
a ride that stops three seconds into a window still pages. Findings are
persisted to the JSONL only once their paging verdict is known, so each
record carries a final `paged` (and `supersededBy` when it lost).

Tuning: raise `PAGE_COALESCE_MS` to catch slower cascades at the cost of
telling the rider later; lower it toward 0 to restore first-wins behaviour.
Reorder `PAGE_RANK` to change which finding wins a window. Replay a real ride
before and after — the 7/29 log is the reference case, and it must page
`stop-count-collapse`.

**2. `~/otp-debug-logs/ride-watch/current-ride.md` — the live view.**
Rewritten on any state change (2s debounce) so *any* Claude session can just
read it to see what is happening right now: trip summary, current leg,
progress, status, whether the rider is aboard, how stale the last GPS fix
is, and findings newest-first. When no trip is running it names the last one
and whether it was a clean ride.

**3. Vault reports — the post-mortem.** When a ride with at least one
finding ends, the daemon writes
`~/otp-debug-logs/ride-watch/report-request-<session>.json` and fires off
`claude -p "$(cat report-prompt.md)"` with `RIDE_WATCH_REQUEST` pointing at
it. That headless session triages every finding as **real-bug**,
**app-behaved-correctly**, or **watcher-false-positive** with telemetry
evidence, builds a replay fixture, and writes
`~/obsidian-vault/Claude/ride-watch/<date>-<session-short>.md`, then sends
one "Report ready" Pushover.

If `claude` is missing or exits non-zero, the daemon sends a single fallback
push — *"Ride ended — N findings. Report pending; open Claude and say 'ride
report'."* — which is exempt from the 2-page cap but still shares the rate
limit. A ride with zero findings requests no report at all; it just updates
`current-ride.md`.

Raw findings are also appended to
`~/otp-debug-logs/ride-watch/<date>-<session>.findings.jsonl`.

## Rider notes — the ride console

The rule engine only notices what the telemetry admits. The rider can see out
of the window. So there is a fourth input: a page at
**https://tre.hopto.org:9966/ride** (add it to the phone's home screen) with a
text box at the bottom and ride-watch's live view at the top.

A note POSTs to `/api/ride-note` in the Flask sidecar, which appends it to the
*same* daily JSONL as the telemetry:

```json
{"kind":"rider-note","event":"RIDER_NOTE","text":"...","t":1785...,"recv":1785...,"session":"...","ip":"..."}
```

The daemon picks it up in stream order and attaches it to the active trip —
by session id, or by "there is only one ride happening" when the sidecar's
session guess misses. Each note is stored with the trip state at that instant
(leg, progress, status, stops remaining, whether the rider is aboard, GPS
staleness), rendered in `current-ride.md` under **Rider notes**, and persisted
as a finding with rule `rider-note` at severity `info`.

Notes carry no push body, so they can **never page** the rider — buzzing
someone about the note they just typed would be absurd — but they are the
highest-value input to the post-ride report, which triages each one against
what the telemetry says was happening at that second (`report-prompt.md`).

The page also polls `/api/ride-status` (5s, paused when hidden) for the current
`current-ride.md`, the newest findings, and today's notes. It is a single
self-contained file, deployed by `ride-watch/deploy-ride-console.sh`; the
frontend's `deploy-prod.sh` re-runs it, because that script's
`rsync --delete` would otherwise sweep the page out of the web root.

## Running it

Installed as a **user** service:

```
systemctl --user status ride-watch
systemctl --user restart ride-watch
systemctl --user stop ride-watch          # stop watching
systemctl --user disable --now ride-watch # stop and don't come back
journalctl --user -u ride-watch -f        # live logs
```

The daemon's own log is `~/otp-debug-logs/ride-watch/daemon.log`
(size-rotated at 5 MB to `.1`). `SIGTERM` shuts down cleanly and leaves any
active trip un-ended, so a restart re-adopts it instead of firing a spurious
"ride ended".

The unit needs **linger** enabled so it runs when nobody is logged in. Check
with `loginctl show-user rwt | grep Linger`. If it says `Linger=no`, run:

```
sudo loginctl enable-linger rwt
```

## Replaying a past ride

Run any historical file through the exact same code path at full speed:

```
python3 ride-watch/ride_watch.py --replay ~/otp-debug-logs/debug-2026-07-29.jsonl
```

Replay is **always dry-run**: it never sends a Pushover and never spawns a
report agent. It also writes to `~/otp-debug-logs/ride-watch/replay/` by
default so it cannot clobber the live status file. Use `--watch-dir DIR` to
send output somewhere else. It prints every finding with its local time and
the pushes that *would* have gone out.

## Tuning the rules

Thresholds are module constants at the top of `ride_watch.py` —
`STOP_COLLAPSE_MAX_PROGRESS`, `DEVIATED_STREAK_MS`, `GPS_GAP_MS`,
`REROUTE_STORM_COUNT`, `DISTANCE_SPIKE_FAR_M`, `MAX_PAGES_PER_TRIP`,
`PUSH_MIN_INTERVAL_MS`, `PAGE_COALESCE_MS`, `PAGE_RANK`, and so on.

The honest workflow for changing one: replay a real ride before and after,
compare which findings appear, and add a test. A rule that pages on a
correct app behaviour is worse than a rule that stays quiet — the rider gets
two interrupts per ride and they should both be worth reading.

Environment overrides: `RIDE_WATCH_DRY_RUN=1` (log pushes instead of sending
them and skip the report agent), `RIDE_WATCH_LOG_DIR`, `RIDE_WATCH_DIR`,
`RIDE_WATCH_PUSHOVER_CREDS`, `RIDE_WATCH_CLAUDE`, `RIDE_WATCH_REPO`.

## Tests

```
python3 ride-watch/test_ride_watch.py
```

Stdlib `unittest`, no installs. Synthetic streams cover every rule (both the
firing case and the case that must stay quiet), the state machine, and page
ranking (supersession inside the window, tie-breaking, flush on a quiet log,
flush on trip end). The last suite replays the real `debug-2026-07-29.jsonl`
and asserts that the 17:28 incident is caught — board-state anomaly plus
stop-count collapse — that the four page-worthy findings in those eight
seconds cost the rider exactly one interrupt, that the one they get is
`stop-count-collapse`, and that they would not have been paged more than
twice. Tests never
touch the network; the Pushover path is verified by credential parsing only.
