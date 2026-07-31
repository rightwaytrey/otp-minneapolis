You are writing a post-ride diagnostic report for a transit-navigation app
(Go Mode) after the ride-watch daemon flagged anomalies during a real trip
taken by the app's single rider. Work autonomously and finish the whole job —
nobody is watching this session, and there is no one to ask questions of.

## Your input

The environment variable `RIDE_WATCH_REQUEST` holds the path to a JSON file:

```
{"session", "date", "startMs", "endMs", "findingsPath",
 "itinerarySummary", "findingsCount", "pagesSent", "endReason"}
```

Read it first. Everything else follows from it.

## What actually happened, and what you must decide

Each line of `findingsPath` is one finding the rule engine emitted:
`{tsMs, time, session, rule, severity, summary, context, paged}`.

A finding is a *suspicion*, not a bug. The rule engine is deliberately
trigger-happy. Your job is to decide, per finding, which of these it is:

- **real-bug** — the app genuinely misbehaved and the rider was misled.
- **app-behaved-correctly** — the telemetry is surprising but the app did the
  right thing given what it knew (e.g. the rider really did board an earlier
  bus, so re-planning was correct).
- **watcher-false-positive** — the rule fired on a benign pattern and the rule
  itself should be tuned.

Every verdict must cite telemetry. Do not guess.

## Method

1. Read the request JSON and the findings file.
2. The raw telemetry is `~/otp-debug-logs/debug-<date>.jsonl` — one JSON
   object per line, appended by the app. Relevant keys: `t` (epoch ms),
   `session`, `type` (redux action), `payload`, and for console lines
   `kind:"console"`, `level`, `args`.
   Filter to this `session` and read a window around each finding — roughly
   60s before to 60s after `tsMs`. **Do not** load the whole file into
   context; it can be tens of megabytes. Use `python3`/`jq`/`grep` to slice
   out just the windows you need, and prefer printing state *transitions*
   over every tick (`UPDATE_POSITION`/`UPDATE_PROGRESS`/`UPDATE_ROUTE_MATCH`
   fire ~1 Hz and will drown you).
   The action types that carry the story: `START_GO_MODE` (a fresh one
   mid-trip is an itinerary replacement), `STOP_GO_MODE`, `SET_RIDING` /
   `CLEAR_RIDING`, `UPDATE_PROGRESS`, `UPDATE_ROUTE_MATCH`,
   `UPDATE_VEHICLE_MATCH`, `START_REROUTE` (check `reason` and `autoApply`),
   `REROUTE_SNAPSHOT`, `ADD_NOTIFICATION`, `SET_ACTIVE_ITINERARY` (an
   explicit rider choice).
3. Build the replay fixture so the ride can be re-run against the code:
   ```
   cd /home/rwt/projects/otprr/otp-react-redux
   node lib/util/go-mode/replay/build-fixture.js --session <session> --label ride-<date>
   ```
   Report the fixture path it prints. If it fails, say so and continue — the
   report matters more than the fixture.
4. For findings you call **real-bug**, locate the responsible code in
   `/home/rwt/projects/otprr/otp-react-redux/lib/` (Go Mode logic lives under
   `lib/util/go-mode/` and `lib/actions/go-mode*`; the reducer is
   `lib/reducers/create-otp-reducer.js`). Name the file and function. Propose
   a **fix category**, not a patch:
   `guard-condition` / `state-machine-ordering` / `stale-data` /
   `threshold-tuning` / `missing-invalidation` / `ui-only`.
   Do **not** edit any application code. This is a read-only investigation.

## The rider's rules (these constrain your output)

- The rider is one person, on a bike + bus commute, reading this on a phone.
- No forced route changes: auto-updates must keep the rider's chosen route
  (same route, next departure). Switching them to a different route or mode
  without an explicit tap is a bug, not a feature.
- Route/vehicle detection matches position against route geometry or live
  vehicles — never stop proximity.
- Never prompt the rider for something the app already knows.
- Notification copy = only the numbers the rider acts on. No coaching
  phrases, no clock times, no exclamation marks.

## Output

Write the report to
`/home/rwt/obsidian-vault/Claude/ride-watch/<date>-<session-short>.md`
where `<session-short>` is the part of the session id after the last `-`.
Create the `ride-watch` directory if needed. Write **only** into
`/home/rwt/obsidian-vault/Claude/` — the rest of the vault is the rider's own
notes and is read-only.

Structure:

```markdown
# Ride report — <date> (session <session>)

**Trip:** <itinerary one-liner from itinerarySummary>
**Window:** <local start>-<local end>  ·  **Findings:** N  ·  **Paged:** M
**Verdict:** <one sentence: what actually went wrong on this ride>

## Timeline
<the 5-15 telemetry moments that explain the ride, local times, one line each>

## Findings
### <local time> — <rule> (<severity>) -> **<verdict>**
<what the telemetry shows, with the actual numbers>
**Evidence:** <action types + timestamps you read>
**Fix:** <category> — <one or two sentences> · `<file>` (real-bug only)

## Rule tuning
<any rule that misfired, and the threshold or guard that would stop it>

## Fixture
<path, or why it could not be built>
```

Keep it tight. Prose only where it carries evidence.

## Finish

Send one Pushover notification with the result. Read credentials from
`~/.config/pushover/credentials` (format `USER_KEY=...` / `API_TOKEN=...`,
one per line) and POST to `https://api.pushover.net/1/messages.json` with
`token`, `user`, `title`, `message`.

- title: `Ride report`
- message: exactly `Report ready: N findings, M look like real bugs.`
  (substitute the numbers; no other text)

Send this **once**, only after the report file is written. If the report
could not be written, send `Report failed: <short reason>` instead.
