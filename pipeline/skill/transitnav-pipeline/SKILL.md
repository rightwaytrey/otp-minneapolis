---
name: transitnav-pipeline
description: Run one action of the TransitNav delivery pipeline — triage (intake), or a cycle step (plan, build, ship-dev, verify, ship-prod), or record answers — as kicked off from the self-hosted Pipeline board (https://rwtpc4.tail9d6464.ts.net:9600/, store read and written with ~/bin/pipeline-db) or by hand (/transitnav-pipeline <action> [cycle]). Use whenever a pipeline inbox Monitor emits an "INBOX #K …" press line (START, ANSWERS, MESSAGE, PLAN APPROVED), or the user asks to run/check a pipeline step or "the pipeline board".
---

# TransitNav pipeline — one action per invocation

The board is https://rwtpc4.tail9d6464.ts.net:9600/ (self-hosted on rwtpc4
since 2026-10-09, tailnet only — see *The board's store and presses*; the old
Artifact RtFoFD16ijxfYUxBXABgQ2 is retired, never read or write it) — ONE page
with two views behind a Chat · Classic switch at the top (the rider's choice,
2026-10: "one link, two views"; Chat is the default, each device remembers the
last one, both views are always live on the same docs and send the same
presses). **Chat** is one conversation per cycle (and one for Triage) built
from `feedback[]`, step summaries, branch reports and questions, with the one
allowed next press and a text box that answers the open question, sends plan
notes, or sends a `MESSAGE — cycle N`. **Classic** is the tabbed app rebuilt
2026-09-21 ("a proper web app, tabs for each step, better feel for the flow of
each individual item"): Flow (every row on a six-station track Planned →
Building → Built → On dev → Verified → Prod, a "Needs you" list, an item drawer
with the row's journey and its branch report) · Triage · Cycles (Plan · Build ·
Ship dev · Verify · Ship prod per cycle) · Log, with inline Requested/Running/
Done status. Either view writes `wake {ok, at, why}` on a requested step / on
`intake` recording whether its press reached an inbox. Its database (files
under `~/.claude/pipeline/db/`, read and written only through `~/bin/pipeline-db`):

- `pipeline/board` — `facts` (devBundle, prodBundle, otprrMain, daemon,
  openRows), `intake` (the Triage panel: status, requestedAt/startedAt/
  finishedAt, note, summary, detail, `pending`, `lastTriageAt`,
  `triagedThroughMs`, **`scannedAt`, `inputs[]`** — see *The intake ledger*
  below), `log[]` (keep ≤ 60), `updatedAt`.
- `cycles/<id>` — one doc per cycle (`c1`, `c2`, …): `number`, `createdAt`,
  `title`, `rows[] {num, title, kind: build|measure|daemon|blocked,
  branch, status}`, `questions[] {id, text, rows[], answer, answeredAt}`,
  `branches[] {name, label, sha, kind: build|measure|daemon, rows[],
  shippable, decision: approve|hold|null, decidedAt, summary, measured,
  tests, files, screenshots[], risks, vehicle}` — ONE OBJECT PER AGENT'S
  BRANCH, rendered as a tab with a Ship / Hold control (per-branch
  decisions added 2026-09-21: "we'll decide to ship on an individual basis";
  since 2026-10-08 `null` and `approve` both mean *ships*, only `hold` keeps
  it out — see *Auto-advance*), `plan {revision, revisedAt, changes, approvedAt, approvedBy}`,
  `feedback[] {t, by: rider|claude, text}` (the plan review conversation),
  `rows[].keep` (false = the rider dropped the row from this cycle),
  `bundle {dev, sha, prod}`, `steps {plan|build|ship-dev|
  verify|ship-prod: {status idle|requested|running|done|blocked (plan also
  draft = a revision posted, awaiting the rider; mode: revise on a round),
  requestedAt, startedAt, finishedAt, note, summary, detail; requestedBy is
  the rider's id, or `"auto"` / `"ride"` for a step the thread started}}`,
  `retiredAt`.

Row `status` vocabulary the page renders: `build`, `built` (branch green),
`measure`, `measured`, `daemon`, `blocked` (needs an answer), `on-dev`,
`pass`, `fail`, `not-exercised`, `in-prod` (shipped to production with the
cycle retired, waiting for a ride to close it), `closed`.

