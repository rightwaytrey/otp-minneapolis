# ride-watch

A daemon that watches the transit-navigation app's live telemetry while the
rider is actually on a trip, flags anomalies as they happen, pages the rider
about the ones they can act on, and — since 2026-07-31 — runs **one Claude
conversation per ride** that follows the trip, talks to the rider in their
phone app, and writes the post-mortem itself.

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
| `notification-repeat` | the same alert (id stem + title) twice in 5 minutes | page |
| `deviated-streak` | `status='deviated'` continuously >90s | warn (page on a transit leg) |
| `gps-gap` | no `UPDATE_POSITION` for >60s mid-trip | warn |
| `progress-without-motion` | leg progress gains >5 points in the time the rider covers 15m | warn |
| `reroute-storm` | more than 3 `START_REROUTE` in 5 minutes | warn |
| `replan-not-converging` | 4 re-plans with no 50m gain on `distanceToDestination`, and the app never said so | page |
| `destination-unreachable` | the app raised `DESTINATION_UNREACHABLE` itself | info |
| `console-error` | a `console.error` line (deduped by message) | info |
| `distance-spike` | `distanceFromRoute` >2000m one tick after <200m | warn |

Two of those were written on 2026-07-31, after a ride where **every single
finding was a rider note** — the engine had nothing to say while the app
buzzed a stationary rider 14 times with the same turn alert, and the rider
typed the complaint out by hand on a bike.

- **`notification-repeat`** keys on the notification id **minus its
  `Date.now()` suffix**, plus the title — `ROUTE_DEVIATION_deviation`,
  `UPCOMING_TURN_<legStart>_<cue>_<stage>`. It was keyed on `(title, message)`
  until 2026-08-31, which is byte-exact and so was beaten by the one thing
  every repeated alert does: drift. 8/28's five "Off Route" pushes said 121m,
  121m, 124m, 120m, 124m and were counted as four separate alerts, none of
  which ever reached the threshold — the rule written for exactly this class
  of bug saw neither of the two deviation storms it was next asked about.
  The threshold is **two** in the window for the same reason: on 8/28 the only
  pair inside any five-minute window was 17:12:57 / 17:14:45, so at three the
  rule would have fired at 17:39:28, five seconds before the rider gave up and
  stopped Go Mode. It fires **once per alert per ride**: the finding says
  "ignore the buzzing", and saying it twice would spend the rider's other
  interrupt on something they have already been told to ignore. On the 7/31
  log it fires at **11:53:07**, the 2nd of 14 buzzes.
- **`replan-not-converging`** is the half `reroute-storm` cannot do.
  `reroute-storm` counts reroute *events* and never looks at whether they are
  working, so it cannot tell "re-planning and converging" from "re-planning in
  circles". This one folds `UPDATE_PROGRESS.distanceToDestination` in and
  counts re-plans since that distance last improved by 50m. On 2026-08-28 the
  destination sat inside the State Fairgrounds, where the street graph stops at
  the fence: 32 minutes of re-planning into the venue interior, never inside
  427m, and the rider was told nothing.
  The thresholds **mirror the client's own guard**
  (`lib/util/go-mode/destination-progress.ts` in otprr — `DESTINATION_GAIN_MIN_M`
  50, `DESTINATION_STALL_REPLANS` 3) deliberately, plus one: the client retires
  the access mode on re-plan 3 and raises `DESTINATION_UNREACHABLE` on re-plan
  4, so firing at 3 would page about a defect the app was in the middle of
  reporting itself. And if that notification *does* reach the stream, the
  daemon records it as `destination-unreachable` (info) and stays quiet — the
  rider already has a high-priority push about it. **This rule is the second
  line: it exists for the ride where the app's own guard failed or never ran.**
  Caveat worth knowing: the daemon can only see re-plans that were *applied*
  (an itinerary swap or a `START_REROUTE`), not the client's attempts, so its
  count is a lower bound. `REROUTE_SNAPSHOT` is **not** a re-plan — it is a
  periodic recording-only alternatives capture, every 90s, and counting it
  would page every rider who ever waited fifteen minutes for a bus.
