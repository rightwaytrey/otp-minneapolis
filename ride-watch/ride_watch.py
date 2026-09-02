#!/usr/bin/env python3
"""ride-watch: live anomaly watcher for transit-navigation Go Mode telemetry.

Follows the day's debug JSONL stream (written by the Flask sidecar's
/api/debug-log endpoint), runs a per-trip rule engine over the redux action
stream, pages the rider via Pushover for at most 2 high-value findings per
ride, keeps a live status file any Claude session can read, and — since
2026-07-31 — runs **one Claude conversation per ride**: a remote-control
session spawned in tmux at trip start, visible in the rider's phone app, fed a
one-line digest ping at each milestone, which talks to the rider mid-ride and
writes the wrap-up report itself.

That thread replaced two headless agents: a `claude -p` per rider note (fresh
context every message — it re-diagnosed the same bug twice in a row on the 7/31
ride and the rider's reaction was "you're fresh context for *every*
message???") and a `claude -p` post-ride report. Both are gone; the rule engine
and Pushover paging below are untouched, because the safety layer must not
depend on an LLM session being healthy.

stdlib only. See README.md next to this file.

Notes grounded in the real telemetry (verified against debug-2026-07-29.jsonl):
- Redux actions carry their action name in the "type" key ("event" is only
  used by kind=session markers), so we accept both.
- Daily files are named by UTC date (the sidecar uses time.gmtime), so
  "midnight rollover" happens at 00:00 UTC (early evening local).
- START_GO_MODE while a trip is already active is an itinerary replacement
  (auto-reroute swap), not a new trip.
- UPDATE_PROGRESS.currentLegProgress is a percentage (0-100).
"""

import argparse
import collections
import datetime
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Configuration (env-overridable)
# ---------------------------------------------------------------------------

HOME = os.path.expanduser("~")
DEBUG_LOG_DIR = os.environ.get("RIDE_WATCH_LOG_DIR", os.path.join(HOME, "otp-debug-logs"))
WATCH_DIR = os.environ.get("RIDE_WATCH_DIR", os.path.join(DEBUG_LOG_DIR, "ride-watch"))
PUSHOVER_CREDS = os.environ.get(
    "RIDE_WATCH_PUSHOVER_CREDS", os.path.join(HOME, ".config", "pushover", "credentials")
)
DRY_RUN = os.environ.get("RIDE_WATCH_DRY_RUN") == "1"
REPO_DIR = os.environ.get(
    "RIDE_WATCH_REPO", os.path.join(HOME, "projects", "otp-minneapolis")
)
# Where the ride thread writes its wrap-up. The daemon never writes here; it
# only computes the path, because only the daemon knows how many rides this
# session has already taken (see _report_path).
REPORT_DIR = os.environ.get(
    "RIDE_REPORT_DIR",
    os.path.join(HOME, "obsidian-vault", "Claude", "ride-watch"),
)

# ---------------------------------------------------------------------------
# Provenance: which daemon is actually running
# ---------------------------------------------------------------------------
#
# On 2026-08-28 the daemon watching the ride had been running for five days
# from a five-day-old copy of this file. It produced five false
# `stalled-progress` findings, missed the arrival event (the SET_ARRIVED
# handler did not exist yet in the code that was loaded), and nearly
# overwrote an earlier ride's report. Nothing anywhere said which version was
# running: the digest header carried session / written-at / push count, the
# status file carried "Updated:", and neither told the ride thread that the
# source it was reading on disk was not the source in memory.
#
# Resolved ONCE, at import, into module constants. This is the whole point and
# it is easy to get backwards: `git rev-parse HEAD` evaluated when the digest
# is written reports what the working tree is NOW, not what this process was
# loaded from — so a five-day-stale daemon would confidently stamp today's SHA
# and the mismatch it exists to expose would become invisible. A header that
# lies about provenance is worse than one that omits it, because nothing
# contradicts it. Both repos here are shared worktrees that move under
# long-running processes, so this is a live hazard, not a theoretical one.
#
# Untracked files are excluded from the dirty check on purpose: other agents
# work in this same checkout and leave scratch files behind constantly, and a
# stamp that reads "-dirty" every single time says nothing. A modified TRACKED
# file is the case where the SHA genuinely does not describe the running code.
#
# Fail soft, always. The daemon must not die, or go quiet, because it could not
# introspect itself; an unresolvable SHA stamps "unknown".


def _git_out(args, timeout=10):
    """Run a read-only git command, returning stripped stdout or None."""
    try:
        proc = subprocess.run(
            ["git", "-C", REPO_DIR] + args,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=timeout,
            universal_newlines=True)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def _resolve_daemon_sha():
    rev = _git_out(["rev-parse", "--short", "HEAD"])
    if not rev:
        return "unknown"
    # --untracked-files=no: see the note above. Also the only form of `status`
    # this daemon ever runs, and it runs it exactly once.
    dirty = _git_out(["status", "--porcelain", "--untracked-files=no"])
    return rev + "-dirty" if dirty else rev


def _source_mtime():
    """When the file this process was loaded from was last written.

    The cheap half of the same question, and the one the rider's own notes say
    to check first: a service whose start time predates its source's mtime is
    running code that no longer exists on disk.
    """
    try:
        return os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return None


DAEMON_GIT_SHA = _resolve_daemon_sha()
DAEMON_STARTED_MS = int(time.time() * 1000)
DAEMON_SOURCE_MTIME = _source_mtime()
# How often the *running* SHA may be compared against the repo's HEAD for the
# "you are stale" line. Only `rev-parse` is re-run (read-only, takes no index
# lock, so it cannot collide with another agent's git in this shared worktree),
# never `status`, and never on the per-event path.
HEAD_RECHECK_MS = 5 * 60 * 1000

# Rule thresholds (ms unless noted)
STARTUP_LOOKBACK_MS = 5 * 60 * 1000        # scan back this far at startup
LOOKBACK_TAIL_BYTES = 16 * 1024 * 1024     # ...reading at most this much tail
SESSION_TIMEOUT_MS = 15 * 60 * 1000        # trip ends after this much silence
# ...and this long after arrival, whether or not the app ever goes quiet.
# Every trip-end this daemon had was a silence: STOP_GO_MODE, the timeout
# above, or replay EOF. On 2026-08-31 the rider arrived at 18:52:14, the app
# latched SET_ARRIVED and then went on emitting UPDATE_POSITION /
# UPDATE_ROUTE_MATCH / UPDATE_PROGRESS at ~1 Hz with `status: "completed"` for
# the next hour and three quarters (18,105 records, still going at 20:36).
# The stream never fell silent, so no silence rule could reach it: no report
# request was written, the ride thread was never asked to wrap up, and
# current-ride.md still showed a live ride two hours after the rider got off.
# Five minutes, not one: the rider typed their note at the destination three
# minutes after arrival that evening, and it belongs to the ride.
ARRIVED_END_MS = 5 * 60 * 1000
# One ride, two session ids. The app re-mounted at 18:52:55 and minted
# `mthw8o2w-i8z1i6` 41 s after `mthw7svy-s4msqc` — same phone, same itinerary,
# same frozen leg, seconds apart. The daemon read them as two rides: two
# adoptions, two "trip started" pings, findings split 18/19 across two ledgers,
# two tmux threads (the second spawn failed: duplicate session ride-1852), and
# every per-ride counter — stall anchor, notification windows, page budget —
# back to zero. A resumed Go Mode emits no START_GO_MODE (the fixture builder
# rejects those sessions for exactly this reason), so the only door a
# continuation can come through is adoption, which is where these gates sit.
CONTINUATION_GAP_MS = 120 * 1000           # since the older trip's last event
CONTINUATION_PROGRESS_PCT = 2.0            # same leg, within this much of it
STOP_COLLAPSE_MAX_PROGRESS = 60.0          # percent
DEVIATED_STREAK_MS = 90 * 1000
GPS_GAP_MS = 60 * 1000
REROUTE_STORM_WINDOW_MS = 5 * 60 * 1000
REROUTE_STORM_COUNT = 3                    # "> 3 in 5 min" pages on the 4th
DISTANCE_SPIKE_FAR_M = 2000.0
DISTANCE_SPIKE_NEAR_M = 200.0
RIDER_ACTION_WINDOW_MS = 30 * 1000         # explicit action shields aboard-swap
# aboard-swap corroboration: the app must have seen the rider's bus in the feed
# this recently for "while aboard" to mean anything. Same scale as the app's own
# VEHICLE_MATCH_FRESH_MS, which is what stops a confirmed match from looking
# healthy forever after its vehicle drops out of the feed.
ABOARD_MATCH_FRESH_MS = 90 * 1000
# Backwards-itinerary rules (8/9). A leg starting before the previous one ends
# is never right, but clocks and rounding differ across the wire, so only call
# it an inversion past a threshold a rider could actually see on a trip sheet.
# The 8/9 START_GO_MODE was inverted by 692,303 ms.
LEG_INVERSION_MS = 60 * 1000
# An alight candidate whose bus arrival is already this far behind the moment
# it was computed is a stale feed reading, not a prediction. 8/9's worst was
# 578,912 ms behind on the FIRST optimize, five minutes before the rider was
# shown anything.
STALE_CANDIDATE_MS = 60 * 1000
# match-vs-riding disagreement (8/2 §11): the confirmed match reported trip
# 1:1191630 while the rider was confirmed on 1:1201789, for the whole ride. One
# tick of disagreement is a poll landing mid-rebind; a sustained one means the
# match and the board state have genuinely parted company.
MATCH_TRIP_DISAGREE_MS = 60 * 1000
# ...and its distance was ~10,268 km, a real haversine against null island.
# Anything past this is not "the bus you are sitting on" under any reading.
MATCH_DISTANCE_ABSURD_M = 5000.0
# stalled-progress (8/2 §12): the rider sat at one spot for 34 minutes inside a
# bike leg, 640 m short of the destination, with Go Mode active and progress
# frozen. Internally consistent, so no existing rule had anything to say. This
# distinguishes "parked" from "tracking broken" only by duration — long enough
# that a light, a queue, or a shop stop never trips it.
STALL_MS = 15 * 60 * 1000
STALL_RADIUS_M = 60.0
STALL_COOLDOWN_MS = 15 * 60 * 1000
# notification-repeat. On 2026-07-31 the app pushed the identical turn alert
# ("Turn right on Village Lane") to a stationary rider 14 times, 30.5s apart,
# for seven minutes — the turn-cue dedup is a 30s rate limiter, not the
# once-per-turn latch its comment claims. The rule engine had nothing to say
# about it: _on_notification only ever looked at MISSED_BUS. Three of the same
# alert inside five minutes is a phone misbehaving at the rider, which is
# exactly the class of thing they should not have to notice and type by hand.
NOTIFICATION_REPEAT_WINDOW_MS = 5 * 60 * 1000
# Two, not three, since 2026-08-31. The rule was written for the 7/31 storm —
# fourteen byte-identical buzzes — and then failed to see either of the two
# deviation storms it was next asked about, twice over: the messages drifted
# ("You are 121m…" / "124m" / "120m") so the byte key never accumulated, and
# even the byte-identical pairs only ever reached two inside the window.
# 8/28 evening is the case that settles it: five ROUTE_DEVIATION pushes, at
# 17:12:57, 17:14:45, 17:36:33, 17:37:28, 17:39:28. The stable key alone gets
# the last three to three-in-window and would have fired at 17:39:28 — five
# seconds before the rider gave up and stopped Go Mode. At two it fires at
# 17:14:45, twenty-five minutes earlier, while "ignore the buzzing" is still
# an instruction the rider can act on. A rule that only reports storms after
# they are over is not a safety layer.
# What keeps two from being noisy: the key fires once per ride (the latch
# below), a page costs one of two per-trip interrupts, and — the load-bearing
# one — intake now drops re-POSTed duplicate records, so two-in-window can no
# longer be one alert counted twice. See _is_duplicate_record.
NOTIFICATION_REPEAT_COUNT = 2              # fires on the 2nd
# progress-without-motion. Map-matching noise reported to the rider as travel.
# On 7/31 the swing was a tenth of a point (0.31 -> 0.21 -> 0.31) inside a 7m
# circle, which is below this threshold and should stay quiet; the same
# signature at 30 points would mean the app is inventing a journey.
MOTION_PROGRESS_PCT = 5.0                  # percentage points gained
MOTION_DISPLACEMENT_M = 15.0               # ...while the fix stayed this close
MOTION_COOLDOWN_MS = 5 * 60 * 1000
# replan-not-converging (8/28 afternoon). The destination was inside the State
# Fairgrounds, where the street graph stops at the fence. The app re-planned
# into the venue interior for 32 minutes, never got inside 427 m, and told the
# rider nothing — every plan real, every plan routing to the same unreachable
# point, each one promising an arrival it could not deliver.
#
# The client now guards this itself (otprr 047ee0af / 94a69bba,
# lib/util/go-mode/destination-progress.ts): it keeps the closest approach
# across ticks, counts re-plans since that closest approach last improved by
# 50 m, retires the access mode after three, and raises DESTINATION_UNREACHABLE.
# So the daemon's job here is NOT to be the primary detector — it is to catch
# the ride where the client's own guard fails or never fires. Which means the
# thresholds must MATCH the client's rather than compete with them: a daemon
# firing on different arithmetic would page about rides the app handled
# correctly, and stay silent on the one it did not.
DEST_GAIN_MIN_M = 50.0                     # == client DESTINATION_GAIN_MIN_M
DEST_STALL_REPLANS = 3                     # == client DESTINATION_STALL_REPLANS
# ...plus one. The client checks destinationStalled at the TOP of its re-plan
# routine and increments the counter at the bottom, so the mode is retired on
# re-plan 3 and the rider is told on re-plan 4. Firing at 3 would race the app
# and page about a defect it was in the middle of reporting itself.
DEST_CLIENT_GRACE_REPLANS = 1
# One re-plan can reach the stream twice: on 8/28 at 16:44:06 a START_REROUTE
# (reason "boarded-earlier") and the START_GO_MODE that applied its result were
# logged in the same second. Counting both would retire a converging trip on
# half the evidence the client used.
DEST_REPLAN_COLLAPSE_MS = 10 * 1000

# Nothing pages when the wrap-up never appears (8/28). The ride thread spawned
# fine, took the wrap-up line, and then sat at a permission prompt for about
# three hours; _thread_missing was false the whole time, so the one fallback
# push never had a reason to fire. A wrap-up that has not been written this
# long after the ride ended is not "still thinking".
REPORT_DEADLINE_MS = 10 * 60 * 1000
# ...and then the pane goes away. A ride thread is that ride's console and
# nothing else, but _kill_previous_threads only ever ran from the SPAWN path,
# so a finished ride's pane lived until the next ride started — and a pane
# spared because its wrap-up was outstanding was never revisited at all. On
# 2026-09-01 ride-1029's trip ended 10:48:47, its wrap-up landed 10:51:22, and
# `tmux ls` still showed it at 11:15 next to ride-1048. The rider caught it
# mid-ride: "Ok makes sure all ride consoles wrap up upon complete."
#
# Two minutes rather than zero. The thread has just been told the ride is over
# and is writing its last lines into a console the rider may still be reading;
# retiring the pane in the same tick as "wrap-up landed" would cut that off.
THREAD_REAP_GRACE_MS = 2 * 60 * 1000

# console.error lines that are known-inert and cost a findings slot every ride.
# Substring match against the first console argument, deliberately narrow.
#
# CapgoUpdater: the live-update plugin has no update URL in the native build.
# Third sighting on 2026-09-01 10:54:17, mid-bus-leg, and confirmed inert —
# nothing in the position, progress, route-match or vehicle-match streams
# changed across it. The plugin config is an iOS-repo fix (backlog 6.9); until
# then it is one of six findings the wrap-up has to triage every single ride.
# This suppresses the FINDING, not the record: the line stays in the raw
# telemetry, so a report can always go and look.
CONSOLE_ERROR_IGNORE = (
    "CapgoUpdater : Error no url or wrong format",
)

# vehicle-match-never. On 2026-09-01 ride 2 the app polled the vehicle matcher
# 775 times across the Orange Line leg and every one came back
# `confidence: "none"`, `vehicleId: null`, `distanceMeters: null`. The app
# behaved correctly — it never claimed a match it did not have — so no rule had
# anything to say, and the ride reached the report as though live-vehicle
# tracking had worked. A transit leg ridden with no live vehicle behind it is a
# fact the report should carry, because every downstream judgement about
# boarding, delay and arrival on that leg was made without it.
# Thirty polls is ~30 s of a 1 Hz stream: long enough that a leg the rider
# passed straight through, or a matcher that had not warmed up, stays quiet.
VEHICLE_MATCH_NEVER_MIN_POLLS = 30

MAX_PAGES_PER_TRIP = 2
PUSH_MIN_INTERVAL_MS = 120 * 1000
# Intake dedup ring. The debug-log client re-POSTs a batch it is not sure
# landed, so the same record arrives twice with the same `t` and the same
# payload id, differing only in `recv` — 208 ms apart on 8/27 13:10:42. That
# pair was read as an "exact same-second duplicate notification" in the ride
# notes and chased as an app bug; it was telemetry. Across 8/27-8/29 the
# streams carry ~1,000-1,700 such records a day (3.7% of 8/27), including
# UPDATE_PROGRESS and ADD_NOTIFICATION, so every counting rule in this file
# was exposed. The ring is small because a re-POST follows its original within
# seconds; it is not a general history.
RECORD_DEDUP_RING = 4096
STATUS_DEBOUNCE_MS = 2000
STOP_INCREASE_COOLDOWN_MS = 60 * 1000
RIDER_NOTE_MAX_CHARS = 500                 # matches the sidecar's own cap

# The ride thread. One remote-control Claude conversation per ride, spawned in
# tmux at trip start, visible in the rider's phone app, fed one line per
# milestone. It is a *conversation*, not a job queue: the whole point is that it
# still remembers the 11:04 stop-count collapse when the rider asks about it at
# 11:31, which a fresh `claude -p` per note never could.
#
# Two rules keep it from becoming noise:
#   * MILESTONES ONLY. Trip start, leg transition, a rule finding, a rider note,
#     trip end — plus a heartbeat if ten minutes pass silently while the rider
#     is still moving. ~1 Hz telemetry never reaches the thread; the digest file
#     does the detail and the ping is one line.
#   * NEVER BLOCK THE TAILER. Bringing a Claude TUI up takes ~10s and every
#     send-keys needs a beat before Enter, so the real tmux work happens on a
#     worker thread. A dead pane, a missing tmux, a rider who typed /exit: all
#     logged, none fatal. Telemetry keeps being read and pages keep going out.
THREAD_ENABLED = os.environ.get("RIDE_THREAD_ENABLED", "1") not in (
    "0", "false", "no", "off")
THREAD_RUNNER = os.path.join(REPO_DIR, "ride-watch", "ride-thread-run.sh")
# Namespace for both the tmux session (`ride-1432`) and the app display name
# ("ride 07-31 14:32"). Overridable so an end-to-end test can spawn a real
# thread without ever colliding with — or cleaning up — the rider's own.
THREAD_NAME_PREFIX = os.environ.get("RIDE_THREAD_NAME_PREFIX", "ride")
THREAD_TMUX_SIZE = (200, 50)               # wide enough that the TUI wraps sanely
THREAD_READY_TIMEOUT_S = 30                # TUI is usually up in 10-12s
THREAD_READY_POLL_S = 1.0
THREAD_READY_MARKER = "❯"             # the ❯ prompt = accepting input
# send-keys of the text and send-keys of Enter must be two calls with a beat
# between them; combined into one call the line is typed but never submitted.
THREAD_SUBMIT_DELAY_S = 1.0
THREAD_HEARTBEAT_MS = 10 * 60 * 1000
THREAD_MOVING_MS = 2 * 60 * 1000           # a fix this recent = still riding
THREAD_LINE_MAX = 400                      # one line, no exceptions
THREAD_MAX_EVENTS = 400                    # digest ledger cap