**The model the rider chose (all three "yes", 2026-09-21):** intake is
continuous and not part of any cycle; a cycle is one dev bundle; cycles
overlap (cycle 2 builds while cycle 1 waits on dev for a ride); questions live on the cycle
card and block only the rows they name, never the cycle. **A cycle retires
when it ships to production** (rider, 2026-09-25: "why is every cycle
waiting in a verify when they are all shipped to production"). Its untested
rows do not hold it open: they become `status: in-prod` and wait in the
backlog for a ride, and Triage closes them on the ride report that shows
them working. Before 09-25 a cycle stayed open until every row was
settled, so cycles 1–6 sat in production for days with Verify red.

**A cycle drives itself once it has started (rider, 2026-10-08).** Its
thread moves through the phases on its own and waits for the rider at
exactly two points: **plan approval** and **ship to prod**. Build runs on the
"Approve plan · build it" press; ship-dev runs on its own as soon as Build
finishes, taking every branch that passed its gates unless the rider pressed
**Hold** on it first; Verify runs on its own when a ride report lands that
`checks.jsonl` shows ran on the dev app with this cycle's code. After
ship-prod the thread retires the cycle and closes its own window. The router
no longer manages cycles at all (see *Routing*). Why: on 10-08 cycles 8–11
all sat with Verify `requested` for eleven hours — c8–c10's presses never
reached an inbox (one wake was `rate_limited`), c11's reached it but the
thread's Monitor had died ("re-arming it failed twice"), and the router's
sweep caught none of them.

**A press acts on the cycle it was pressed on, and on no other (rider,
2026-09-24, "a very serious bug").** Verify used to fan out: one press on any
cycle marked every cycle on dev `requested`, and Cancel cancelled them all;
ship-prod retired every cycle its sha contained. Now every button — Verify
included — writes and runs only `cycles/<that id>`. No stage writes another
cycle's doc: a cycle carried along by someone else's bundle is recorded when
its own press comes (it finds its sha already on dev / in prod and says so).

The backlog protocol in `~/.claude/CLAUDE.md` still governs everything:
`~/.claude/plans/please-make-a-centralized-sharded-petal.md` is the only
index of open work; the board is a **status mirror and a control surface,
never a second backlog**.

## The board's store and presses (self-hosted since 2026-10-09)

The board moved off Claude Artifacts on 2026-10-09: the rider wanted the cycle's
real terminal in the page, and an Artifact sandbox can frame nothing. It is now
`~/pipeline-board/` on rwtpc4 — `server.py` (systemd --user unit
`pipeline-board`, 127.0.0.1:8098, exposed tailnet-only by `tailscale serve
--https=9600`), `shim.js` (the page's `window.claude`), `index.html` (the
Artifact's page, built by `build-index.py`, otherwise unchanged). The Artifact
db was migrated with its versions (board v1253, c15 v43, …) and is frozen.

**The store** — one JSON file per doc, `~/.claude/pipeline/db/<collection>/<doc>.json`
= `{data, version, updatedAt}`. Agents and threads use `~/bin/pipeline-db`
(full path — `~/bin` is not on the sessions' PATH):

    ~/bin/pipeline-db get cycles/c15            # {"exists","data","version"}
    ~/bin/pipeline-db list cycles               # {"docs":[{"id","data","version"}]}
    ~/bin/pipeline-db update cycles/c15 --data '{"steps":{"verify":{"status":"running","startedAt":1791…}}}' --if-version 43
    ~/bin/pipeline-db update cycles/c15 --file /path/patch.json --if-version 43
    ~/bin/pipeline-db set    cycles/c15 --file /path/whole.json --if-version 43

`update` deep-merges objects, **replaces arrays wholesale** (so `feedback[]`,
`log[]`, `rows[]`, `questions[]`, `branches[]` are read-modify-write: `get`,
append in a temp JSON file, `update --file` with the whole new array), and
`{"__delete__": true}` removes a key. Always pin `--if-version` with the
version you read: exit 3 prints `{"error":"version_mismatch","version":N}` —
someone wrote since your read; re-read and redo, never drop the pin. Every
write bumps `version` and pushes the page live (SSE) — no reload needed.
Wherever this skill says `ArtifactData`, `write_db` or `read_db`, it means
`~/bin/pipeline-db`.

**Presses** — the page's buttons POST to the server, which runs
`~/bin/pipeline-route.sh <N|board> --thread - --key <key>` itself: a press
naming `cycle N` goes straight to `~/.claude/pipeline/cN/inbox.log` (and starts
the cycle's window if it is not there); `START triage` (even "… then plan
cycle N") and any press naming no cycle go to the router's inbox,
`~/.claude/pipeline/board/inbox.log`, which the router follows with
`~/bin/pipeline-board-watch.sh`. A press can also be sent by hand:
`~/bin/pipeline-db press "MESSAGE — cycle 15 (msg …)…"`.

**There are no comment threads any more.** Every inbox line says
`thread=-`; there is no auto-reply, nothing to `resolve`, no
`acknowledge_duplicate`. Wherever this skill says "reply on the thread", the
reply IS the step's `summary`/`detail` plus one `{t, by: "claude", text}` line
appended to `cycles/cN.feedback[]` (for triage: `intake.summary` and the board
`log`) — that is what the rider reads. `wake` on a step now means "the press was
written to an inbox".

## Who runs a press

**The board's own worker session**, since 2026-09-21 22:46: `~/bin/pipeline-worker.sh`
keeps one Claude session named "pipeline board" (no Remote Control since 2026-10-09, the rider) alive as window `board` of
the `transitnav` tmux session (`tmux attach -t transitnav`; the claude-monitor board
shows it under its TransitNav tab; stop it for good with
`touch ~/.claude/pipeline-worker-stop` then `/exit`; moved there from its own
`tmux -L pipeline` server on 2026-09-22). Since 2026-10-09 its kickoff arms a
Monitor on `~/bin/pipeline-board-watch.sh` (its own inbox) instead of watching
the Artifact's comments — no session watches comments any more. (Before 09-21 a
press woke whichever session had last published the board: "who is hijacking
this session".)

**Since 2026-09-24 that session is a router, and each cycle has its own thread**
(the rider: "an agent per cycle — then context is not polluted together and
follows the flow of each loop"). The same morning a Verify press for cycles 1–6
(08:15:31, thread 432d2c2d) landed in the same minute as two others and never
reached the worker: it saw "both notifications", a tool-less auto-reply said
"I can't run Verify from this session", and the page still recorded
`wake.ok: true` (that flag means the comment was *sent*, not *delivered*).
Nothing re-checked the db, so six cycles sat at `requested`. Since
2026-10-08 each cycle's thread re-checks its own doc every 30 minutes (the
*self-check*), which replaced the router's sweep.

## Routing — the router's brief (`pipeline-worker.sh`, window `board`)

The router does two things and nothing else: (a) it runs **Triage** and the
15-minute `scan`; (b) it **opens a new cycle** (on `then plan cycle N`). Since
2026-10-09 it forwards nothing — the board's server routes every press that
names a cycle straight into that cycle's inbox. It does not sweep cycles, does
not stop or retire threads, and makes no decision about any cycle — each
cycle's own thread does all of that. Its Monitor,
`~/bin/pipeline-board-watch.sh` (re-armed every 15 min, which is also its scan
tick), emits `INBOX #K key=… thread=- text=<path> :: <first line>` for each
press in `~/.claude/pipeline/board/inbox.log`; read the `text=` file, act,
then append K to `~/.claude/pipeline/board/handled`:

| Press | Router does |
|---|---|
| `START triage` (± `then plan cycle N`) | runs **triage** itself (below). On "then plan", creates `cycles/cN` as described under *What a button sends*, then routes `START plan — cycle N` (text: the rows triage opened) with `~/bin/pipeline-route.sh N --thread - --key plan:<requestedAt>` (text on stdin). The triage summary says the plan is with cycle N's thread; the thread writes the draft. |
| anything else (a press that named no cycle) | if it does name a cycle after all, route it verbatim with `pipeline-route.sh N --thread - --key <its key>`; otherwise answer it in one board `log` line. |

`pipeline-route.sh` starts the cycle's thread if its window is not there
(mechanical; it will not start a cycle that has retired — the press is still
written to the inbox, and the script says so).

**Every 15 minutes** (self-paced loop): `scan` (silent), and
`intake.status: requested` with no `startedAt` → run triage. Nothing about
cycles: a press whose comment never reached the router is picked up by its
own cycle's self-check from the db.

## Cycle threads — a cycle thread's brief (`pipeline-cycle-thread.sh N`, window `cN`)

A cycle thread is a Claude session named "cycle N" (no Remote Control since 2026-10-09, the rider) that owns
`cycles/cN` from plan to retirement and keeps its story in one context: every
revision, answer, branch report and ride verdict of that cycle. It never
watches the board's comments. It follows ONE Monitor,
`~/bin/pipeline-cycle-watch.sh N` (timeout 30 min), which emits two kinds
of line:

- `INBOX #K key=… thread=… text=<path> :: <first line>` — a press the router
  forwarded. Read the `text=` file and run that one action on **cycle N
  only** as the sections below say (*On every invocation* applies). Append K
  to `~/.claude/pipeline/cN/handled` as soon as the step is marked `running`
  (a MESSAGE or ANSWERS: once answered) — from then on the step in the db is
  the record, and the self-check finishes it if this session dies. Every
  line carries `thread=-` (no comment threads since 2026-10-09): when the
  action finishes, the reply is the step `summary` plus one `{by: "claude"}`
  line in `feedback[]`. A line for a step you are already running is not new
  work.
- `RIDE <session> start=<ms> end=<ms> req=<request path|->` — a trip has
  ended (since 2026-10-09 the trip itself is the trigger, not the vault
  report). Run *Verify on a ride* below. The watcher keys trips by session +
  start in `cN/trips.seen`; `req=-` is a clean ride (the daemon writes a
  `report-request-*.json` only for a ride with findings —
  `ride_watch.py` `_write_report_request(trip) if n > 0` — but every ended
  trip updates `state.json` `lastTrip`, and the watcher reads both).

**When the Monitor expires** (every 30 min), run the **self-check** and
re-arm the same command. Also run it once at startup, before anything else.

### The self-check — this cycle only

Read `cycles/cN` (and nothing else of the db but `pipeline/board.facts`), then:

1. `retiredAt` set → *Closing the thread* below.
2. A step `requested` with no `startedAt` and `requestedAt` more than 5 min
   old → run it as if its press had arrived (its comment never reached the
   inbox — e.g. a `rate_limited` wake). Log `picked up by the cycle's
   self-check` on the board. Plan approval and ship-prod are never *invented*
   here: only a step the page already marked `requested` is run.
3. A step `running` that this session is not working on (no live agent of
   yours for it — after a relaunch, every `running` step is in this state) →
   find out what was done (worktrees, branch commits, otprr `main`, the dev
   store's manifest, the backlog) and finish it from there; never start over
   blindly.
4. Build `done` and ship-dev `idle` → run ship-dev (*Auto-advance*). Ship-dev
   `done` and a built, shippable branch that is not on `main`, not held, and
   whose `decidedAt` is later than ship-dev's `finishedAt` (the rider released
   a Hold) → run ship-dev again for that branch.
5. Ship-dev `done`, rows still `on-dev`/`not-exercised`, and a trip that
   ended after both ship-dev's `finishedAt` and `steps.verify.lastRide.endedAt`
   (a `report-request-*.json` in `~/otp-debug-logs/ride-watch/` with a later
   `endMs`, or `state.json` `lastTrip.endMs`) → *Verify on a ride* with it (a
   `RIDE` line that was lost).
6. Otherwise nothing. A plan in `draft`, an open question, ship-prod: those
   wait for the rider. The self-check is silent when it finds nothing — no
   board log, no feedback entry, no reply.

### Auto-advance — what runs without a press

- **Build** starts on `PLAN APPROVED · START build` (the rider's one press).
  ANSWERS that unblock rows of an approved plan build those rows at once —
  while Build runs (launch their agents) or after it is done (a re-build of
  just those rows, `steps.build` back to `running`), then ship-dev follows.
- **Ship-dev** starts the moment Build is `done`, in the same turn: it ships
  every built branch with `shippable !== false` and `decision !== "hold"`
  (null and `"approve"` both mean *ship*). A branch the build gate failed
  never gets that far — Build writes it with `decision: "hold"` and the
  failure in `risks`. **What the rider sees:** each branch tab appears on the
  board as its agent's report is checked (not all at the end), with **Hold**
  available until ship-dev starts; when Build finishes the thread appends one
  `{by: "claude"}` line to `feedback[]` — "Build done: shipping A, B to dev
  now; held: C; gate failed: D" — and the chat shows it. While ship-dev
  runs the board locks Ship/Hold; re-read the decisions once more right
  before the merge, so a Hold that landed during the lock-wait counts.
  A held branch the rider later sets back to Ship goes out on the next
  self-check (rule 4).
- **Verify** runs on its own on each trip that ends (below). There is no
  Verify button any more (rider, 2026-10-09: "automate the verification —
  it should be able to tell when verify can run from a user run").
- Ship-prod and plan approval never run without the rider's press (or the
  rider asking in this session directly — then write the same doc fields
  the page would, so the board shows it).

### Verify on a ride — never for another cycle

On a `RIDE <session> start=… end=… req=…` line (or self-check rule 5), for
cycle N only. The ride is the trip: its window is `start`..`end` from the line
(or from the request file / `state.json` `lastTrip`). The vault report in
`~/obsidian-vault/Claude/ride-watch/` (the request's `reportPath`) is optional
evidence the agent reads if it exists; nothing waits for it.

The thread does rule 1 from its doc alone; if that passes it starts ONE
fresh Opus agent that does rules 2–4 itself and reports `applies` plus the
verify verdicts. The thread never opens the logs. Whatever the outcome, the
thread then writes `steps.verify.lastRide` (rule 5) so the board can say what
happened to that ride.

1. Skip unless ship-dev is `done`, the cycle is not retired, it has rows
   `on-dev` or `not-exercised`, and the trip ended after ship-dev's
   `finishedAt` (`applies: "no-rows"` otherwise; still write `lastRide` if the
   trip ended after ship-dev — a ride before the code shipped is not
   recorded at all).
2. **Wait for the mirror.** `~/otp-debug-logs` is an rsync mirror of the
   Linode and can lag ~30 min: before scoring, the agent waits (Monitor with
   an until-loop, at most 35 min) until `~/otp-debug-logs/debug-YYYY-MM-DD.jsonl`
   holds records of this session at or after `end`. Timed out → score what is
   there and say so.
3. **Which app and bundle ran** come from the Linode's check logs, never from
   a report: `ssh -o BatchMode=yes rwt@100.126.171.72 'tail -n 400
   app-bundles-dev/checks.jsonl; tail -n 400 app-bundles/checks.jsonl'`. Take
   the `native` version from the day file (the RIDE line's session); the
   telemetry `device` id is not the checks' `device`, so match on time and
   `native` (dev app 0.0.x, prod app 1.0.x; ignore `device: ship-web-check`).
   A real-device check in the dev store before the ride's end → dev app,
   bundle = its `running`. Only prod-store checks → **a prod-app ride:**
   `applies: "prod-app"`, it does not verify dev rows. Real-device checks in
   both stores inside the window → do not guess: one `{by: "claude"}` line in
   `feedback[]` saying which ride and why it is unclear, and stop.
4. The bundle carries this cycle's code when it is `bundle.dev` or a later
   dev bundle whose web sha contains `bundle.sha` (`git -C
   ~/projects/otprr/otp-react-redux merge-base --is-ancestor <bundle.sha>
   <web_sha>`; the dev store's current manifest names `web_sha`). Daemon
   rows: any ride after the daemon restart. No → `applies: "older-bundle"`.
   Yes → `applies: true`: mark `steps.verify` `running` (requestedBy
   `"ride"`, note = the session), log `verify started on its own (ride
   <session> <end clock>)`, and run `verify` below with that ride. Reply goes
   to `feedback[]`: one line of counts.
5. **Always record the ride** (unless rule 1 found it ended before the code
   shipped): `steps.verify.lastRide = {session, endedAt: <end ms>, app:
   "dev"|"prod", bundle: "<bundle the checks show>", applies: true |
   "prod-app" | "older-bundle" | "no-rows", scoredAt: <ms> (only when
   applies is true), counts: {pass, fail, notExercised} (only when scored)}`
   — `pipeline-db update cycles/cN` with `--if-version`. The board shows it
   on the Verify step and in the cycle's status line ("Last ride Oct 9
   4:01 PM · App Store app · doesn't count for dev rows").

Never fan out: every live cycle's thread sees the same `RIDE` line and
decides for itself. Another cycle's rows and `steps` are never touched.

### Closing the thread

After ship-prod finishes (or a self-check finds `retiredAt`): write the
final summary and `feedback[]` line as usual, then `touch ~/.claude/pipeline/cN/retired
~/.claude/pipeline/cN/stop` and `tmux kill-window -t transitnav:cN` — that
ends this session and its window; the stop file keeps the wrapper from
relaunching, and `retired` keeps `pipeline-route.sh` from starting it again.
If the kill is refused, say so in one line on the board log and leave the
stop file; the next `/exit` ends it.

### The rider talking to this session directly

The rider can type into the session directly — in its live terminal on the
board's Chat view, or in tmux window `transitnav:cN` (there is no Remote
Control session to open in the Claude app since 2026-10-09). The board only shows `feedback[]`, so after every such turn
append the gist — one or two plain sentences each — as `{t, by: "rider",
text}` and `{t, by: "claude", text}` to `cycles/cN.feedback[]`
(read-modify-write, pin `if_version`). Monitor events, the kickoff and the
self-check are not the rider and are never mirrored. A direct instruction
from the rider is their own consent, within cycle N.

- **Every phase is still a fresh Opus agent** (plan, build per branch, ship,
  verify). The thread dispatches, checks the agents' claims in the diff, and
  writes the board — the 09-23 rule is unchanged; what is per-cycle is the
  dispatcher's memory, not the workers'. Since 2026-10-08 the agent also
  writes its own live progress to `feedback[]` as it works (*Agent progress*
  below) — the thread's end-of-step write is still the one that sets
  `summary`/`detail`/`rows[]`/`status`.
- **The thread is a manager (Opus since 2026-10-09 — on Fable the threads kept failing their turns on the Fable limit); every piece of
  WORK is an Opus agent** (rider, 2026-10-08: "use opus!! all agents doing
  work should. fable only for decision or manager agents"). The thread's own
  tools are limited to: its Monitor; reading the inbox line and its text
  file; reading and writing `cycles/cN` and the board log; `Agent` with
  `model: "opus"`; reading an agent's report and spot-checking a claim with
  one short read-only command (`git log -1`, `git diff --stat`, `ls`). It
  does NOT itself ssh to the Linode, read `~/otp-debug-logs`, run replays,
  run `merge-base` analysis, read ride reports, or edit the backlog or record
  files — the agent for that phase does all of it and reports back, and the
  thread checks and writes the board. A self-check is doc reads and writes
  only; whatever it decides to run, an Opus agent runs.
- **Shared things need a lock, because cycles now run at the same time.**
  Merging to otprr `main` / otp-minneapolis `master`, `ship_web.sh`, a daemon
  restart and a Linode deploy happen only inside `mkdir
  ~/.claude/pipeline/ship.lock && echo "cN $(date +%s)" >
  ~/.claude/pipeline/ship.lock/owner` … `rm -rf ~/.claude/pipeline/ship.lock`.
  If the mkdir fails, another cycle is shipping: wait (Monitor with an
  until-loop on the directory), and treat a lock older than 2 h as stale only
  after checking that its owner's window is idle. The backlog and record files
  are shared too: re-read right before each edit, keep edits small, never
  rewrite the file wholesale.
- A cycle thread ends itself when its cycle retires (*Closing the thread*).
  If the rider `/exit`s it, it relaunches, re-arms from `handled` and runs
  the self-check.

## What a button sends

The page writes the request into the db first, then sends a press, which
the server writes into the owning inbox (*The board's store and presses*).
One action per press:

- `START triage` — intake. `START triage — then plan cycle N` means the rider
  ticked "then open cycle N with what it finds and plan it": finish triage as
  below, then, ONLY if triage opened or touched at least one row that is not
  already in a live cycle, create `cycles/cN` yourself in the page's shape
  (`{id, number, createdAt, title, rows: [], questions: [], branches: [],
  bundle: {}, steps: {plan: {status: "running", requestedAt, startedAt,
  requestedBy: "triage"}}}`, N = highest existing number + 1 — re-read the
  cycles collection right before creating it), log `opened (from triage)` on
  the board, and run `plan` on it with those rows as the named candidates.
  Rows already `on-dev`, `built` or planned in a live cycle stay there; a new
  sighting on such a row is a note on it, not a second plan. If nothing
  qualifies, open no cycle and say so in the triage reply. The thread gets ONE
  reply covering both (triage counts, then the plan's row list), and Build
  still waits for the rider's press.
- `START plan — cycle N` — the page has already created `cycles/cN` with
  `steps.plan.status = requested`. **Plan is iterative (since 2026-09-22
  evening, the rider: "it absolutely has to be an iterative process with
  back and forth from me").** A plan lands as a **draft** (`steps.plan.status
  = "draft"`, `plan.revision = 1`), never as `done`; only the rider's
  "Approve plan" press sets `done`, and Build stays locked until then.
- `START plan — cycle N · revise (revision R)` — the rider sent notes on the
  draft: `feedback[]` holds the conversation (`{t, by: rider|claude, text}`)
  and `rows[].keep === false` marks rows they dropped. Read both, revise the
  plan in the backlog's `## Pipeline cycle N — plan` section, then write
  `plan.revision = R`, `plan.revisedAt`, `plan.changes` (one or two sentences
  in the rider's terms: what moved, what was dropped or added and why),
  `steps.plan.summary` (the whole current plan, not a delta), append a
  `{by: "claude"}` entry to `feedback[]` answering their notes point by
  point (that entry is the reply), and set `steps.plan.status` back to
  `"draft"`. Never set `done`.
- `PLAN APPROVED — cycle N (revision R) · START build — cycle N` — since
  2026-09-23 the rider's "Approve plan · build it" press is ONE press: the page
  sets `steps.plan.status = done` and `steps.build.status = requested` together
  and sends this one comment. Record the approval on the backlog's cycle plan
  section (revision, time); rows with `keep === false` leave the cycle's plan
  and go back to the backlog's candidates (drop them from `rows[]` on the cycle
  doc, or mark `status: "next cycle"` — the page shows "Dropped" either way);
  then run `build` below on the cycle in the same invocation (fresh agents, one
  per branch), and ship-dev after it (*Auto-advance*). Since 2026-10-08 the
  page has no Build or Ship-dev button.
- `START build — cycle N` / `START ship-dev — cycle N` — sent only by the
  page's **Resume** on a `blocked` step (the comment still carries
  `Approved:`/`Held:` lines; read the decisions from the doc instead, they
  may have changed). `START ship-prod — cycle N` — the rider's press.
- `ANSWERS — cycle N` followed by `Q1 (21.4): …` lines — the page has
  already stored `answer`/`answeredAt` on the questions. While the plan is
  in review (`draft`), the page also sets `steps.plan.status = requested`
  with `mode: revise`: record the answers on their rows AND post the next
  plan revision as above (an answer usually changes rows or branches), then
  reply with what changed.

- `MESSAGE — cycle N (msg T)` followed by the rider's text — the rider typed
  in the cycle's chat on the board; the page already appended `{t: T, by:
  "rider", text}` to `feedback[]`. **The board shows only `feedback[]` and step
  summaries, not comment threads**, so reply by appending `{t, by: "claude",
  text}` to `cycles/cN.feedback[]` (read-modify-write) — that is the whole
  reply. A message is not a press: answer it, and do only what it asks
  within cycle N. Plan approval and ship-prod still need their buttons; a
  message may ask for what runs on its own anyway (build an unblocked row of
  the approved plan, ship a released branch, verify a ride).

Plan approval authorizes Build, Ship-dev to the **dev** app, and Verify on
dev rides for that plan — nothing reaches production without the Ship-prod
press. No hold for dev (zero users there).

## On every invocation

1. Read the doc(s): `~/bin/pipeline-db get pipeline/board` and
   `~/bin/pipeline-db get cycles/cN` (note each `version`). Confirm the step is
   `requested` (or, for an automatic step, `idle` — then mark it requested
   with `requestedBy: "auto"` yourself); if its predecessor in the cycle is
   not `done`, say so and stop (the rider can override in words).
2. Mark it `running` with `startedAt` (`~/bin/pipeline-db update cycles/cN
   --data … --if-version V`; nested objects merge, arrays replace wholesale —
   read-modify-write `log`, `rows`, `questions`).
3. Do the work (below).
4. Finish: `status` `done` or `blocked`, `finishedAt`, a rider-facing
   `summary` (what changed, in their terms), a `detail` string with shas /
   bundle numbers / row numbers; update `rows[].status`; refresh
   `pipeline/board.facts`; append a `log` entry.
5. Reply: the `summary` plus one `{by: "claude"}` line in `feedback[]` (a
   `blocked` step asks its question there too). There are no comment threads
   to reply on or resolve (since 2026-10-09).

Subagents: every `Agent` call sets `model` explicitly — `"opus"` for plan,
verify, triage and anything measure-first; `"sonnet"` for a well-specified
build branch and for ship-dev (the rule is in `build` below; rider,
2026-10-09) — and every phase of every
cycle (triage, plan, build, verify, ship) starts a **fresh** agent — never
`SendMessage` a plan agent to make it build, or a cycle-N agent to work on
cycle N+1 (rider, 2026-09-23). Git: never `git stash`
in otprr; commit only your own paths; the shared checkouts
(`~/projects/otprr/otp-react-redux`, `~/projects/otp-minneapolis`) are
merge-only — all edits happen in worktrees.

### Agent progress — the board shows the agent's real chat, not just the end

An `Agent` call runs in the background: the thread is notified only when it
**finishes**, so until 2026-10-08 the board showed nothing of a phase but one
summary line written after the fact (rider: "I get so very little context on
what an agent is doing in the chat window — give me the real chat from the
agent"). Fix: **the dispatched agent narrates itself**, straight into
`cycles/cN.feedback[]` — the same field the Chat view already renders — so the
rider sees it live instead of only at the end.

Every build/verify/ship/plan/triage agent prompt must include this instruction
verbatim (adapt only the checkpoint list to the phase): *"As you work, append
your own progress to `cycles/cN.feedback[]` with `~/bin/pipeline-db`
(`get cycles/cN`, append to the array in a temp JSON file, `update cycles/cN
--file <it> --if-version <the version you read>`; exit 3 means it changed —
re-read and redo): one `{t, by:
'claude', text}` entry at each real checkpoint — starting the row / what you
read in current source, the gate's result with real numbers, the commit sha,
anything you ruled out, and your final report before you return. Plain
sentences, one or two per entry, the rider's terms — not a tool-call log and
not your internal reasoning. Cap it around 6–8 entries for the whole run; a
quiet phase (nothing to report between two checkpoints) skips an entry rather
than padding one in."*

This narration is **in addition to**, not instead of, the thread's own
end-of-step `summary`/`detail`/`rows[]` write (*On every invocation* above) —
the agent's entries are the play-by-play, the thread's is still the record.
A self-check or router action that runs no agent (a doc read, a re-arm) writes
nothing to `feedback[]`, same as before.

### The literal terminal, embedded in the board — live ttyd

Since the board went self-hosted (2026-10-09) the Chat view embeds each
cycle's real tmux window — live and **writable** — as a ttyd iframe (board:
`:19999`, cycle N: `:20000+N`, each via `tailscale serve`). The server starts
them on demand from `/api/terminals`; each attaches through a grouped session
`web-cN` (`tmux new-session -A -s web-cN -t transitnav ; select-window -t
cN`, destroyed when unattached), because a plain `tmux attach -t
transitnav:cN` makes cN the shared session's current window for every
attached client. A narrower viewer resizes that window (tmux
`window-size latest`). **Do not write `terminalSnapshot` any more** — the
2026-10-09 morning workaround (a text snapshot through the Artifact db) is
gone with the sandbox that forced it; the old fields stay in the docs,
unread. The "Terminal" button (→ Termius via the 7443 session dashboard)
stays as it is. `~/pipeline-terminals/serve.py` (the read-only mirror page on
:9500) is stopped and superseded.

## The intake ledger (`intake.inputs[]`, since 2026-09-22)

The Triage section is a **ledger**: one line per comment, ride report or
nightly, with what it became and where that is now (the rider: "it should be
easy to parse exactly what comments have come in, what reports there are, and
if they've been evaluated and assigned to a build yet"). The page derives
"where now" itself from the cycle docs by row number; the session only writes
the entries. One entry:

```
{ id: "c-<ms>" | "r-<report basename>" | "n-<date>", kind: comment|report|nightly,
  at: <ms it arrived>, text, image: <path|null>, session, source,
  seenAt: <ms the scan registered it>, triagedAt: <ms|null>,
  flag: "<one line the scan knows, e.g. 'report promoted nothing; the daemon paged you 09:28'>",
  became: null                       // not looked at yet — the page tints the line
        | { type: row|sighting|praise|question|none, rows: ["25.1"], note: "2nd sighting" } }
```

`~/.claude/skills/transitnav-pipeline/scan.py [--since MS]` prints candidate
entries (rider notes from the day files, reports by mtime, the newest nightly)
with `became` unset. Keep ≤ 80 entries; drop entries older than 14 days whose
`became` is set.

### `scan` — register, never evaluate (every 15 min in the router)

`python3 ~/.claude/skills/transitnav-pipeline/scan.py --since <max inputs[].at>`,
merge by `id` (skip ids already present), set `seenAt`, `became: null`, and a
`flag` where the journal says the daemon paged for an unpromoted report
(`journalctl --user -u ride-watch --since … | grep 'no promotion'`). Write
`intake.inputs` and `intake.scannedAt`. No log entry, no thread reply — a scan
is silent. Do not scan a ride in progress's own notes into the ledger until
the ride ends (`current-ride.md`); the report that follows carries them.

## `triage` — comments → backlog rows (intake)

Run `scan` first so the ledger is complete. Then evaluate every entry whose
`became` is null and set it: `row` (new row numbers), `sighting` (existing
row numbers + "Nth sighting" in `note`), `praise`, `question` (a question back
to the rider, which goes in the summary, never a row), or `none`; `triagedAt`.
An input that opens a row in one tier and a sighting on another lists both
rows under one type. Rider-facing `note` in their words (which answer of theirs
settled it, which screenshot).

Inputs since `intake.triagedThroughMs`:
- Rider notes: `~/otp-debug-logs/debug-YYYY-MM-DD.jsonl` records with
  `"kind": "rider-note"` (`source` feedback / ride-thread / console). A
  feedback note may carry `image:` — **look at the picture**.
- Ride reports in `~/obsidian-vault/Claude/ride-watch/` newer than the
  watermark, and whether each promoted its own rows (the daemon's wrap-up
  does this now — check the backlog's mtime and the journal:
  `journalctl --user -u ride-watch --since … | grep -i promot`).
- The rider's vault-root notes (read-only), the latest
  `~/obsidian-vault/Claude/verify-nightly/` report.

Traps: **`~/otp-debug-logs` is an rsync mirror and can lag ~30 min** —
re-grep before setting the watermark. **Check for a live ride first**
(`~/otp-debug-logs/ride-watch/current-ride.md`): a ride in progress is not
triaged; record only what is already verifiable as an observation. **A
ride whose wrap-up deadline expired has no report** (journal: "no wrap-up
for <session> 10 min after the ride ended") — write it by hand to the
request's `reportPath` (`report-request-<session>-<HHMM>.json`) with the
ride-thread contract in `~/projects/otp-minneapolis/ride-watch/
ride-thread-sysprompt.md`, build the fixture, promote. Report agents are
often wrong about the mechanism: read the code the report names before
copying its explanation into a row. **A measurement is only as good as
the query it sent** — a probe that answers with the exact shape of a
different call (2026-09-21 22.2: a "TRANSIT+BICYCLE" probe that returned
the BICYCLE-only answer) was mis-sent; print the request you made next to
the answer before drawing a conclusion from it.

For each observation: dedupe first (a recurrence is an Nth-sighting note
on the existing row); verify the mechanism against current source; write
the row with `file:line`, timestamps, session ids, image path, and what
was ruled out; Session index row in the record file; keep "Open — N rows"
in step. Praise and questions go in the summary, not in a row.

**`in-prod` rows close here.** For every ride report, check the rows of
retired cycles with `status: in-prod` (their backlog notes say "in
production …, closes on the first ride that exercises it"). Telemetry that
shows the fixed behaviour → the row passes: cut it from the backlog, append
it to the record with the ride's session id as evidence, set the cycle
doc's row to `closed`. A ride that shows it still broken → `fail` with an
Nth-sighting note. Not exercised → leave it. These are writes to a retired
cycle's own doc from ride evidence, not a press, so the per-cycle press
rule does not apply.

Finish: `intake.triagedThroughMs`, `intake.lastTriageAt`, `intake.inputs`
(every entry's `became` set), `intake.pending` (only what the ledger cannot
show, else ""), `facts.openRows`. The `summary` is the paragraph behind the
card's "What the last run said"; the ledger is what the rider reads.

If the request said "then plan cycle N", continue into the plan step as described under *What a button sends*; the plan's rows are the ledger entries whose `became.rows` this run set, minus rows in a live cycle.

## `plan` — choose a cycle's rows and write the close-out

- Candidates: the request `note` if the rider named rows; otherwise from
  the backlog — rider asks and most-sighted first, then rows whose
  Sequencing predecessor is closed. Never re-plan a row that is `on-dev`
  in another cycle; it closes on ride evidence, not on more code.
- For each row: read the code it names **now**; correct the row if it is
  wrong; decide repo, branch (`gomode/<task>` otprr, `ride-watch/<task>`
  daemon), tests, and ship vehicle (`transitnav-ios/scripts/lane.sh`
  answers native-vs-web). Rows that need a design call from the rider
  become `questions[]` entries and get `kind: blocked`.
  **A question that hinges on a layout or a visual carries the visual in its
  own text** — the mock or screenshot link plus one line of what it shows —
  never only on a Build tab (rider, 2026-09-22: "What's the mock? Can we add
  visuals to any questions like this?").
- Write the plan into the backlog as `## Pipeline cycle N — plan` (rows →
  branches → tests → vehicle, Sequencing, what is measure-first), fill
  `rows[]`, `branches[]`, `questions[]` on the cycle doc, and put the
  rider-facing list in `steps.plan.summary`.
- **Finish as a draft, not done:** `steps.plan.status = "draft"`,
  `plan = {revision: 1, revisedAt, changes: ""}`, `feedback: [one {by:
  "claude"} line]` — that line is the reply: it says this is revision 1 and
  invites notes. The rider then
  drops rows, answers questions and sends notes on the card; each round is a
  `· revise` press (above) and posts revision R+1. Build waits for the
  rider's **Approve plan** press, which is the only thing that sets `done`.
  A plan that is approved on revision 1 is fine; a plan that flips to
  `done` by itself is the bug the rider complained about. Say in the
  revision-1 reply that approving it builds AND ships to the dev app on its
  own, so the rider knows the press is the last word before dev.

**Plan never writes `branches[]`.** A plan's proposed branches live in
`rows[].branch` and in `steps.plan.summary`; `branches[]` objects are written
only by Build, one per agent's branch, with the sha the agent produced. On 2026-09-23 the cycle-5 plan wrote three tabs with an empty sha,
the page put Approve-for-dev buttons on them, and the rider approved code that
did not exist yet. The page now hides the decision buttons on a tab with no sha
until Build is done, but do not write them at plan time at all.

## `build` — agents implement on branches

- One `Agent` per branch. **Model (rider, 2026-10-09: "sonnet for cycle
  implementations, opus for planning them"):** `model: "sonnet"` for a branch
  whose plan already corrected the mechanism and names the files, the tests
  and the gate — most branches. `model: "opus"` when the plan marks the branch
  **measure-first** (the right answer may be "ship nothing"), when it touches
  more than ~5 files, or when it is in the reroute / Go Mode state machines
  (`lib/actions/go-mode.ts`, `lib/util/go-mode/replan-*`, `trip-session`).
  The plan writes the model next to each branch in `rows[].branch`'s line of
  the plan section, so Build does not decide it on the fly. Worktrees: otprr
  `git -C ~/projects/otprr/otp-react-redux worktree add
  ~/projects/otprr/worktrees/<task> -b gomode/<task> main`; daemon
  `git -C ~/projects/otp-minneapolis worktree add
  ~/projects/otp-mpls-worktrees/<task> -b ride-watch/<task> master`.
  Give each agent the row numbers (it reads them from the backlog), the
  files, the tests, the gate, the commit recipe, and the rule that it
  reports what it **measured**. Skip rows whose question is unanswered;
  when `ANSWERS` arrive mid-build, launch the unblocked rows' agents.
- otprr gate, in the worktree: `docker run --rm -e NODE_ENV=test -v
  $PWD:/app -v ~/projects/otprr/otp-react-redux/node_modules:/app/
  node_modules -w /app node:20-alpine yarn jest <paths>` (targeted; never
  bare `yarn jest`), then `yarn unit` once, `sh -c 'yarn -s typecheck &&
  yarn -s check:i18n-en-fr'`. Before committing: `rmdir node_modules &&
  ln -s ~/projects/otprr/otp-react-redux/node_modules node_modules && cp
  -r ~/projects/otprr/otp-react-redux/.husky/_ .husky/`, and
  `PATH=/home/rwt/.nvm/versions/node/v22.16.0/bin:$PATH` for the hook.
  Daemon gate: `python3 ride-watch/test_ride_watch.py` plus a replay of
  the day file showing the false positive gone and the true positive
  kept. Fixtures live tracked under
  `lib/util/go-mode/replay/fixtures/`.
- **Verify each agent's claim in the diff** before believing it; set each
  row to `built` with its sha in `detail`, and write ONE `branches[]` object
  per branch **as soon as that agent's report is checked** — not all at the
  end — so the rider can Hold it while the rest still build (rider-facing
  `summary`, the `measured` before/after with real numbers, `tests`, `files`,
  `screenshots`, `risks`, `vehicle`; `shippable: false` for a pure
  measurement). A branch that passed its gate gets `decision: null` (= ships);
  one whose gate failed gets `decision: "hold"`, `decidedBy: "build"` and
  "Gate failed: …" first in `risks`. Never overwrite a decision the rider
  already made on the tab (read-modify-write, pin `if_version`).
- Build ends with `summary` = what will ship and what is held, then goes
  straight on to ship-dev in the same turn (*Auto-advance*), with the
  `feedback[]` line described there.

## `ship-dev` — merge, gate, bundle to the dev icon

Runs on its own right after Build (and on self-check rule 4); there is no
button. Its agent is `model: "sonnet"` (mechanical: merge, gate, bundle,
restart) with one hard rule: a merge conflict means **hold that branch and
move on**, never resolve it by guessing — the hold's `risks` names the
conflicting files and the thread decides. Re-read `branches[]` from the db at
the start — decisions made during Build count, the comment's lists do not.

1. Merge every built branch with `shippable !== false` and `decision !==
   "hold"` into otprr `main` (`--no-ff`), resolve conflicts, run the gate
   on the touched suites + `tsc` + i18n, push. A branch that fails the merge
   gate is backed out, set to `decision: "hold"` with the reason, and the
   rest still ship. Held branches stay on their worktrees and go into a
   later bundle when the rider sets them back to Ship (rebase first if main
   moved under them). A daemon tab → step 3; `shippable: false` tabs are
   never merged. Nothing left to ship → ship-dev `done` with "nothing to
   ship: …" in the summary.
2. `cd ~/projects/transitnav-ios && TRANSITNAV_TARGET=dev
   scripts/ship_web.sh "<notes>"` (node 22 on PATH; it clobbers the :9967
   dev server's `tmp/config.yml` — `docker restart otp-frontend-dev`
   after). Record the bundle number and the checksum it verified over the
   public URL in `bundle.dev` / `bundle.sha`.
3. Daemon rows: merge to otp-minneapolis `master`, push, `systemctl --user
   restart ride-watch`, confirm `ActiveEnterTimestamp` is after the last
   file change. Linode rows: `deployment/deploy-app.sh <host> --only
   <service>`.
4. Write "on dev as `YYYY.MMDD.n` (web `<sha>`)" onto every shipped row —
   rows stay open, `status: on-dev`; add rehearsal moves to the backlog's
   table if the cycle added any. `facts.devBundle`, `facts.otprrMain`,
   `facts.daemon`. No hold (zero users on dev).

## `verify` — score one ride, this cycle only

All four steps are the cycle's own thread's, for its own cycle only. It
starts from *Verify on a ride* (a trip that ended), which has already
settled that the ride ran on the dev app with this cycle's code.

1. The ride: the trip the RIDE line named (session + window); score it from
   the day file, after the mirror has caught up.
   **Which bundle actually ran** comes from `checks.jsonl` on the Linode
   (see *Verify on a ride*), never from the report.
2. For this cycle (`ship-dev` done, `verify` not done), every
   `on-dev` row in it: a dated sentence on the row — **pass** (telemetry
   shows the fixed behaviour), **fail** (with the new mechanism corrected
   on the row), or **not exercised** (never a pass). Nightly verdicts for
   harness rows.
3. Passes: cut from the backlog, append under their tier in
   `transitnav-backlog-record.md` with sha + evidence, `status: closed`.
   Fails: Nth-sighting note, `status: fail`, back to the backlog for a
   later cycle's Plan. `facts.openRows`.
4. This cycle's `steps.verify`: `done` when every row is pass/closed or
   bounced; otherwise `done` with the not-exercised rows named (they wait
   for another ride — the next dev ride scores them again on its own).
   No new dev ride since the last scoring is **not** `blocked` — the rider
   can do nothing about it on the board: leave the step `done` and say
   "no new ride" in the summary. `blocked` is only for something the rider
   must answer. Reply with the counts in one line ("Cycle N: 2 pass, 1 fail,
   3 not exercised — …") in `feedback[]`, and set `lastRide.scoredAt` and
   `lastRide.counts`.

## `ship-prod` — promote the verified sha

Only on this press. `WEB_SHA=<dev sha> SKIP_WEB=1 scripts/ship_web.sh
"<one line>"` in `transitnav-ios` (production target) — the **same sha**
the ride verified (rebuild at that sha with `build-web.sh` first if
needed). One line of comma-separated rider-visible changes, no internals.
Verify the checksum; `bundle.prod`, `facts.prodBundle`; note the promotion
on each row and in the record's Session index. A native change needs
`testflight.yml` + `promote.sh <build>` instead — `lane.sh` says which.
**Then retire this cycle** (`retiredAt`), whether or not every row was
exercised: each `on-dev` / `not-exercised` row becomes `status: in-prod`,
and its backlog row gets a dated note — "in production `<bundle>` (cycle N
retired); closes on the first ride that exercises it". Those rows are
closed later by Triage, from ride evidence, like any other observation.
Rows already `fail` stay in the backlog as they are. Other cycles whose sha
this bundle contains are not touched; each records the promotion on its
own Ship-prod press. If this cycle's sha is already in production (`git
merge-base --is-ancestor <sha> <prod sha>`), do not ship again: record
`bundle.prod`, mark the rows, retire. **Then close the thread** (*Closing
the thread* under *Cycle threads*): final summary + `feedback[]` line, touch
`retired` and `stop`, kill window `cN`.

## `answers`

Write each answer onto the row it names in the backlog (verbatim, dated),
flip the row's `kind`/`status` from `blocked` to `build`. If the plan is
approved, build that row now: launch its agent if Build is running, or run
a re-build of just the unblocked rows if Build is done — ship-dev follows
on its own. While the plan is still a draft, the answer feeds the next
revision instead. Append what changed to `feedback[]` as `{by: "claude"}` —
that is the reply the board's chat shows.

## By hand

`/transitnav-pipeline scan` registers new inputs on the ledger (silent);
`/transitnav-pipeline self-check N` runs cycle N's self-check by hand;
`/transitnav-pipeline status` reads the board and cycles and reports them;
`/transitnav-pipeline <action> [cycle]` runs one without the button. To
correct state, `~/bin/pipeline-db update` the doc directly (pin `--if-version`).