- **`progress-without-motion`** asks a physical question: did the progress bar
  gain more than 5 points in the time it took the rider to cover 15m? The
  anchor resets when they genuinely travel, so the window is *adaptive* — it is
  minutes wide for a standing rider (map-matching noise sold as travel) and a
  couple of seconds for a moving one. Both ends are real: on the 7/29 Orange
  Line ride it caught the bar jumping 35% → 71% **in one second** while the bus
  covered 6.7m, twice, which nothing else in the engine noticed.

## The three surfaces

**1. The ride thread — the conversation.** One Claude session per ride, in the
rider's phone app, from trip start to well after trip end. It is the surface
the rider actually talks to; see *The ride thread* below.

**2. Pushover — the interrupt.** Only `page`-severity findings, at most
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
| 38 | `replan-not-converging` | the app cannot get them there and has not said so; every minute spent waiting for the next plan is spent |
| 35 | `notification-repeat` | their phone is buzzing wrongly; "ignore it" is actionable this second |
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

**3. The ride console — passive status.** `https://tre.hopto.org:9966/ride`,
where the rider sees what the daemon sees and can drop a note into the stream
with one thumb. It no longer answers; see *Rider notes* below.

Two files back all of this:

- `~/otp-debug-logs/ride-watch/current-ride.md` — the live view, rewritten on
  any state change (2s debounce) so *any* Claude session can read what is
  happening right now: trip summary, current leg, progress, status, whether the
  rider is aboard, how stale the last GPS fix is, the ride thread's health, and
  findings newest-first. When no trip is running it names the last one and
  whether it was a clean ride.
- `~/otp-debug-logs/ride-watch/<date>-<session>.findings.jsonl` — raw findings,
  append-only, each with its final paging verdict. One file per session per
  day, so a session that carried two rides has both rides' findings in it —
  split them on the ride's start time, not the session id.

## The ride thread

**One conversation per ride, in the rider's Claude app.**

Before this, a rider note spawned a headless `claude -p` that answered it and
exited. On the 7/31 ride replies 3 and 4 re-diagnosed the same bug back to back
and the rider asked *"Oh you're fresh context for **every** message???"*. They
were. A ride is one continuous thing and it needs one continuous reader.

### How it is spawned

On `START_GO_MODE` (or on adopting a trip mid-stream after a restart) the
daemon runs:

```
tmux new-session -d -s ride-<HHMM> -x 200 -y 50 -c <repo> \
  "ride-watch/ride-thread-run.sh 'ride <MM-DD HH:MM>'"
```

`ride-thread-run.sh` execs `claude --remote-control "<display name>"` with the
settings file and `ride-thread-sysprompt.md`. The thread appears in the rider's
app list under the display name; `cwd` is this repo, so it inherits `CLAUDE.md`
and the auto-memory index. The daemon polls `tmux capture-pane` for the `❯`
prompt (ready in ~10-12s, bounded at 30s) before typing anything.

**A new ride kills the previous ride's thread** — the rider's attention is on
the trip they are taking. Only sessions matching `^<prefix>-\d{4}$` are killed,
which is why a hand-spawned `ride-test-smoke` survives; the filter is
`ride_thread_sessions()` and it has its own tests.

### How data reaches it

The daemon **types one line per milestone** and nothing else:

```
[ride-watch] <what changed> — digest: ~/otp-debug-logs/ride-watch/<session>.digest.md
```

Milestones are exactly: **trip start · leg transition · any rule finding ·
rider note · trip end · a heartbeat only if 10 minutes pass silently while the
rider is still moving.** A whole ride of ~1 Hz telemetry comes out as a handful
of lines. Detail lives in the digest, which is rewritten *before* every push and
carries the trip summary, current state, everything new since the last push, all
findings and all rider notes — so a thread that reads it is never behind, even
if a push was lost.