# Page coalescing. A failure rarely produces one finding: on 2026-07-29 the
# app flipped the riding tripId at 17:28:45 and only eight seconds later
# reported one stop remaining at 0% of the leg. First-come-first-served paging
# plus the 120s rate limit meant the rider was told about the tripId (a
# diagnostic detail) and never about the stop count (the thing that would have
# made them get off at the wrong stop). So a page is held briefly and the
# highest-ranked page in the window is the one that goes out.
#
# The window is deliberately short: long enough to catch a cascade like
# 17:28:45 -> 17:28:53, short enough that the rider hears about it while they
# can still act on it. It is set by the *first* page in the window and later
# pages do not extend it, so a continuing storm cannot defer paging forever.
PAGE_COALESCE_MS = 15 * 1000

# Actionability rank, highest first. The question each rank answers is "how
# much does this change what the rider does in the next minute?", not "how
# broken is the app" — the post-ride report covers the latter.
#   stop-count-collapse     the banner is lying about when to get off; the
#                           rider acts on it immediately
#   missed-bus-while-riding a wrong alert telling a seated rider to move
#   aboard-swap             the on-screen route no longer matches their bus
#   riding-flip             board state is suspect, but the rider is still on
#                           the right vehicle — diagnostic
#   deviated-streak         position tracking looks off; nothing to do about it
# Rules not listed rank mid-pack, so a newly added page rule is neither
# silently starved nor able to outrank the stop counter before anyone has
# thought about where it belongs. Add it here when you add the rule.
#   notification-repeat     their phone is buzzing wrongly; "ignore it" is an
#                           instruction they can act on this second
PAGE_RANK = {
    "stop-count-collapse": 50,
    # itinerary-backwards  every time on the trip sheet is suspect; the rider
    #                      is reading it right now to decide what to do
    "itinerary-backwards": 45,
    "missed-bus-while-riding": 40,
    # replan-not-converging  the app cannot get them there and has not said
    #                        so; "finish from here yourself" is the only
    #                        instruction left, and every minute they keep
    #                        waiting for the next plan is spent
    "replan-not-converging": 38,
    "notification-repeat": 35,
    "aboard-swap": 30,
    "riding-flip": 20,
    "deviated-streak": 10,
}
PAGE_RANK_DEFAULT = 25

TRANSIT_MODES = {
    "BUS", "TRAM", "RAIL", "SUBWAY", "FERRY", "GONDOLA", "CABLE_CAR",
    "FUNICULAR", "TROLLEYBUS", "MONORAIL", "TRANSIT", "COACH",
}

LOG_MAX_BYTES = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# Logging (simple size-rotated file + stderr)
# ---------------------------------------------------------------------------



# `recv` may legitimately be None, so absence needs its own sentinel.
_UNSEEN = object()

class Log:
    def __init__(self, path, echo=True):
        self.path = path
        self.echo = echo
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, level, msg):
        line = "%s %s %s" % (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg
        )
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if self.echo:
            print(line, file=sys.stderr, flush=True)

    def info(self, msg):
        self.write("INFO", msg)

    def warn(self, msg):
        self.write("WARN", msg)

    def error(self, msg):
        self.write("ERROR", msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fmt_hms(ms):
    if not ms:
        return "?"
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def fmt_date(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def fmt_ms_span(ms):
    """A duration a rider reads at a glance: "11m20s", "45s"."""
    secs = int(round(abs(ms) / 1000.0))
    if secs < 60:
        return "%ds" % secs
    return "%dm%02ds" % (secs // 60, secs % 60)


def fmt_pct(v):
    """Leg progress as a percentage, with the small end kept honest.

    UPDATE_PROGRESS.currentLegProgress is a percentage on 0-100. On 2026-07-31
    a reply agent was handed the bare number 0.3077 under a unitless key, read
    it as a fraction, and told a rider standing 4m into a 1326m leg that they
    were "31% along". Rounding to whole percent has the opposite failure —
    0.3077 prints as "0%", which reads as "no data" — so anything under 10
    keeps a decimal. Every surface that shows progress goes through here.
    """
    if not isinstance(v, (int, float)):
        return "?"
    return ("%.1f%%" if abs(v) < 10 else "%.0f%%") % v


def short_session(session):
    if not session:
        return "unknown"
    return session.rsplit("-", 1)[-1]


def one_line(text, limit=THREAD_LINE_MAX):
    """Collapse anything to a single bounded line.

    Everything typed into the ride thread goes through here: a newline in a
    rider's note would submit half a sentence and leave the rest in the box.
    """
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def ride_thread_sessions(names, prefix=THREAD_NAME_PREFIX):
    """Of these tmux session names, the ones this daemon owns.

    Ours are exactly `<prefix>-HHMM`. The rider hand-spawns threads in the same
    namespace (`ride-test-smoke` was live while this was written) and killing
    one of those mid-sentence would be unforgivable, so the match is anchored
    and the suffix must be four digits.
    """
    pat = re.compile(r"^%s-\d{4}$" % re.escape(prefix))
    return [n for n in names if pat.match(n)]


def read_pushover_creds(path):
    """Return (user_key, api_token).

    The rider's file is `KEY=VALUE` (USER_KEY=... / API_TOKEN=...); bare
    two-line files are also accepted so the format can change without
    breaking paging.
    """
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    kv = {}
    bare = []
    for line in lines:
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip().upper()] = v.strip()
        else:
            bare.append(line)
    user = kv.get("USER_KEY") or kv.get("USER") or kv.get("PUSHOVER_USER_KEY")
    token = kv.get("API_TOKEN") or kv.get("TOKEN") or kv.get("PUSHOVER_API_TOKEN")
    if user and token:
        return user, token
    if len(bare) >= 2:
        return bare[0], bare[1]
    raise ValueError("could not parse pushover credentials at %s" % path)


def meters_between(a, b):
    """Great-circle distance between two (lat, lon) fixes, in metres."""
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(h)))


# The `Date.now()` every notification id ends in. Stripping it is what turns
# "a fresh id per fire" back into "the same alert".
_NOTIFICATION_STAMP_RE = re.compile(r"_\d{12,}$")


def notification_key(payload):
    """A stable identity for "the same alert", across the app's drifting text.

    The rule this feeds was keyed on `(title, message)` until 2026-08-31,
    which is byte-exact and therefore defeated by the one thing every repeated
    alert does: drift. "You are 121m from the planned route" and "You are 124m
    from the planned route" are the same alert about the same fault, 175 s
    apart, and were two separate keys each accumulating its own count. Neither
    8/28 deviation storm ever reached the threshold in any single key.

    The fix the old docstring already named: the id minus its `Date.now()`
    suffix. `ROUTE_DEVIATION_deviation_1787956593046` becomes
    `ROUTE_DEVIATION_deviation`, which is stable across fires and survives the
    message changing underneath it. `UPCOMING_TURN_<legStart>_<cue>_<stage>`
    keeps the cue index and the stage, so `_1_prepare` and `_1_act` and
    `_2_prepare` stay three different alerts, which is correct — they are.

    The title rides along in the key because the id stem is not always
    discriminating enough on its own (a synthetic stream, or an app build that
    reuses a stem across turns), and because a changed title is by definition a
    different thing being said to the rider. Payloads with no usable id fall
    back to (type, title) — never the message, which is the part that drifts.
    """
    title = (payload.get("title") or "").strip()
    ntype = (payload.get("type") or "").strip()
    nid = (payload.get("id") or "").strip()
    stem = _NOTIFICATION_STAMP_RE.sub("", nid) if nid else ""
    if stem:
        return (stem, title)
    if not title and not ntype:
        return None
    return (ntype, title)


def leg_is_transit(leg):
    if not isinstance(leg, dict):
        return False
    if leg.get("transitLeg") is True:
        return True
    return (leg.get("mode") or "").upper() in TRANSIT_MODES


def summarize_itinerary(payload):
    """Compact leg summary from a START_GO_MODE payload; None if unavailable."""
    if not isinstance(payload, dict):
        return None
    itin = payload.get("itinerary")
    if not isinstance(itin, dict) or not itin.get("legs"):
        return None
    legs = []
    for leg in itin["legs"]:
        if not isinstance(leg, dict):
            continue
        route = None
        r = leg.get("route")
        if isinstance(r, dict):
            route = r.get("shortName") or r.get("longName")
        elif isinstance(r, str):
            route = r
        route = route or leg.get("routeShortName") or leg.get("routeLongName")
        legs.append({
            "mode": leg.get("mode"),
            "transit": leg_is_transit(leg),
            "route": route,
            "headsign": leg.get("headsign"),
            "from": ((leg.get("from") or {}).get("name")),
            "to": ((leg.get("to") or {}).get("name")),
            "startTime": leg.get("startTime"),
            "endTime": leg.get("endTime"),
        })
    return {
        "legs": legs,
        "startTime": itin.get("startTime"),
        "endTime": itin.get("endTime"),
        "duration": itin.get("duration"),
    }


def itinerary_one_liner(summary):
    if not summary:
        return "itinerary unavailable (summarized payload)"
    parts = []
    for leg in summary["legs"]:
        mode = leg.get("mode") or "?"
        if leg.get("transit"):
            label = leg.get("route") or leg.get("headsign") or mode
            parts.append("%s %s (%s)" % (mode, label, fmt_hms(leg.get("startTime"))))
        else:
            parts.append(mode)
    return " > ".join(parts)


# ---------------------------------------------------------------------------
# Trip state
# ---------------------------------------------------------------------------


class Trip:
    def __init__(self, session, start_ms, itinerary_summary, adopted=False):
        self.session = session
        # Every session id this one ride has been seen under. The app mints a
        # new one on every mount, so a ride the rider never interrupted can
        # arrive under two (2026-08-31 18:52). The first stays `session` —
        # findings ledger, digest and report path all hang off it — and the
        # rest are aliases in RideWatch.trips. See _adopt_continuation.
        self.sessions = [session]
        self.device = None            # which phone; the anchor for a remount
        self.start_ms = start_ms
        self.itinerary = itinerary_summary        # latest itinerary summary
        self.adopted = adopted                    # trip inferred mid-stream
        self.swap_seq = 0                         # bumped on each itinerary swap
        self.swap_times = []                      # ms of each swap
        self.last_event_ms = start_ms
        self.riding = None                        # SET_RIDING payload + swap_seq
        self.progress = None                      # last UPDATE_PROGRESS snapshot
        self.last_pos_ms = start_ms
        self.last_fix = None                      # (lat, lon) of the last fix
        self.gps_gap_open = False
        self.gps_gap_started_ms = None            # last_pos_ms when the gap opened
        self.arrived_ms = None                    # SET_ARRIVED; the trip is over
        self.arrived_leg = None                   # leg index when it latched
        self.notification_times = collections.defaultdict(collections.deque)
        self.notification_repeat_last = {}        # key -> ms of last finding
        self.motion_anchor = None                 # where progress was last real
        self.motion_fired_ms = 0
        self.prev_stops = None
        self.stops_swap_pending = False   # itinerary swapped since last count
        self.collapse_fired_seq = set()
        self.stop_increase_last_ms = 0
        self.deviated_since_ms = None
        self.deviated_fired = False
        self.reroute_times = collections.deque()
        self.reroute_storm_last_ms = 0
        self.prev_dist = None
        self.last_route_match = None               # last UPDATE_ROUTE_MATCH
        self.last_vehicle_match = None             # last UPDATE_VEHICLE_MATCH
        self.match_disagree_since_ms = None        # match tripId != riding's
        self.match_disagree_fired = False
        self.match_distance_fired = False          # re-arms when sane again
        self.stall_anchor = None                   # ((lat, lon), first_seen_ms)
        self.stall_fired_ms = 0
        # How many position fixes have landed since the anchor was set. A
        # stalled-progress finding that cannot say this reads as a dead GPS;
        # on 8/28 the receiver was healthy throughout (2,168 distinct fixes,
        # ~4.1 m apart) and five findings were triaged as a tracking failure.
        self.fixes_since_anchor = 0
        # -- destination convergence (mirrors the client's DestinationProgress)
        self.dest_best_m = None                    # closest committed approach
        self.dest_replans_since_gain = 0
        self.dest_last_replan_ms = 0               # collapses one replan logged twice
        self.dest_unreachable_ms = None            # the app said it itself
        self.dest_stall_fired = False
        self.last_rider_action_ms = 0
        # legIndex -> {"polls", "matched", "firstMs", "lastMs", "bestConfidence"}
        # for legs the itinerary calls transit. Read once, at trip end, by
        # _rule_vehicle_match_never: "did the live matcher ever succeed on this
        # leg?" is a question only the whole leg can answer.
        self.vehicle_match_legs = {}
        # searchId -> {"mode", "tMs"} for searches the rider ran mid-ride, so a
        # ROUTING_RESPONSE can be judged against what was actually asked for.
        self.searches = collections.OrderedDict()
        self.bike_egress_fired = set()            # searchIds already reported
        self.console_seen = set()
        self.notes = []                           # rider-typed notes, in order
        # -- the ride thread ---------------------------------------------
        self.thread = None            # {"tmux","display","spawnedMs","ok"}
        self.thread_events = []       # milestone ledger, oldest first
        self.thread_cursor = 0        # events already handed to the thread
        self.thread_pushes = 0
        self.last_thread_push_ms = 0
        self.findings = []
        self.pages_sent = 0
        self.pending_pages = []                   # page candidates in the window
        self.pending_until_ms = None              # when the window closes
        self.end_ms = None
        self.end_reason = None
        # Chosen by _write_report_request, then watched by the report deadline
        # after this Trip has been dropped from self.trips. Held here so the
        # name the ride thread was given and the name the daemon watches for
        # are the same string, not two independent guesses at it.
        self.report_path = None

    def current_leg_transit(self):
        """Best-effort: is the leg the rider is currently on a transit leg?"""
        idx = None
        if self.progress:
            idx = self.progress.get("currentLegIndex")
        if self.itinerary and idx is not None:
            legs = self.itinerary["legs"]
            if 0 <= idx < len(legs):
                return legs[idx]["transit"]
        # Fallbacks when the itinerary payload was summarized away
        if self.progress and self.progress.get("stopsRemaining") is not None:
            return True
        return self.riding is not None


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------


class RideWatch:
    def __init__(self, dry_run=DRY_RUN, replay=False, watch_dir=WATCH_DIR,
                 log=None, spawn_thread=None, push_line=None,
                 thread_enabled=None, report_dir=REPORT_DIR,
                 kill_thread=None):
        self.dry_run = dry_run
        self.replay = replay
        self.watch_dir = watch_dir
        self.report_dir = report_dir
        os.makedirs(watch_dir, exist_ok=True)
        self.log = log or Log(os.path.join(watch_dir, "daemon.log"))
        self.trips = {}               # session -> Trip (active)
        self.all_findings = []        # every finding this process has emitted
        self.ended_trips = []         # Trip objects, for replay/test inspection
        self.recently_ended = {}      # session -> end_ms (blocks re-adoption)
        self._declined_completed = set()   # sessions refused adoption, logged once
        # Sessions whose ride this daemon closed at arrival. Re-adopting one
        # is how a single 8/27 ride became nine (see _maybe_adopt).
        self.ended_arrived = set()
        # Onboard-flow anomalies seen BEFORE a trip exists. The "I'm already on
        # a bus" flow runs entirely pre-START_GO_MODE, so its findings have no
        # trip to hang on yet; they are flushed when the trip opens.
        self.pending_onboard = {}     # session -> [(t, rule, summary, ctx, push)]
        # Wrap-ups that have been asked for and not yet appeared. Deliberately
        # NOT keyed off self.trips: _end_trip deletes the Trip, which is how
        # the missing-report case escaped every timer in this file. Restored
        # from state.json below so a restart in the ten minutes after a ride
        # does not lose the deadline. See _check_report_deadlines.
        self.report_deadlines = []
        # Panes whose ride is over and whose wrap-up is settled, waiting out
        # THREAD_REAP_GRACE_MS before they are retired. The other half of the
        # lifecycle report_deadlines opens: a deadline says "this pane still
        # owes work", a reap says "this pane owes nothing and should stop
        # existing". Persisted, so a restart inside the grace window still
        # closes the console rather than leaving it for the next ride to kill.
        self.thread_reaps = []        # [{"tmux", "atMs", "why"}]
        # Panes this daemon killed, name -> ms. Read by _check_report_deadlines
        # so it can never page the rider about a wrap-up it prevented itself.
        self._panes_killed = {}
        # device -> [session ids seen on it]. A brand-new session id on a phone
        # we already know is an app re-mount, which is what tells a resumed
        # trip from a daemon that simply started mid-ride.
        self.device_sessions = {}
        # Intake dedup ring (see RECORD_DEDUP_RING).
        self._seen_records = collections.deque()
        self._seen_record_keys = {}
        self.duplicate_records = 0
        self.last_trip_summary = self._load_state()
        self.clock_ms = 0             # replay: max event t; live: wall clock
        self.last_push_ms = 0         # global rate limit (shared w/ fallback)
        self.push_log = []            # [{tsMs, title, body, sent, kind}]
        self._status_dirty = True
        self._status_last_write = 0
        # -- the ride thread ------------------------------------------------
        # spawn_thread/push_line are the injection seam, exactly like the old
        # spawn_reply one: tests hand in stubs so the suite exercises the real
        # lifecycle and push cadence without tmux, without `claude`, and
        # without the ~10s of real waiting each spawn costs.
        self.spawn_thread = spawn_thread
        self.push_line = push_line
        self.kill_thread = kill_thread
        self.thread_enabled = (THREAD_ENABLED if thread_enabled is None
                               else thread_enabled)
        self._thread_lock = threading.RLock()
        self._thread_jobs = []         # queued tmux work (worker thread)
        self._thread_wake = threading.Event()
        self._thread_worker = None
        self._thread_status = {}       # tmux name -> True/False once known
        self._head_cached = (None, None)   # (head, behind), TTL-cached; never the stamp
        self._head_checked_ms = 0

    # -- clock ------------------------------------------------------------

    def now_ms(self):
        if self.replay:
            return self.clock_ms
        return int(time.time() * 1000)

    # -- persisted "last trip" summary ------------------------------------

    def _state_path(self):
        return os.path.join(self.watch_dir, "state.json")

    def _load_state(self):
        try:
            with open(self._state_path()) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        self.report_deadlines = [d for d in (data.get("reportDeadlines") or [])
                                 if isinstance(d, dict) and d.get("reportPath")]
        # Same reason as the deadlines: "restart the daemon on commit" happens
        # mid-evening, and a console whose reap was two minutes out when the
        # process died must still close rather than wait for the next ride.
        self.thread_reaps = [r for r in (data.get("threadReaps") or [])
                             if isinstance(r, dict) and r.get("tmux")]
        self._panes_killed = dict(
            (k, v) for k, v in (data.get("panesKilled") or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float)))
        # A ride closed at arrival whose app is STILL streaming outlives this
        # process — that is the shape of the whole 8/31 fault — and
        # restart-on-commit happens to this daemon mid-evening. Without this,
        # a restart re-adopts the finished ride as a new one on the next tick.
        self.ended_arrived = set(
            x for x in (data.get("endedArrived") or []) if isinstance(x, str))
        return data.get("lastTrip")

    def _save_state(self):
        try:
            with open(self._state_path(), "w") as f:
                json.dump({"lastTrip": self.last_trip_summary,
                           "reportDeadlines": self.report_deadlines,
                           "threadReaps": self.thread_reaps,
                           # Bounded the same way: the newest 32 panes, far
                           # more evenings than a deadline can outlive.
                           "panesKilled": dict(sorted(
                               self._panes_killed.items(),
                               key=lambda kv: kv[1])[-32:]),
                           # Bounded: one session id per app load, so the tail
                           # is every ride of the last few days.
                           "endedArrived": sorted(self.ended_arrived)[-32:]}, f)
        except OSError:
            pass

    # -- event intake ------------------------------------------------------

    def process_line(self, raw):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(obj, dict):
            self.process(obj)

    def process(self, obj):
        try:
            self._process(obj)
        except Exception as exc:  # never let one bad line kill the daemon
            self.log.error("event processing failed: %r (line type=%s)" % (
                exc, obj.get("type") or obj.get("event")))

    def _is_duplicate_record(self, obj, t, session, kind, typ):
        """The same record, delivered twice by a client that re-POSTed a batch.

        The debug-log client retries a batch it is not sure landed, so an
        identical record arrives again: same `t` (the app's own Date.now()),
        same payload id, same everything — only the sidecar's `recv` differs,
        by 208 ms on 2026-08-27 at 13:10:42. That pair went into the ride
        notes as "an exact same-second duplicate notification", was chased as
        an app defect, and was telemetry the whole time. The 8/27 note warns
        about precisely this trap and the next plan repeated it anyway.

        It is not rare and it is not confined to notifications: ~1,000-1,700
        records a day across 8/27-8/29, 3.7% of 8/27's stream, including
        UPDATE_PROGRESS, UPDATE_POSITION and ADD_NOTIFICATION. Every rule here
        that counts events was reading a stream that lies about its counts,
        which matters most for notification-repeat now that two-in-window is
        reportable: without this, one alert counted twice IS the finding.

        Deduped on identity AND arrival, because identity alone was wrong.
        The first version of this asserted that "two distinct events of one
        type in one millisecond do not happen at ~1 Hz telemetry". They do:
        2026-08-27 13:35:02 carries 197 POSITION_RESPONSE actions in 584 ms,
        one per in-flight request settling. On identity alone this dropped 492
        genuine records that day -- 461 of them POSITION_RESPONSE -- while
        catching 1,207 real re-POSTs.

        `recv` is what separates them. It is stamped by the sink per REQUEST,
        so every record written by one POST shares it: a re-POST necessarily
        carries a different `recv`, and a burst inside one batch necessarily
        shares one. So a repeat is a duplicate only when it arrives in a
        different delivery than the one that first carried it.

        Prefers the client's own entry id when the record has one. That is
        exact where this is inferential -- a re-send carries the original's
        id, and each member of a same-millisecond burst carries its own -- so
        once clients mint ids the heuristic below is only for historical logs.
        """
        payload = obj.get("payload")
        pid = payload.get("id") if isinstance(payload, dict) else None
        entry_id = obj.get("id")
        if isinstance(entry_id, str) and entry_id:
            key = ("id", entry_id)
        else:
            key = (session, kind, typ, t, pid if isinstance(pid, str) else None)
        recv = obj.get("recv")
        seen_recv = self._seen_record_keys.get(key, _UNSEEN)
        if seen_recv is not _UNSEEN and seen_recv != recv:
            self.duplicate_records += 1
            if self.duplicate_records % 100 == 1:
                self.log.info(
                    "dropped a re-POSTed duplicate record (%s at %s); %d so far"
                    % (typ, fmt_hms(t), self.duplicate_records))
            return True
        if seen_recv is _UNSEEN:
            self._seen_record_keys[key] = recv
            self._seen_records.append(key)
            while len(self._seen_records) > RECORD_DEDUP_RING:
                self._seen_record_keys.pop(self._seen_records.popleft(), None)
        return False

    def _process(self, obj):
        t = obj.get("t") or int((obj.get("recv") or 0) * 1000)
        if not isinstance(t, (int, float)) or t <= 0:
            return
        t = int(t)
        self.clock_ms = max(self.clock_ms, t)
        session = obj.get("session") or "unknown"
        kind = obj.get("kind")
        typ = obj.get("type")
        if typ is None and kind != "session":
            typ = obj.get("event")

        if self._is_duplicate_record(obj, t, session, kind, typ):
            return

        trip = self.trips.get(session)
        if trip:
            trip.last_event_ms = max(trip.last_event_ms, t)
            if not trip.device:
                trip.device = obj.get("device")

        if typ == "START_GO_MODE":
            self._on_start_go_mode(session, t, obj)
        elif typ == "STOP_GO_MODE":
            if trip:
                self._end_trip(trip, t, "stop")
        elif typ == "RIDER_NOTE" or kind == "rider-note":
            # Typed by the rider on the /ride console mid-trip. Handled before
            # the `trip is not None` branch because the note's session id is a
            # best-effort guess by the sidecar and may not match a known trip.
            self._on_rider_note(session, t, obj, trip)
        elif typ in ("START_ONBOARD_OPTIMIZE", "SET_ONBOARD_RESULT"):
            # Before the `trip is not None` chain on purpose: the onboard flow
            # runs entirely BEFORE START_GO_MODE opens a trip, so on 8/9 every
            # one of these fell through and the daemon saw none of it.
            self._rule_stale_alight_candidate(session, t, typ,
                                              obj.get("payload"), trip)
        elif trip is not None:
            if kind == "console":
                self._rule_console(trip, t, obj)
            elif typ == "UPDATE_POSITION":
                trip.last_pos_ms = max(trip.last_pos_ms, t)
                # Only a fix that actually closes the gap closes the gap. A
                # phone coming back onto the network replays its buffered
                # fixes, each stamped with its own OLD time, and every one of
                # them used to clear this flag — so check_timers re-opened the
                # gap on the very next event and fired again. On 2026-08-27
                # that produced sixteen gps-gap findings inside one second,
                # with the reported gap shrinking 108s -> 60s as the backlog
                # drained. One unbroken gap should be one finding.
                if self.now_ms() - t < GPS_GAP_MS:
                    trip.gps_gap_open = False
                    trip.gps_gap_started_ms = None
                self._on_position(trip, obj.get("payload") or {})
            elif typ == "SET_ARRIVED":
                # The client now latches arrival (otp-react-redux
                # progress-calculator hasArrivedAtDestination) and dispatches
                # this. Before that latch existed the daemon had no notion of a
                # trip ENDING at all — only STOP_GO_MODE, a 15-minute silence,
                # or replay EOF — which is why on 2026-08-27 it went on judging
                # a finished trip for four and a half hours and produced ~25 of
                # that ride's 42 findings about a rider sitting at their desk
                # and then driving home.
                self._note_arrival(trip, t, "SET_ARRIVED")
            elif typ == "UPDATE_PROGRESS":
                self._on_progress(trip, t, obj.get("payload") or {})
            elif typ == "UPDATE_ROUTE_MATCH":
                self._on_route_match(trip, t, obj.get("payload") or {})
            elif typ == "UPDATE_VEHICLE_MATCH":
                self._on_vehicle_match(trip, t, obj.get("payload") or {})
            elif typ == "SET_RIDING":
                self._on_set_riding(trip, t, obj.get("payload") or {})
            elif typ == "CLEAR_RIDING":
                trip.riding = None
                self._mark_dirty()
            elif typ == "TRANSITION_LEG":
                self._on_transition_leg(trip, t, obj.get("payload") or {})
            elif typ == "ADD_NOTIFICATION":
                self._on_notification(trip, t, obj.get("payload") or {})
            elif typ == "START_REROUTE":
                self._on_start_reroute(trip, t, obj.get("payload") or {})
            elif typ in ("REMEMBER_SEARCH", "ROUTING_REQUEST"):
                self._note_search(trip, t, typ, obj.get("payload"))
            elif typ == "ROUTING_RESPONSE":
                self._rule_bike_egress_missing(trip, t, obj.get("payload"))
            elif typ == "SET_ACTIVE_ITINERARY":
                # Rider picked an itinerary from the list — explicit action.
                trip.last_rider_action_ms = t
        elif typ == "UPDATE_PROGRESS":
            # Go Mode is clearly active but we never saw START_GO_MODE
            # (daemon started mid-trip, or the app resumed Go Mode from
            # persisted state, which emits none): consider adopting.
            self._maybe_adopt(session, t, obj)

        # Time-based rules ride on the advancing clock.
        self.check_timers()

    # -- state machine ------------------------------------------------------

    def _on_start_go_mode(self, session, t, obj):
        payload = obj.get("payload") or {}
        summary = summarize_itinerary(payload)
        trip = self.trips.get(session)
        if trip is None:
            trip = Trip(session, t, summary)
            # The rider asked for a ride under this id: whatever we decided
            # about the last one is history.
            self.ended_arrived.discard(session)
            self._declined_completed.discard(session)
            trip.device = obj.get("device")
            self._note_device_session(trip.device, session)
            self.trips[session] = trip
            self.log.info("trip started: session=%s itinerary=%s" % (
                session, itinerary_one_liner(summary)))
            self._begin_ride_thread(trip, t)
        else:
            # Itinerary replacement mid-trip.
            self._clear_arrival(trip, t, "itinerary swap")
            trip.swap_seq += 1
            trip.swap_times.append(t)
            if summary is not None:
                trip.itinerary = summary
            # Keep prev_stops: a stop count that collapses to 1 *because of*
            # a swap is exactly the anomaly rule (a) exists to catch. Only
            # rule (b) (stop-count-increase) is excused across a swap.
            trip.stops_swap_pending = True
            trip.prev_dist = None
            self.log.info("itinerary swap #%d: session=%s -> %s" % (
                trip.swap_seq, session, itinerary_one_liner(summary)))
            # Not a push on its own — the swap either fires aboard-swap (which
            # is) or is the routine re-plan the rider asked for. It still goes
            # in the ledger so the next digest explains the new itinerary.
            self._thread_event(trip, t, "itinerary swap #%d -> %s" % (
                trip.swap_seq, itinerary_one_liner(summary)))
            self._rule_aboard_swap(trip, t)
            # An applied re-plan. This is the signal the client's own
            # noteReplanAttempt fires on, one step downstream: the daemon
            # cannot see an attempt, only the itinerary it produced — which
            # is the better evidence anyway, since a swap that changed
            # nothing is the fact the rule is about.
            self._note_replan(trip, t, "itinerary-swap")
        self._flush_pending_onboard(trip)
        self._rule_itinerary_backwards(trip, t, summary)
        self._mark_dirty()

    def _maybe_adopt(self, session, t, obj):
        """Open a trip for a session we never saw start — or decline to.

        Two things must be true before adoption is the right answer, and
        2026-08-31 evening got both wrong.

        The ride must not already be over. Both of that evening's phantom
        trips were adopted off post-arrival ticks of a trip the app itself
        called `status: "completed"` — 76% of leg 3, 42 m from the door,
        SET_ARRIVED already latched. Watching a finished ride produced 37
        findings about a rider standing still, two threads, and two reports.
        A completed trip is not a ride in progress under any reading.

        And it must not be a ride this daemon is already watching under an
        older session id. See _continuation_of.
        """
        p = obj.get("payload") or {}
        ended = self.recently_ended.get(session, 0)
        if t - ended <= 60 * 1000:
            return
        # A ride this daemon already closed at arrival does not come back
        # sixty seconds later. Replaying 8/27 with the arrival rule and
        # without this turned that afternoon's one 4.5-hour ride into NINE:
        # close at arrival, re-adopt off the next tick, arrive again five
        # minutes on, forever, because post-arrival ticks do not all say
        # "completed" — the app went on map-matching a stationary rider and
        # calling it `deviated`. Only an explicit START_GO_MODE re-opens this
        # session, which is the rider asking for a ride in so many words.
        if session in self.ended_arrived:
            return
        if p.get("status") == "completed":
            if session not in self._declined_completed:
                self._declined_completed.add(session)
                self.log.info(
                    "not adopting session %s: the app says the trip is already"
                    " completed (leg %s at %s)"
                    % (session, p.get("currentLegIndex"),
                       fmt_pct(p.get("currentLegProgress"))))
            return
        prior = self._continuation_of(session, t, obj, p)
        if prior is not None:
            self._adopt_continuation(prior, session, t, p)
            return
        trip = Trip(session, t, None, adopted=True)
        trip.device = obj.get("device")
        self.trips[session] = trip
        self.log.info("adopted mid-stream trip for session %s" % session)
        # An adopted trip is a ride in progress — usually the daemon was
        # just restarted under a rider who is still on the bus — so it
        # gets a thread too, marked as adopted in the digest.
        self._begin_ride_thread(trip, t)
        # After the thread exists, so the console hears it: a finding filed
        # before the spawn goes into the ledger and nowhere else.
        self._rule_resumed_trip(trip, t, obj)
        self._on_progress(trip, t, p)
        self._mark_dirty()

    def _note_device_session(self, device, session):
        """Remember which session ids a phone has been seen under."""
        if not device or not session:
            return
        seen = self.device_sessions.setdefault(device, [])
        if session not in seen:
            seen.append(session)
            del seen[:-16]

    def _rule_resumed_trip(self, trip, t, obj):
        """A ride that begins without a START_GO_MODE cannot be replayed.

        Two ways in. The app re-mounts onto a trip it is already running and
        the debug-log client mints a fresh session id — Go Mode resumed from
        persisted state emits no START_GO_MODE at all, which is exactly why
        build-fixture.js rejects such sessions. Or this daemon was restarted
        under a rider who is still on the bus, which is nobody's bug.

        The two are told apart by the phone: a NEW session id on a device this
        process has already seen is the app re-mounting. That is the one worth
        a `warn` — it is an app defect, it splits the telemetry across two
        ledgers, and the ride it produces has no fixture. A first sighting of
        the device is the daemon's own restart and lands at `info`.

        A re-mount that lands on a ride still in flight never reaches here:
        _continuation_of catches it, the two ids become one ride, and
        _adopt_continuation files `session-churn` instead. This rule is for
        the one that arrives too late for that — after the prior ride ended,
        or onto a trip the daemon had declined.
        """
        device = obj.get("device")
        prior = [s for s in self.device_sessions.get(device, [])
                 if s != trip.session] if device else []
        self._note_device_session(device, trip.session)
        remount = bool(prior)
        p = obj.get("payload") or {}
        if remount:
            summary = ("ride resumed with no START_GO_MODE: session %s is new"
                       " on a phone last seen as %s, so this ride has no"
                       " fixture and cannot be replayed"
                       % (trip.session, prior[-1]))
        else:
            summary = ("ride adopted mid-stream with no START_GO_MODE (leg %s"
                       " at %s); it has no fixture and cannot be replayed"
                       % (p.get("currentLegIndex"),
                          fmt_pct(p.get("currentLegProgress"))))
        self._finding(
            trip, t, "resumed-trip", "warn" if remount else "info", summary,
            {"session": trip.session, "device": device,
             "priorSessions": prior,
             "cause": "app-remount" if remount else "daemon-started-mid-ride",
             "legIndex": p.get("currentLegIndex"),
             "legProgressPct": p.get("currentLegProgress"),
             "replayable": False})

    def _continuation_of(self, session, t, obj, p):
        """The live ride this brand-new session id is plainly a resumption of.

        The app re-mounts and the debug-log client mints a fresh session id;
        nothing in the stream says the two belong together, so the daemon read
        one continuous situation as two rides. The rider's half of that is an
        app fix (keep the id across a mount). The daemon's half is to notice.

        Four gates, and they must all hold, because merging two genuinely
        separate rides is the worse error: one report would describe two trips
        and the second ride's findings would land in the first ride's ledger.

          * the same phone (`device`), which is stable across a mount;
          * within CONTINUATION_GAP_MS of the older trip's last event — the
            8/31 remount was 41 s wide, and a rider who finishes a ride and
            starts another does not do it inside two minutes;
          * the same leg index; and
          * the same position within that leg (CONTINUATION_PROGRESS_PCT). A
            genuinely new ride starts at leg 0 at ~0%, which is what makes
            this gate the load-bearing one: it is not "the same phone
            recently", it is "the same phone, still exactly where the ride we
            are already watching left off".

        Only reachable from the adoption path, i.e. only when the new session
        arrived with no START_GO_MODE of its own. An explicit start is the
        rider asking for a ride and is always taken at its word.
        """
        device = obj.get("device")
        leg = p.get("currentLegIndex")
        prog = p.get("currentLegProgress")
        if not device or leg is None or not isinstance(prog, (int, float)):
            return None
        best = None
        for trip in self._active_trips():
            if session in trip.sessions or trip.device != device:
                continue
            gap = t - trip.last_event_ms
            if gap < 0 or gap > CONTINUATION_GAP_MS:
                continue
            last = trip.progress or {}
            if last.get("currentLegIndex") != leg:
                continue
            was = last.get("currentLegProgress")
            if (not isinstance(was, (int, float))
                    or abs(was - prog) > CONTINUATION_PROGRESS_PCT):
                continue
            if best is None or trip.last_event_ms > best.last_event_ms:
                best = trip
        return best

    def _adopt_continuation(self, trip, session, t, p):
        """Carry the ride forward under its new session id.

        The new id becomes an alias in self.trips; `trip.session` does not
        move, so the findings ledger, the digest, the report request and the
        vault report all stay one file about one ride, and the thread the
        rider is already talking to keeps talking about it.

        Recorded as a finding rather than done quietly. The split is the app's
        bug and someone has to fix it there; a daemon that silently papered
        over it would leave the evidence nowhere. It is a warn, not a page:
        the rider can do nothing about it while riding.
        """
        gap_ms = t - trip.last_event_ms
        trip.sessions.append(session)
        self.trips[session] = trip
        self._note_device_session(trip.device, session)
        self.log.info(
            "session %s continues %s (same device, leg %s at %s, %ds later)"
            % (session, trip.session, p.get("currentLegIndex"),
               fmt_pct(p.get("currentLegProgress")), gap_ms // 1000))
        self._finding(
            trip, t, "session-churn", "warn",
            "the app re-mounted mid-ride and minted session %s %ds after the"
            " last event on %s; counted as one ride"
            % (session, gap_ms // 1000, trip.session),
            {"newSession": session, "priorSession": trip.session,
             "gapMs": gap_ms, "device": trip.device,
             "legIndex": p.get("currentLegIndex"),
             "legProgress": p.get("currentLegProgress")})
        trip.last_event_ms = max(trip.last_event_ms, t)
        self._on_progress(trip, t, p)
        self._mark_dirty()

    def _note_arrival(self, trip, t, source):
        """Latch arrival once, from whichever evidence reaches us first.

        SET_ARRIVED is the client's own latch and fires once per mount, which
        makes it unreachable for a trip this daemon adopted afterwards: on
        2026-08-31 it fired at 18:52:14.782, before the trip it belonged to
        existed here. `status: "completed"` on UPDATE_PROGRESS is the same
        fact restated every tick, so it is the belt to that brace.
        """
        if trip.arrived_ms is not None:
            return
        trip.arrived_ms = t
        trip.arrived_leg = (trip.progress or {}).get("currentLegIndex")
        self.log.info("arrived (%s): session=%s" % (source, trip.session))
        self._thread_event(trip, t, "arrived at destination")
        self._mark_dirty()

    def _clear_arrival(self, trip, t, why):
        """The ride demonstrably resumed after we decided it had finished.

        Arrival is an inference and ARRIVED_END_MS acts on it, so a wrong one
        would close a live ride five minutes later and stop watching it —
        strictly worse than the hole it fixes. Boarding a vehicle, advancing
        to a later leg, and re-planning are all things a finished trip does
        not do; any of them puts the ride back in progress.
        """
        if trip.arrived_ms is None:
            return
        self.log.info("arrival cleared (%s): session=%s" % (why, trip.session))
        trip.arrived_ms = None
        trip.arrived_leg = None
        self._thread_event(trip, t, "ride resumed after arrival (%s)" % why)
        self._mark_dirty()

    def _active_trips(self):
        """The live trips, each exactly once.

        self.trips is keyed by session id and one ride can hold more than one
        of those (_adopt_continuation), so iterating .values() would tick the
        same trip twice per pass — two heartbeats, two timeout checks, and a
        KeyError on the second _end_trip.
        """
        out, seen = [], set()
        for trip in list(self.trips.values()):
            if id(trip) in seen:
                continue
            seen.add(id(trip))
            out.append(trip)
        return out

    def _flush_pending_onboard(self, trip):
        """Emit onboard-flow findings that had no trip to hang on yet."""
        for (ts, rule, summary, ctx) in self.pending_onboard.pop(
                trip.session, []):
            self._finding(trip, ts, rule, "warn", summary, ctx)

    def _rule_itinerary_backwards(self, trip, t, summary):
        """A leg that starts before the previous one ends (8/9).

        The rider photographed this: a trip sheet reading 7:29 PM above 7:18 PM,
        because the onboard optimizer grafted an onward plan anchored to a
        realtime arrival that was already nine minutes stale. The daemon watched
        the whole ride and raised nothing, which is why the bug was found days
        later in a photo instead of during the ride.

        summarize_itinerary already carries each leg's startTime/endTime, so
        this is a walk over what we have.
        """
        legs = (summary or {}).get("legs") or []
        worst = None
        for i in range(1, len(legs)):
            prev_end = legs[i - 1].get("endTime")
            start = legs[i].get("startTime")
            if not isinstance(prev_end, (int, float)):
                continue
            if not isinstance(start, (int, float)):
                continue
            by = prev_end - start
            if by > LEG_INVERSION_MS and (worst is None or by > worst[1]):
                worst = (i, by)
        if worst is None:
            return
        idx, by = worst
        self._finding(
            trip, t, "itinerary-backwards", "page",
            "leg %d starts %s before leg %d ends — the trip sheet runs backwards"
            % (idx, fmt_ms_span(by), idx - 1),
            {"byMs": int(by), "leg": idx,
             "legs": [{k: leg.get(k) for k in
                       ("mode", "route", "startTime", "endTime")}
                      for leg in legs]},
            push_body="Trip times run backwards (leg %d starts %s before leg %d "
                      "ends). Check the trip sheet before you act on it."
                      % (idx, fmt_ms_span(by), idx - 1))

    def _rule_stale_alight_candidate(self, session, t, typ, payload, trip):
        """An alight option computed from an arrival already behind the clock.

        The earlier half of the same 8/9 failure, and the earlier warning: the
        FIRST optimize that evening already carried a candidate 578,912 ms
        behind its own timestamp, five minutes before the rider was shown the
        options that sent them to a route 22 that had gone.

        WARN, not page, and once per trip. Measured against the 8/9 log it
        fires four times — and pages are capped at 2 per trip, first come — so
        as a page it spent the whole budget on the precursor and suppressed
        itinerary-backwards, the one finding the rider could actually act on.
        A stale candidate on its own asks nothing of a rider beyond "look
        before you pick"; if it goes on to produce a backwards trip sheet, that
        rule pages. This one belongs in the ledger and the post-ride report.
        """
        if typ == "START_ONBOARD_OPTIMIZE":
            items = (payload or {}).get("candidates") or []
        else:
            items = payload if isinstance(payload, list) else []
        worst = None
        for item in items:
            if not isinstance(item, dict):
                continue
            epoch = item.get("busArrivalEpoch")
            if not isinstance(epoch, (int, float)):
                continue
            behind = t - epoch
            if behind > STALE_CANDIDATE_MS and (worst is None
                                                or behind > worst[1]):
                worst = (item, behind)
        if worst is None:
            return
        item, behind = worst
        name = item.get("stopName") or item.get("stopId") or "a stop"
        summary = ("alight candidate for %s is dated %s in the past — the feed "
                   "reading is stale, not a prediction" % (name,
                                                           fmt_ms_span(behind)))
        ctx = {"behindMs": int(behind), "eventType": typ,
               "stopId": item.get("stopId"), "stopName": item.get("stopName"),
               "busArrivalEpoch": item.get("busArrivalEpoch"),
               "realtime": item.get("realtime")}
        if trip is not None:
            # Once per trip: one bad feed reading produces an optimize per
            # rediscovery, and four identical lines say nothing the first did.
            if any(f["rule"] == "stale-alight-candidate"
                   for f in trip.findings):
                return
            self._finding(trip, t, "stale-alight-candidate", "warn",
                          summary, ctx)
        else:
            # No trip yet — the onboard flow runs before START_GO_MODE. Hold
            # the first one for the trip that is about to open.
            held = self.pending_onboard.setdefault(session, [])
            if held:
                return
            held.append((t, "stale-alight-candidate", summary, ctx))
            self.log.info("held onboard finding for session %s: %s"
                          % (session, summary))

    def _end_trip(self, trip, t, reason):
        # Whole-leg verdicts, before anything counts the findings: a rule whose
        # question is "did this ever happen across the leg?" can only be
        # answered once the ride is over, and the report request quotes
        # len(trip.findings).
        self._rule_vehicle_match_never(trip, t)
        # Flush first: a page must not be lost because the trip ended three
        # seconds into its coalescing window.
        self._flush_pages(trip, t, force=True)
        trip.end_ms = t
        trip.end_reason = reason
        # Every session id this ride was seen under, not just the first: an
        # alias left behind in self.trips would be re-adopted as a new ride on
        # the next tick, and _active_trips would still hand the ended trip to
        # the timers.
        for key in [s for s, tr in self.trips.items() if tr is trip]:
            del self.trips[key]
            self.recently_ended[key] = t
            if reason == "arrived":
                self.ended_arrived.add(key)
        self.ended_trips.append(trip)
        n = len(trip.findings)
        self.log.info("trip ended: session=%s reason=%s findings=%d" % (
            trip.session, reason, n))
        self.last_trip_summary = {
            "session": trip.session,
            "date": fmt_date(trip.start_ms),
            "endedAt": fmt_hms(t),
            "reason": reason,
            "findings": n,
            "itinerary": itinerary_one_liner(trip.itinerary),
        }
        self._save_state()
        req_path = self._write_report_request(trip) if n > 0 else None
        # The wrap-up is the thread's job now (no headless report agent): tell
        # it the ride is over and where the request file is, and it writes the
        # vault report from the context it has been holding all ride.
        # "%d recorded note(s)", not "%d note(s)": the count is of notes that
        # reached the telemetry stream. On 8/2 it read "0 note(s)" to a thread
        # the rider had typed three notes into, which invited the wrap-up to
        # report that the rider said nothing. The thread's own conversation is
        # the other half, and the sysprompt tells it to use both.
        line = "trip ended (%s) after %dm — %d finding(s), %d recorded note(s)" % (
            reason, max(0, (t - trip.start_ms) // 60000), n, len(trip.notes))
        if req_path:
            line += " — wrap-up now; request: %s" % req_path
        self._thread_event(trip, t, line)
        self._thread_push(trip, line)
        # Fallback: findings with nobody to write them up. Same push the report
        # agent's failure used to send — it is still exactly the right sentence.
        if n > 0 and self._thread_missing(trip):
            self.log.warn("no ride thread for %s; falling back to a page"
                          % trip.session)
            self._report_fallback_push(n)
        elif req_path:
            # A thread that spawned fine and took the wrap-up line is not the
            # same thing as a wrap-up. Arm a deadline. (8/28)
            self._arm_report_deadline(trip, t, n)
        # The other half of the lifecycle. A pane that owes a wrap-up is now
        # held by its deadline and reaped when that settles; a pane that owes
        # nothing — no findings, so no request, or a spawn that failed — has
        # no reason to outlive the ride at all. Before this, neither branch
        # reaped anything and the pane waited for the NEXT ride's spawn.
        if not self._deadline_for_pane((trip.thread or {}).get("tmux")):
            self._schedule_thread_reap((trip.thread or {}).get("tmux"),
                                       max(int(t), self.now_ms()),
                                       "trip ended (%s), no wrap-up owed" % reason)
        self._mark_dirty()
        self.write_status(force=True)

    def _arm_report_deadline(self, trip, t, findings_n):
        """Watch for the wrap-up that was asked for, and page if it never lands.

        The 8/28 hole: _report_fallback_push had exactly one call site, guarded
        by _thread_missing, which is true only when the tmux spawn failed or
        the pane is dead. The evening's thread was neither — it spawned, took
        the "trip ended … wrap-up now" line, and then sat at a permission
        prompt for about three hours. No page was ever sent, because from the
        daemon's side everything had gone right.

        It survives `del self.trips[trip.session]` two ways, both necessary.
        The entry is a plain dict on self.report_deadlines rather than a Trip,
        so nothing about it depends on the trip still being live — check_timers
        iterates self.trips and would never have seen an ended one. And it is
        written into state.json, so a daemon restarted inside the deadline
        window re-adopts the promise instead of quietly dropping it, which
        matters because "restart the daemon on commit" is now a thing that
        happens to this process mid-evening.
        """
        path = trip.report_path
        if not path:
            return
        # No thread object at all means nobody was ever asked to write this:
        # a replay, or the RIDE_THREAD_ENABLED=0 kill switch. A deadline there
        # would later "discover" a report that was never promised — re-running
        # the 7/29 log would page the rider about a ride from last month. The
        # spawn-failed case does not reach here; _thread_missing pages it now.
        if trip.thread is None:
            return
        # From now, not from `t`. A timeout end is stamped with the ride's
        # LAST EVENT, fifteen minutes in the past, so the deadline was already
        # expired the moment it was armed: on 8/31 at 18:00:34 the daemon
        # logged "wrap-up expected ... by 17:55:33" and paged the rider about
        # the missing report in the same second, before the thread had been
        # handed the request. Ten minutes has to be ten minutes of the
        # thread's time.
        due = max(int(t), self.now_ms()) + REPORT_DEADLINE_MS
        self.report_deadlines.append({
            "session": trip.session,
            "reportPath": path,
            "dueMs": due,
            # When the promise was made. _check_report_deadlines compares it
            # against _panes_killed so it can tell "the thread had ten minutes
            # and wrote nothing" from "this daemon killed the pane".
            "armedMs": self.now_ms(),
            "findings": findings_n,
            "requestPath": self._report_request_path(trip),
            # Which pane was asked. _kill_previous_threads reads this: the
            # next ride's thread must not kill the one still writing.
            "tmux": (trip.thread or {}).get("tmux"),
        })
        self.log.info("wrap-up expected at %s by %s" % (path, fmt_hms(due)))
        self._save_state()

    def _check_report_deadlines(self, now):
        """Has each promised wrap-up appeared? Page for the ones that have not.

        Deliberately checks the file rather than the thread: the question the
        rider cares about is whether the report exists, and a pane that looks
        alive has already been shown to prove nothing. reportPath is the exact
        string handed to the thread in the request file, so this cannot drift
        into watching for a name nobody was asked to write.
        """
        if not self.report_deadlines:
            return
        keep, changed = [], False
        for entry in self.report_deadlines:
            path = entry.get("reportPath")
            try:
                landed = bool(path) and os.path.exists(path)
            except OSError:
                landed = False
            if landed:
                self.log.info("wrap-up landed for %s: %s"
                              % (entry.get("session"), path))
                changed = True
                # The pane has done the one thing it was being kept alive for.
                self._schedule_thread_reap(entry.get("tmux"), now,
                                           "wrap-up landed")
                continue
            if now < entry.get("dueMs", 0):
                # Still inside the window — but if the pane that was asked has
                # gone, waiting the rest of it out changes nothing. Hand the
                # request to the thread that IS alive instead (2.6's preferred
                # fix: same session, same rider, and it is running anyway).
                if self._maybe_reassign_wrap_up(entry, now):
                    changed = True
                keep.append(entry)
                continue
            changed = True
            # Never page about a report this daemon prevented. The pane is
            # dead by our own hand and the thread never had the ten minutes
            # the deadline claims to have given it, so "report pending, open
            # Claude" is a page about our own bug — and it costs one of two
            # ride interrupts, usually while the rider is on the next bus.
            killed = self._panes_killed.get(entry.get("tmux"))
            if killed is not None and killed >= entry.get("armedMs", 0):
                self.log.error(
                    "no wrap-up for %s (%s) and none was possible: this daemon"
                    " killed its pane %s at %s. Not paging the rider about a"
                    " report it prevented."
                    % (entry.get("session"), path, entry.get("tmux"),
                       fmt_hms(killed)))
                continue
            self.log.warn(
                "no wrap-up for %s %d min after the ride ended (%s); paging"
                % (entry.get("session"), REPORT_DEADLINE_MS // 60000, path))
            self._report_fallback_push(entry.get("findings") or 0)
            self._schedule_thread_reap(entry.get("tmux"), now,
                                       "wrap-up deadline expired")
        if changed:
            self.report_deadlines = keep
            self._save_state()

    def _deadline_for_pane(self, name):
        if not name:
            return None
        for entry in self.report_deadlines:
            if entry.get("tmux") == name:
                return entry
        return None

    def _maybe_reassign_wrap_up(self, entry, now):
        """Give an orphaned wrap-up to a thread that is actually alive.

        The pane that was asked can be gone before its deadline for reasons
        that have nothing to do with the thread: a spawn that collided on the
        name, a rider who typed /exit, a kill this daemon made itself. Waiting
        out the remaining minutes and then paging is the worst of both — no
        report, and an interrupt.

        Once per entry, and only onto a pane belonging to a live trip: the ride
        thread holds the ride in its conversation, so the one that is running
        now is the only other party that can write anything at all.
        """
        pane = entry.get("tmux")
        if not pane or entry.get("reassigned"):
            return False
        # Cheap liveness only — no tmux subprocess on the tailer's 5 s tick.
        # A pane we killed, or one whose spawn reported failure, is gone; a
        # pane we know nothing about is assumed fine. The kill has to be
        # NEWER than the promise: _panes_killed survives restarts and pane
        # names are clock minutes, so yesterday's ride-1029 must not condemn
        # today's.
        killed = self._panes_killed.get(pane)
        gone = ((killed is not None and killed >= entry.get("armedMs", 0))
                or self._thread_status.get(pane) is False)
        if not gone:
            return False
        target = None
        for trip in self._active_trips():
            name = (trip.thread or {}).get("tmux")
            if name and name != pane and self._thread_ok(trip):
                target = trip
        if target is None:
            return False
        entry["reassigned"] = True
        entry["tmux"] = (target.thread or {}).get("tmux")
        entry["dueMs"] = now + REPORT_DEADLINE_MS
        line = ("you also owe the previous ride's wrap-up: write %s from %s"
                % (entry.get("reportPath"), entry.get("requestPath")))
        self.log.warn("wrap-up for %s reassigned from %s to %s (%s)"
                      % (entry.get("session"), pane, entry["tmux"], line))
        self._thread_event(target, now, line)
        self._thread_push(target, line)
        return True

    def check_timers(self):
        """Silence-based rules + trip timeout. Called per event and on ticks.

        This is also how a buffered page gets out when the log goes quiet: the
        live loop ticks every 5s regardless of traffic, so a closed coalescing
        window is never waiting on the next telemetry line.
        """
        now = self.now_ms()
        for trip in self._active_trips():
            if now - trip.last_event_ms > SESSION_TIMEOUT_MS:
                self._end_trip(trip, trip.last_event_ms, "timeout")
                continue
            # The ride is over and the app is still talking. Every other
            # trip-end in this file waits for the stream to stop; on 8/31 it
            # never did, and the ride got no report at all. Ended at `now`
            # rather than at the arrival five minutes back so a note typed at
            # the destination is still inside the ride it belongs to.
            if (trip.arrived_ms is not None
                    and now - trip.arrived_ms > ARRIVED_END_MS):
                self._end_trip(trip, now, "arrived")
                continue
            self._flush_pages(trip, now)
            # gps-gap: no position fix for >60s mid-trip.
            # Not after arrival: a phone idle in the rider's pocket at the
            # destination is not a diagnostic event. On 2026-08-27 the "mid-trip"
            # wording was also simply untrue — the rider had been at 4Front for
            # two minutes when the first one fired.
            if (trip.arrived_ms is None
                    and not trip.gps_gap_open
                    and now - trip.last_pos_ms > GPS_GAP_MS
                    and trip.gps_gap_started_ms != trip.last_pos_ms):
                trip.gps_gap_open = True
                trip.gps_gap_started_ms = trip.last_pos_ms
                gap_s = (now - trip.last_pos_ms) // 1000
                self._finding(trip, now, "gps-gap", "warn",
                              "no GPS fix for %ds mid-trip" % gap_s,
                              {"lastFixMs": trip.last_pos_ms})
            # deviated-streak may mature between UPDATE_PROGRESS ticks
            self._check_deviated_streak(trip, now)
            self._check_stalled(trip, now)
            self._maybe_heartbeat(trip, now)
        # Outside the loop on purpose: a promised wrap-up outlives its trip,
        # which was deleted from self.trips the moment the ride ended. The
        # live loop ticks every 5s whether or not telemetry is arriving, so a
        # phone that has gone home and stopped talking still gets its deadline
        # checked.
        self._check_report_deadlines(now)
        # ...and the same is true of a console whose ride is over: the reap it
        # is waiting out is not attached to any live trip either.
        self._reap_due_threads(now)

    # -- rules --------------------------------------------------------------

    def _on_progress(self, trip, t, p):
        prev = trip.progress
        trip.progress = {
            "currentLegIndex": p.get("currentLegIndex"),
            "currentLegProgress": p.get("currentLegProgress"),
            "status": p.get("status"),
            "stopsRemaining": p.get("stopsRemaining"),
            "stopsTrusted": p.get("stopsTrusted"),
            "nextStopName": p.get("nextStopName"),
            # Straight-line metres left to the destination, recomputed by the
            # client every tick (progress-calculator's distanceToFinalStop).
            # It has been in the stream since 2026-08-28 and until now the
            # daemon threw it away — which is why the afternoon's 32 minutes
            # of non-convergent re-planning were invisible here.
            "distanceToDestination": p.get("distanceToDestination"),
            "tMs": t,
        }
        self._note_destination_distance(trip, p.get("distanceToDestination"))
        self._mark_dirty()

        # The app's own verdict on its own trip, restated every tick. It is
        # the only arrival evidence an adopted trip can ever see.
        if p.get("status") == "completed":
            self._note_arrival(trip, t, "status=completed")
        elif (trip.arrived_ms is not None
                and isinstance(trip.arrived_leg, int)
                and isinstance(p.get("currentLegIndex"), int)
                and p.get("currentLegIndex") > trip.arrived_leg):
            self._clear_arrival(trip, t, "leg %s -> %s"
                                % (trip.arrived_leg, p.get("currentLegIndex")))

        # Leg transition: the one routine milestone worth a ping. It is where
        # the rider's next decision lives (get off, walk, board) and it is the
        # moment the thread's picture of the ride would otherwise go stale.
        leg = p.get("currentLegIndex")
        if prev is not None and leg is not None and prev.get("currentLegIndex") != leg:
            line = "leg %s -> %s (%s)" % (
                prev.get("currentLegIndex"), leg, self._leg_label(trip, leg))
            self._thread_event(trip, t, line)
            self._thread_push(trip, line)

        # deviated-streak bookkeeping
        if p.get("status") == "deviated":
            if trip.deviated_since_ms is None:
                trip.deviated_since_ms = t
                trip.deviated_fired = False
            self._check_deviated_streak(trip, t)
        else:
            trip.deviated_since_ms = None
            trip.deviated_fired = False

        # stop-count rules (transit legs only; stopsRemaining is null on
        # street legs)
        stops = p.get("stopsRemaining")
        progress = p.get("currentLegProgress")
        if stops is not None and isinstance(stops, (int, float)):
            prev_stops = trip.prev_stops
            same_leg = (
                prev is not None
                and prev.get("currentLegIndex") == p.get("currentLegIndex")
            )
            if prev_stops is not None and same_leg:
                if (stops == 1 and prev_stops > 1
                        and isinstance(progress, (int, float))
                        and progress < STOP_COLLAPSE_MAX_PROGRESS
                        and trip.current_leg_transit()
                        and trip.swap_seq not in trip.collapse_fired_seq):
                    trip.collapse_fired_seq.add(trip.swap_seq)
                    self._finding(
                        trip, t, "stop-count-collapse", "page",
                        "stopsRemaining %d -> 1 at %.0f%% of transit leg %s"
                        % (prev_stops, progress, p.get("currentLegIndex")),
                        {"prevStops": prev_stops, "stops": stops,
                         "legProgressPct": progress,
                         "nextStop": p.get("nextStopName")},
                        push_body="Stop count wrong — app says 1 left at %.0f%% of the leg. Ignore the banner."
                                  % progress)
                elif (stops > prev_stops
                        and not trip.stops_swap_pending
                        and t - trip.stop_increase_last_ms > STOP_INCREASE_COOLDOWN_MS):
                    trip.stop_increase_last_ms = t
                    self._finding(
                        trip, t, "stop-count-increase", "warn",
                        "stopsRemaining rose %d -> %d with no itinerary swap"
                        % (prev_stops, stops),
                        {"prevStops": prev_stops, "stops": stops,
                         "legIndex": p.get("currentLegIndex")})
            trip.prev_stops = int(stops)
            trip.stops_swap_pending = False
        else:
            # Street leg: stopsRemaining is absent, so there is nothing to
            # compare against on the next transit leg.
            trip.prev_stops = None
            trip.stops_swap_pending = False

        self._check_progress_without_motion(trip, t, p)

    def _on_position(self, trip, p):
        """Remember where the rider actually is.

        The real payload is `{coords: {latitude, longitude, accuracy, …},
        timestamp}`. Older synthetic streams wrap it as `{position: {coords}}`,
        so both are accepted rather than making a fixture lie about the shape.
        """
        coords = p.get("coords")
        if not isinstance(coords, dict):
            nested = p.get("position")
            coords = nested.get("coords") if isinstance(nested, dict) else None
        if not isinstance(coords, dict):
            return
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            trip.last_fix = (lat, lon)
            # Stall anchor: the oldest fix the rider has not meaningfully left.
            anchor = trip.stall_anchor
            if anchor is None or meters_between(anchor[0], (lat, lon)) > STALL_RADIUS_M:
                trip.stall_anchor = ((lat, lon), trip.last_pos_ms)
                trip.fixes_since_anchor = 1
            else:
                trip.fixes_since_anchor += 1

    def _check_progress_without_motion(self, trip, t, p):
        """Leg progress advancing faster than the rider physically moved.

        The anchor is the last place progress was believed. It is reset when
        the rider genuinely travels (past MOTION_DISPLACEMENT_M — a real move,
        not GPS jitter) or when the leg changes, so the question the rule
        actually asks is *physical*: did the progress bar gain more than
        MOTION_PROGRESS_PCT points in the time it took the rider to cover 15m?

        That window is adaptive, and both of its ends are real defects:
        - Stationary: the window is minutes wide. This is the 7/31 shape —
          map-matching noise reported to a standing rider as travel.
        - Moving: the window is a couple of seconds. This is the 7/29 shape —
          on the Orange Line the bar went 35% -> 71% in ONE second while the
          bus covered 6.7m, i.e. the app teleported the rider a kilometre up
          the leg. Nothing else in the engine noticed.
        """
        prog = p.get("currentLegProgress")
        leg = p.get("currentLegIndex")
        if not isinstance(prog, (int, float)) or trip.last_fix is None:
            return
        anchor = trip.motion_anchor
        fresh = {"fix": trip.last_fix, "progress": prog, "leg": leg, "tMs": t}
        if anchor is None or anchor["leg"] != leg:
            trip.motion_anchor = fresh
            return
        moved = meters_between(anchor["fix"], trip.last_fix)
        if moved > MOTION_DISPLACEMENT_M:
            trip.motion_anchor = fresh          # they really went somewhere
            return
        if prog - anchor["progress"] <= MOTION_PROGRESS_PCT:
            return
        if trip.motion_fired_ms and t - trip.motion_fired_ms <= MOTION_COOLDOWN_MS:
            return
        trip.motion_fired_ms = t
        self._finding(
            trip, t, "progress-without-motion", "warn",
            "leg progress %s -> %s while the fix moved %.0fm" % (
                fmt_pct(anchor["progress"]), fmt_pct(prog), moved),
            {"fromPct": anchor["progress"], "toPct": prog,
             "movedMeters": round(moved, 1), "legIndex": leg,
             "sinceMs": anchor["tMs"]})
        # Re-anchor: a drift that keeps drifting is one finding, not a stream.
        trip.motion_anchor = fresh

    def _check_deviated_streak(self, trip, now):
        # A rider who has arrived and walked off across the campus is not deviating
        # from a route they have finished. On 2026-08-27 this fired nine times
        # between 15:11 and 17:11 on a trip that ended at 15:10.
        if trip.arrived_ms is not None:
            return
        if trip.deviated_since_ms is None or trip.deviated_fired:
            return
        dur = now - trip.deviated_since_ms
        if dur > DEVIATED_STREAK_MS:
            trip.deviated_fired = True
            on_transit = trip.current_leg_transit()
            sev = "page" if on_transit else "warn"
            secs = dur // 1000
            self._finding(
                trip, now, "deviated-streak", sev,
                "status deviated for %ds%s" % (
                    secs, " on transit leg" if on_transit else ""),
                {"sinceMs": trip.deviated_since_ms, "onTransit": on_transit},
                push_body="Shown deviated %ds while on the bus. Position tracking may be off." % secs
                          if on_transit else None)

    def _check_stalled(self, trip, now):
        """The rider has not moved for a long time, mid-leg, trip still active.

        8/2: stationary at one point from 21:50 to 22:24 — 34 minutes, 640 m
        short of the destination — with Go Mode active, currentLegProgress 0
        and timeRemaining frozen at 217 s. Every number was internally
        consistent (no movement, no progress), which is exactly why no existing
        rule had anything to say about it.

        This cannot tell "parked" from "tracking broken" and does not pretend
        to: it reports the fact and lets the post-ride triage decide. Warn, not
        page — a rider who stopped somewhere knows they stopped, and the one
        who has been abandoned by a frozen tracker is not helped by a buzz.
        Re-arms on a cooldown so a long lunch is one finding, not twenty.
        """
        # Standing still at your destination is not a stalled trip. On 2026-08-27
        # this counted up 15/15/30/45/60/75 minutes stationary at 4Front, all of
        # it after the rider had arrived.
        if trip.arrived_ms is not None:
            return
        anchor = trip.stall_anchor
        if anchor is None or trip.last_fix is None:
            return
        # A GPS gap is a different fault with its own rule; do not double-report
        # a rider who simply stopped sending fixes as one who stopped moving.
        if now - trip.last_pos_ms > GPS_GAP_MS:
            return
        held_ms = now - anchor[1]
        if held_ms < STALL_MS:
            return
        if now - trip.stall_fired_ms < STALL_COOLDOWN_MS:
            return
        trip.stall_fired_ms = now
        leg = (trip.progress or {}).get("currentLegIndex")
        # `lat`/`lon` are the rider's CURRENT fix as of 2026-08-31. They used
        # to be the anchor's — where the rider FIRST stopped, up to STALL_MS
        # (15 min) ago and up to STALL_RADIUS_M (60 m) away — while `last_fix`
        # was loaded three lines up and used only as a null guard. Nothing in
        # the finding said when the last fix arrived or how many had, so five
        # 8/28 findings were triaged as a dead GPS receiver. It was not dead:
        # 2,168 distinct fixes came in, ~4.1 m apart, the whole time. The
        # anchor is still here under its own name, because "where they stopped"
        # and "where they are" are different questions and the rule is about
        # the gap between them.
        drift = (meters_between(anchor[0], trip.last_fix)
                 if trip.last_fix else None)
        self._finding(
            trip, now, "stalled-progress", "warn",
            "stationary %dm inside leg %s with the trip still active"
            " (GPS live: %d fixes, last %ds ago)" % (
                held_ms // 60000, leg, trip.fixes_since_anchor,
                (now - trip.last_pos_ms) // 1000),
            {"heldMs": held_ms, "legIndex": leg,
             "lat": trip.last_fix[0], "lon": trip.last_fix[1],
             "anchorLat": anchor[0][0], "anchorLon": anchor[0][1],
             "anchorSetMs": anchor[1],
             "lastFixMs": trip.last_pos_ms,
             "sinceLastFixMs": now - trip.last_pos_ms,
             "fixesSinceAnchor": trip.fixes_since_anchor,
             "movedFromAnchorM": round(drift, 1) if drift is not None else None,
             "legProgress": (trip.progress or {}).get("currentLegProgress")})

    def _on_route_match(self, trip, t, p):
        """Remember where the app thinks the rider is, then run the spike rule.

        The snapshot is not used by any rule — it is context for the ride
        thread, which gets asked things like "is it actually following me?" and
        needs the last number the app computed, not a rule's verdict on it.
        """
        trip.last_route_match = {
            "tMs": t,
            "legIndex": p.get("legIndex"),
            "distanceFromRoute": p.get("distanceFromRoute"),
            "isOnRoute": p.get("isOnRoute"),
            "progressAlongLeg": p.get("progressAlongLeg"),
        }
        self._rule_distance_spike(trip, t, p)

    def _on_vehicle_match(self, trip, t, p):
        """Last live-vehicle match: which bus the app believes is theirs."""
        match = p.get("match") if isinstance(p.get("match"), dict) else None
        trip.last_vehicle_match = {
            "tMs": t,
            "consecutiveMatches": p.get("consecutiveMatches"),
            "emptyPolls": p.get("emptyPolls"),
            "confidence": (match or {}).get("confidence"),
            "vehicleId": (match or {}).get("vehicleId"),
            "label": (match or {}).get("label"),
            "tripId": (match or {}).get("tripId"),
            "distanceMeters": (match or {}).get("distanceMeters"),
        }
        self._tally_vehicle_match(trip, t, match)
        self._rule_match_distance_absurd(trip, t, match)
        self._rule_match_trip_disagrees(trip, t, match)

    def _tally_vehicle_match(self, trip, t, match):
        """Per-transit-leg record of whether the matcher ever found anything.

        Kept per leg rather than per ride because a ride with a transfer can
        match one bus and not the other, and "the Orange Line matched" is not
        an answer about the 539.
        """
        if not trip.current_leg_transit():
            return
        leg = (trip.progress or {}).get("currentLegIndex")
        if not isinstance(leg, int):
            return
        tally = trip.vehicle_match_legs.get(leg)
        if tally is None:
            tally = {"polls": 0, "matched": False, "firstMs": t, "lastMs": t,
                     "route": self._leg_label(trip, leg)}
            trip.vehicle_match_legs[leg] = tally
        tally["polls"] += 1
        tally["lastMs"] = t
        confidence = (match or {}).get("confidence")
        # "matched" means the matcher named a vehicle. `confidence: "none"`
        # with `vehicleId: null` is the matcher correctly reporting that it
        # has nothing — 775 times in a row on 2026-09-01 ride 2.
        if (match or {}).get("vehicleId") or (
                confidence and confidence != "none"):
            tally["matched"] = True

    def _rule_vehicle_match_never(self, trip, t):
        """A transit leg ridden with no live vehicle behind it, ever.

        Not a page: the app did nothing wrong, and there is nothing the rider
        can do about the feed while sitting on the bus. It is for the report —
        every judgement made about boarding, delay and arrival on that leg was
        made without live vehicle data, and a report that does not say so
        reads as though the tracking worked.
        """
        for leg in sorted(trip.vehicle_match_legs):
            tally = trip.vehicle_match_legs[leg]
            if tally["matched"] or tally["polls"] < VEHICLE_MATCH_NEVER_MIN_POLLS:
                continue
            span_s = max(0, (tally["lastMs"] - tally["firstMs"]) // 1000)
            self._finding(
                trip, t, "vehicle-match-never", "warn",
                "no live vehicle ever matched on leg %d (%s): %d polls over"
                " %ds, all empty"
                % (leg, tally.get("route") or "transit", tally["polls"], span_s),
                {"legIndex": leg, "polls": tally["polls"],
                 "spanSeconds": span_s, "firstPollMs": tally["firstMs"],
                 "lastPollMs": tally["lastMs"]})

    def _rule_match_distance_absurd(self, trip, t, match):
        """The rider is not 10,000 km from the bus they are sitting on.

        8/2: every UPDATE_VEHICLE_MATCH while aboard reported ~10,268 km,
        decaying ~10 m/s — a real haversine against a null-island coordinate
        the feed published for the rider's own vehicle. Confidence still read
        'confirmed' because the match keys on vehicleId, so nothing downstream
        noticed. Diagnostic, not actionable: the rider cannot do anything with
        this, so it warns rather than pages.
        """
        if not match:
            return
        d = match.get("distanceMeters")
        if not isinstance(d, (int, float)) or d <= MATCH_DISTANCE_ABSURD_M:
            # Back to a plausible distance — re-arm, so a second episode later
            # in the ride is still reported.
            trip.match_distance_fired = False
            return
        if trip.match_distance_fired:
            return
        # Once per episode, not once per tick: on 8/2 this condition held for
        # the entire ride and would otherwise have written 582 identical
        # findings into the ledger the post-ride report reads.
        trip.match_distance_fired = True
        self._finding(
            trip, t, "match-distance-absurd", "warn",
            "vehicle match reports %.0f km to the rider's own bus" % (d / 1000.0),
            {"distanceMeters": d, "vehicleId": match.get("vehicleId"),
             "tripId": match.get("tripId"),
             "confidence": match.get("confidence")})

    def _rule_match_trip_disagrees(self, trip, t, match):
        """The matched trip and the boarded trip have parted company.

        8/2: the match sat on 1:1191630 (the ghost record for the vehicle's
        NEXT block trip) while SET_RIDING held 1:1201789 for the whole ride.
        That disagreement is what armed the boarded-earlier replan loop. One
        tick of it is a poll landing mid-rebind, so it has to be sustained.
        """
        riding = trip.riding
        if not match or not riding:
            trip.match_disagree_since_ms = None
            return
        m_trip, r_trip = match.get("tripId"), riding.get("tripId")
        if not m_trip or not r_trip or m_trip == r_trip:
            trip.match_disagree_since_ms = None
            return
        if trip.match_disagree_since_ms is None:
            trip.match_disagree_since_ms = t
            return
        if t - trip.match_disagree_since_ms < MATCH_TRIP_DISAGREE_MS:
            return
        if trip.match_disagree_fired:
            return
        trip.match_disagree_fired = True
        self._finding(
            trip, t, "match-trip-disagrees", "warn",
            "vehicle match trip %s disagrees with riding trip %s for %ds"
            % (m_trip, r_trip, (t - trip.match_disagree_since_ms) // 1000),
            {"matchTripId": m_trip, "ridingTripId": r_trip,
             "vehicleId": match.get("vehicleId"),
             "confidence": match.get("confidence")})

    def _rule_distance_spike(self, trip, t, p):
        d = p.get("distanceFromRoute")
        if not isinstance(d, (int, float)):
            return
        prev = trip.prev_dist
        if (prev is not None and prev < DISTANCE_SPIKE_NEAR_M
                and d > DISTANCE_SPIKE_FAR_M):
            self._finding(
                trip, t, "distance-spike", "warn",
                "distanceFromRoute jumped %.0fm -> %.0fm in one tick" % (prev, d),
                {"prev": prev, "dist": d, "legIndex": p.get("legIndex")})
        trip.prev_dist = d

    def _on_set_riding(self, trip, t, p):
        self._clear_arrival(trip, t, "boarded a vehicle")
        new = {
            "tripId": p.get("tripId"),
            "vehicleId": p.get("vehicleId"),
            "routeId": p.get("routeId"),
            "headsign": p.get("headsign"),
            "legIndex": p.get("legIndex"),
            "boardedAt": p.get("boardedAt"),
            "swap_seq": trip.swap_seq,
            "setAtMs": t,
        }
        old = trip.riding
        if (old and old.get("tripId") and new["tripId"]
                and old["tripId"] != new["tripId"]
                and old.get("legIndex") == new["legIndex"]
                and old.get("swap_seq") == trip.swap_seq):
            self._finding(
                trip, t, "riding-flip", "page",
                "riding tripId flipped %s -> %s on leg %s" % (
                    old["tripId"], new["tripId"], new["legIndex"]),
                {"oldTripId": old["tripId"], "newTripId": new["tripId"],
                 "vehicleId": new["vehicleId"], "legIndex": new["legIndex"]},
                push_body="Trip id flipped %s to %s on the same leg. Board state suspect."
                          % (old["tripId"], new["tripId"]))
        trip.riding = new
        self._mark_dirty()

    def _on_transition_leg(self, trip, t, p):
        """Mirror the app's alight clear.

        The app never dispatches CLEAR_RIDING on alighting — TRANSITION_LEG's
        own reducer sets riding: null when the new leg is past the boarded one
        (reducers/go-mode.ts, "Advancing past the boarded transit leg means the
        rider alighted"). This daemon mirrors action TYPES, not reducers, so it
        held the riding fact for the whole ride: on 2026-08-02 it still thought
        the rider was aboard the Orange Line at 22:24, 53 minutes after they
        got off, which is what let ordinary bike-leg reroutes fire aboard-swap.

        An un-anchored fact (legIndex -1, the rider is aboard but we don't know
        which leg) is deliberately NOT cleared — same as the app, which asserts
        exactly that in __tests__/util/go-mode/riding.ts.
        """
        leg_index = p.get("legIndex")
        if not isinstance(leg_index, int):
            return
        riding = trip.riding
        if riding is None:
            return
        ridden_leg = riding.get("legIndex")
        if not isinstance(ridden_leg, int) or ridden_leg < 0:
            return
        if leg_index > ridden_leg:
            trip.riding = None
            self.log.info(
                "riding cleared on alight: session=%s leg %s -> %s"
                % (trip.session, ridden_leg, leg_index))
            self._mark_dirty()

    def _rule_aboard_swap(self, trip, t):
        if trip.riding is None:
            return
        # Being "aboard" has to mean aboard NOW. The sticky fact alone was the
        # bug: on 8/2 it was still set 53 minutes after the rider got off, so
        # three ordinary bike-leg deviation replans read as aboard-swaps. The
        # real fix is upstream — _on_transition_leg now clears the fact on
        # alight, exactly as the app does — and that alone removes all three.
        #
        # This is the remaining corroboration: the app must have SEEN the
        # rider's bus in the feed recently. A confirmed match keeps its
        # confidence long after its vehicle drops out (the app's own
        # VEHICLE_MATCH_FRESH_MS rule), so a fact with no recent sighting
        # behind it is not evidence the rider is aboard right now.
        #
        # Deliberately NOT also requiring a transit current leg, though the
        # backlog item asked for it. Measured against both recorded rides it
        # suppresses two GENUINE detections (7/29 17:28:48, 8/2 21:29:25) and
        # prevents no false positive — because a swap that lands the rider on
        # a walk leg while they are physically on a bus is the starkest form
        # of the very thing this rule exists to catch, not a reason to go quiet.
        match = trip.last_vehicle_match
        if not match or t - (match.get("tMs") or 0) > ABOARD_MATCH_FRESH_MS:
            return
        if t - trip.last_rider_action_ms <= RIDER_ACTION_WINDOW_MS:
            return  # rider explicitly picked a new itinerary
        route = trip.riding.get("headsign") or trip.riding.get("routeId") or "bus"
        self._finding(
            trip, t, "aboard-swap", "page",
            "itinerary replaced while aboard %s (trip %s), no rider action"
            % (route, trip.riding.get("tripId")),
            {"riding": {k: trip.riding.get(k) for k in
                        ("tripId", "vehicleId", "routeId", "legIndex")},
             "swapSeq": trip.swap_seq},
            push_body="Itinerary replaced while aboard %s. On-screen route may not match your bus." % route)

    def _on_notification(self, trip, t, p):
        nid = p.get("id") or ""
        if nid.startswith("MISSED_BUS") and trip.riding is not None:
            route = trip.riding.get("headsign") or trip.riding.get("routeId") or "bus"
            self._finding(
                trip, t, "missed-bus-while-riding", "page",
                "MISSED_BUS notification while riding %s is held" % route,
                {"notificationId": nid, "message": p.get("message"),
                 "riding": trip.riding.get("tripId")},
                push_body="Missed-bus alert while aboard %s. Ignore it." % route)
        if (nid.startswith("DESTINATION_UNREACHABLE")
                or p.get("type") == "DESTINATION_UNREACHABLE"):
            self._on_destination_unreachable(trip, t, p)
        self._rule_notification_repeat(trip, t, p)

    def _rule_notification_repeat(self, trip, t, p):
        """The same alert, over and over, at a rider who cannot make it stop.

        Keyed through notification_key() — the id minus its `Date.now()`
        suffix, plus the title — since 2026-08-31. It was keyed on
        `(title, message)` before that, which is byte-exact and so was beaten
        by the message drifting: 8/28's five "Off Route" pushes said 121m,
        121m, 124m, 120m, 124m and were counted as four separate alerts, none
        of which ever reached the threshold. The rule written for exactly this
        class of bug did not fire on either of the two deviation storms it was
        next asked about. The id stem was named as the better key in this
        docstring's previous version; it is now the key.

        On the 7/31 log the storm fires at 11:53:07 — the 2nd of 14 buzzes,
        seven minutes before the rider gave up and typed the complaint out on
        a bike.
        """
        key = notification_key(p)
        if key is None:
            return
        title = key[1] or (p.get("type") or "").strip()
        message = (p.get("message") or "").strip()
        window = trip.notification_times[key]
        window.append(t)
        while window and t - window[0] > NOTIFICATION_REPEAT_WINDOW_MS:
            window.popleft()
        if len(window) < NOTIFICATION_REPEAT_COUNT:
            return
        # Once per alert per ride. The finding says "ignore the buzzing"; a
        # second one five minutes later says nothing new and would spend the
        # rider's other interrupt on a thing they have already been told to
        # ignore. A different turn is a different key and can still fire.
        if key in trip.notification_repeat_last:
            return
        trip.notification_repeat_last[key] = t
        mins = max(1, int(round((t - window[0]) / 60000.0)))
        self._finding(
            trip, t, "notification-repeat", "page",
            "same notification %dx in %d min: %s" % (len(window), mins, title),
            {"title": title, "message": message, "count": len(window),
             "windowMs": t - window[0], "notificationId": p.get("id"),
             # The stable stem the count was actually accumulated under. The
             # message is one sample of a drifting family; this is the family.
             "key": key[0], "type": p.get("type")},
            push_body="Same alert %d times in %d min: %s. Ignore the buzzing."
                      % (len(window), mins, title[:50]))

    def _on_start_reroute(self, trip, t, p):
        if p.get("autoApply") is False:
            trip.last_rider_action_ms = t  # explicit reroute button
        trip.reroute_times.append(t)
        while trip.reroute_times and t - trip.reroute_times[0] > REROUTE_STORM_WINDOW_MS:
            trip.reroute_times.popleft()
        if (len(trip.reroute_times) > REROUTE_STORM_COUNT
                and t - trip.reroute_storm_last_ms > REROUTE_STORM_WINDOW_MS):
            trip.reroute_storm_last_ms = t
            self._finding(
                trip, t, "reroute-storm", "warn",
                "%d reroutes within 5 min" % len(trip.reroute_times),
                {"count": len(trip.reroute_times),
                 "reason": p.get("reason")})
        self._note_replan(trip, t, p.get("reason") or "reroute")

    # -- destination convergence -------------------------------------------
    #
    # reroute-storm above counts reroute EVENTS and nothing else: it cannot
    # tell "re-planning and converging" (a rider on a changing bus network)
    # from "re-planning in circles" (a destination the graph cannot reach).
    # These three methods add the missing half — the distance to the
    # destination — and mirror the client's own guard so the daemon fires only
    # where the client's failed to. See DEST_* above.

    def _note_destination_distance(self, trip, d):
        """Fold this tick's distance-to-destination in.

        Deliberately identical arithmetic to noteDestinationDistance() in
        lib/util/go-mode/destination-progress.ts, down to the fact that
        `dest_best_m` is the last COMMITTED best rather than the running
        minimum: a 20 m improvement does not move it, because 20 m is GPS
        scatter. The 8/28 afternoon's 427 m floor wandered by tens of metres
        for half an hour without the rider getting anywhere.
        """
        if not isinstance(d, (int, float)) or isinstance(d, bool):
            return
        if not math.isfinite(d):
            return
        if trip.dest_best_m is None or d <= trip.dest_best_m - DEST_GAIN_MIN_M:
            trip.dest_best_m = float(d)
            # A real gain clears everything, retirement included — whatever
            # changed, the rider is moving again and gets the machinery back.
            trip.dest_replans_since_gain = 0
            trip.dest_stall_fired = False

    def _note_replan(self, trip, t, why):
        """One re-plan happened. Count it, then ask whether they add up."""
        # No tick has produced a distance yet: "no net reduction" is not a fact
        # you can hold about a distance nobody has measured. Same guard as the
        # client's null-state check, and for the same reason — without it a
        # trip whose destination has no coordinates retires its own re-planning
        # after three attempts on no evidence at all.
        if trip.dest_best_m is None:
            return
        if t - trip.dest_last_replan_ms < DEST_REPLAN_COLLAPSE_MS:
            return
        trip.dest_last_replan_ms = t
        trip.dest_replans_since_gain += 1
        self._rule_replan_not_converging(trip, t, why)

    def _rule_replan_not_converging(self, trip, t, why):
        """Re-planning that is not getting the rider any closer, unannounced.

        8/28 afternoon: the destination sat inside the State Fairgrounds, where
        the street graph stops at the fence. Thirty-two minutes of re-planning
        into the venue interior, never inside 427 m, each plan promising an
        arrival it could not deliver, and the rider told nothing. reroute-storm
        watched the whole thing and had nothing to say, because it counts
        reroutes and never looks at whether they are working.

        The client now catches this itself and raises DESTINATION_UNREACHABLE.
        So this rule is deliberately the SECOND line: if that notification has
        reached the stream, the app is behaving correctly and the rider has
        already been told — spending one of two interrupts repeating it would
        make the daemon the noise. It fires only for the ride where the app's
        own guard failed or never ran, which is exactly the ride nobody is
        watching.
        """
        if trip.dest_stall_fired or trip.arrived_ms is not None:
            return
        if trip.dest_unreachable_ms is not None:
            return
        if trip.dest_replans_since_gain < DEST_STALL_REPLANS + DEST_CLIENT_GRACE_REPLANS:
            return
        trip.dest_stall_fired = True
        far = int(round(trip.dest_best_m))
        self._finding(
            trip, t, "replan-not-converging", "page",
            "%d re-plans with no %dm gain; still %dm from the destination"
            % (trip.dest_replans_since_gain, int(DEST_GAIN_MIN_M), far),
            {"replansSinceGain": trip.dest_replans_since_gain,
             "bestDistanceM": trip.dest_best_m,
             "gainMinM": DEST_GAIN_MIN_M,
             "lastReplanReason": why,
             # False is the whole reason this is a page: the app was supposed
             # to say this itself and did not.
             "appSaidUnreachable": False},
            push_body="Re-planning is not getting you closer — still %dm out "
                      "after %d tries. Finish from here your own way."
                      % (far, trip.dest_replans_since_gain))

    # -- the list view ------------------------------------------------------

    @staticmethod
    def _search_modes(query):
        """The mode set a search asked for, upper-cased.

        `mode` is the legacy comma string the app still persists
        ("WALK,TRANSIT"); `modes` is the newer array of {mode, qualifier}.
        Read both, because which one a build sends is not this daemon's
        business to know.
        """
        modes = set()
        if not isinstance(query, dict):
            return modes
        raw = query.get("mode")
        if isinstance(raw, str):
            modes.update(m.strip().upper() for m in raw.split(",") if m.strip())
        for m in (query.get("modes") or []):
            if isinstance(m, dict) and m.get("mode"):
                modes.add(str(m["mode"]).upper())
            elif isinstance(m, str):
                modes.add(m.upper())
        return modes

    def _note_search(self, trip, t, typ, payload):
        """Remember what a mid-ride search asked for, keyed by its search id.

        The response arrives as a separate record carrying only `searchId`, so
        without this the daemon can see a list of itineraries and have no idea
        what was requested — and "no bike egress" is only a defect if bike was
        asked for.
        """
        if not isinstance(payload, dict) or payload.get("__summary"):
            return
        sid = payload.get("id") or payload.get("searchId")
        query = payload.get("query") if isinstance(
            payload.get("query"), dict) else payload
        modes = self._search_modes(query)
        if not sid or not modes:
            return
        trip.searches[sid] = {"modes": sorted(modes), "tMs": t}
        # Bounded: a rider re-planning hard produces a few dozen per ride.
        while len(trip.searches) > 64:
            trip.searches.popitem(last=False)

    @staticmethod
    def _response_itineraries(payload):
        """Itineraries out of a ROUTING_RESPONSE, whichever shape it is in.

        Returns None — "could not look" — rather than [] when the payload was
        stubbed by the recorder's size cap, which is a different fact from
        "the search returned nothing".
        """
        if not isinstance(payload, dict) or payload.get("__summary"):
            return None
        node = payload.get("response", payload)
        for path in (("plan", "itineraries"),
                     ("data", "plan", "itineraries"),
                     ("itineraries",)):
            cur = node
            for key in path:
                cur = cur.get(key) if isinstance(cur, dict) else None
                if cur is None:
                    break
            if isinstance(cur, list):
                return cur
        return None

    def _rule_bike_egress_missing(self, trip, t, payload):
        """A bike+transit search whose results all end on foot.

        Rider-caught on the bus on 2026-08-31: "search from here never shows
        bike egress and ride to destination". Bike egress is the last leg of
        the itinerary — the rider gets off the bus and rides the bike they are
        carrying — so its absence from every result of a search that asked for
        BICYCLE is the exact shape of the complaint.

        Only fires on a search that asked for both BICYCLE and TRANSIT, and
        only when at least one returned itinerary actually uses transit: an
        all-walking fallback list is a different (and honest) answer.

        Note for whoever reads this next: as of 2026-09-01 every recorded
        ROUTING_RESPONSE payload is stubbed by the recorder's size cap
        (`{"__summary": true, "chars": 461450}`), so this rule cannot fire on
        any telemetry recorded to date. It goes live with the payload-ladder
        deploy (backlog 2.1), not with this commit.
        """
        itineraries = self._response_itineraries(payload)
        sid = payload.get("searchId") if isinstance(payload, dict) else None
        search = trip.searches.get(sid) if sid else None
        if search is None and len(trip.searches) == 1:
            # One search in flight: the response is unambiguously its.
            sid, search = next(iter(trip.searches.items()))
        if not search:
            return
        modes = set(search.get("modes") or [])
        if "BICYCLE" not in modes or not (modes & TRANSIT_MODES):
            return
        if itineraries is None:
            self.log.info(
                "bike+transit search %s: response payload was summarized away,"
                " cannot check for bike egress (backlog 2.1)" % sid)
            return
        if sid in trip.bike_egress_fired:
            return
        transit_itins = [it for it in itineraries
                         if isinstance(it, dict)
                         and any(leg_is_transit(l) for l in (it.get("legs") or []))]
        if not transit_itins:
            return
        with_bike_egress = 0
        for it in transit_itins:
            legs = [l for l in (it.get("legs") or []) if isinstance(l, dict)]
            if legs and (legs[-1].get("mode") or "").upper() == "BICYCLE":
                with_bike_egress += 1
        if with_bike_egress:
            return
        trip.bike_egress_fired.add(sid)
        self._finding(
            trip, t, "bike-egress-missing", "warn",
            "bike+transit search returned %d transit option(s) and not one of"
            " them ends on the bike" % len(transit_itins),
            {"searchId": sid, "modes": sorted(modes),
             "itineraries": len(itineraries),
             "transitItineraries": len(transit_itins),
             "lastLegModes": sorted(set(
                 ((it.get("legs") or [{}])[-1].get("mode") or "?")
                 for it in transit_itins))})

    def _on_destination_unreachable(self, trip, t, p):
        """The app worked out for itself that it cannot get there.

        Recorded, not paged: the rider has a high-priority push about it on
        their phone already. It goes in the ledger so the wrap-up can tell
        "the graph could not reach the destination" apart from "the daemon's
        convergence rule fired", and it latches the daemon's own rule off.
        """
        if trip.dest_unreachable_ms is not None:
            return
        trip.dest_unreachable_ms = t
        self._finding(
            trip, t, "destination-unreachable", "info",
            "app gave up re-planning to the destination: %s"
            % one_line(p.get("message") or "", 120),
            {"notificationId": p.get("id"),
             "replansSinceGain": trip.dest_replans_since_gain,
             "bestDistanceM": trip.dest_best_m})

    def _rule_console(self, trip, t, obj):
        if obj.get("level") != "error":
            return
        args = obj.get("args") or []
        try:
            msg = args[0] if args else ""
            if isinstance(msg, dict):
                msg = msg.get("message") or json.dumps(msg, sort_keys=True)
            msg = str(msg)[:300]
        except Exception:
            msg = "<unprintable>"
        if msg in trip.console_seen:
            return
        trip.console_seen.add(msg)
        if any(ignore in msg for ignore in CONSOLE_ERROR_IGNORE):
            # Known-inert and re-confirmed each ride; see CONSOLE_ERROR_IGNORE.
            # Logged, not filed: the daemon log still shows it happened.
            self.log.info("console.error suppressed (known inert): %s"
                          % msg[:120])
            return
        self._finding(trip, t, "console-error", "info",
                      "console.error: %s" % msg[:120], {"message": msg})

    # -- rider notes --------------------------------------------------------

    def _trip_context(self, trip):
        """What the trip looked like at this instant.

        A note says "it just told me the wrong stop" — worthless a week later
        unless we also recorded which leg, how far along, and whether the app
        thought the rider was aboard. The post-ride report correlates the two.
        """
        p = trip.progress or {}
        ctx = {
            "legIndex": p.get("currentLegIndex"),
            # Named for its unit on purpose: this is a percentage on 0-100, and
            # the unitless key it replaced is what made a reply agent tell the
            # rider "31% along" when 0.3077 meant 0.31% (see fmt_pct).
            "legProgressPct": p.get("currentLegProgress"),
            "status": p.get("status"),
            "stopsRemaining": p.get("stopsRemaining"),
            "nextStopName": p.get("nextStopName"),
            "onTransitLeg": trip.current_leg_transit(),
            "swapSeq": trip.swap_seq,
            "secondsSinceFix": max(0, (self.now_ms() - trip.last_pos_ms) // 1000),
        }
        if trip.riding:
            ctx["riding"] = {k: trip.riding.get(k) for k in
                             ("tripId", "vehicleId", "routeId", "headsign",
                              "legIndex")}
        else:
            ctx["riding"] = None
        return ctx

    def _on_rider_note(self, session, t, obj, trip):
        """Ingest a note the rider typed on the /ride console.

        Notes are the rider's own words about the ride, so they are the most
        valuable input the post-ride report gets — but they are an observation,
        never an alarm. They land at `info` severity and carry no push body, so
        they can never consume one of the rider's two interrupts. Paging
        someone about the note they just wrote would be absurd.
        """
        text = obj.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        text = text.strip()[:RIDER_NOTE_MAX_CHARS]
        if trip is None:
            # The sidecar guesses the session from the log tail and can miss.
            # If exactly one trip is running, the note is plainly about it —
            # that is the timestamp correlation the note stream exists for.
            # _active_trips(), not .values(): a ride the app re-mounted holds
            # two session keys, and counting those as two rides would drop the
            # note the rider just typed.
            active = self._active_trips()
            if len(active) == 1:
                trip = active[0]
        if trip is None:
            # No trip, no thread to hand it to. It is still logged: the rider
            # can open any Claude session and it is in the daemon log, and if a
            # ride starts in the next few seconds the next note lands properly.
            self.log.info("rider note outside any trip (session=%s): %s"
                          % (session, text))
            return
        trip.last_event_ms = max(trip.last_event_ms, t)
        source = obj.get("source") or "console"
        context = self._trip_context(trip)
        context["text"] = text
        context["source"] = source
        note = {"tsMs": int(t), "time": fmt_hms(t), "text": text,
                "source": source, "context": context}
        trip.notes.append(note)
        # The finding is what reaches the ride thread (see _finding), so the
        # note is answered in the conversation the rider is already reading —
        # UNLESS the thread is where it came from. A thread-recorded note is
        # already in that conversation; pushing it back would echo the rider's
        # own words at them. It still lands in the ledger, the digest and the
        # report request, which is the whole point of recording it.
        self._finding(trip, t, "rider-note", "info",
                      "rider note: %s" % text[:160], context,
                      thread_push=(source != "ride-thread"))

    # -- findings, paging, surfaces ----------------------------------------

    def _finding(self, trip, ts_ms, rule, severity, summary, context,
                 push_body=None, thread_push=True):
        finding = {
            "tsMs": int(ts_ms),
            "time": fmt_hms(ts_ms),
            "session": trip.session,
            "rule": rule,
            "severity": severity,
            "summary": summary,
            "context": context,
        }
        trip.findings.append(finding)
        self.all_findings.append(finding)
        self.log.info("FINDING [%s/%s] %s %s" % (severity, rule, fmt_hms(ts_ms), summary))
        # Persist only once the paging decision is known, so the record always
        # says whether the rider was told. For a page that means after the
        # coalescing window closes (at most PAGE_COALESCE_MS later).
        if severity == "page" and push_body:
            finding["paged"] = "pending"
            self._buffer_page(trip, ts_ms, rule, push_body, finding)
        else:
            if severity == "page":
                finding["paged"] = False
            self._persist_finding(trip, finding)
        # Every finding is a milestone, including a rider note (which arrives
        # as rule "rider-note"): the note IS the ping that gets it answered,
        # which is why there is no separate note trigger.
        line = (summary if rule == "rider-note"
                else "finding [%s] %s: %s" % (severity, rule, summary))
        self._thread_event(trip, ts_ms, line)
        if thread_push:
            self._thread_push(trip, line)
        self._mark_dirty()

    def _persist_finding(self, trip, finding):
        try:
            with open(self._findings_path(trip), "a") as f:
                f.write(json.dumps(finding) + "\n")
        except OSError as exc:
            self.log.error("could not append finding: %r" % exc)

    def _findings_path(self, trip):
        return os.path.join(
            self.watch_dir,
            "%s-%s.findings.jsonl" % (fmt_date(trip.start_ms), trip.session))

    def _buffer_page(self, trip, ts_ms, rule, body, finding):
        """Hold a page for the coalescing window instead of sending it now."""
        # Close an already-expired window first: the window belongs to the
        # page that opened it, so a late arrival starts a fresh one rather
        # than being judged against a decision that should already be out.
        self._flush_pages(trip, self.now_ms())
        if not trip.pending_pages:
            trip.pending_until_ms = self.now_ms() + PAGE_COALESCE_MS
        trip.pending_pages.append({
            "tsMs": int(ts_ms), "rule": rule, "body": body,
            "rank": PAGE_RANK.get(rule, PAGE_RANK_DEFAULT), "finding": finding,
        })
        self.log.info("page buffered (%s, rank %d, window closes %s): %s" % (
            rule, PAGE_RANK.get(rule, PAGE_RANK_DEFAULT),
            fmt_hms(trip.pending_until_ms), body))

    def _flush_pages(self, trip, now, force=False):
        """Send the most actionable buffered page; drop the rest.

        Ties go to the earlier finding — if two pages are equally actionable,
        the one that fired first described the problem first.
        """
        if not trip.pending_pages:
            return
        if not force and now < trip.pending_until_ms:
            return
        pending = trip.pending_pages
        trip.pending_pages = []
        trip.pending_until_ms = None
        winner = min(pending, key=lambda e: (-e["rank"], e["tsMs"]))
        for entry in pending:
            if entry is winner:
                continue
            self.log.info("page dropped (superseded by %s): %s" % (
                winner["rule"], entry["body"]))
            entry["finding"]["paged"] = False
            entry["finding"]["supersededBy"] = winner["rule"]
            self.push_log.append({
                "tsMs": int(now), "title": "Ride watch", "body": entry["body"],
                "sent": False, "kind": "page",
                "suppressed": "superseded-by-%s" % winner["rule"]})
            self._persist_finding(trip, entry["finding"])
        winner["finding"]["paged"] = self._page(trip, winner["body"])
        self._persist_finding(trip, winner["finding"])
        self._mark_dirty()

    def _page(self, trip, body):
        """Send a page if the trip's budget and the rate limit allow it."""
        if trip.pages_sent >= MAX_PAGES_PER_TRIP:
            self.log.info("page suppressed (cap %d/trip): %s" % (MAX_PAGES_PER_TRIP, body))
            return False
        if self._send_push("Ride watch", body, kind="page"):
            trip.pages_sent += 1
            return True
        return False

    def _send_push(self, title, body, kind="page"):
        """Send a Pushover message. Returns True if sent (or dry-run-logged).

        Global 120s rate limit applies to every send, including the
        trip-end fallback (which is exempt only from the per-trip cap).
        """
        now = self.now_ms()
        if self.last_push_ms and now - self.last_push_ms < PUSH_MIN_INTERVAL_MS:
            self.log.info("push suppressed (rate limit): %s" % body)
            self.push_log.append({"tsMs": now, "title": title, "body": body,
                                  "sent": False, "kind": kind,
                                  "suppressed": "rate-limit"})
            return False
        self.last_push_ms = now
        entry = {"tsMs": now, "title": title, "body": body, "sent": False,
                 "kind": kind}
        self.push_log.append(entry)
        if self.dry_run:
            self.log.info("DRY-RUN push: [%s] %s" % (title, body))
            entry["sent"] = "dry-run"
            return True
        ok = self._post_pushover(title, body)
        entry["sent"] = ok
        return ok

    def _post_pushover(self, title, body):
        try:
            user_key, api_token = read_pushover_creds(PUSHOVER_CREDS)
        except (OSError, ValueError) as exc:
            self.log.error("pushover creds unreadable: %r" % exc)
            return False
        data = urllib.parse.urlencode({
            "token": api_token, "user": user_key,
            "title": title, "message": body,
        }).encode()
        try:
            req = urllib.request.Request(
                "https://api.pushover.net/1/messages.json", data=data)
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = resp.status == 200
            self.log.info("pushover sent (%s): %s" % (ok, body))
            return ok
        except Exception as exc:
            self.log.error("pushover send failed: %r" % exc)
            return False

    # -- post-ride report ---------------------------------------------------
    #
    # The daemon no longer runs the report itself. It writes the request file
    # and pings the ride thread, which has watched the whole ride and writes
    # the vault report from held context (see ride-thread-sysprompt.md). The
    # request file is unchanged — it is now an input to a conversation instead
    # of to a headless `claude -p`.

    def _ride_slug(self, trip):
        """`<date>-<session-short>`, the stem every wrap-up artifact hangs off."""
        return "%s-%s" % (fmt_date(trip.start_ms),
                          trip.session.rsplit("-", 1)[-1])

    def _report_path(self, trip):
        """Vault path for this ride's report — keyed on the ride, not the session.

        The phone keeps one session id for as long as the app stays loaded, so
        every trip taken in an evening shares it. The wrap-up path used to be
        derived from that id alone, which meant ride 2 resolved to the file
        ride 1's report was already in and overwrote it. On 2026-08-28 the
        rider took two Orange Line trips on session mtdh67f3-0z5p24 an hour
        apart; on 2026-08-27 the same thing happened and only survived because
        the thread noticed the collision by hand and invented `-ride2`.

        So the daemon picks the name — it is the only party that can see the
        earlier ride's file — and the suffix is the convention that was already
        being improvised. Existing single-ride reports keep their names.
        """
        base = os.path.join(self.report_dir, self._ride_slug(trip))
        if not os.path.exists(base + ".md"):
            return base + ".md"
        n = 2
        while os.path.exists("%s-ride%d.md" % (base, n)):
            n += 1
        return "%s-ride%d.md" % (base, n)

    def _report_request_path(self, trip):
        """One request file per ride, not per session.

        Same reason as _report_path: two rides on one session id used to write
        the same request file, so the second ride destroyed the first ride's
        inputs before anyone had read them. The start time is the ride's only
        stable identity; the session id stays in the name so a grep by session
        still finds every ride the app took under it.
        """
        return os.path.join(
            self.watch_dir, "report-request-%s-%s.json"
            % (trip.session, datetime.datetime.fromtimestamp(
                trip.start_ms / 1000).strftime("%H%M")))

    def _write_report_request(self, trip):
        report_path = self._report_path(trip)
        trip.report_path = report_path
        req = {
            "session": trip.session,
            # Usually [session]. More than one means the app re-mounted
            # mid-ride and the later ids are the same ride (_adopt_continuation).
            "sessions": list(trip.sessions),
            "date": fmt_date(trip.start_ms),
            "startMs": trip.start_ms,
            "endMs": trip.end_ms,
            "findingsPath": self._findings_path(trip),
            # Where to write the wrap-up. Use it verbatim: deriving a path from
            # `session` collides with an earlier ride on the same session id.
            # Held on the trip as well so the report deadline watches for the
            # same file the thread was asked to write, not a second guess at it.
            "reportPath": report_path,
            # findingsPath is a per-DAY, per-session ledger and can hold an
            # earlier ride's findings too. Only records at or after this
            # timestamp belong to this ride; findingsCount counts only those.
            "findingsFrom": trip.start_ms,
            "itinerarySummary": trip.itinerary or {"unavailable": True},
            "findingsCount": len(trip.findings),
            # The rider's notes are in findingsPath too (rule "rider-note"),
            # but the count is surfaced here so the report agent knows up front
            # whether this ride has the rider's own account of it.
            "notesCount": len(trip.notes),
            "riderNotes": trip.notes,
            "pagesSent": trip.pages_sent,
            "endReason": trip.end_reason,
        }
        path = self._report_request_path(trip)
        with open(path, "w") as f:
            json.dump(req, f, indent=2)
        self.log.info("report request written: %s" % path)
        return path

    def _report_fallback_push(self, findings_n):
        # Takes a count, not a Trip: the report-deadline path fires long after
        # _end_trip dropped the Trip object, and may fire in a process that
        # never saw the ride at all (state.json survives a restart).
        self._send_push(
            "Ride watch",
            "Ride ended — %d findings. Report pending; open Claude and say 'ride report'."
            % findings_n,
            kind="fallback")

    # -- the ride thread ----------------------------------------------------
    #
    # Lifecycle: spawn one tmux session per ride running ride-thread-run.sh
    # (which execs `claude --remote-control`), wait for the TUI, then type one
    # line per milestone into it. The thread reads the digest file for detail
    # and answers the rider in the same conversation.
    #
    # Everything here is best-effort by design. tmux missing, pane dead, rider
    # typed /exit, `claude` broken: each is logged and the ride carries on. The
    # rule engine and its pages do not depend on any of it.

    def _thread_name(self, trip):
        """`ride-1852`, unless that name is already somebody's.

        The name is the clock minute, and on 2026-08-31 two rides landed in
        the same one: the app re-mounted at 18:52:55, 41 s after 18:52:14, and
        both trips resolved to `ride-1852`. The second `tmux new-session`
        failed with "duplicate session: ride-1852" and set
        `_thread_status["ride-1852"] = False` — which is keyed by PANE, not by
        trip, so it condemned the FIRST ride's live, ready pane as well. From
        then on `_thread_missing` was true for both trips, so when each ended
        the daemon sent the "report pending" fallback page instead of asking
        the pane — which was still sitting there — to write the wrap-up. Two
        pages, no wrap-up, one healthy console.

        A suffix rather than a longer name: `ride-1852b` still reads as "the
        18:52 ride" in the rider's app list, which is the whole point of the
        name.
        """
        base = "%s-%s" % (THREAD_NAME_PREFIX, datetime.datetime.fromtimestamp(
            trip.start_ms / 1000).strftime("%H%M"))
        taken = self._live_thread_names() | self._panes_awaiting_wrap_up()
        taken.update(r.get("tmux") for r in self.thread_reaps)
        if base not in taken:
            return base
        for suffix in "bcdefghijklmnopqrstuvwxyz":
            if base + suffix not in taken:
                return base + suffix
        return base

    def _thread_display(self, trip, name=None):
        """What the rider sees in their Claude app list."""
        stamp = datetime.datetime.fromtimestamp(
            trip.start_ms / 1000).strftime("%m-%d %H:%M")
        # Carry the disambiguating suffix through, so the pane the daemon
        # types into and the conversation the rider opens are the same ride.
        suffix = ""
        if name and "-" in name:
            tail = name.rsplit("-", 1)[-1]
            if len(tail) > 4:
                suffix = tail[4:]
        return "%s %s%s" % (THREAD_NAME_PREFIX, stamp, suffix)

    def _begin_ride_thread(self, trip, t):
        """Spawn the ride's thread and send the kickoff line."""
        self._thread_event(trip, t, "trip started%s — %s" % (
            " (adopted mid-stream)" if trip.adopted else "",
            itinerary_one_liner(trip.itinerary)))
        if not self.thread_enabled:
            self.log.info("ride thread disabled (RIDE_THREAD_ENABLED=0)")
            return
        name = self._thread_name(trip)
        display = self._thread_display(trip, name)
        spawn = self.spawn_thread
        if spawn is None:
            if self.replay:
                self.log.info("replay: not spawning a ride thread")
                return
            spawn = self._tmux_spawn
        trip.thread = {"tmux": name, "display": display,
                       "spawnedMs": self.now_ms(), "ok": None}
        try:
            # None = pending: the real spawner hands the ~10s of TUI startup to
            # the worker thread and answers later.
            trip.thread["ok"] = spawn(name, display)
        except Exception as exc:
            self.log.error("ride thread spawn failed: %r" % exc)
            trip.thread["ok"] = False
            return
        self.log.info("ride thread %s (%s) spawned for session %s"
                      % (name, display, trip.session))
        self._thread_push(trip, "trip started %s%s — %s" % (
            fmt_hms(trip.start_ms), " (adopted)" if trip.adopted else "",
            itinerary_one_liner(trip.itinerary)))

    def _thread_ok(self, trip):
        """Usable? Pending counts as usable — pushes queue behind the spawn."""
        th = trip.thread
        if th is None:
            return False
        status = self._thread_status.get(th["tmux"])
        if status is not None:
            th["ok"] = status
        return th.get("ok") is not False

    def _thread_missing(self, trip):
        """True when this ride has no working thread to hold its wrap-up.

        A replay never promises one, so it never falls back to a page; a real
        ride whose spawn failed does.
        """
        if trip.thread is None:
            return not self.replay
        self._thread_ok(trip)
        return trip.thread.get("ok") is not True

    def _thread_event(self, trip, ts_ms, text):
        """Record a milestone for the digest's "new since last push" section."""
        trip.thread_events.append("%s %s" % (fmt_hms(ts_ms), one_line(text)))
        if len(trip.thread_events) > THREAD_MAX_EVENTS:
            drop = len(trip.thread_events) - THREAD_MAX_EVENTS
            del trip.thread_events[:drop]
            trip.thread_cursor = max(0, trip.thread_cursor - drop)

    def _thread_push(self, trip, line):
        """Rewrite the digest, then type one line into the thread."""
        if not self._thread_ok(trip):
            return False
        try:
            digest = self._write_digest(trip)
        except OSError as exc:
            self.log.error("digest write failed: %r" % exc)
            digest = self._digest_path(trip)
        # Detail lives in the file; the line says only what changed.
        text = one_line("[ride-watch] %s — digest: %s" % (line, digest))
        trip.thread_cursor = len(trip.thread_events)
        trip.last_thread_push_ms = self.now_ms()
        trip.thread_pushes += 1
        push = self.push_line
        if push is None:
            if self.replay:
                return False
            push = self._tmux_push
        try:
            push(trip.thread["tmux"], text)
        except Exception as exc:
            self.log.error("ride thread push failed: %r" % exc)
            return False
        self.log.info("ride thread push: %s" % text)
        self._mark_dirty()
        return True

    def _maybe_heartbeat(self, trip, now):
        """One line every 10 minutes of silence, but only while still moving.

        A rider stuck at a stop for 20 minutes does not need to be told that
        nothing is happening; a 40-minute Orange Line leg with no findings
        should still show the thread is alive and following.
        """
        if trip.thread is None or not trip.last_thread_push_ms:
            return
        if now - trip.last_thread_push_ms < THREAD_HEARTBEAT_MS:
            return
        if now - trip.last_pos_ms > THREAD_MOVING_MS:
            return
        self._thread_push(trip, "still riding — %s" % self._short_state(trip))

    def _leg_label(self, trip, idx):
        legs = (trip.itinerary or {}).get("legs") or []
        if not isinstance(idx, int) or not (0 <= idx < len(legs)):
            return "leg %s" % idx
        leg = legs[idx]
        if leg.get("transit"):
            return " ".join(str(x) for x in
                            (leg.get("mode"), leg.get("route"),
                             leg.get("headsign")) if x)
        return leg.get("mode") or "leg %s" % idx

    def _short_state(self, trip):
        p = trip.progress
        if not p:
            return "no progress yet"
        out = "leg %s at %s, %s" % (
            p.get("currentLegIndex"), fmt_pct(p.get("currentLegProgress")),
            p.get("status"))
        if p.get("stopsRemaining") is not None:
            out += ", %s stops left" % p["stopsRemaining"]
        return out

    # -- the digest ---------------------------------------------------------

    def _repo_head_now(self):
        """(head, commits_behind) as of a few minutes ago. Never the stamp.

        This is the OTHER half of the version question and must never be
        confused with DAEMON_GIT_SHA: that constant says what is running, this
        says what is on disk, and the whole value of the pair is that they can
        disagree. Note that this is the exact opposite of what a build script
        wants — a build proves the tree did not move under it and fails if it
        did. A daemon fully expects the tree to move underneath it; the drift
        IS the signal. So this never aborts, never re-execs, never restarts
        anything. It reports.

        Cached for HEAD_RECHECK_MS so neither the digest nor the status file
        shells out to git on a hot path, and read-only (`rev-parse` and
        `rev-list` take no index lock) because other agents commit in this same
        worktree.
        """
        now = int(time.time() * 1000)
        if (self._head_checked_ms
                and now - self._head_checked_ms < HEAD_RECHECK_MS):
            return self._head_cached
        self._head_checked_ms = now
        head = _git_out(["rev-parse", "--short", "HEAD"]) or None
        behind = None
        base = DAEMON_GIT_SHA.split("-", 1)[0]
        if head and base != "unknown" and head != base:
            # How far behind, in commits. "five days stale" was the thing
            # nobody could see on 8/28; a number makes it unignorable.
            count = _git_out(["rev-list", "--count", "%s..HEAD" % base])
            if count and count.isdigit():
                behind = int(count)
        self._head_cached = (head, behind)
        return self._head_cached

    def _daemon_lines(self):
        """Who is running, in two lines, at the top of everything a human reads.

        On 2026-08-28 a daemon five days stale produced five false
        stalled-progress findings, missed the arrival event, and nearly
        overwrote a report — and no artifact it wrote said which version it
        was. The ride thread sat reading source on disk that the process in
        memory had never loaded.
        """
        started = datetime.datetime.fromtimestamp(
            DAEMON_STARTED_MS / 1000).strftime("%Y-%m-%d %H:%M:%S")
        line = "Daemon: %s @ %s (started %s" % (
            os.path.basename(os.path.abspath(__file__)), DAEMON_GIT_SHA, started)
        if DAEMON_SOURCE_MTIME:
            line += ", source mtime %s" % datetime.datetime.fromtimestamp(
                DAEMON_SOURCE_MTIME).strftime("%Y-%m-%d %H:%M:%S")
        line += ")"
        out = [line]
        head, behind = self._repo_head_now()
        base = DAEMON_GIT_SHA.split("-", 1)[0]
        if head and base != "unknown" and head != base:
            out.append(
                "Tree now: %s  ** STALE — this daemon is running %s%s."
                " Findings may come from code that no longer exists. Restart:"
                " `systemctl --user restart ride-watch` **"
                % (head, base,
                   ", %d commit(s) behind" % behind if behind else ""))
        if self.duplicate_records:
            out.append("Duplicate (re-POSTed) records dropped: %d"
                       % self.duplicate_records)
        return out

    def _digest_path(self, trip):
        return os.path.join(self.watch_dir, "%s.digest.md" % trip.session)

    def _write_digest(self, trip):
        """The whole ride so far, rewritten before every push.

        The ping is one line because the digest is the message: state now,
        what changed since the thread last looked, every finding, every note.
        A thread that reads this file is never behind, even if a push was lost.
        """
        now = self.now_ms()
        L = ["# Ride digest — session %s" % trip.session, "",
             "Written: %s" % datetime.datetime.now().strftime(
                 "%Y-%m-%d %H:%M:%S")]
        L.extend(self._daemon_lines())
        L.extend(["Pushes so far: %d" % trip.thread_pushes, ""])
        L.append("## Trip")
        L.append("")
        L.extend(self._trip_state_lines(trip, now))
        L.append("- Progress units: currentLegProgress is a percentage on"
                 " 0-100; UPDATE_ROUTE_MATCH.progressAlongLeg is the same"
                 " value as a 0-1 fraction. Do not confuse them.")
        if trip.end_ms:
            L.append("- **Ended: %s (%s)**" % (fmt_hms(trip.end_ms),
                                               trip.end_reason))
        L.append("")
        L.append("## Where the evidence is")
        L.append("")
        L.append("- Raw telemetry: %s (filter on session `%s`)"
                 % (current_log_path(), trip.session))
        L.append("- Findings: %s" % self._findings_path(trip))
        L.append("- Live status: %s"
                 % os.path.join(self.watch_dir, "current-ride.md"))
        req = self._report_request_path(trip)
        if os.path.exists(req):
            L.append("- Report request (wrap-up): %s" % req)
        L.append("")
        new = trip.thread_events[trip.thread_cursor:]
        L.append("## New since the last push (%d)" % len(new))
        L.append("")
        L.extend(["- %s" % e for e in new] or ["- (nothing)"])
        L.append("")
        L.append("## Findings (%d)" % len(trip.findings))
        L.append("")
        L.extend(["- %s [%s] %s: %s" % (f["time"], f["severity"], f["rule"],
                                        one_line(f["summary"]))
                  for f in trip.findings[-THREAD_MAX_EVENTS:]]
                 or ["- (none)"])
        L.append("")
        L.append("## Rider notes (%d)" % len(trip.notes))
        L.append("")
        for note in trip.notes[-THREAD_MAX_EVENTS:]:
            c = note["context"] or {}
            L.append("- %s — %s  _(at %s of leg %s, %s, %s stops left)_" % (
                note["time"], one_line(note["text"]),
                fmt_pct(c.get("legProgressPct")), c.get("legIndex"),
                c.get("status"), c.get("stopsRemaining")))
        if not trip.notes:
            L.append("- (none)")
        L.append("")
        path = self._digest_path(trip)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(L) + "\n")
        os.replace(tmp, path)
        return path

    # -- tmux (the real spawner and pusher) ---------------------------------

    def _tmux(self, args, timeout=20):
        return subprocess.run(
            ["tmux"] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=timeout,
            universal_newlines=True)

    def _tmux_spawn(self, name, display):
        """Queue the real spawn. Returns None — "ask again later".

        The TUI takes ~10s to come up and the tailer must not stop reading
        telemetry for it, so the waiting happens on the worker thread.
        """
        self._thread_enqueue(("spawn", name, display))
        return None

    def _tmux_push(self, name, line):
        self._thread_enqueue(("push", name, line))
        return True

    def _tmux_kill(self, name):
        """Queue the retirement. Same reason as the spawn: the tailer must not
        wait on tmux, and a kill must not overtake the last push into the pane
        it is killing."""
        self._thread_enqueue(("kill", name, None))
        return True

    def _thread_enqueue(self, job):
        with self._thread_lock:
            self._thread_jobs.append(job)
            if self._thread_worker is None or not self._thread_worker.is_alive():
                self._thread_worker = threading.Thread(
                    target=self._thread_worker_loop, daemon=True)
                self._thread_worker.start()
            self._thread_wake.set()

    def _thread_worker_loop(self):
        """Serialize tmux work. Order matters: pushes must not overtake the
        spawn they belong to, and two send-keys must not interleave."""
        while True:
            self._thread_wake.wait(1.0)
            while True:
                with self._thread_lock:
                    job = self._thread_jobs.pop(0) if self._thread_jobs else None
                    if job is None:
                        self._thread_wake.clear()
                        break
                try:
                    if job[0] == "spawn":
                        self._tmux_spawn_blocking(job[1], job[2])
                    elif job[0] == "kill":
                        self._tmux_kill_blocking(job[1])
                    else:
                        self._tmux_push_blocking(job[1], job[2])
                except Exception as exc:
                    self.log.error("ride thread worker job %s failed: %r"
                                   % (job[0], exc))

    def _tmux_spawn_blocking(self, name, display):
        self._kill_previous_threads(keep=name)
        cmd = "%s %s" % (shlex.quote(THREAD_RUNNER), shlex.quote(display))
        res = self._tmux(["new-session", "-d", "-s", name,
                          "-x", str(THREAD_TMUX_SIZE[0]),
                          "-y", str(THREAD_TMUX_SIZE[1]),
                          "-c", REPO_DIR, cmd])
        if res.returncode != 0:
            self.log.error("tmux new-session failed (%d): %s"
                           % (res.returncode, one_line(res.stdout)))
            self._thread_status[name] = False
            return
        deadline = time.time() + THREAD_READY_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(THREAD_READY_POLL_S)
            pane = self._tmux(["capture-pane", "-p", "-t", name])
            if pane.returncode != 0:
                continue
            if THREAD_READY_MARKER in (pane.stdout or ""):
                self._thread_status[name] = True
                self.log.info("ride thread %s ready in %.0fs" % (
                    name, THREAD_READY_TIMEOUT_S - (deadline - time.time())))
                return
        # No prompt yet. If the pane is still alive the keystrokes will buffer
        # in the tty and be read when it comes up, so this is a warning rather
        # than a dead thread; only a vanished session is a failure.
        alive = self._tmux(["has-session", "-t", name]).returncode == 0
        self._thread_status[name] = alive
        self.log.warn("ride thread %s not ready after %ds (session alive=%s)"
                      % (name, THREAD_READY_TIMEOUT_S, alive))

    def _tmux_push_blocking(self, name, line):
        # -l types the line literally: a note containing `;` or `C-c` must
        # never be interpreted as a tmux key name.
        res = self._tmux(["send-keys", "-t", name, "-l", line])
        if res.returncode != 0:
            self.log.error("send-keys failed for %s (%s); thread considered gone"
                           % (name, one_line(res.stdout)))
            self._thread_status[name] = False
            return
        time.sleep(THREAD_SUBMIT_DELAY_S)
        res = self._tmux(["send-keys", "-t", name, "Enter"])
        if res.returncode != 0:
            self.log.error("submit failed for %s (%s)"
                           % (name, one_line(res.stdout)))
            self._thread_status[name] = False

    def _tmux_kill_blocking(self, name):
        res = self._tmux(["kill-session", "-t", name])
        if res.returncode != 0:
            # Already gone (the rider typed /exit, or tmux is not running) is
            # the ordinary case and not an error: the pane is closed either
            # way, which is all this was for.
            self.log.info("ride thread %s was already gone (%s)"
                          % (name, one_line(res.stdout)))
        self._thread_status[name] = False

    def _panes_awaiting_wrap_up(self):
        """tmux panes that were asked for a wrap-up and have not delivered.

        Emptied by _check_report_deadlines the moment the report lands or the
        deadline expires, so a pane is protected for at most REPORT_DEADLINE_MS
        and a dead one cannot pin the namespace forever.
        """
        return set(e.get("tmux") for e in self.report_deadlines
                   if e.get("tmux"))

    def _live_thread_names(self):
        """Panes belonging to a trip that is still running."""
        return set(name for name in
                   ((tr.thread or {}).get("tmux") for tr in self._active_trips())
                   if name)

    def _schedule_thread_reap(self, name, now, why):
        """This pane's ride is over and it owes nothing. Retire it shortly.

        Shortly, not now: THREAD_REAP_GRACE_MS. And never a pane that some
        live trip is still using as its console — two rides can land on the
        same clock-minute name, and reaping the wrong one would take the
        rider's live console away mid-ride.
        """
        if not name:
            return
        if name in self._live_thread_names():
            return
        if any(r.get("tmux") == name for r in self.thread_reaps):
            return
        due = int(now) + THREAD_REAP_GRACE_MS
        self.thread_reaps.append({"tmux": name, "atMs": due, "why": why})
        self.log.info("ride thread %s retires at %s (%s)"
                      % (name, fmt_hms(due), why))
        self._save_state()

    def _reap_due_threads(self, now):
        """Close the consoles whose grace period has run out.

        The second sweep the spare in _kill_previous_threads never had. A pane
        spared there is spared because a deadline is holding it; when that
        deadline settles — report landed, or window expired —
        _check_report_deadlines schedules it here, and this is what actually
        ends it. Nothing else in this file ever revisited a spared pane, which
        is how ride-1029 was still running 26 minutes after its trip ended.
        """
        if not self.thread_reaps:
            return
        keep, changed = [], False
        live = self._live_thread_names()
        owed = self._panes_awaiting_wrap_up()
        for reap in self.thread_reaps:
            name = reap.get("tmux")
            if now < reap.get("atMs", 0):
                keep.append(reap)
                continue
            changed = True
            if name in live:
                # A new ride took this name back. It is somebody's console
                # again and this reap is stale.
                self.log.info("ride thread %s not retired: a live ride is"
                              " using it" % name)
                continue
            if name in owed:
                # Re-armed since: a reassigned wrap-up landed on it.
                self.log.info("ride thread %s not retired: a wrap-up is"
                              " outstanding on it" % name)
                continue
            self._retire_thread(name, reap.get("why") or "ride complete")
        if changed:
            self.thread_reaps = keep
            self._save_state()

    def _retire_thread(self, name, why):
        """Kill one ride pane and remember that we did."""
        self._panes_killed[name] = self.now_ms()
        killer = self.kill_thread
        if killer is None:
            if self.replay:
                return
            killer = self._tmux_kill
        try:
            killer(name)
        except Exception as exc:
            self.log.error("ride thread %s kill failed: %r" % (name, exc))
            return
        self.log.info("ride thread %s wrapped up: %s" % (name, why))

    def _kill_previous_threads(self, keep=None):
        """The new ride's thread is the rider's thread; retire the old ones.

        Except one that is still writing a wrap-up. 8/31 15:52:31: the daemon
        asked ride-1535 for the report and 17 s later the next ride started
        and killed that pane. Again at 17:07:50 -> 17:08:43 with ride-1700,
        and that report was never written — the deadline paged about it at
        17:17:50, which is the safety net working and the report still gone.
        A ride the rider takes seventeen seconds later does not make the last
        one's write-up expendable.
        """
        res = self._tmux(["list-sessions", "-F", "#{session_name}"])
        if res.returncode != 0:
            return []          # no tmux server yet: nothing to clean up
        owed = self._panes_awaiting_wrap_up()
        killed, spared = [], []
        for name in ride_thread_sessions((res.stdout or "").split()):
            if name == keep:
                continue
            if name in owed:
                spared.append(name)
                continue
            if self._tmux(["kill-session", "-t", name]).returncode == 0:
                killed.append(name)
                # Remembered for two reasons: _check_report_deadlines must
                # never page about a report we made impossible, and a pane
                # killed here needs no reap of its own.
                self._panes_killed[name] = self.now_ms()
        if killed:
            self.log.info("previous ride thread(s) killed: %s"
                          % ", ".join(killed))
            before = len(self.thread_reaps)
            self.thread_reaps = [r for r in self.thread_reaps
                                 if r.get("tmux") not in killed]
            if len(self.thread_reaps) != before:
                self._save_state()
        if spared:
            self.log.info("ride thread(s) spared, wrap-up outstanding: %s"
                          % ", ".join(spared))
        return killed

    # -- live status file ---------------------------------------------------

    def _mark_dirty(self):
        self._status_dirty = True

    def _trip_state_lines(self, trip, now):
        """The trip's current state as bullets.

        Shared verbatim by current-ride.md and the thread digest: two surfaces
        describing the same second must not describe it differently.
        """
        lines = [
            "- Started: %s %s" % (fmt_date(trip.start_ms), fmt_hms(trip.start_ms)),
            "- Itinerary: %s" % itinerary_one_liner(trip.itinerary),
        ]
        if trip.swap_seq:
            lines.append("- Itinerary swaps: %d (last %s)" % (
                trip.swap_seq, fmt_hms(trip.swap_times[-1])))
        p = trip.progress
        if p:
            stops = ""
            if p.get("stopsRemaining") is not None:
                stops = ", %s stops left (next: %s)" % (
                    p["stopsRemaining"], p.get("nextStopName"))
            lines.append("- Leg %s at %s, status %s%s" % (
                p.get("currentLegIndex"),
                fmt_pct(p.get("currentLegProgress")), p.get("status"), stops))
        if trip.riding:
            lines.append("- Riding: trip %s vehicle %s (%s) since %s" % (
                trip.riding.get("tripId"), trip.riding.get("vehicleId"),
                trip.riding.get("headsign"), fmt_hms(trip.riding.get("boardedAt"))))
        else:
            lines.append("- Riding: not aboard")
        lines.append("- Last fix: %ds ago" % max(0, (now - trip.last_pos_ms) // 1000))
        lines.append("- Pages sent: %d/%d" % (trip.pages_sent, MAX_PAGES_PER_TRIP))
        if trip.thread:
            lines.append("- Ride thread: tmux %s (%s), %d push(es)" % (
                trip.thread["tmux"],
                {True: "up", False: "gone", None: "starting"}.get(
                    trip.thread.get("ok"), "?"),
                trip.thread_pushes))
        return lines

    def maybe_write_status(self):
        if not self._status_dirty:
            return
        now = self.now_ms() if self.replay else int(time.time() * 1000)
        if now - self._status_last_write >= STATUS_DEBOUNCE_MS:
            self.write_status()

    def write_status(self, force=False):
        self._status_dirty = False
        self._status_last_write = self.now_ms() if self.replay else int(time.time() * 1000)
        lines = ["# Ride watch — live status", ""]
        lines.append("Updated: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # Which daemon wrote this. Cheap here and load-bearing: a rider console
        # or a Claude session reading a status file has no other way to know
        # whether the process that produced it is running the source they are
        # about to read. (8/28)
        lines.extend(self._daemon_lines())
        lines.append("")
        if not self.trips:
            if self.last_trip_summary:
                s = self.last_trip_summary
                clean = " (clean ride)" if s.get("findings") == 0 else ""
                lines.append(
                    "No active trip. Last: %s %s — session %s, %d finding(s)%s, "
                    "ended %s (%s)." % (
                        s.get("date"), s.get("itinerary"), s.get("session"),
                        s.get("findings", 0), clean, s.get("endedAt"),
                        s.get("reason")))
            else:
                lines.append("No active trip. Last: none recorded yet.")
        # Every per-session file opens with the same header as the combined
        # one: title, blank, Updated:, then the daemon-provenance lines. Sliced
        # by content rather than a fixed count because _daemon_lines() varies
        # (the STALE warning and the duplicate-record count come and go).
        header = lines[:3] + self._daemon_lines()
        # _active_trips(): a snapshot (the tailer may be starting or ending a
        # trip while this runs), and one entry per ride, not per session id.
        for trip in self._active_trips():
            now = self.now_ms()
            section = []
            section.append("## Active trip — session %s%s" % (
                trip.session, " (adopted mid-stream)" if trip.adopted else ""))
            section.append("")
            section.extend(self._trip_state_lines(trip, now))
            section.append("")
            # The rider's own words go above the machine findings: when both
            # exist, the note is the one that says what actually went wrong.
            if trip.notes:
                section.append("### Rider notes (%d, newest first)" % len(trip.notes))
                section.append("")
                for note in reversed(trip.notes[-20:]):
                    c = note["context"]
                    where = "leg %s" % c.get("legIndex")
                    if isinstance(c.get("legProgressPct"), (int, float)):
                        where += " at %s" % fmt_pct(c["legProgressPct"])
                    if c.get("stopsRemaining") is not None:
                        where += ", %s stops left" % c["stopsRemaining"]
                    if c.get("status"):
                        where += ", %s" % c["status"]
                    section.append("- %s — %s  _(%s)_" % (
                        note["time"], note["text"], where))
                section.append("")
            if trip.findings:
                section.append("### Findings (%d, newest first)" % len(trip.findings))
                section.append("")
                for fnd in reversed(trip.findings[-30:]):
                    section.append("- %s [%s] %s: %s" % (
                        fnd["time"], fnd["severity"], fnd["rule"], fnd["summary"]))
            else:
                section.append("### Findings: none")
            section.append("")
            lines.extend(section)
            # ...and the same section on its own, named for the rider it
            # belongs to. The combined file above is the operator's view and
            # describes every trip on the server at once; handing that to a
            # rider's console would show them somebody else's live position.
            # /api/ride-status reads this one when it knows whose console is
            # asking. See _session_for_device in preferences_api.py.
            self._write_atomic(
                os.path.join(self.watch_dir, "%s.current-ride.md" % trip.session),
                "\n".join(header + section) + "\n")

        self._write_atomic(
            os.path.join(self.watch_dir, "current-ride.md"),
            "\n".join(lines) + "\n")

    def _write_atomic(self, path, text):
        """Write via a temp file and rename, so a reader never sees a half file.

        The /ride console polls these every few seconds; os.replace is what
        keeps it from catching one mid-write.
        """
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError as exc:
            self.log.error("status write failed (%s): %r" % (path, exc))

    # -- finalize (replay EOF / shutdown) -----------------------------------

    def flush_pending_pages(self):
        """Close every open coalescing window now (shutdown path).

        A clean shutdown leaves active trips un-ended so a restart re-adopts
        them, but a page still inside its window has nowhere to be re-adopted
        from — send it before going away.
        """
        for trip in self._active_trips():
            self._flush_pages(trip, self.now_ms(), force=True)

    def finalize_replay(self):
        """End any still-active trips at replay EOF."""
        for trip in self._active_trips():
            self._end_trip(trip, trip.last_event_ms, "replay-eof")
        self.write_status(force=True)


# ---------------------------------------------------------------------------
# File following
# ---------------------------------------------------------------------------


def current_log_path():
    """The sidecar names daily files by UTC date."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return os.path.join(DEBUG_LOG_DIR, "debug-%s.jsonl" % day)


class Tailer:
    """Follows the current UTC-day JSONL file; handles rollover, absence,
    truncation, and partial trailing lines."""

    def __init__(self, log):
        self.log = log
        self.path = None
        self.fh = None
        self.offset = 0
        self.buf = b""

    def _open(self, path, seek_end=False, lookback_cb=None):
        self._close()
        try:
            self.fh = open(path, "rb")
        except OSError:
            self.fh = None
            return
        self.path = path
        self.buf = b""
        if lookback_cb:
            # Startup: look at only the tail of the file and replay the last
            # few minutes, so an already-in-progress trip is picked up without
            # reprocessing (or even reading) the whole day's history.
            size = os.path.getsize(path)
            start = max(0, size - LOOKBACK_TAIL_BYTES)
            self.fh.seek(start)
            chunk = self.fh.read(size - start)
            self.offset = size
            lines = chunk.split(b"\n")
            self.buf = lines.pop()  # partial tail; the writer will complete it
            if start > 0 and lines:
                lines.pop(0)        # partial head from the arbitrary seek
            cutoff = time.time() - STARTUP_LOOKBACK_MS / 1000
            n = 0
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    recv = json.loads(raw).get("recv") or 0
                except (ValueError, AttributeError):
                    continue
                if recv >= cutoff:
                    lookback_cb(raw.decode("utf-8", "replace"))
                    n += 1
            self.log.info("opened %s (lookback scanned %d lines, replayed %d)"
                          % (path, len(lines), n))
        elif seek_end:
            self.fh.seek(0, os.SEEK_END)
            self.offset = self.fh.tell()
            self.log.info("opened %s at end (offset %d)" % (path, self.offset))
        else:
            self.offset = 0
            self.log.info("opened %s from start" % path)

    def _close(self):
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
        self.fh = None

    def poll(self, on_line, startup=False):
        """Read any new complete lines and feed them to on_line."""
        want = current_log_path()
        if self.path != want:
            if os.path.exists(want):
                if self.path:
                    self._drain(on_line)  # finish the old day first
                    self.log.info("rolling over %s -> %s" % (self.path, want))
                self._open(want, lookback_cb=on_line if startup else None)
            elif self.fh is None:
                return  # today's file not created yet
        if self.fh is None:
            if os.path.exists(want):
                self._open(want, lookback_cb=on_line if startup else None)
            else:
                return
        self._drain(on_line)

    def _drain(self, on_line):
        if not self.fh:
            return
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:
            # Truncated/replaced: reopen, skip anything stale.
            self.log.warn("%s shrank (%d < %d); reopening at end" % (
                self.path, size, self.offset))
            self._open(self.path, seek_end=True)
            return
        if size == self.offset:
            return
        self.fh.seek(self.offset)
        chunk = self.fh.read(size - self.offset)
        self.offset = self.fh.tell()
        data = self.buf + chunk
        lines = data.split(b"\n")
        self.buf = lines.pop()  # possibly-partial tail
        for raw in lines:
            if raw.strip():
                on_line(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_replay(path, watch=None, watch_dir=None):
    """Feed a historical file through the daemon code path at max speed.

    Always dry-run: replayed telemetry must never page the rider or spawn
    report agents. Output goes to a separate directory by default so a
    replay can never clobber the live daemon's current-ride.md — reportPath
    included, so a replayed ride cannot name a real vault report either.
    """
    if watch is None:
        wd = watch_dir or os.path.join(WATCH_DIR, "replay")
        watch = RideWatch(dry_run=True, replay=True, watch_dir=wd,
                          report_dir=os.path.join(wd, "reports"))
    watch.dry_run = True
    watch.replay = True
    with open(path, "rb") as f:
        for raw in f:
            watch.process_line(raw.decode("utf-8", "replace"))
    watch.finalize_replay()
    return watch


def run_live(watch_dir=None):
    watch = RideWatch(dry_run=DRY_RUN, replay=False, watch_dir=watch_dir or WATCH_DIR)
    log = watch.log
    log.info("ride-watch starting (sha=%s, dry_run=%s, log_dir=%s, watch_dir=%s)"
             % (DAEMON_GIT_SHA, watch.dry_run, DEBUG_LOG_DIR, watch.watch_dir))
    tailer = Tailer(log)
    stop = {"flag": False}

    def on_signal(signum, _frame):
        log.info("signal %d received; shutting down" % signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    tailer.poll(watch.process_line, startup=True)
    watch.write_status(force=True)

    last_tick = 0.0
    while not stop["flag"]:
        tailer.poll(watch.process_line)
        now = time.time()
        if now - last_tick >= 5.0:
            last_tick = now
            watch.check_timers()
        watch.maybe_write_status()
        time.sleep(0.5)

    # Clean shutdown: keep active trips un-ended (a restart re-adopts them),
    # but do not let the restart eat a page that was still inside its window.
    watch.flush_pending_pages()
    watch.write_status(force=True)
    log.info("ride-watch stopped cleanly (active trips preserved: %d)"
             % len(watch._active_trips()))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Go Mode ride watcher")
    ap.add_argument("--replay", metavar="FILE",
                    help="process a historical JSONL file at max speed "
                         "(forces dry-run) and exit")
    ap.add_argument("--watch-dir", metavar="DIR",
                    help="override the output directory (status file, "
                         "findings, logs)")
    args = ap.parse_args(argv)
    if args.replay:
        watch = run_replay(args.replay, watch_dir=args.watch_dir)
        sent = [p for p in watch.push_log if p.get("sent")]
        print("\nreplay complete: %d trip(s), %d finding(s), "
              "%d push(es) would have been sent (%d suppressed)"
              % (len(watch.ended_trips), len(watch.all_findings),
                 len(sent), len(watch.push_log) - len(sent)))
        for f in watch.all_findings:
            print("  %s  %-8s %-24s %s"
                  % (f["time"], f["severity"], f["rule"], f["summary"]))
        for p in sent:
            print("  PUSH  %s: %s" % (p["kind"], p["body"]))
        return 0
    return run_live(watch_dir=args.watch_dir)


if __name__ == "__main__":
    sys.exit(main())