Two mechanical details, both learned the hard way and both load-bearing:
`send-keys` of the text and `send-keys Enter` must be **separate calls with a
beat between them** (combined, the line is typed but never submitted), and every
line is collapsed to one line by `one_line()`, because a newline in a rider's
note would submit half a sentence.

None of it runs on the tailer: spawning and typing happen on a worker thread, so
a 12-second TUI startup never stalls telemetry reading, and a dead pane, a
missing tmux or a rider who typed `/exit` are logged and survived.

### What it does

`ride-thread-sysprompt.md` is the brief: one short line for a routine milestone,
investigate the raw JSONL when a finding fires, terse numbers-only prose, never
ask what the digest already says. Hard prohibitions while the trip is live —
**never edit code, restart a service or deploy** (the frontend hot-reloads onto
the phone the rider is navigating by) and **never send a notification** (paging
is the daemon's job).

On the trip-end ping it writes the report to the `reportPath` the daemon put in
the request file — `~/obsidian-vault/Claude/ride-watch/<date>-<session-short>.md`
for the session's first ride, `…-ride2.md`, `…-ride3.md` for the ones after it.
**The path is keyed on the ride, not the session.** The phone keeps one session
id for as long as the app stays loaded, so every trip taken in an evening
shares it; deriving the name from the id alone meant an evening's second
wrap-up silently overwrote the first ride's report. That happened on 08-27 and
again on 08-28 — twice under `mtdh67f3-0z5p24`, an hour apart — and only
survived because the thread improvised a `-ride2` suffix by hand. The daemon
picks the name now (`_report_path`): it is the only party that can see the
earlier ride's file. The request file is per-ride for the same reason
(`report-request-<session>-<HHMM>.json`), so ride 2 cannot destroy ride 1's
inputs before anyone has read them.

The findings ledger stays per-day-per-session and therefore holds both rides;
the request carries `findingsFrom` (the ride's start) and the thread triages
only records at or after it. The report triages every
finding and note as **real-bug**, **app-behaved-correctly**,
**watcher-false-positive** or **no-rule-covers-this** with telemetry evidence;
builds the replay fixture; lists the fix backlog; gives the rider three lines in
the thread; and stays available for follow-ups.

### Permissions

`ride-thread-settings.json`, passed with `--settings`. A permission prompt lands
on the phone of someone on a bicycle, so the routine job is allowed up front:
read anywhere under `~`, a read-only Bash set (`python3`, `tail`, `grep`, `ls`,
`git log`/`show`/`diff`, `node …/build-fixture.js`), and writes **only** under
`~/obsidian-vault/Claude/` and `~/otp-debug-logs/ride-watch/`. Anything else
prompts, which is the intended fallback. The deny list encodes the mid-ride
prohibitions (`systemctl`, `docker`, `git commit/push`, `*deploy*`, edits under
`~/projects`) and beats the broad project `.claude/settings.json` this session
also loads.

Two gotchas worth keeping, both found empirically:

- **`Write(path)` rules match nothing.** The CLI warns about it. Only
  `Edit(path)` rules govern file writes, and they cover every file-editing tool.
- **`permissions.defaultMode` loses** to whatever mode the project was last left
  in — the first test thread came up in *auto* mode despite the file saying
  `manual`. The mode is therefore pinned on the command line in the runner
  (`--permission-mode manual`).

Verified end to end: Read, Grep, `python3`, `git log`, `cd … && node
build-fixture.js` and a vault write all ran with **zero prompts**.

### Failure and the fallback page

If a ride produces findings and has **no working thread** to hold them, the
daemon sends the single fallback push it always had — *"Ride ended — N findings.
Report pending; open Claude and say 'ride report'."* — exempt from the 2-page cap
but still inside the rate limit. A replay never promises a thread, so it never
falls back. A ride with zero findings writes no request file and pings the
thread to say the ride was clean.

That guard only ever asked *"is the pane alive?"*, which on 2026-08-28 was the
wrong question: the thread spawned fine, took the wrap-up line, and then sat at
a permission prompt for about three hours. Everything looked right from the
daemon's side and **nothing paged, ever**. So a ride whose wrap-up was actually
requested now also arms a **report deadline** (`REPORT_DEADLINE_MS`, 10 min):
when it expires, the daemon checks whether the exact `reportPath` it handed the
thread exists, and sends the same fallback push if it does not.

Two details make it work. The deadline lives on `self.report_deadlines` as a
plain dict, not on the `Trip` — `_end_trip` does `del self.trips[session]`, so
`check_timers`' own loop can never see an ended ride, which is precisely how
this case escaped every timer in the file. And it is written into `state.json`,
so a daemon restarted inside the window re-adopts the promise instead of
dropping it — which matters now that restarting on a commit is a thing that
happens to this process mid-evening. A trip with no thread object at all (a
replay, or `RIDE_THREAD_ENABLED=0`) arms nothing: nobody was asked.

`RIDE_THREAD_ENABLED=0` (set in the systemd unit) disables the whole thing: the
daemon then behaves exactly as it did before threads existed.
`RIDE_THREAD_NAME_PREFIX` renames both the tmux session and the display name,
which is how the end-to-end test spawns a real thread that can never collide
with — or clean up — the rider's own.

## Rider notes — the ride console

The rule engine only notices what the telemetry admits. The rider can see out
of the window. So there is one more input: a page at
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
someone about the note they just typed would be absurd. What they do now is
**ping the ride thread**, which answers in the Claude app from the context of
the whole ride, and they remain the highest-value input to the wrap-up report:
the rule engine only notices what the telemetry admits, and the rider can see
out of the window.

A note typed when no trip is running is logged and nothing more — there is no
thread to hand it to.

The page also polls `/api/ride-status` (5s, paused when hidden) for the current
`current-ride.md`, the newest findings, today's notes, and any replies still on
disk. It is a single self-contained file, deployed by
`ride-watch/deploy-ride-console.sh`; the frontend's `deploy-prod.sh` re-runs it,
because that script's `rsync --delete` would otherwise sweep the page out of the
web root.

### The console is passive now

It used to be the conversation: a note spawned a headless `claude -p` and the
answer appeared underneath it. That is retired (2026-07-31) along with the reply
budget, the watchdog and the per-note agent. The page still shows replies that
are already on disk — the list simply stops growing — and it no longer renders a
"thinking…" placeholder, because nothing is thinking. `/api/ride-status` and
`/api/ride-note` are unchanged, so the Flask side needed no edit.

Two smaller things went with it, both from the 7/31 report: the toast strip is
collapsed when empty (it used to hold 30px open under the composer for a message
that is usually not there) and the success toasts are gone. "Saved" then "Sent"
was two announcements of a thing the rider could already see — the note
appearing in the list is the confirmation. Toasts now fire only for the offline
queue and for errors.

`reply-prompt.md` and `report-prompt.md` stay on disk as reference, each with a
deprecation header. `report-prompt.md` is still useful: the ride thread is
pointed at it for the long-form triage brief when a ride was complicated.


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

### Knowing which daemon is running

On 2026-08-28 the process watching the ride had been up for five days, running
a five-day-old copy of `ride_watch.py`. It produced five false
`stalled-progress` findings, missed the arrival event entirely (`SET_ARRIVED`
handling did not exist in the code that was loaded), and nearly overwrote an
earlier ride's report. Nothing it wrote said which version it was — the digest
header carried session / written-at / push count, the status file carried
"Updated:", and the ride thread spent the evening reading source on disk that
the process in memory had never loaded.

So the digest and every status file now open with:

```
Daemon: ride_watch.py @ a1b2c3d-dirty (started 2026-08-31 09:12:03, source mtime 2026-08-28 21:54:11)
```

and, when the repo has moved on:

```
Tree now: e4f5g6h  ** STALE — this daemon is running a1b2c3d, 7 commit(s) behind.
Findings may come from code that no longer exists. Restart: `systemctl --user restart ride-watch` **
```

Note this is the *opposite* of what a build script wants. A build proves the
tree did not move under it and fails if it did; this daemon fully expects the
tree to move underneath it, and the drift is the signal rather than an error.
So it never aborts, never re-execs and never restarts itself — it reports,
loudly, in the header the ride thread already reads before every reply.

**The SHA is resolved once, at import, into `DAEMON_GIT_SHA`.** This is the
whole point and it is easy to get backwards: `git rev-parse HEAD` evaluated at
write time reports what the working tree is *now*, so a five-day-stale daemon
would stamp today's SHA and confidently claim to be current — the mismatch the
stamp exists to expose becomes invisible, and the header positively asserts
something false. A header that lies about provenance is worse than one that
omits it, because nothing contradicts it. `-dirty` comes from
`git status --porcelain --untracked-files=no`, also once: other agents work in
this same checkout and leave scratch files behind constantly, so a stamp that
reads dirty every time would say nothing. If git is unavailable the stamp is
`unknown` — the daemon must not die, or go quiet, because it could not
introspect itself.

The *comparison* against the repo's current HEAD is a separate, TTL-cached read
(`HEAD_RECHECK_MS`, 5 min) of `rev-parse` only — read-only, takes no index
lock, and so cannot collide with another agent's git in this shared worktree.
It never touches the stamp.

**Restart on change (optional, not installed).** `ride-watch-restart.path` and
`ride-watch-restart.service` sit next to `ride-watch.service` in this
directory, uninstalled:

```
cp ride-watch/ride-watch-restart.{path,service} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ride-watch-restart.path
```

Why a **path unit** and not a git hook: the daemon went stale because the file
moved under a running process, which a `post-commit` hook only sometimes
notices. A hook does not fire on `git pull`, `git checkout`, `git rebase`, or
another agent's commit in this shared worktree; it lives in `.git/hooks`, which
is not versioned and is silently absent after a fresh clone; and it fires on
every commit whether or not it touched this directory. `PathChanged=` catches
all of them and nothing else. A Makefile target has the opposite problem — it
is honest and discoverable, and it only helps someone who remembers to run it,
which is exactly the step that was missed.

The restart is gated on the daemon being idle (`ExecCondition` greps
`current-ride.md` for "No active trip"). A restart mid-ride is *safe* —
`KillMode=process` leaves the rider's live tmux thread alone, the trip is
re-adopted on the next `UPDATE_PROGRESS`, and the report deadline survives in
`state.json` — but it is not free: per-trip rule state resets and the adopted
trip spawns a **second** ride thread the rider then sees in their app. A file
changing mid-ride is not a reason to interrupt one. The STALE line covers that
case by telling the ride thread, live, what it is actually running.

**Drift hazard:** `~/.config/systemd/user/ride-watch.service` is a real file,
not a symlink to the copy in this directory, so the two can diverge silently.
They are identical as of 2026-08-31. Check with:

```
diff -u ride-watch/ride-watch.service ~/.config/systemd/user/ride-watch.service
```

## Replaying a past ride

Run any historical file through the exact same code path at full speed:

```
python3 ride-watch/ride_watch.py --replay ~/otp-debug-logs/debug-2026-07-29.jsonl
```

Replay is **always dry-run**: it never sends a Pushover and never spawns a ride
thread. It also writes to `~/otp-debug-logs/ride-watch/replay/` by default so it
cannot clobber the live status file. Use `--watch-dir DIR` to send output
somewhere else. It prints every finding with its local time and the pushes that
*would* have gone out.

## Tuning the rules

Thresholds are module constants at the top of `ride_watch.py` —
`STOP_COLLAPSE_MAX_PROGRESS`, `DEVIATED_STREAK_MS`, `GPS_GAP_MS`,
`REROUTE_STORM_COUNT`, `DISTANCE_SPIKE_FAR_M`, `NOTIFICATION_REPEAT_COUNT`,
`MOTION_PROGRESS_PCT`, `MOTION_DISPLACEMENT_M`, `MAX_PAGES_PER_TRIP`,
`PUSH_MIN_INTERVAL_MS`, `PAGE_COALESCE_MS`, `PAGE_RANK`, and so on.

The honest workflow for changing one: replay a real ride before and after,
compare which findings appear, and add a test. A rule that pages on a
correct app behaviour is worse than a rule that stays quiet — the rider gets
two interrupts per ride and they should both be worth reading.

Ride-thread knobs live next to them: `THREAD_HEARTBEAT_MS`, `THREAD_MOVING_MS`,
`THREAD_READY_TIMEOUT_S`, `THREAD_SUBMIT_DELAY_S`, `THREAD_LINE_MAX`.

Environment overrides: `RIDE_WATCH_DRY_RUN=1` (log pushes instead of sending
them), `RIDE_WATCH_LOG_DIR`, `RIDE_WATCH_DIR`, `RIDE_WATCH_PUSHOVER_CREDS`,
`RIDE_WATCH_REPO`, `RIDE_THREAD_ENABLED`, `RIDE_THREAD_NAME_PREFIX`.

**Units, once, so nothing has to guess again:**
`UPDATE_PROGRESS.currentLegProgress` is a **percentage on 0-100**;
`UPDATE_ROUTE_MATCH.progressAlongLeg` is the same quantity as a **0-1
fraction**. Everything the daemon renders goes through `fmt_pct()`, which keeps
a decimal below 10 — `0.3077` is `0.3%`, not `0%` and emphatically not `31%`,
which is what a reply agent told the rider on 7/31 after reading a unitless
key. Context keys carry the unit in the name (`legProgressPct`), and the digest
states the convention outright.

## Tests

```
python3 ride-watch/test_ride_watch.py
```

193 tests, stdlib `unittest`, no installs. Synthetic streams cover every rule
(both the firing case and the case that must stay quiet), the state machine, and
page ranking (supersession inside the window, tie-breaking, flush on a quiet log,
flush on trip end).

Three suites replay real telemetry, because a rule that only works on synthetic
data does not work:

- `TestRealIncidentReplay` (7/29) — the 17:28 incident is caught, the four
  page-worthy findings in those eight seconds cost the rider exactly one
  interrupt, the one they get is `stop-count-collapse`, and the two progress
  teleports (17:05, 17:20) are flagged.
- `TestNotificationStormReplay` (7/31) — `notification-repeat` fires at
  **11:53:07**, before the rider had to type the complaint by hand, and the 14
  buzzes cost them one finding.
- `TestThreadCadenceOnRealRides` (both) — every push is a milestone, one thread
  and one kickoff per ride, a push per finding and nothing extra, heartbeats
  bounded by the length of the ride, and a whole ride fits in a handful of lines.

`TestRideThread` covers the conversation itself against stubs: spawn on trip
start (and on mid-stream adoption), the kill switch, spawn and push failures
that must not stop the ride, the tmux-session filter that protects a
hand-spawned thread, milestone cadence, the digest being current at every push,
and the fallback page. `NoProcesses` asserts the thing this design exists for —
**a rider note spawns no process at all.**

`TestDuplicateRecords`, `TestStalledProgressContext`,
`TestReplanNotConverging`, `TestReportDeadline` and `TestDaemonProvenance` cover
the 2026-08-31 work. Two of those are worth calling out because the naive
implementation passes them by accident:
`test_a_head_that_moves_does_not_change_the_reported_running_sha` pins that the
stamp is a startup constant and not a write-time `rev-parse` (which would make
a stale daemon claim to be current), and
`test_the_deadline_survives_the_trip_being_deleted` pins that the report
deadline does not live on the `Trip` that `_end_trip` deletes.

Fourteen tests skip when the real logs are only present gzipped; `gunzip -k`
the day you need (`debug-2026-07-29`, `-07-31`, `-08-10`) to run them.

Tests never touch the network, never start tmux, and never invoke the real
`claude`; the Pushover path is verified by credential parsing only.

The Flask side of the contract is `transitnav/test_ride_api.py`
(`venv-prefs/bin/python test_ride_api.py`), which asserts the `/api/ride-status`
payload shape the console reads — including that append-only reply rows collapse
by `id`.
