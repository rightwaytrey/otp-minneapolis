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
import glob
import hashlib
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


# The files that decide how this daemon behaves. Everything else in the repo —
# the graph, the nginx templates, the deployment env files, even this repo's
# CLAUDE.md — can move without changing a single thing the running process
# does, and on 2026-09-08 that is exactly what happened: twelve commits, none
# of them touching ride-watch/, and every artifact the daemon wrote carried
# "STALE ... findings may come from code that no longer exists". Two rides
# spent their opening line telling the rider about it. The tree moving is not
# the question; THESE files moving is.
DAEMON_SOURCE_FILES = (
    "ride-watch/ride_watch.py",
    "ride-watch/ride-thread-run.sh",
    "ride-watch/ride-thread-sysprompt.md",
    "ride-watch/ride-thread-settings.json",
)


def _source_digests():
    """Content digest of each daemon file, as it is on disk right now.

    Content, not `git rev-parse HEAD:<path>`: the process loaded the WORKING
    TREE, so an uncommitted edit is every bit as much a drift as a commit —
    and this needs no git at all, which means it still answers in a checkout
    where another agent holds the index lock. A file that is missing is
    recorded as None rather than skipped, so deleting one is a change too.
    """
    out = {}
    for rel in DAEMON_SOURCE_FILES:
        path = os.path.join(REPO_DIR, rel)
        try:
            with open(path, "rb") as f:
                out[rel] = hashlib.sha1(f.read()).hexdigest()
        except OSError:
            out[rel] = None
    return out


DAEMON_GIT_SHA = _resolve_daemon_sha()
DAEMON_STARTED_MS = int(time.time() * 1000)
DAEMON_SOURCE_MTIME = _source_mtime()
# What the daemon's own files looked like when this process read them. The
# STALE banner compares against THIS, not against HEAD.
DAEMON_SOURCE_DIGESTS = _source_digests()
# How often the *running* SHA may be compared against the repo's HEAD for the
# "you are stale" line. Only `rev-parse` is re-run (read-only, takes no index
# lock, so it cannot collide with another agent's git in this shared worktree),
# never `status`, and never on the per-event path.
HEAD_RECHECK_MS = 5 * 60 * 1000

# Rule thresholds (ms unless noted)
STARTUP_LOOKBACK_MS = 5 * 60 * 1000        # scan back this far at startup
LOOKBACK_TAIL_BYTES = 16 * 1024 * 1024     # ...reading at most this much tail

# Before an adoption asserts "this ride has no START_GO_MODE", go and look.
#
# 2026-09-15 ride 1 (`mu2rh9og-fw6prf`): the live daemon acted on none of
# records 69-329 of debug-2026-09-15.jsonl -- four START_GO_MODE and two
# STOP_GO_MODE between 09:24:31 and 09:26:31 -- then adopted off the
# UPDATE_PROGRESS at record 330 (09:26:31.238) and filed `resumed-trip`
# "no START_GO_MODE ... cannot be replayed" 18 ms after a START_GO_MODE that
# was already three lines above it in the same POST batch (records 324-331 all
# carry recv 09:26:33.285). The same file replays correctly, so the records
# were there and the follower did not hand them over.
#
# The follower bug that lost them is not understood (see Tailer._drain's
# diagnostics). This is the part that can be made not to matter: the stream is
# on disk either way, so read it back before making a claim about what is not
# in it. The window is generous because it only ever costs one tail read of an
# already-open file, and the claim it is guarding is load-bearing -- a ride
# called unreplayable gets no fixture and the wrap-up spends itself arguing.
ADOPT_START_LOOKBACK_MS = 15 * 60 * 1000
# ...reading at most this much tail. A START_GO_MODE carries the whole
# itinerary: records 69/144/309/325 of 09-15 are 27 KB, 112 KB, 110 KB and
# 110 KB. 8 MB is ~70 such records, far more than one Go Mode run.
ADOPT_START_LOOKBACK_BYTES = 8 * 1024 * 1024

# Follower diagnostics. A poll that delivers lines logs one INFO line, at most
# this often, so a 1 Hz telemetry stream does not turn daemon.log into a second
# copy of the telemetry. The per-drain summaries are kept in a ring regardless
# and dumped in full when a trip opens or is adopted, which is the moment the
# next miss will need them.
TAILER_DRAIN_LOG_INTERVAL_S = 60.0
TAILER_DRAIN_RING = 12
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
# arrived-never-ended (18.3b). The close above is a WATCHER close, and it has
# been quietly covering for the client: on 2026-09-17 ride `mu69yw00-bo98a0`
# SET_ARRIVED landed at 21:41:31 and the app's own STOP_GO_MODE did not arrive
# until 21:56:03 — 14m32s — yet the ride's report says `endReason: arrived`,
# because this daemon had already closed the trip itself at 21:46:31 and
# nothing anywhere said the client had failed to. The masking is the reason the
# gap went unseen for the whole of Tier 13.
#
# The client's own dwell timer is AUTO_END_AFTER_ARRIVAL_MS = 3 min
# (otprr lib/actions/go-mode.ts, the arrived branch of handlePositionUpdate).
# Three minutes plus ninety seconds of slack, which still leaves half a minute
# — six live ticks — before ARRIVED_END_MS takes the trip away. The ordering is
# asserted, not assumed: see the test that reads both constants.
#
# The slack is measured, and the measurement is itself the finding. Across
# every arrival in the 24 day files on disk (08-25..09-18, 12 arrivals), the
# app's own STOP_GO_MODE landed at 243 s, 289 s, 298 s, 322 s, 359 s, 468 s,
# 873 s, 2951 s, 5308 s and 10451 s after SET_ARRIVED, and twice never at all
# (08-31 18:52, the double-mount). NOT ONE came in under the 180 s the client
# gives itself. There is no clean gap to put a threshold in, so it goes just
# above the single fastest close on record: 270 s excuses 09-15's 243 s (a
# timer that is merely a tick or two late — tracking drops to a 30 s interval
# at arrival, so "late" is cheap) and reports the other ten. A rule that fires
# on ten rides in twelve would normally be a rule with no discriminating power;
# here it is the honest count, this is a `warn` and costs the rider nothing,
# and when 13.5 lands the rule should go quiet — which makes it that fix's
# regression test.
ARRIVED_NEVER_ENDED_MS = 270 * 1000
# arrived-far-from-destination (21.2, daemon half). The client's own arrival
# radius, from otprr lib/util/go-mode/progress-calculator.ts ARRIVAL_RADIUS_M.
# The app grants arrival either inside that radius OR on overallProgress
# >= 99.5 with only a 120 m veto (ARRIVAL_MAX_DISTANCE_M), and on a long trip
# the last half-percent of OVERALL progress is the last 80 m of the final leg:
# 2026-09-21 08:54:10.060 UPDATE_PROGRESS overallProgress 99.527,
# currentLegProgress 94.21, distanceToDestination 83.26, status completed ->
# SET_ARRIVED 08:54:10.075 and a "Trip complete" notification, with the rider
# still 83 m out and 1m26s of walking left (they reached 27.8 m at 08:55:36).
# Accuracy was 2.8-8.4 m throughout, so this is not GPS.
#
# A warn, never a page: the rider is standing there looking at the screen that
# just told them they had arrived, and a buzz saying the same thing 83 m early
# helps nobody. The number is what the report needs.
#
# The threshold discriminates on the recorded days: the other two arrivals on
# 09-20 and 09-21 latched at 74.2 m and 73.7 m — the 75 m branch working
# correctly — and neither fires.
ARRIVAL_RADIUS_M = 75.0
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
# stop-count-collapse. The percentage was always a proxy for "the count says
# the rider is nearly there and the leg says they are not", and it is only a
# proxy: it assumes the second-to-last stop sits past STOP_COLLAPSE_MAX_PROGRESS
# of the leg. On the Orange Line it does not. 2026-09-21 09:32:47 the count
# went 2 -> 1 at 41 % of a 7885 m leg and the daemon spent the ride's only page
# on it — the rider was 31 m from Knox Ave & American Blvd Station, the leg's
# penultimate stop, pulling in at 8.4 m/s, with 3775 m still to run to I-35W &
# 98th St because the longest hop on that leg is the last one (22.3).
#
# So the leg's own geometry decides now, and the percentage is only the
# fallback for a leg that carries no stop coordinates. The stop the count
# implies is the one it has just consumed; if the rider is within
# STOP_COLLAPSE_NEAR_STOP_M of it, the count is RIGHT and there is nothing to
# say. 100 m: the 09-21 drops were 31 m (American Blvd) and 29 m (76th St), and
# a station platform plus a bus length plus GPS is comfortably inside that,
# while the 7/29 incident this rule was written for collapsed to 1 at the very
# START of the leg, kilometres from any stop the count could have meant.
STOP_COLLAPSE_MAX_PROGRESS = 60.0          # percent; fallback only (see above)
STOP_COLLAPSE_NEAR_STOP_M = 100.0
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
# ...and the anchor has to be dropped when the DENOMINATOR changes, not only
# when the rider moves. An itinerary swap re-bases currentLegProgress onto a
# new leg 0, so the two ticks straddling it describe different quantities:
# 2026-09-20 12:55:24.829 the rider's own onboard pick (GO_MODE_CONTROL_TAP
# `onboard-preview-confirm` -> CLEAR_ONBOARD -> START_GO_MODE) took the bar
# from 11 % to 66 % in 9 ms at the IDENTICAL fix (44.86033, -93.30134,
# distanceToDestination unchanged at 4914.3 m) and this rule called it a
# teleport (21.6). Nothing about the rider changed; the leg they were being
# measured against did.
# position-teleport (18.3a). progress-without-motion measures the MATCH, which
# is downstream of the app's continuity gate and therefore lags the input it
# ought to be reporting: on 2026-09-17 ride `mu63yfrb-ekv1fl` the position
# stream began flipping between two tracks at 18:25:40 and the only rule that
# ever noticed fired at 18:28:04 — 2m24s later, about the frozen progress bar
# the gate produced while absorbing the jumps. Nothing in this file watched the
# position stream itself. This rule does.
#
# A "teleport" is a pair of CONSECUTIVE fixes that cannot both be true: far
# apart, moments apart, and both claiming good accuracy. All three clauses
# carry weight and the thresholds are measured, not guessed — the numbers below
# are from a scan of every UPDATE_POSITION pair in all 24 day files on disk
# (08-25..09-18, ~60k fixes):
#
#   >150 m  the fastest thing the rider rides is the Orange Line at ~30 m/s, so
#           150 m in a second is five times any real speed, and the smallest
#           jump in the 09-17 cluster was 196.6 m. Below 150 m the scan starts
#           picking up ordinary freeway fixes: the same ride has jumps of 126 m
#           (17:50:22), 107 m (17:51:39) and 133 m (18:23:04) that the ride
#           report listed but which are not separable from a fast bus.
#   <=2 s   the fixes arrive at 1 Hz; two seconds allows one dropped tick and
#           no more. A longer gap is a GPS gap, which gps-gap already covers.
#   <30 m   both ends, because the whole point is that neither fix admits to
#           being uncertain. The 09-17 cluster's accuracies are 9-22 m.
#
# One jump is not a finding: 08-28 17:20:35 (184 m), 09-01 10:39:52 (320 m) and
# 09-04 15:44:56 (179 m) are each a single isolated pair in a whole ride, and a
# lone outlier is a GPS artefact the matcher is built to absorb. What is
# diagnostic is the RATE — two tracks alternating means a jump every few
# seconds — so the rule counts jumps in a rolling minute.
TELEPORT_MIN_M = 150.0
TELEPORT_MAX_GAP_MS = 2 * 1000
TELEPORT_MAX_ACCURACY_M = 30.0
TELEPORT_WINDOW_MS = 60 * 1000
# Measured on the same scan: a rolling minute reaches 2 on exactly three
# recorded rides, all three of which are the two-stream defect (09-15
# `mu2rh9og-fw6prf` 09:36:55, 09-17 `mu63yfrb-ekv1fl` 18:25:48, 09-17 evening
# `mu69yw00-bo98a0` 21:01:04), and reaches 5 on none of them: the worst minute
# on record is 4 (ride 1, at 18:26:17). So `warn` at 2 names the cause of
# 09-17's deviation replan 67 s before it happened, and `page` at 5 is
# deliberately above everything ever recorded — a phone whose position stream
# is unusable five times a minute is a different event from the one measured
# here, and the rider's two interrupts are not spent on a diagnosis they cannot
# act on. If 5 ever fires, it is new.
TELEPORT_WARN_COUNT = 2
TELEPORT_PAGE_COUNT = 5
# One finding per episode, not one per jump: the 09-17 cluster is ten jumps in
# 2m06s and is one defect. The cooldown is the same 5 minutes
# progress-without-motion uses, and an escalation to `page` is allowed through
# it once — a worsening stream is news even mid-cooldown.
TELEPORT_COOLDOWN_MS = 5 * 60 * 1000
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
# unreachable-but-routable (2026-09-09 09:41:35). The app raised
# DESTINATION_UNREACHABLE — "Still 1670m from 2345 Old Shakopee Road West and
# re-planning isn't closing the gap" — while every REROUTE_SNAPSHOT it had
# taken that ride came back with itineraries ending ON the address: all 27 of
# them at (44.81655, -93.30986), 0.5 m from the requested `toPlace`. The graph
# could reach it the whole time. That is a different defect from 8/28's
# Fairgrounds interior, where the snapshots genuinely stopped at the fence,
# and the snapshots are what tell them apart.
#
# Three minutes: the capture runs on a ~90 s cadence (measured 80-101 s across
# 53 captures on 2026-09-09), so three minutes is "the last two captures" and
# never "something from the other end of the ride".
UNREACHABLE_SNAPSHOT_WINDOW_MS = 3 * 60 * 1000
UNREACHABLE_GAP_M = 100.0                  # a plan ending this close arrived
REROUTE_SNAP_RING = 8                      # ~12 min of captures, all we read
# early-leg-transition (13.4). The app advanced onto a transit leg while it
# still had no idea the rider was aboard anything and the leg they were on was
# nowhere near finished. On 2026-09-09 08:24:55 it stepped to leg 1 (METRO
# Orange Line) with the bike leg at 71.88 % and riderSpeedMps 5.9 — the rider
# was still riding to the station — and SET_RIDING did not arrive for another
# 2m36s. Ride 2 the same morning is the control: leg 0 read 100 % at the
# transition and SET_RIDING landed 2 s later.
EARLY_TRANSITION_PROGRESS_PCT = 90.0

# --- the 2026-09-13 Green Line ride (15.7) ----------------------------------
#
# Four rules, all of them warn or info, all of them about the same hour: the
# rider was aboard a train the app had no idea they were on. None of them is a
# page and none belongs in PAGE_RANK — the rider was already looking at the
# screen each is about, and a ride has two interrupts to spend on things they
# cannot see. What they are for is the ledger and the report: on 09-13 the
# daemon's whole machine record of the ride was one rider note.
#
# access-leg-transit-speed. A WALK/BICYCLE leg does not travel at 15 m/s. On
# 09-13 the speed was >= 12 m/s continuously from 11:35:50 (13.69) and peaked
# at 21.05 at 11:36:11, all on leg 0 (BICYCLE) with no riding fact — the rider
# had boarded at Dale St before Go Mode even started. 12 m/s is 27 mph: above
# any bicycle and above the 5.9 m/s that early-leg-transition measured on a
# rider genuinely sprinting for a station, and below a freeway. 20 s so a
# single absurd fix cannot fire it (the daemon has seen 1414 m accuracy).
ACCESS_TRANSIT_SPEED_MPS = 12.0
ACCESS_TRANSIT_SPEED_MS = 20 * 1000
#
# boarding-prompt-empty. "I'm on the bus" renders `vehicleMatch.nearbyVehicles`
# (otprr BoardingPrompt.tsx:82), which only the transit-leg matcher writes, so
# on an access leg the button searches nothing and says "No buses detected
# nearby" (15.2). The daemon cannot see the screen, but it can see the two
# halves: no UPDATE_NEARBY_VEHICLES in the 30 s before the prompt (the matcher
# did not run) while the route feed the trip sheet *was* polling held a vehicle
# inside the radius the matcher itself would have used. That radius is the
# client's own speedAdjustedRadius: 200 m plus 45 s of travel at the rider's
# speed. At 11:36:24.799 the last poll was 15 s old, the rider was doing
# 15.2 m/s (radius 885 m) and train 32141 was 634.8 m away: the search would
# have found it. The 11:36:37 prompt is the control — the rider had slowed to
# 4.0 m/s (radius 378 m) and 32141 had pulled 748.5 m ahead, so a matcher that
# ran would have come back empty and the prompt was telling the truth.
BOARDING_PROMPT_NEARBY_WINDOW_MS = 30 * 1000
BOARDING_PROMPT_BASE_RADIUS_M = 200.0      # == client speedAdjustedRadius base
BOARDING_PROMPT_SPEED_SECONDS = 45.0       # ...and its seconds-of-travel term
# A feed reading older than this says nothing about where a train is now; the
# trip sheet polls every ~20 s, so this is several missed polls.
BOARDING_PROMPT_MAX_RADIUS_M = 2500.0      # ...and its cap
BOARDING_PROMPT_FEED_MAX_AGE_MS = 2 * 60 * 1000
#
# onboard-anchor-behind-rider. STOP_GO_MODE wipes the client's
# `tracking.lastPosition`; an onboard flow begun before the next fix lands
# falls to `findAnchorIndex` index 0 and builds the alight list from the FIRST
# stop of the line (15.5). On 09-13: STOP 11:38:36.664, BEGIN_ONBOARD_FLOW
# 11:38:38.013, START_ONBOARD_OPTIMIZE 11:38:38.346 with first candidate Union
# Depot — 4760 m east of the last fix (11:38:35.033) — and the next fix landed
# 11:38:39.035, 689 ms too late. The three correct flows that hour anchored at
# 380 m, 97 m and 39 m, so 2 km is nowhere near either population.
#
# The candidates carry `stopId`/`stopName`/`busArrivalEpoch` and NO
# coordinates, so this cannot be answered at the optimize. The coordinates
# arrive with the per-candidate ONBOARD_CANDIDATE_SNAPSHOT (`request.from`,
# keyed back to the candidate by `busArrivalEpoch`) — 11:38:45.234 for Union
# Depot, 6.9 s later. The finding is therefore stamped at the optimize it is
# about and carries `detectedMs` for the snapshot that resolved it.
ONBOARD_ANCHOR_FAR_M = 2000.0
# A fix this old cannot convict an anchor: the rider may have moved. The real
# one was 3.3 s old.
ONBOARD_ANCHOR_FIX_MAX_AGE_MS = 3 * 60 * 1000
# ...and distance alone was never enough. On 2026-09-15 at 15:46:02 this rule
# fired on an anchor 2044 m away and the claim was simply wrong: the rider was
# at 44.86543, -93.30193 (103 m past Knox Ave & 76th St), the anchor was I-35W
# & 66th St Station 2044 m NORTH, and the Orange Line runs northbound there —
# the anchor was 2 km AHEAD, which is what an alight list is supposed to be
# built from (17.9c).
#
# The direction test, and why it is this one. The candidate list is the trip's
# REMAINING stops in trip order: candidates[0] is the stop `findAnchorIndex`
# decided the rider is at, and everything after it is further along the line.
# If the anchor really is ahead, the rest of the list recedes — 09-15's second
# candidate (I-35W & Lake St) sat 9.5 km out against the anchor's 2.0 km. If
# the anchor is BEHIND, the list walks back toward the rider and past them —
# 09-13's second candidate (Capitol / Rice St) was 3.23 km against Union
# Depot's 4.76 km. So: fire only when some later candidate is meaningfully
# NEARER the rider than the anchor is.
#
# Rejected — the rider's own motion, which is the obvious direction test and
# cannot work here. At 11:38:38 on 09-13, the flow this rule was written for,
# the rider was stationary: speed 0.0 and twenty-odd byte-identical fixes, so a
# "receding from the anchor" test would have suppressed the only true positive
# on record. GPS `heading` is no better: at 15:46:02 the bus was crossing 76th
# St eastbound at heading 88.5° with the anchor bearing 14.8° — a 73.7°
# difference, "ahead" under a 90° test but only just, and a bus mid-curve would
# flip it. Stop order is the one signal in this stream that survives a
# stationary rider.
ONBOARD_ANCHOR_AHEAD_MARGIN_M = 250.0
# It fails closed: a list with no later candidate placed on the map cannot be
# judged either way and says nothing. On both recorded flows every candidate
# got its ONBOARD_CANDIDATE_SNAPSHOT within ~12 s, so that costs nothing real.
#
# aboard-swap exemptions (17.9a, 17.9b).
#
# (a) A swap that keeps the rider's plan is not "the on-screen route no longer
# matches your bus". 2026-09-15 15:36:33: START_REROUTE `boarded-earlier`,
# autoApply: true, 6 s after SET_RIDING, replaced
#   WALK > BUS 1:904 trip 1:1346556 > WALK > TRAM 1:902 trip 1:890194 > WALK
# with
#   BUS 1:904 trip 1:1346665 > WALK > TRAM 1:902 trip 1:890194 > WALK
# — the same two route ids in the same order and the same 16:32:43 arrival. It
# dropped the spent walk leg and re-anchored leg 0 onto the bus actually
# boarded, which is the designed splice. Note what is deliberately NOT
# compared: the bus tripId changed (1:1346556 -> 1:1346665), because boarding
# an earlier bus of the same route is the whole point of `boarded-earlier`. The
# ride report read that as "the same Orange Line trip"; it was not, and
# comparing the OLD plan's tripId with the NEW plan's would have left this
# false positive standing. Route ids plus arrival time is one test.
#
# (a2) It is not the only one, and on its own (a) fails OPEN. 2026-09-17
# 17:59:24, ride `mu63yfrb-ekv1fl`: SET_RIDING trip 1:1346874 at 17:59:18,
# START_REROUTE `boarded-earlier` autoApply: true six seconds later, and a plan
#   BUS 1:904 trip 1:1346874 (98th St -> Lake St) > BICYCLE
# — the same route 1:904 and the same alight stop as the plan going out, but
# the arrival moved 18:35:19 -> 18:33:30, an improvement of 1m49s. (a) demands
# the arrival be unchanged, so it let this through and the rule paged — and
# that page was the ride's ONLY page (`pagesSent: 1`), spent on the app doing
# exactly the right thing.
#
# So the second exemption, and it is the stronger of the two: the new plan's
# transit leg carries the tripId the rider is ON. A replan that lands the rider
# on the vehicle they are physically sitting in cannot be "the on-screen route
# no longer matches your bus", whatever it does to the arrival time.
#
# The comparison that matters is riding.tripId against the NEW plan, never old
# plan against new plan — which is also why it covers 2026-09-15 15:36:33, the
# first sighting, measured in the day file: SET_RIDING at 15:36:27 carried trip
# 1:1346665 (the earlier bus the rider had just boarded) and the incoming plan
# was BUS 1:904 trip 1:1346665. One test, both sightings. (a) is kept as well
# rather than replaced, because the two catch different things: (a) covers a
# swap that changes no plan at all while there is no transit leg to match the
# rider's vehicle against (2026-08-31 17:38:11, a byte-identical replacement),
# and (a2) covers a swap that improves the plan while keeping the rider's
# vehicle. Either one is enough to excuse the swap.
#
# (b) The onboard picker's commit emits no reroute marker at all, so a rider
# tap read as automatic. 15:43:28.647 CLEAR_ONBOARD -> 15:43:28.650
# START_GO_MODE, 3 ms apart, 22 s after SET_ONBOARD_RESULT rendered 5 options.
# That pair runs only from confirmOnboardAlightStop (lib/actions/go-mode.ts),
# whose sole caller is AlightRecommendation.tsx's onSelect. Keyed on the action
# sequence rather than on which control produced it, deliberately: the client is
# growing a preview screen where a tap only opens the preview and a separate
# Confirm commits, and the commit will still be CLEAR_ONBOARD + START_GO_MODE.
ONBOARD_COMMIT_WINDOW_MS = 5 * 1000
#
# session-restart-while-aboard (17.7). 2026-09-15 15:49:45: `record-mode` /
# `start` / `resumed-session`, RESUME_GO_MODE (duration 2439.981, end
# 16:28:33), `bundle_hold`, then `bundle_health` / `bundle_apply` five seconds
# later — bundle_hold at relaunch followed by a health verdict is the
# crash-recovery path, not a normal resume. STOP_GO_MODE came 24 s after. The
# same session did it once before at 15:47:11, and `riding` was set across
# both. No rule covered it: `resumed-trip` keys on a ride that arrives with no
# START_GO_MODE at all, and both of these arrived inside a trip the daemon had
# opened itself.
#
# `resumed-session` and RESUME_GO_MODE land 3 ms apart and are one relaunch, so
# the second marker inside this window is folded into the first.
SESSION_RESTART_DEDUP_MS = 5 * 1000
#
# note-unverifiable (17.11). Ride B, 15:53:50, rider note "Clicking does
# nothing" with a screenshot — and nothing in the stream records that a tap
# happened, so the claim could be neither confirmed nor contradicted.
#
# FIRST the note has to be that kind of note (NOTE_NO_RESPONSE_RE, below). A
# note that does not claim a control responded or failed to respond is not
# made unanswerable by the absence of tap records, and the rule spent three
# firings in two days saying otherwise. Then, and only then, two independent
# things make such a note unanswerable, and either is the finding:
#
#   * no rider-gesture record in the minute before it. RIDER_GESTURE_TYPES
#     below is the allowlist; it deliberately excludes the act of writing the
#     note itself (SET_GO_MODE_BACKGROUNDED + SET_MOBILE_SCREEN + a
#     LOCATION_CHANGE to /feedback precede every single note in both rides, so
#     counting those would make the rule dead on arrival).
#   * a request that timed out was in flight across the note. 15:53:50.965
#     sits inside FIND_FEEDS_ERROR's window: the error landed 15:53:59.330
#     saying "Request timed out after 20000 ms", so the request was issued
#     15:53:39.330 and the app was stalled on it while the rider tapped.
#
# The timeout half can only be known once the error arrives, which is up to its
# own timeout later, so the decision waits this long and is resolved on the
# 5 s tick. Findings are not pages; nothing is lost by deciding 30 s late.
NOTE_GESTURE_LOOKBACK_MS = 60 * 1000
NOTE_EVIDENCE_GRACE_MS = 30 * 1000
# Once per ride. The statement the finding makes — "the client emits no tap
# records, so a claim about a control cannot be checked" — is the same
# statement whichever note it hangs on, and a ride's notes come in fours.
#
# Action types only a rider gesture produces. Checked against every action type
# in the two 09-15 sessions, and against the call sites in otprr: e.g.
# SET_GO_MODE_ACTIVE_LEG comes from TripSheet.tsx handleLegClick/handleClose and
# nothing else. Excluded on purpose: SET_MOBILE_SCREEN and
# @@router/LOCATION_CHANGE (both fire on boot — 15:49:45 and 15:53:39 — and on
# the way to the feedback screen), SET_LOCATION and SET_QUERY_PARAM (the
# relaunch path dispatches both from POSITION_RESPONSE), SET_ONBOARD_VEHICLE /
# SET_ONBOARD_TRIP / SET_ONBOARD_STATUS (all three follow automatically from a
# reroute) and SET_GO_MODE_BACKGROUNDED (the note-writing act).
# One entry is not airtight: START_GO_MODE is also how an auto-reroute installs
# its replacement (15:36:33). It stays, because a false gesture can only make
# note-unverifiable quieter and never noisier, and dropping it would let a
# genuine "I picked this itinerary" tap read as no action at all.
RIDER_GESTURE_TYPES = frozenset((
    "SET_ACTIVE_ITINERARY", "SET_VISIBLE_ITINERARY", "SET_ITINERARY_VIEW",
    "UPDATE_ITINERARY_FILTER", "SET_GO_MODE_ACTIVE_LEG", "SET_ACTIVE_LEG",
    "SET_MAP_FOLLOW", "SET_MAP_PICK_MODE", "SET_VIEWED_STOP",
    "BEGIN_ONBOARD_FLOW", "CLEAR_ONBOARD", "DISMISS_BOARDING_PROMPT",
    "SHOW_BOARDING_PROMPT", "ADD_LOCATION_SEARCH", "CLEAR_LOCATION",
    "REMEMBER_LOCAL_USER_PLACE", "SET_EARLY_ALIGHT", "SET_DEPARTURE_OVERRIDE",
    "START_GO_MODE", "STOP_GO_MODE",
))
# ...and the tap records the client does not emit yet. A parallel change is
# adding them; when it lands they will satisfy the gesture half by themselves
# and this rule goes quiet on its own, which is the point. Matched by shape
# rather than by an exact name nobody has chosen yet.
TAP_RECORD_RE = re.compile(r"(^|_)(TAP|TAPPED|PRESS|PRESSED|CLICK|CLICKED"
                           r"|GESTURE|LONG_PRESS)($|_)")
TAP_RECORD_KINDS = frozenset(("tap", "gesture", "ui", "interaction"))
# ...and the note itself has to be ABOUT a control before any of that is
# evidence of anything. The rule's premise — "nothing records that a tap
# happened, so this claim cannot be checked" — only holds for a note that
# CLAIMS a control did or did not respond. It does not hold for a layout
# complaint, a question, or a feature ask, and on three notes in two days it
# fired on exactly those: 2026-09-20 12:54:48 ("The “tap to return” is
# still overlapping on pages"), 2026-09-21 08:26:05 (a note about displayed
# times, answerable from the realtime stream alone) and 09:12:04 (a feature
# ask). See 17.11.
#
# Two vocabularies again, and the ORDER matters. NOTE_NO_RESPONSE_RE is the
# claim; NOTE_CONTROL_RE is only the noun. Requiring the noun as well would
# lose "Reset to planned? ... And it did nothing." (2026-09-17 17:57:39, a
# real one), and accepting the noun alone would keep every false positive
# above — "tap to return", "the edit trip buttons", "the 2 gps buttons" are
# all controls named in notes that claim nothing about a response. Measured
# against all 37 rider notes on disk (09-07 through 09-21): the claim regex
# alone selects exactly two, 2026-09-15 15:53:50 "Clicking does nothing" and
# 2026-09-17 17:57:39.
NOTE_NO_RESPONSE_RE = re.compile(
    r"(do|does|did|doing)(es)?\s+nothing"
    r"|nothing\s+(happen|happens|happened|happening)"
    r"|(is|are|was|were)?\s*not\s+(working|responding|respond)"
    r"|(isn|aren|doesn|don|didn|won|wouldn|can|couldn)['\u2019]?t\s+"
    r"(work|working|respond|responding|do\s+anything|tap|click|press|select)"
    r"|(does|did)\s+not\s+(work|respond|do\s+anything)"
    r"|no\s+response|unresponsive|not\s+(clickable|tappable|pressable)",
    re.I)
NOTE_CONTROL_RE = re.compile(
    r"\b(tap|taps|tapped|tapping|click|clicks|clicked|clicking|press|presses"
    r"|pressed|pressing|button|buttons|toggle|toggles|toggled|slider|sliders"
    r"|swipe|swipes|swiped|checkbox|long[- ]press)\b", re.I)
# "Request timed out after 20000 ms" (every FIND_*/REALTIME_* error on 09-15)
# and the structured form ROUTING_ERROR carries, {timedOut, timeoutMs, url}.
TIMEOUT_MESSAGE_RE = re.compile(r"timed out after (\d+)\s*ms", re.I)
# A window this wide is not evidence of anything; the real ones were 20 000 ms.
TIMEOUT_WINDOW_MAX_MS = 120 * 1000
#
# same-route-transfer. Two consecutive transit legs on the same routeId with
# different tripIds is the rider getting off their own vehicle to wait for the
# next one of the same route — never a transfer, always a defect in the alight
# ranking (15.4). On 09-13 the 11:40:00 START_GO_MODE installed Green Line
# 1:879781 Lexington->Snelling then Green Line 1:902233 Snelling 11:59->Raymond:
# sixteen minutes on a platform to board the train behind the one they were on.
# The 11:37:59 install had the same shape (1:879781 then 1:905008).

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

# ...and two minutes is all the promotion step ever got, which is the second
# half of 15.8. The prompt fix of 2026-09-13 made the wrap-up responsible for
# promoting its findings into the backlog, and it did not take, because
# _check_report_deadlines landed the wrap-up on `os.path.exists(reportPath)`
# alone and scheduled the reap in the same tick. Both prompts ask for the
# promotion AFTER the report is written (ride-thread-sysprompt.md step 3,
# report-prompt.md "Promote the findings to the backlog") and the daemon's own
# wrap-up line says "WRITE THE REPORT FIRST" — so the promotion window was
# THREAD_REAP_GRACE_MS: 120 seconds for a report with eight findings.
#
# 2026-09-15 is the evidence, twice in seven minutes. ng2uqc's report landed
# 15:53:04 and the pane wrapped up 15:55:05; 8lyyq1's landed ~16:00 and went
# the same way. Neither plan file was touched after 13:42 that day. Two rides,
# eleven rows' worth of evidence, nothing promoted — the eighth miss in the
# wrap-up family (see 12.4).
#
# So a wrap-up is not done when the file appears. It is done when the backlog
# has changed. PLAN_PATHS is digested when the deadline is armed and compared
# on every tick; while the report exists and no plan file has moved, the pane
# is kept alive and the deadline entry is kept in report_deadlines, which is
# what spares the pane in _kill_previous_threads and _reap_due_threads alike.
#
# The terminating condition, because "keep it alive until the backlog moves"
# on its own is a pane that never closes:
#   * a plan file changes            -> settled, reap, no page;
#   * this long after the report      -> give up, reap, and page ONCE if the
#     landed and still nothing        report named at least one real bug;
#   * the report named no real bugs   -> settled immediately, reap, no page.
#     (nothing to promote)
# Eight minutes rather than two: long enough for a wrap-up to read both plan
# files, dedupe eleven rows against sixteen tiers and write them, and short
# enough that the console still closes inside twenty minutes of the ride. A
# report with nothing to promote never enters the window at all.
PROMOTION_DEADLINE_MS = 8 * 60 * 1000
# The one backlog, and the record file beside it. Either moving counts: a
# wrap-up whose findings all dedupe onto existing rows edits the backlog, and
# one that also closes a row moves it into the record — both are the promotion
# step doing its job. Instance-level (self.plan_paths) so a test can point this
# at a temp file and never read or write the rider's real plan.
PLAN_PATHS = (
    os.path.join(os.path.expanduser("~"), ".claude", "plans",
                 "please-make-a-centralized-sharded-petal.md"),
    os.path.join(os.path.expanduser("~"), ".claude", "plans",
                 "transitnav-backlog-record.md"),
)
# How the daemon knows the report had something to promote: the verdict the
# report prompt makes mandatory per finding. Both 09-15 reports used a section
# heading — `## 1. ... — REAL BUG` (ng2uqc) and `## 1. ... — **real-bug**`
# (8lyyq1) — and the prompt's own template puts it in one too
# (`### <time> — <rule> (<severity>) -> **real-bug**`), so headings are read
# first and the whole file only if no heading carries a verdict at all.
REAL_BUG_RE = re.compile(r"real[-\s]?bug", re.I)
# The same verdict, but anchored at the start of a triage-table cell so a
# "what decided it" cell that merely mentions the phrase does not vote
# (18.5). Leading "(" and Markdown bold/italic markers are skipped.
VERDICT_CELL_RE = re.compile(r"^[(\[]?[*_]*\s*real[-\s]?bug\b", re.I)

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
#
# "An image named ...": the map sprite re-registration burst. Every map mount
# throws EIGHTEEN of these — one per numbered route-marker image 1..17 plus
# "rect" — and because the image name is inside the message each one is a
# DISTINCT string, so trip.console_seen does not collapse them and the wrap-up
# gets eighteen findings out of one mount. Recorded 2026-08-31 in
# ~/otp-debug-logs/debug-2026-08-31.jsonl: sessions mthw7svy-s4msqc and
# mthw8o2w-i8z1i6, 18 distinct messages each, 36 records, all with the same
# `addImage@...` stack.
#
# NOT OURS and not a Go Mode bug (backlog 4.20): upstream
# @opentripplanner/transitive-overlay's loadImages checks map.hasImage(id)
# synchronously and calls map.addImage inside an async .then, so concurrent
# effect runs all pass the guard and the later ones throw. It fires on any map
# mount, Go Mode or not. The real fix is a patch-package entry in otprr; until
# that lands this stops one upstream race from consuming a ride's entire
# findings budget.
#
# The substring is the stable PREFIX, deliberately: the varying image name sits
# in the middle, so no single substring can span it, and matching on
# "already exists" alone would swallow unrelated errors.
CONSOLE_ERROR_IGNORE = (
    "CapgoUpdater : Error no url or wrong format",
    'An image named ',
)

# wake-lock-denied. The screen wake lock is what keeps the phone awake while
# Go Mode navigates; when it is refused the screen sleeps mid-ride and the
# rider loses the map, the turn cards, and the fix rate with them.
#
# The client says so out loud — otprr `use-active-trip-guards.ts` logs
# `console.warn('Wake lock request failed:', err)` — and until 2026-09-04 no
# rule heard it, because `_rule_console` reads `level == "error"` only. Three
# rides now carry the refusal and all three reached their report silently; the
# 09-04 one was found by a human reading the raw JSONL.
#
# The PREFIX is matched, not the whole string, because the second console arg
# is the DOMException and the sink writes it as its own array element:
#   {"kind": "console", "level": "warn",
#    "args": ["Wake lock request failed:",
#             {"message": "Permission was denied", "name": "NotAllowedError",
#              "stack": ""}]}
# (~/otp-debug-logs/debug-2026-09-04.jsonl, session mtn4ui3s-xfjx8m, entry ids
# mtn53eer-6ssi0g-h / -12 and mtn5akh8-s2nbaj-h / -y.)
WAKE_LOCK_WARN_PREFIX = "Wake lock request failed"
# One launch's refusals are ONE finding. The requester retries on a bounded
# ladder — 4 tries, 5 s apart, re-armed per return to visibility
# (`use-active-trip-guards.ts` WAKE_LOCK_RETRY_MS / WAKE_LOCK_RETRIES) — so a
# single denied launch emits up to five warns spread over ~20 s. A refusal that
# arrives after this much quiet is a new launch or a new resume and gets its
# own finding: on 09-04 the two relaunches were 5 m 35 s apart, and each
# produced exactly two warns 5 s apart.
WAKE_LOCK_BURST_QUIET_MS = 30 * 1000

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

# panel-torn-down. A rider-facing screen that vanishes under the thumb that is
# using it, because the query change the rider just made pushed the router back
# to the map. 8.9 shipped the settings tab on the morning of 2026-09-04 and the
# first rider to touch it could not drag a single slider (Tier 9.1):
# `settings-screen.tsx:113` onBikeSpeedChange -> `setRoutingPreferences(prefs)`
# with no options -> `routing-profiles.ts:64` `replan` is true under a live
# query -> `setQueryParam(next, randId())` -> `form.js:81` `routingQuery()` ->
# `api.js:106` `dispatch(push(path))` -> the `/settings` route unmounts. It
# happened three times in one eight-minute session (15:01:34, 15:01:41,
# 15:06:11 CDT, session mtndpstb-m2vpey) and the ledger said nothing, because
# no rule in this file reads the router at all.
#
# The whole signal is an ordering, and it is available from telemetry alone:
# a SET_QUERY_PARAM carrying a key the RIDER moves, then a LOCATION_CHANGE off
# a panel route, inside half a second. The rider deliberately backing out of
# /settings looks identical EXCEPT that no query change precedes it, which is
# what makes the window the discriminator rather than a heuristic.
#
# Explicitly NOT used as a discriminator: the record's own `action` field.
# Every one of the thirteen LOCATION_CHANGE records in that session reads
# "POP", including the three teardowns and the rider's own navigations, so
# PUSH-vs-POP carries no information here.
PANEL_TEARDOWN_WINDOW_MS = 500
# The measured gaps on 09-04 were 8 ms, 11 ms and 26 ms — a `push` dispatched
# synchronously from the same reducer pass. 500 ms is two orders of magnitude
# of slack for a slow phone and still far short of any deliberate tap.

# One rider fighting one screen is ONE finding, with the count in its text.
# The 09-04 episode ran 15:01:34.972 -> 15:06:11.587: 4 m 37 s between the pair
# on the search form and the one inside Go Mode, all three the same defect on
# the same tab. A window shorter than that reports it as two unrelated events.
# Nothing waits on this in practice: _end_trip force-flushes, so a ride's
# finding is always in the ledger before its report request is written.
PANEL_TEARDOWN_QUIET_MS = 10 * 60 * 1000

# Query keys a RIDER moves. `routingPreferences` is the settings tab's sliders,
# `from`/`to` the location fields, `time` the departure picker.
#
# Deliberately excluded, because the app sets them for its own reasons and a
# navigation that follows one is not a screen being taken away: `wheelchair`
# (user.js:302, applied from the saved accessibility default), `bannedTrips`,
# `numItineraries` (field-trip.js), and `banned` / `preferred` / `modes` /
# `routeLock` / `activeProfileId` (route-lock.ts, go-mode.ts). Note that Go
# Mode's own reroute (go-mode.ts:1357) does dispatch from/to/time/date — but it
# runs on the map screen, so the "previous location was a panel" gate is what
# keeps it out, not this list.
RIDER_QUERY_KEYS = frozenset(("routingPreferences", "from", "to", "time"))

# Routes that are a screen of their own, from otprr lib/util/webapp-routes.js
# (read at 26ab9817): every entry there that names its own `component` —
# SettingsScreen, LocalPlacesScreen, LocalPlaceEditorScreen,
# FavoritePlaceScreen, SavedTripList, SavedTripScreen, UserAccountScreen — plus
# the two terms pages. Matched by prefix so the `:id` and `:step` children come
# along.
#
# The file's FIRST route entry is deliberately absent: `/`, `/@/:latLonZoom`,
# `/start/:latLonZoom`, `/route`, `/route/:id`, `/schedule`, `/schedule/:id`,
# `/nearby`, `/trip/:id` are all `shouldRenderWebApp: true`, i.e. the map
# screen itself. A query change that moves the rider from `/schedule/:id` to
# `/` is an ordinary search from the stop viewer, not a panel being torn down,
# and treating the stop and route viewers as panels would fire this rule on
# the app's most common flow.
PANEL_ROUTE_PREFIXES = ("/settings", "/places", "/account",
                        "/terms-of-service", "/terms-of-storage")
# ...and three routes under those prefixes where leaving IS the function:
# `/account` and `/account/create` are RedirectWithQuery entries that exist to
# send the rider somewhere else, and `/signedin` is the Auth0 callback.
NOT_A_PANEL_ROUTE = frozenset(("/account", "/account/create", "/signedin"))


MAX_PAGES_PER_TRIP = 2
PUSH_MIN_INTERVAL_MS = 120 * 1000
# ...except the one page that says a ride produced findings and no report.
#
# 2026-09-15 09:40:42: "no wrap-up for mu2rh9og-fw6prf 10 min after the ride
# ended ... paging" was followed by "push suppressed (rate limit)" -- the
# deviated-streak page had gone out at 09:39:22, 80 s earlier, so the rider was
# never told that ride 1's report was missing. It still is. Every other page is
# about something the rider can see out of the window; this one is the only
# notice that a ride's whole record is about to be lost, and the ten-minute
# deadline it rides on has already made it late. So it bypasses the global
# 120 s limit and spends its own, much longer, budget instead.
REPORT_PAGE_MIN_INTERVAL_MS = 10 * 60 * 1000
# Every push body the rider sees is one bounded line. 120 is the number the
# suite has asserted since the copy rules were written; this is that, minus
# room for the ellipsis one_line() adds when it has to cut.
PUSH_BODY_MAX = 118

# -- boot crashes and bundle verdicts (the events that happen with no ride) --
#
# On 2026-09-02 the OTA bundle `2026.0902.3` white-screened the rider's phone
# and the sink recorded NOTHING from it for the whole incident. The client now
# fixes its half (otprr `lib/util/debug-log-boot.js`, merged 26e0afec): a
# `sendBeacon` armed at main.js's first import writes a `boot-error` /
# `boot-rejection` record the instant the app throws, and the 5 s health gate
# in `lib/util/native-updates.ts` writes a `bundle_health` verdict saying
# whether the bundle was confirmed or is about to be rolled back.
#
# A boot crash is the single most page-worthy thing this daemon can see: the
# app the rider would otherwise hear it from is the thing that is broken. It
# is also the only one that happens OUTSIDE a ride, so it cannot be charged
# against a trip's two interrupts (there is no trip) and it cannot be
# coalesced against anything (nothing else is happening). Hence a budget of
# its own, per phone.
#
# Thirty minutes, not per-boot: the client sends at most three beacons per
# boot, but a phone that cannot start gets relaunched by hand, and each
# relaunch is a fresh boot with a fresh three. One interrupt per half hour
# says "your app is not starting" exactly as well as twenty do.
BOOT_PAGE_INTERVAL_MS = 30 * 60 * 1000
# ...and if it starts again inside the hour, the rider is told once that it
# did. A page saying "it broke" and no page saying "it is back" leaves them
# checking.
BOOT_RECOVERY_WINDOW_MS = 60 * 60 * 1000
# A minified stack's message is long and the useful part is the front. Two
# limits: the page is one bounded line the rider reads on a lock screen, while
# the finding is read by the wrap-up agent and can afford the whole sentence.
BOOT_MESSAGE_MAX = 44
BOOT_SUMMARY_MESSAGE_MAX = 160
# The href's origin (`capacitor://localhost`) is identical on every boot; the
# hash is what 6.46 was actually diagnosed from, so that is what is kept.
BOOT_HREF_MAX = 24
# Boot findings shown on current-ride.md. Small: this is a status file about
# right now, and the ledger keeps the history.
BOOT_EVENT_RING = 12
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
# A note the rider types in the minutes after a ride ends is about that ride.
# The daemon used to drop it ("rider note outside any trip"): on 2026-09-09 the
# 09:03:42 note "We should finish a trip on auto if within x distance for x
# time" — a sentence about the trip that had just ended — landed 53 s after the
# 09:02:49 arrival close and reached no ledger, no digest and no report. Five
# minutes, and only for a trip this same session id ran: the rider is still
# holding the phone that finished the ride, and a note from some other session
# is not evidence about this one.
NOTE_ATTACH_GRACE_MS = 5 * 60 * 1000

# The per-session caches the 09-13 rules read (last fix, route feeds, last
# vehicle search, pending onboard anchor) are keyed by a session id the app
# mints on every mount, and this daemon is meant to run for days. Bound them.
SESSION_CACHE_MAX = 64
SESSION_CACHE_KEEP = 16
ROUTE_FEED_KEEP = 8

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
# ...but the ❯ box is drawn while the TUI is thinking too, and it is drawn
# under a permission dialog as well, so the marker alone is not "listening".
# Five consecutive rides lost their wrap-up to this (backlog 12.4): the line
# was typed into a pane that was not at the prompt and the keystrokes went
# somewhere else. On 2026-09-08 12:09 a `capture-pane` caught it in the act —
# the pane was sitting on "Compound command contains `cd` with a relative file
# read while a `Read()` deny rule exists — Do you want to proceed?" and the
# Enter that followed answered THAT, taking the wrap-up with it.
#
# BUSY is survivable: the tty buffers keystrokes and the TUI reads them when
# the turn ends, which is exactly what the spawn path already relies on. So a
# busy pane is waited for and then typed into anyway. BLOCKED is not: typing
# into a permission dialog answers the dialog. A blocked pane is waited for
# and never typed into.
THREAD_BUSY_MARKERS = ("esc to interrupt",)
THREAD_BLOCKED_MARKERS = ("Do you want to proceed?", "Do you want to allow",
                          "Do you want to make this edit")
# How long a push waits for a pane that is not listening. The ordinary
# milestone gives up quickly — a leg transition is worthless ten minutes late.
THREAD_PUSH_HOLD_MS = 60 * 1000
# ...and a busy pane is held for much less than that, because typing into one
# is SAFE. Waiting the full hold on every push while the rider is chatting to
# the thread would delay every milestone for a minute to avoid a problem that
# does not exist. Blocked gets the whole hold; busy gets this.
THREAD_PUSH_BUSY_HOLD_MS = 10 * 1000
THREAD_PUSH_POLL_S = 2.0
# The wrap-up is the one line that must land, so it keeps trying right up to
# a minute before the missing-report page would fire anyway.
THREAD_PUSH_WRAP_UP_HOLD_MS = 9 * 60 * 1000
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
#
# This table holds the TRIP page rules and only those, because _buffer_page is
# its only reader. The three device pushes (boot-crash, bundle-health,
# boot-recovery) never enter the buffer — see _page_device — and their order
# against a buffered ride page is settled in code, not here: boot-crash and a
# withheld bundle-health send instantly and outrank everything (the app being
# broken beats any ride page), while boot-recovery ranks below every entry
# above and defers to a page mid-window (17.21, _maybe_page_recovery).
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
    # unreachable-but-routable  the app has just told the rider to give up on
    #                           getting there, and it is wrong: a plan that
    #                           ends at the door came back seconds ago. "Ask
    #                           again" is an instruction, and it expires.
    "unreachable-but-routable": 37,
    "notification-repeat": 35,
    "aboard-swap": 30,
    # session-restart-while-aboard  the app relaunched under a seated rider,
    #                               so the screen they were navigating by went
    #                               away. Nothing to do about it, but it
    #                               explains the blank screen in front of them
    #                               — below aboard-swap, which is about the
    #                               route being wrong, and above riding-flip.
    "session-restart-while-aboard": 28,
    "riding-flip": 20,
    # position-teleport  the phone's own position stream is unusable, so every
    #                    distance and turn on screen is suspect. Above
    #                    deviated-streak because it explains what the rider is
    #                    looking at (a turn card that just changed under them)
    #                    rather than only reporting that tracking looks off,
    #                    and below riding-flip because there is still nothing
    #                    to do about it but distrust the screen. It takes
    #                    TELEPORT_PAGE_COUNT jumps in a minute to get here,
    #                    which no recorded ride has ever reached.
    "position-teleport": 12,
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


_PEEK_T_RE = re.compile(rb'"t"\s*:\s*(\d{12,14})')


def peek_record_ms(raw):
    """A record's `t` without parsing it. Diagnostics only.

    The follower logs the first and last `t` of every poll that delivers
    lines, and a START_GO_MODE line is 110 KB of itinerary -- json.loads on
    both ends of every drain would cost more than the diagnostic is worth.
    This is a regex for a 12-to-14-digit epoch-ms `t`, which can in principle
    match a `t` inside `payload` before the envelope's own. That is acceptable
    HERE and nowhere else: the number is for a human reading daemon.log next
    to the JSONL, never for a rule.
    """
    m = _PEEK_T_RE.search(raw)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


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


def href_page(href):
    """The screen a record was emitted on, as a route path.

    The app is a hash router inside a Capacitor shell, so every record carries
    `href` = "capacitor://localhost#/feedback" (or ".../#/" for the map). The
    origin is identical on every record ever written and the query string is
    router bookkeeping; the fragment path is the only part that says where the
    rider was. Used by note-unverifiable so a report can place the note (17.11)
    — the rider-note records themselves carry no href, because they come in
    through the /ride console sidecar rather than the beacon.
    """
    if not isinstance(href, str) or not href:
        return None
    frag = href.split("#", 1)[1] if "#" in href else href
    path = frag.split("?", 1)[0].split("&", 1)[0].strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return path[:120]


def short_boot_href(href, limit=BOOT_HREF_MAX):
    """The part of a boot URL that differs between one boot and the next.

    The native app always loads `capacitor://localhost`, so the origin costs
    twenty characters of a page body and carries no information. What killed
    2026.0902.3 was a legacy `routeLock` in the HASH, and the only way anyone
    reconstructed that URL was by hand out of the previous day's log. So the
    hash is what survives the truncation, and the origin is what is dropped.
    """
    if not isinstance(href, str) or not href:
        return None
    tail = href
    if "#" in tail:
        tail = tail[tail.index("#"):]
    else:
        m = re.match(r"^[A-Za-z][\w+.-]*://[^/]*(/.*)$", tail)
        if m:
            tail = m.group(1)
    return one_line(tail, limit)


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


def is_panel_route(pathname):
    """Is this path a rider-facing screen of its own, rather than the map?

    See PANEL_ROUTE_PREFIXES for the derivation and for what is left out.
    """
    if not isinstance(pathname, str) or not pathname:
        return False
    path = pathname.rstrip("/") or "/"
    if path in NOT_A_PANEL_ROUTE:
        return False
    return any(path == prefix or path.startswith(prefix + "/")
               for prefix in PANEL_ROUTE_PREFIXES)


def leg_is_transit(leg):
    if not isinstance(leg, dict):
        return False
    if leg.get("transitLeg") is True:
        return True
    return (leg.get("mode") or "").upper() in TRANSIT_MODES


def leg_stop_points(leg):
    """The leg's remaining stop calls, in order, as [{name, lat, lon}].

    THE FIELD IS `intermediatePlaces`, not `intermediateStops`. Measured in
    2026-09-21's 09:24:21 START_GO_MODE: on the Orange Line leg that produced
    22.3, `intermediateStops` is **null** and `stopCalls` and `steps` are null
    too; `intermediatePlaces` carries the two stops with `lat`/`lon`/`name` and
    a `stop.gtfsId`. Both names are read here because OTP's GraphQL schema has
    both and the client's selection set has changed before.

    The alight stop (`to`) is appended, because that is the stop the count is
    counting down to: `stopsRemaining` is 3 at the top of a leg with two
    intermediate stops, and the stop it names next is stops[-stopsRemaining].
    """
    if not isinstance(leg, dict) or not leg_is_transit(leg):
        return None
    out = []
    seq = leg.get("intermediatePlaces")
    if not isinstance(seq, list) or not seq:
        seq = leg.get("intermediateStops")
    for place in (seq if isinstance(seq, list) else []):
        if not isinstance(place, dict):
            continue
        lat, lon = place.get("lat"), place.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        out.append({"name": place.get("name"), "lat": float(lat),
                    "lon": float(lon)})
    to = leg.get("to")
    if isinstance(to, dict) and isinstance(to.get("lat"), (int, float)) \
            and isinstance(to.get("lon"), (int, float)):
        out.append({"name": to.get("name"), "lat": float(to["lat"]),
                    "lon": float(to["lon"])})
    return out or None


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
            # The ids, not just the label. "METRO Green Line > METRO Green
            # Line" is two legs of the same route and reads like a transfer
            # only because the shortName is null on the Green Line; what says
            # it is a defect is routeId equal and tripId different (15.4).
            "routeId": (leg.get("routeId")
                        or (r.get("gtfsId") if isinstance(r, dict) else None)),
            "tripId": leg.get("tripId"),
            "headsign": leg.get("headsign"),
            "from": ((leg.get("from") or {}).get("name")),
            "to": ((leg.get("to") or {}).get("name")),
            "startTime": leg.get("startTime"),
            "endTime": leg.get("endTime"),
            # The stops the leg still has to call at, in order, ending at the
            # alight stop. stop-count-collapse (22.3) needs coordinates, not a
            # percentage, to say whether a count that dropped to 1 dropped at
            # the stop it names.
            "stops": leg_stop_points(leg),
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
        # Which web bundle the phone was running. Stamped from the device's
        # `bundle` session event (otprr lib/main.js), which lands at app start
        # and so is normally already known by the time a ride opens. A report
        # that cannot name the bundle cannot say which build a defect belongs
        # to — and since the OTA lane shipped, the build is no longer implied
        # by the store version.
        self.bundle = None
        self.bundle_native = None
        self.start_ms = start_ms
        self.itinerary = itinerary_summary        # latest itinerary summary
        self.adopted = adopted                    # trip inferred mid-stream
        self.swap_seq = 0                         # bumped on each itinerary swap
        self.swap_times = []                      # ms of each swap
        self.last_event_ms = start_ms
        self.riding = None                        # SET_RIDING payload + swap_seq
        # The last CONFIRM_VEHICLE: (ms, vehicleId, tripId). The riding fact
        # it established should outlive everything but alighting, so a
        # CLEAR_RIDING with no TRANSITION_LEG or STOP_GO_MODE behind it is the
        # app dropping a fact it had confirmed. See _rule_riding_fact_dropped.
        self.confirm_vehicle = None
        self.riding_dropped_fired = False
        self.progress = None                      # last UPDATE_PROGRESS snapshot
        self.last_pos_ms = start_ms
        self.last_fix = None                      # (lat, lon) of the last fix
        self.gps_gap_open = False
        self.gps_gap_started_ms = None            # last_pos_ms when the gap opened
        self.arrived_ms = None                    # SET_ARRIVED; the trip is over
        self.arrived_leg = None                   # leg index when it latched
        self.arrived_source = None                # which evidence latched it
        self.arrived_never_ended_fired = False    # 18.3b, once per arrival
        self.arrived_far_fired = False            # 21.2, once per arrival
        # START_GO_MODE's `roundTrip` block (round-trip feature, 09-05): the
        # outbound arrival of a round trip is a PAUSE at the stay, not the end
        # of the journey, and the app is right to latch it early-ish. The
        # stream marks it and arrived-far-from-destination steps aside.
        self.round_trip = False
        # The last position fix with its metadata: (tMs, lat, lon, accuracy).
        # last_fix above is the coordinate alone and is reset by rules that do
        # not care when it arrived; position-teleport needs the pair.
        self.last_fix_meta = None
        self.teleports = collections.deque()      # ms of each jump, pruned
        self.teleport_fired_ms = 0
        self.teleport_paged = False
        self.notification_times = collections.defaultdict(collections.deque)
        self.notification_repeat_last = {}        # key -> ms of last finding
        self.motion_anchor = None                 # where progress was last real
        self.motion_fired_ms = 0
        # The span currently being held: {"tMs", "pct", "leg", "fix",
        # "meters", "ticks"} while currentLegProgress has not changed. What
        # progress-without-motion used to report was the RELEASE tick alone
        # (2026-09-09 09:35:12: "moved 3.4 m"), which is the least interesting
        # second of the episode; the 44 s and 74 m before it are the finding.
        self.progress_freeze = None
        # legIndex -> last currentLegProgress seen on it. Read by
        # early-leg-transition, which needs the leg the rider is LEAVING.
        self.leg_progress_last = {}
        self.early_transition_legs = set()        # legs already reported
        # The open run of transit-grade speed on an access leg, or None:
        # {"fromMs", "leg", "minMps", "maxMps"}. See ACCESS_TRANSIT_SPEED_MPS.
        self.fast_access = None
        self.fast_access_legs = set()             # legs already reported
        # (routeId, tripId, tripId) signatures already reported by
        # same-route-transfer. Keyed on the pair, not the trip: the 09-13
        # session installed two different same-route pairs within three
        # minutes and both are worth one line each.
        self.same_route_pairs = set()
        self.boarding_prompt_fired = False
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
        # The periodic REROUTE_SNAPSHOT captures, reduced to the one number a
        # rule can use: how far the best plan in that capture ENDS from the
        # destination that was asked for. [{"tMs", "gapM", "itineraries"}],
        # newest last, bounded. This is the only observable in the stream that
        # says whether the graph can still reach the destination at all.
        self.reroute_snaps = collections.deque(maxlen=REROUTE_SNAP_RING)
        self.snapshots_since_gain = 0              # cadence, not re-plans
        self.unreachable_routable_fired = False
        self.last_rider_action_ms = 0
        # The onboard picker's commit is CLEAR_ONBOARD immediately followed by
        # START_GO_MODE (3 ms on 09-15) and carries no reroute marker, so this
        # is the only thing that tells the swap it came from a rider's finger.
        # See ONBOARD_COMMIT_WINDOW_MS (17.9b).
        self.clear_onboard_ms = 0
        # App relaunches seen inside this trip while the rider was aboard.
        # Counted because 09-15 ride A had two (15:47:11, 15:49:45) and the
        # second is news; only the first is worth one of two ride interrupts.
        self.restart_aboard_ms = 0
        self.restart_aboard_count = 0
        # note-unverifiable fires once a ride: the statement is about the
        # instrumentation, not about the note (17.11).
        self.note_unverifiable_fired = False
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
        # The open wake-lock refusal burst, or None. One launch's ladder of
        # denials is one finding, so the warns are accumulated here and the
        # finding is emitted once the burst goes quiet (or the ride ends) —
        # which is also the only moment the count is known.
        # {"firstMs", "lastMs", "count", "errorName", "errorMessage"}
        self.wake_lock_burst = None
        self.wake_lock_bursts = 0                 # launches denied this ride
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
        self.pending_onboard = {}     # session -> [(t, rule, severity, summary, ctx)]
        # The last position fix per SESSION, not per trip. The onboard flow
        # runs between rides by definition — on 09-13 the bad anchor was built
        # 1.7 s after a STOP_GO_MODE — so a rule that asks "where was the
        # rider" cannot reach for trip.last_fix. {session: (lat, lon, tMs)}.
        self.session_fix = {}
        # session -> {routeId: {"tMs", "vehicles": [(lat, lon, vehicleId,
        # tripId, label)]}}: the last REALTIME_VEHICLE_POSITIONS_RESPONSE the
        # trip sheet polled for a route. This is the feed the app HAD while
        # its boarding prompt said "No buses detected nearby" (15.2), and the
        # only way the daemon can tell an empty search from an empty road.
        self.route_vehicles = {}
        # session -> ms of the last UPDATE_NEARBY_VEHICLES. The matcher's only
        # output; its absence is what says the search never ran.
        self.nearby_vehicles_ms = {}
        # session -> the anchor candidate of the last START_ONBOARD_OPTIMIZE,
        # waiting for the snapshot that carries its coordinates.
        self.onboard_anchor = {}
        # stopId -> (lat, lon), learned from ONBOARD_CANDIDATE_SNAPSHOT
        # requests. The optimize payload names stops and never places them.
        self.stop_coords = {}
        # session -> (ms, type) of the last record only a rider's finger
        # produces (RIDER_GESTURE_TYPES, plus any tap record the client grows).
        # Read by note-unverifiable, which is about the absence of these.
        self.session_last_gesture = {}
        # session -> deque of (startMs, endMs, type) for requests that came
        # back saying they had timed out. Reconstructed backwards from the
        # error, which is the only record that carries the timeout: the window
        # is [errorMs - timeoutMs, errorMs].
        self.session_timeouts = {}
        # session -> the route path of the last record that carried an href.
        # A rider note comes in through the /ride console sidecar and carries
        # none of its own, so this is the only thing that can say which screen
        # the rider was looking at when they typed it (17.11).
        self.session_href = {}
        # Rider notes whose "could anyone check this?" verdict is still
        # pending: the timeout half of note-unverifiable cannot be known until
        # the request that swallowed the tap comes back. See
        # NOTE_EVIDENCE_GRACE_MS and _check_pending_notes.
        self.pending_notes = []
        # The one backlog and its record file. Instance-level so a test never
        # reads or writes the rider's real plan. See PLAN_PATHS.
        self.plan_paths = list(PLAN_PATHS)
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
        # device -> {"version", "native", "atMs", "source"}: the web bundle a
        # phone is running. Fed by the `bundle` session event and by the
        # `bundle` field on a crash beacon. Read by the ride's own bundle
        # stamp and by the bundle_health rule, whose verdict names no version
        # of its own. Persisted, because a phone reports its bundle once per
        # app start and this daemon restarts more often than that.
        self.device_bundles = {}
        # device -> ms of the last boot-crash / withheld-verdict page. The
        # budget these pages are charged to, separate from any trip's.
        self.device_boot_page_ms = {}
        # device -> the page timestamp already followed up with "it is back",
        # so one crash episode produces at most one recovery page.
        self.device_boot_ack = {}
        # The newest boot findings, for current-ride.md. These have no trip to
        # be listed under; the whole point is that the app never got that far.
        self.boot_events = []
        # The router and the query, per session, for _rule_panel_torn_down.
        # Per SESSION and not per Trip on purpose: the settings tab is reached
        # from the search form as often as from a live ride, and two of the
        # three teardowns on 2026-09-04 happened before START_GO_MODE existed
        # — a Trip-scoped rule would have seen one of the three.
        self.panel_route = {}      # session -> {"pathname", "atMs"}
        self.rider_query = {}      # session -> {"atMs", "keys"}
        # session -> an open burst of teardowns; see PANEL_TEARDOWN_QUIET_MS.
        self.panel_teardowns = {}
        # Intake dedup ring (see RECORD_DEDUP_RING).
        self._seen_records = collections.deque()
        self._seen_record_keys = {}
        self.duplicate_records = 0
        self.last_trip_summary = self._load_state()
        self.clock_ms = 0             # replay: max event t; live: wall clock
        self.last_push_ms = 0         # global rate limit (shared w/ fallback)
        # ...which the "report pending" page is now exempt from; it spends
        # REPORT_PAGE_MIN_INTERVAL_MS of its own instead.
        self.last_report_page_ms = 0
        self.push_log = []            # [{tsMs, title, body, sent, kind}]
        # The file this daemon is reading, when it is not today's live one:
        # run_replay points it at the replayed file so the adoption path's
        # look-back reads the stream it is actually processing. None means
        # "whatever current_log_path() says", which is the live answer.
        self.stream_path = None
        # Shared with the Tailer in run_live (see Tailer._note_drain).
        self.stream_drains = collections.deque(maxlen=TAILER_DRAIN_RING)
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
        # Panes the rider has already been paged about for sitting on a
        # permission prompt, and the count of lines that never got typed
        # because of one. Both are for the log and the status file; neither
        # is persisted, because a pane does not outlive the process anyway.
        self._thread_blocked_paged = set()
        self._thread_pushes_undelivered = 0
        self._head_cached = (None, None)   # (head, behind), TTL-cached; never the stamp
        self._head_checked_ms = 0
        # Which of the daemon's OWN files have changed on disk since this
        # process read them. TTL-cached beside the head check, and the only
        # thing that may raise STALE.
        self._source_drift_cached = []
        self._source_drift_checked_ms = 0

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
        # A phone announces its bundle once, at app start, and this daemon is
        # restarted on every commit — so without this a restart mid-evening
        # loses the version every ride report and every withheld verdict is
        # about, until the rider next relaunches the app.
        self.device_bundles = dict(
            (k, v) for k, v in (data.get("deviceBundles") or {}).items()
            if isinstance(k, str) and isinstance(v, dict))
        # ...and the boot-page budget, for the same reason it is a budget at
        # all: a restart that forgets it would page again about the crash it
        # has already paged about.
        self.device_boot_page_ms = dict(
            (k, int(v)) for k, v in (data.get("deviceBootPages") or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float)))
        self.device_boot_ack = dict(
            (k, int(v)) for k, v in (data.get("deviceBootAck") or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float)))
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
                           "endedArrived": sorted(self.ended_arrived)[-32:],
                           # One entry per phone; the rider has one, and a
                           # spare bound keeps a stray device id from growing
                           # the file without limit.
                           "deviceBundles": dict(sorted(
                               self.device_bundles.items(),
                               key=lambda kv: kv[1].get("atMs") or 0)[-8:]),
                           "deviceBootPages": dict(sorted(
                               self.device_boot_page_ms.items(),
                               key=lambda kv: kv[1])[-8:]),
                           "deviceBootAck": dict(sorted(
                               self.device_boot_ack.items(),
                               key=lambda kv: kv[1])[-8:])}, f)
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

        # Two per-session ledgers, fed from every record and stealing none of
        # them: note-unverifiable (17.11) asks what the rider had touched
        # before they typed, and whether a request was hanging while they did.
        # Deliberately NOT branches of the chain below — a `*_ERROR` elif here
        # would shadow whatever rule wants those types next — and deliberately
        # above `elif trip is not None`, because ride B's whole note window
        # (15:53:39 cold start to the 15:53:59 FIND_FEEDS_ERROR) happened two
        # minutes before START_GO_MODE opened a trip.
        self._note_rider_gesture(session, t, kind, typ, obj)
        self._note_request_timeout(session, t, typ, obj)
        self._note_href(session, obj)

        if typ == "START_GO_MODE":
            self._on_start_go_mode(session, t, obj)
        elif typ == "STOP_GO_MODE":
            if trip:
                self._end_trip(trip, t, "stop")
        elif kind in ("boot-error", "boot-rejection"):
            # Keyed on `kind`, because there is no `typ` to key on: the crash
            # beacon carries neither `type` nor `event` (otprr
            # debug-log-boot.js captureBootError), and the line above only
            # falls back to `event` for non-session records. That is why these
            # fell through this whole chain inertly from the moment the client
            # started sending them.
            #
            # And it sits ABOVE `elif trip is not None`, like the rider-note
            # and onboard branches, because a boot crash is by definition
            # outside a ride: the app never got far enough to have one.
            self._rule_boot_crash(session, t, obj, trip)
        elif (typ == "RESUME_GO_MODE"
              or (kind == "session" and obj.get("event") == "resumed-session")):
            # The crash-recovery relaunch (17.7). Above the `trip is not None`
            # chain because a session record has no `typ` for that chain to
            # key on — the same blind spot the bundle branch below was opened
            # for — and because both halves of one relaunch must reach the
            # same rule: they land 3 ms apart and are folded together there.
            self._rule_session_restart_while_aboard(session, t, obj, trip)
        elif kind == "session" and obj.get("event") in ("bundle",
                                                        "bundle_health"):
            # Same blind spot, other half: `typ` is left None for every
            # session record, so the bundle the phone is running and the
            # health gate's verdict on it both arrived and were dropped.
            self._on_session_event(session, t, obj, trip)
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
            if typ == "START_ONBOARD_OPTIMIZE":
                self._note_onboard_anchor(session, t, obj.get("payload"))
        elif typ == "CLEAR_ONBOARD":
            # Half of the onboard picker's commit; START_GO_MODE is the other
            # half, 3 ms later. Above the trip chain for the same reason as the
            # branch above — the flow runs between rides as often as inside one
            # — and stamped on the trip because that is who aboard-swap asks.
            if trip is not None:
                trip.clear_onboard_ms = t
                self._mark_dirty()
        elif typ == "ONBOARD_CANDIDATE_SNAPSHOT":
            # The only record in the stream that places a candidate stop on
            # the map. Same reason it sits here: on 09-13 every one of these
            # arrived with no trip open.
            self._on_candidate_snapshot(session, t, obj.get("payload"), trip)
        elif typ in ("REALTIME_VEHICLE_POSITIONS_RESPONSE",
                     "UPDATE_NEARBY_VEHICLES", "SHOW_BOARDING_PROMPT"):
            # Above the trip chain because the boarding prompt and the feed
            # poll both run outside a ride (the onboard flow taps the same
            # button); the rule itself does nothing without a trip.
            self._on_boarding_evidence(session, t, typ, obj.get("payload"),
                                       trip)
        elif typ == "SET_QUERY_PARAM":
            # Above the `trip is not None` chain for the same reason as the
            # onboard branch: the rider edits the query from the search form
            # as often as from a live ride, and on 2026-09-04 two of the three
            # teardowns this feeds happened before START_GO_MODE.
            self._note_query_param(session, t, obj)
        elif typ == "@@router/LOCATION_CHANGE":
            self._rule_panel_torn_down(session, t, obj, trip)
        elif trip is not None:
            if kind == "console":
                self._rule_console(trip, t, obj)
                self._rule_wake_lock_denied(trip, t, obj)
            elif typ == "UPDATE_POSITION":
                trip.last_pos_ms = max(trip.last_pos_ms, t)
                self._note_session_fix(session, t, obj.get("payload") or {})
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
                self._on_position(trip, t, obj.get("payload") or {})
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
            elif typ == "CONFIRM_VEHICLE":
                pv = obj.get("payload") or {}
                trip.confirm_vehicle = (t, pv.get("vehicleId"),
                                        pv.get("tripId"), pv.get("label"))
            elif typ == "CLEAR_RIDING":
                self._rule_riding_fact_dropped(trip, t)
                trip.riding = None
                self._mark_dirty()
            elif typ == "TRANSITION_LEG":
                trip.confirm_vehicle = None
                self._on_transition_leg(trip, t, obj.get("payload") or {})
            elif typ == "ADD_NOTIFICATION":
                self._on_notification(trip, t, obj.get("payload") or {})
            elif typ == "START_REROUTE":
                self._on_start_reroute(trip, t, obj.get("payload") or {})
            elif typ == "REROUTE_SNAPSHOT":
                self._on_reroute_snapshot(trip, t, obj.get("payload") or {})
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
        elif typ == "UPDATE_POSITION":
            # No trip — but the onboard flow runs between rides and asks where
            # the rider is (onboard-anchor-behind-rider). The fix is recorded
            # per session either way; only the trip bookkeeping above is
            # trip-scoped.
            self._note_session_fix(session, t, obj.get("payload") or {})

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
            trip.round_trip = bool(payload.get("roundTrip"))
            self._note_device_session(trip.device, session)
            self._stamp_trip_bundle(trip)
            self.trips[session] = trip
            self.log.info("trip started: session=%s itinerary=%s" % (
                session, itinerary_one_liner(summary)))
            self._log_stream_window("trip start for %s" % session)
            self._begin_ride_thread(trip, t)
        else:
            # Itinerary replacement mid-trip.
            #
            # Read before it is overwritten: aboard-swap's route-preserving
            # exemption (17.9a) compares the plan going out with the plan
            # coming in, and `trip.itinerary = summary` below is the moment the
            # old one stops existing.
            prev_summary = trip.itinerary
            # The onboard picker's commit, which emits no reroute marker of its
            # own: CLEAR_ONBOARD 3 ms ago means a rider's finger did this
            # (17.9b). Stamped as a rider action before _rule_aboard_swap
            # reads last_rider_action_ms, which is the gate it already has for
            # every other explicit pick.
            if (trip.clear_onboard_ms
                    and 0 <= t - trip.clear_onboard_ms
                    <= ONBOARD_COMMIT_WINDOW_MS):
                trip.last_rider_action_ms = t
                self.log.info(
                    "itinerary swap is a rider onboard pick: session=%s "
                    "CLEAR_ONBOARD %d ms before START_GO_MODE"
                    % (session, t - trip.clear_onboard_ms))
            self._clear_arrival(trip, t, "itinerary swap")
            if payload.get("roundTrip"):
                trip.round_trip = True
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
            self._rule_aboard_swap(trip, t, prev_summary, summary)
            # An applied re-plan. This is the signal the client's own
            # noteReplanAttempt fires on, one step downstream: the daemon
            # cannot see an attempt, only the itinerary it produced — which
            # is the better evidence anyway, since a swap that changed
            # nothing is the fact the rule is about.
            self._note_replan(trip, t, "itinerary-swap")
        self._flush_pending_onboard(trip)
        self._rule_itinerary_backwards(trip, t, summary)
        self._rule_same_route_transfer(trip, t)
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
        # Before anything infers what this ride is, read the stream back and
        # see whether it says so outright. 2026-09-15: it did -- the adopt at
        # 09:26:31.238 happened 18 ms after a START_GO_MODE for the same
        # session that was three lines above it in the same POST batch, and
        # the daemon filed "no START_GO_MODE ... cannot be replayed" anyway.
        #
        # Above _continuation_of on purpose. That rule's own contract is that
        # it is "only reachable ... when the new session arrived with no
        # START_GO_MODE of its own", and an explicit start is the rider asking
        # for a ride in so many words. A session that turns out to have one is
        # not a continuation of anything; it is a trip this daemon should have
        # opened already.
        recovered = self._recover_go_mode_starts(session, t)
        if recovered:
            self._open_from_recovered_start(session, t, obj, p, recovered)
            return
        prior = self._continuation_of(session, t, obj, p)
        if prior is not None:
            self._adopt_continuation(prior, session, t, p)
            return
        trip = Trip(session, t, None, adopted=True)
        trip.device = obj.get("device")
        self._stamp_trip_bundle(trip)
        self.trips[session] = trip
        self.log.info("adopted mid-stream trip for session %s" % session)
        self._log_stream_window("adopt of %s" % session)
        # An adopted trip is a ride in progress — usually the daemon was
        # just restarted under a rider who is still on the bus — so it
        # gets a thread too, marked as adopted in the digest.
        self._begin_ride_thread(trip, t)
        # After the thread exists, so the console hears it: a finding filed
        # before the spawn goes into the ledger and nowhere else.
        self._rule_resumed_trip(trip, t, obj)
        self._on_progress(trip, t, p)
        self._mark_dirty()

    def _open_from_recovered_start(self, session, t, obj, p, recovered):
        """Open the trip off the START_GO_MODE the follower never delivered.

        The difference this makes to the ride, all of it downstream of one
        record the daemon already had on disk: the ride window starts when the
        rider pressed Go rather than at the first tick the daemon happened to
        see (09-15: 09:26:29 instead of 09:26:31, and on a worse miss it would
        be minutes); the itinerary summary exists at all, so the thread's
        kickoff line names the route instead of "itinerary unavailable"; every
        rule that keys off trip.itinerary works; and the ride is replayable,
        so the fixture step of the wrap-up succeeds.

        Each recovered START is fed through the ordinary handler in order, so
        the first opens the trip and any later one lands as the itinerary swap
        it actually was -- no second state machine to keep in step with the
        first.
        """
        self.log.warn(
            "%s had no trip open at %s, but the stream holds %d"
            " START_GO_MODE record(s) for it from %s that this daemon never"
            " processed; opening the trip from them instead of adopting"
            % (session, fmt_hms(t), len(recovered), fmt_hms(recovered[0][0])))
        self._log_stream_window("recovered start for %s" % session)
        for start_ms, start_obj in recovered:
            self._on_start_go_mode(session, start_ms, start_obj)
        trip = self.trips.get(session)
        if trip is None:
            # _on_start_go_mode does not fail, but it is not this function's
            # business to assume so: falling through to the adopt is worse
            # than a log line and no trip.
            self.log.error("recovered start for %s opened no trip" % session)
            return
        trip.last_event_ms = max(trip.last_event_ms, t)
        # Not `resumed-trip`: this ride HAS a start and IS replayable, and the
        # whole cost of 09-15 was a thread spending its wrap-up disproving the
        # opposite. What is worth a finding is the thing that actually went
        # wrong -- the follower handed over none of it.
        self._finding(
            trip, t, "missed-start", "warn",
            "the daemon opened this ride %s late: %d START_GO_MODE record(s)"
            " from %s were in the stream and were never processed"
            % (fmt_ms_span(t - recovered[0][0]), len(recovered),
               fmt_hms(recovered[0][0])),
            {"session": session,
             "device": obj.get("device"),
             "startMs": recovered[0][0],
             "noticedMs": int(t),
             "startsRecovered": [ms for ms, _ in recovered],
             "recoveredFrom": self._stream_path(),
             "replayable": True})
        self._on_progress(trip, t, p)
        self._mark_dirty()

    # -- reading the stream back ------------------------------------------
    #
    # Everything else here is fed by the follower. These two go to the file
    # directly, because the one claim that cannot be made from the follower's
    # output alone is "this is not in the stream".

    def _stream_path(self):
        return self.stream_path or current_log_path()

    def _tail_records(self, max_bytes=ADOPT_START_LOOKBACK_BYTES):
        """The last records of the stream, newest-last. [] if unreadable.

        Shaped after preferences_api._tail_lines: seek back a bounded number
        of bytes, drop the partial line the arbitrary seek lands in, parse
        what is left. The day's file runs to 30 MB (09-15 finished at
        29,941,984 bytes) and nothing on the event path is allowed to read it
        whole.
        """
        path = self._stream_path()
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                chunk = f.read()
        except OSError as exc:
            self.log.warn("could not read back the stream (%s): %r"
                          % (path, exc))
            return []
        lines = chunk.split(b"\n")
        if size > max_bytes and lines:
            lines.pop(0)  # partial head from the arbitrary seek
        out = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def _recover_go_mode_starts(self, session, t):
        """The START_GO_MODE records of the Go Mode run that is live at `t`.

        Returned oldest-first as (tMs, record). Empty when the stream really
        does not hold one — which is the case this daemon was built for (its
        own restart mid-ride, or an app re-mount, both of which emit none) and
        which `resumed-trip` is still right about.

        A STOP_GO_MODE resets the list, because everything before it belongs
        to a trip that is over: on 09-15 the session's records hold
        START 09:24:31, STOP 09:24:44, START 09:24:52, STOP 09:25:13,
        START 09:26:29, START 09:26:31, and the run that was live at the
        09:26:31.238 adopt is the last two. Opening from 09:24:31 would put
        two finished trips inside one ride's window.
        """
        cutoff = t - ADOPT_START_LOOKBACK_MS
        starts = []
        for obj in self._tail_records():
            if obj.get("session") != session:
                continue
            ot = obj.get("t")
            if not isinstance(ot, (int, float)) or ot > t or ot < cutoff:
                continue
            typ = obj.get("type")
            if typ == "STOP_GO_MODE":
                starts = []
            elif typ == "START_GO_MODE":
                starts.append((int(ot), obj))
        starts.sort(key=lambda e: e[0])
        return starts

    def _log_stream_window(self, why):
        """Dump the follower's recent drains. Called when a trip opens.

        This is the record that did not exist on 09-15. A trip opening (or
        being adopted) is the only moment worth spending the lines on, and it
        is exactly the moment at which the next miss will be noticed.
        """
        if not self.stream_drains:
            return
        self.log.info("stream window at %s: %s" % (why, "; ".join(
            "%d line(s) %d->%d t %s..%s" % (
                d.get("lines", 0), d.get("from", 0), d.get("to", 0),
                fmt_hms(d.get("firstT")), fmt_hms(d.get("lastT")))
            for d in self.stream_drains)))

    def _note_device_session(self, device, session):
        """Remember which session ids a phone has been seen under."""
        if not device or not session:
            return
        seen = self.device_sessions.setdefault(device, [])
        if session not in seen:
            seen.append(session)
            del seen[:-16]

    # -- the phone itself: boot crashes and bundle verdicts -----------------
    #
    # Everything else in this file is about a ride. These three are about the
    # app, and they are here because the app failing to start is the one
    # failure a rider cannot be told about by the app.

    def _on_session_event(self, session, t, obj, trip):
        """Session-scoped facts the app reports once per app start."""
        event = obj.get("event")
        if event == "bundle":
            self._note_bundle(obj.get("device"), t, obj.get("version"),
                              obj.get("native"), source="bundle")
        elif event == "bundle_health":
            self._rule_bundle_health(session, t, obj, trip)

    def _note_bundle(self, device, t, version, native=None, source="bundle"):
        """Remember which web bundle a phone is running.

        `{"kind": "session", "event": "bundle", "version": "2026.0902.5",
        "native": "1.0.54"}` — otprr lib/main.js, in the stream since the OTA
        lane shipped and ignored the whole time for the reason above. Worth
        having twice over: a ride report that cannot name the bundle cannot
        say which build a defect belongs to, and a withheld `bundle_health`
        verdict carries only `confirmed` and `reason`, so the version it is
        about has to come from here.

        Deliberately does NOT call _note_device_session. That map is what
        tells `resumed-trip` a re-mount from a daemon restart, and recording a
        session id here — one lands on every single app start — would make
        every first adopted ride after a restart look like an app re-mount.
        """
        if not isinstance(version, str) or not version:
            return
        if device:
            prev = (self.device_bundles.get(device) or {}).get("version")
            self.device_bundles[device] = {
                "version": version,
                "native": native if isinstance(native, str) else None,
                "atMs": int(t), "source": source}
            if prev != version:
                self.log.info("device %s is running bundle %s (native %s, via %s)"
                              % (device, version, native, source))
                self._save_state()
        for trip in self._active_trips():
            if device and trip.device == device:
                self._stamp_trip_bundle(trip)
        self._mark_dirty()

    def _stamp_trip_bundle(self, trip):
        """Record on the ride which bundle the phone was running."""
        info = self.device_bundles.get(trip.device) if trip.device else None
        if not info or not info.get("version"):
            return
        if trip.bundle != info.get("version"):
            self.log.info("trip %s is running bundle %s"
                          % (trip.session, info.get("version")))
        trip.bundle = info.get("version")
        trip.bundle_native = info.get("native")
        self._mark_dirty()

    def _rule_boot_crash(self, session, t, obj, trip):
        """The app threw before it could run. Page, once per phone per 30 min.

        The record (otprr lib/util/debug-log-boot.js): `kind` is `boot-error`
        for a window `error` or `boot-rejection` for an unhandled promise,
        with `message`, `stack`, `source`, `line`, `col`, `bundle`,
        `sinceBootMs` and `storage` (key NAMES and sizes only, never values),
        under the ordinary envelope's `device` / `href` / `ua`. It arrives by
        `sendBeacon`, so unlike everything else in the stream it survives the
        force-quit that follows a white screen.

        Paged out of the phone's own budget rather than a trip's: there is no
        trip to charge, and on the one occasion this has happened the app was
        unopenable for 45 minutes, which is not a thing to spend a ride's two
        interrupts on. See BOOT_PAGE_INTERVAL_MS.
        """
        device = obj.get("device")
        raw_message = obj.get("message") or "(no message)"
        message = one_line(raw_message, BOOT_MESSAGE_MAX)
        bundle = obj.get("bundle")
        if isinstance(bundle, str) and bundle:
            # The crash names the bundle it happened on, which is better
            # evidence than the last one we were told about.
            self._note_bundle(device, t, bundle, source=obj.get("kind"))
            bundle_from = "event"
        else:
            # Legitimately absent: noteBundleVersion is resolved from a
            # promise, so a throw in the first tick genuinely has no bundle
            # yet. Fall back to what the phone last reported, and say so.
            bundle = (self.device_bundles.get(device) or {}).get("version")
            bundle_from = "device" if bundle else None
        href = short_boot_href(obj.get("href"))
        where = obj.get("source")
        if where and obj.get("line") is not None:
            where = "%s:%s" % (where.rsplit("/", 1)[-1], obj.get("line"))
        elif where:
            where = where.rsplit("/", 1)[-1]
        since = obj.get("sinceBootMs")
        summary = ("the app threw during boot on bundle %s: %s"
                   % (bundle or "(unknown)",
                      one_line(raw_message, BOOT_SUMMARY_MESSAGE_MAX)))
        if where:
            summary += " (at %s)" % where
        if isinstance(since, (int, float)):
            # Sub-second in milliseconds: a boot throw is usually a few
            # hundred ms in, and fmt_ms_span rounds every one of those to
            # "0s", which reads as "no data" rather than "immediately".
            summary += " %s into the boot" % (
                "%dms" % since if since < 1000 else fmt_ms_span(since))
        context = {
            "device": device, "session": session, "kind": obj.get("kind"),
            "message": obj.get("message"), "stack": obj.get("stack"),
            "source": obj.get("source"), "line": obj.get("line"),
            "col": obj.get("col"), "bundle": bundle,
            "bundleKnownFrom": bundle_from, "sinceBootMs": since,
            "href": obj.get("href"), "storage": obj.get("storage"),
            "ua": obj.get("ua"),
        }
        finding = self._device_finding(session, device, t, "boot-crash",
                                       "page", summary, context, trip)
        body = ("App crashed on boot on %s: %s" % (bundle, message)
                if bundle else "App crashed on boot: %s" % message)
        if href:
            body += " - %s" % href
        self._page_device(device, t, "boot-crash", body, finding, trip)

    def _rule_bundle_health(self, session, t, obj, trip):
        """The 5 s health gate's verdict on the bundle that just booted.

        `{"kind": "session", "event": "bundle_health", "confirmed": <bool>,
        "reason": "confirmed" | "boot-error" | "not-rendered"}` — otprr
        lib/util/native-updates.ts confirmBundleHealthyWhenStable. It goes
        into the buffered stream always, and ALSO by beacon when it is
        withheld, so the daemon may see the same verdict twice; intake's
        duplicate ring does not collapse those (they carry different entry ids
        on purpose), so the per-device page budget is what keeps it to one
        interrupt.

        A withheld verdict means the shell rolls this bundle back at the next
        launch. It pages on the SAME budget as the crash that usually precedes
        it, deliberately: a boot error and the verdict it produced five
        seconds later are one incident, and the rider should be told once.

        A confirmed verdict is ordinary and lands at `info` — except on a
        phone we paged about within the last hour, where "it is working again"
        is the other half of a page already spent.
        """
        device = obj.get("device")
        confirmed = bool(obj.get("confirmed"))
        reason = obj.get("reason") or ("confirmed" if confirmed else "withheld")
        bundle = (self.device_bundles.get(device) or {}).get("version")
        context = {"device": device, "session": session,
                   "confirmed": confirmed, "reason": reason,
                   "bundle": bundle, "href": obj.get("href")}
        if confirmed:
            self._device_finding(
                session, device, t, "bundle-health", "info",
                "the app confirmed bundle %s healthy (%s)"
                % (bundle or "(unknown)", reason), context, trip)
            self._maybe_page_recovery(device, t, bundle)
            return
        summary = ("the app withheld its health verdict on bundle %s (%s), so"
                   " the shell rolls it back at the next launch"
                   % (bundle or "(unknown)", reason))
        finding = self._device_finding(session, device, t, "bundle-health",
                                       "page", summary, context, trip)
        self._page_device(
            device, t, "bundle-health",
            "Bundle %s not confirmed (%s); it rolls back on relaunch"
            % (bundle or "unknown", reason), finding, trip)

    def _maybe_page_recovery(self, device, t, bundle):
        """One line closing a boot crash we already paged about.

        Two things it must never do (17.21), both measured on 2026-09-15 ride
        A. The crash page went out 15:18:11 and its follow-up was rate-limited
        14 s later, so the ack stayed unset and kept re-arming on every
        `confirmed` verdict for the whole hour. It finally sent on the
        15:47:16 verdict — 29 minutes on, with a ride running — and took the
        global push slot from a `session-restart-while-aboard` page that had
        been buffered at 15:47:11 and was still 10 s inside its coalescing
        window. The rider would have been told "App came back on 2026.0915.1"
        and not "The app restarted while you were on ORANGE Downtown
        Minneapolis", out of a two-page ride budget.

        Not a replay artefact. `now_ms()` in a replay is the event clock, not
        a compressed wall clock, and the day file's own `recv` says the two
        batches reached the sink 3.22 s and 1.05 s after they were written: on
        the real ride the page is buffered at wall 15:47:15.0 and flushes at
        15:47:30.0 while this line sends at 15:47:17.9, 12.1 s ahead of it.
        For the page to survive, the recovery would have to land more than
        PUSH_MIN_INTERVAL_MS (120 s) before a flush that is only
        PAGE_COALESCE_MS (15 s) after the page's own arrival, which no
        spacing allows.

        So: this line is the one push in this file designed to be losable
        ("one more chance at the next launch", below), which makes it the one
        push that must never cost anything else its slot. It ranks below every
        entry in PAGE_RANK — enforced here rather than in that table, which
        only `_buffer_page` reads and which is asserted to hold exactly the
        trip page rules. `boot-crash` and a withheld `bundle-health`
        deliberately keep their instant path: the app being broken outranks
        any ride page.
        """
        if not device:
            return
        paged_ms = self.device_boot_page_ms.get(device)
        if not paged_ms or t - paged_ms > BOOT_RECOVERY_WINDOW_MS:
            return
        if self.device_boot_ack.get(device) == paged_ms:
            return
        body = "App came back on %s" % (bundle or "the current bundle")
        # Collapsed, not deferred: a ride on this phone has already told the
        # rider the app relaunched under them mid-ride, which is the same news
        # said better — it names the bus and gives them something to do. A
        # second line saying the app is back adds nothing a rider looking at a
        # working app does not already know, and the standing copy rule is
        # that a push carries only what they act on. The relaunch has to be
        # AFTER the crash being acknowledged or it says nothing about it.
        restarted = self._restart_paged_since(device, paged_ms)
        if restarted is not None:
            self.log.info(
                "boot-recovery collapsed into %s's relaunch page at %s: the"
                " ride already said the app came back"
                % (restarted.session, fmt_hms(restarted.restart_aboard_ms)))
            self._log_recovery_suppressed(
                t, body, "collapsed-into-session-restart")
            self.device_boot_ack[device] = paged_ms
            self._save_state()
            return
        # ...and if some other ride page is mid-window, wait. The ack is left
        # unset on purpose, so this takes the next launch's verdict instead,
        # which is the fallback the send path below already relies on.
        waiting = self._trip_page_waiting()
        if waiting is not None:
            self.log.info(
                "boot-recovery deferred: %s holds %d page(s) in the coalescing"
                " window until %s, and every ride page outranks this line"
                % (waiting.session, len(waiting.pending_pages),
                   fmt_hms(waiting.pending_until_ms)))
            self._log_recovery_suppressed(t, body, "ride-page-waiting")
            return
        if self._send_push("Ride watch", one_line(body, PUSH_BODY_MAX),
                           kind="boot-recovery"):
            # Only on a send: a recovery page lost to the 120 s rate limit
            # gets one more chance at the next launch, which is the shape of
            # the thing anyway.
            self.device_boot_ack[device] = paged_ms
            self._save_state()

    def _log_recovery_suppressed(self, t, body, why):
        """Record a recovery line that was not sent, so the file still says so.

        _send_push writes a push_log row for everything it refuses; these two
        never reach it, and a decision that leaves no row is a decision the
        wrap-up cannot read.
        """
        self.push_log.append({
            "tsMs": int(t), "title": "Ride watch",
            "body": one_line(body, PUSH_BODY_MAX),
            "sent": False, "kind": "boot-recovery", "suppressed": why})

    def _restart_paged_since(self, device, since_ms):
        """A live ride on this phone that has reported a relaunch since `since_ms`.

        `restart_aboard_ms` is stamped by _rule_session_restart_while_aboard
        whether the page went out or was superseded: either way the rider's
        ride budget has been spent on this relaunch, and the recovery line is
        not the way to spend more of it.
        """
        for trip in self._active_trips():
            if device and trip.device != device:
                continue
            if trip.restart_aboard_ms and trip.restart_aboard_ms >= since_ms:
                return trip
        return None

    def _trip_page_waiting(self):
        """Any live ride holding a page inside its coalescing window.

        Deliberately every ride, not just this phone's: the rate limit is
        global because the rider is one person with one lock screen.
        """
        for trip in self._active_trips():
            if trip.pending_pages:
                return trip
        return None

    def _device_finding(self, session, device, t, rule, severity, summary,
                        context, trip=None, boot_health=True):
        """A finding about the PHONE rather than about a ride.

        The trip-less sibling of _finding. Boot crashes, bundle verdicts and
        a panel torn down from the search form all happen with no trip to
        hang on — the app never reached Go Mode, or
        never reached anything at all — so the record goes into the same
        per-day, per-session ledger _findings_path would have chosen for a
        ride under that session id. If a ride does open under it later, the
        crash that preceded it is already in the file the report reads.

        `boot_health` is what separates the two kinds: only a finding about
        the app failing to START belongs in current-ride.md's boot section.

        When a trip IS live (a crash on a mid-ride re-mount) the finding is
        filed on that trip and pushed to its thread as well — but never into
        the trip's page buffer, because these pay out of their own budget and
        must not be able to supersede, or be superseded by, a ride page.
        """
        finding = {
            "tsMs": int(t), "time": fmt_hms(t), "session": session,
            "device": device, "rule": rule, "severity": severity,
            "summary": summary, "context": context,
        }
        self.all_findings.append(finding)
        if boot_health:
            # `boot_health=False` for a finding that has no trip to hang on
            # but is not about the app failing to START (panel-torn-down is
            # the first): current-ride.md's "App boot health" section would
            # otherwise report a settings screen closing as a boot problem.
            self.boot_events.append(finding)
            del self.boot_events[:-BOOT_EVENT_RING]
        self.log.info("FINDING [%s/%s] %s %s"
                      % (severity, rule, fmt_hms(t), summary))
        if trip is not None:
            trip.findings.append(finding)
            line = "finding [%s] %s: %s" % (severity, rule, summary)
            self._thread_event(trip, t, line)
            self._thread_push(trip, line)
        if severity == "page":
            # Same rule as _finding: persisted once the paging verdict is
            # known, so the record always says whether the rider was told.
            finding["paged"] = "pending"
        else:
            self._persist_device_finding(finding, trip)
        self._mark_dirty()
        return finding

    def _page_device(self, device, t, rule, body, finding, trip=None):
        """Page about the phone, on the phone's own budget.

        Not the trip budget and not the coalescing buffer. There is nothing to
        coalesce against — the app is not running, so nothing else is being
        reported — and nothing to charge, because these fire outside a ride.
        One page per phone per BOOT_PAGE_INTERVAL_MS keeps a relaunch loop
        down to one interrupt. The global 120 s rate limit still applies on
        top, in _send_push, exactly as it does for every other push here.
        """
        body = one_line(body, PUSH_BODY_MAX)
        last = self.device_boot_page_ms.get(device) if device else None
        if last is not None and 0 <= t - last < BOOT_PAGE_INTERVAL_MS:
            self.log.info("page suppressed (one per phone per %dm): %s"
                          % (BOOT_PAGE_INTERVAL_MS // 60000, body))
            self.push_log.append({"tsMs": int(t), "title": "Ride watch",
                                  "body": body, "sent": False, "kind": rule,
                                  "suppressed": "device-budget"})
            sent = False
        else:
            sent = self._send_push("Ride watch", body, kind=rule)
            if sent and device:
                self.device_boot_page_ms[device] = int(t)
                # A fresh crash re-arms the "it came back" follow-up.
                self.device_boot_ack.pop(device, None)
                self._save_state()
        finding["paged"] = sent
        self._persist_device_finding(finding, trip)
        self._mark_dirty()
        return sent

    def _persist_device_finding(self, finding, trip):
        if trip is not None:
            self._persist_finding(trip, finding)
            return
        self._append_finding(
            self._findings_path_for(fmt_date(finding["tsMs"]),
                                    finding["session"]), finding)

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

        And it no longer asserts "no START_GO_MODE" on the follower's word
        alone. _maybe_adopt reads the stream back first
        (_recover_go_mode_starts); a session whose start IS on disk is opened
        from it and files `missed-start` instead, and never reaches here. On
        2026-09-15 this rule told a ride thread that a perfectly replayable
        ride could not be replayed, and the thread spent its whole wrap-up
        window disproving it instead of writing the report.
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
        trip.arrived_source = source
        trip.arrived_never_ended_fired = False
        self.log.info("arrived (%s): session=%s" % (source, trip.session))
        self._thread_event(trip, t, "arrived at destination")
        self._rule_arrived_far_from_destination(trip, t, source)
        self._mark_dirty()

    def _rule_arrived_far_from_destination(self, trip, t, source):
        """"You have arrived" while the app's own tick says they have not. (21.2)

        The distance is not inferred from anything here: the client publishes
        it on every UPDATE_PROGRESS as `distanceToDestination`, and the tick
        that latches the arrival carries it. On 2026-09-21 that tick is
        08:54:10.060 (83.26 m, overallProgress 99.527, currentLegProgress
        94.21) and SET_ARRIVED is 15 ms behind it, so `trip.progress` is the
        right snapshot by construction — the progress branch of _process runs
        before the SET_ARRIVED branch for the same millisecond.

        Warn, never a page. The mechanism is in the client (hasArrivedAtDest-
        ination grants on overallProgress >= 99.5 with only a 120 m veto, so
        the last half-percent of a 14 km trip is the last 80 m of the final
        leg) and the fix is an OTA; what the daemon owes the report is the
        number, because §7 of a ride report otherwise has to reconstruct it by
        hand from the fixes.

        A round trip's outbound arrival is a pause at the stay and is skipped:
        START_GO_MODE carries a `roundTrip` block with the return itinerary
        when that is what this is.
        """
        if trip.arrived_far_fired or trip.round_trip:
            return
        p = trip.progress or {}
        dist = p.get("distanceToDestination")
        if not isinstance(dist, (int, float)) or dist <= ARRIVAL_RADIUS_M:
            return
        trip.arrived_far_fired = True
        self._finding(
            trip, t, "arrived-far-from-destination", "warn",
            "arrival latched %.0f m from the destination (the app's own"
            " radius is %.0f m; overallProgress %s, currentLegProgress %s)"
            % (dist, ARRIVAL_RADIUS_M,
               # Not fmt_pct: the whole mechanism lives in the last half a
               # percent of overall progress, and "100%" hides it.
               ("%.2f%%" % p["overallProgress"])
               if isinstance(p.get("overallProgress"), (int, float)) else "?",
               fmt_pct(p.get("currentLegProgress"))),
            {"distanceToDestinationM": round(float(dist), 1),
             "arrivalRadiusM": ARRIVAL_RADIUS_M,
             "overallProgressPct": p.get("overallProgress"),
             "legProgressPct": p.get("currentLegProgress"),
             "legIndex": p.get("currentLegIndex"),
             "arrivedSource": source})

    def _rule_arrived_never_ended(self, trip, t, ending=None):
        """The rider arrived and the app never closed the trip. (18.3b)

        The client auto-ends AUTO_END_AFTER_ARRIVAL_MS (3 min) after arrival.
        When it does not, this daemon closes the trip itself at ARRIVED_END_MS
        (5 min) and writes `endReason: arrived` — which reads exactly like the
        app having ended it. That is what hid this for a month: on 2026-09-17
        the app's STOP_GO_MODE came at 21:56:03, 14m32s after SET_ARRIVED at
        21:41:31, and the ride's own report says the ride ended on arrival.

        So the rule runs from TWO places and is latched to fire once:

        - check_timers, ABOVE the ARRIVED_END_MS close and at a threshold
          thirty seconds — six live ticks — below it, so our own close cannot
          pre-empt it. This is the path that fires on a live ride.
        - _end_trip, for the tail cases the timer cannot reach: a replay whose
          clock only advances on events, a stream that falls silent, a trip
          closed by the 15-minute timeout. Reaching _end_trip with an arrival
          latched and a reason that is not `stop` IS the proof — STOP_GO_MODE
          is the only thing that ends a trip with reason `stop`.

        The finding says whether fixes are still arriving, because the two
        cases have different fixes and only the stream can tell them apart:
        no fixes means the client's dwell timer is starved (it lives in the
        arrived branch of handlePositionUpdate and is therefore tick-driven,
        which is 13.5); fixes still arriving means the timer is running and
        failing, which is a different bug in a different place.
        """
        if trip.arrived_ms is None or trip.arrived_never_ended_fired:
            return
        if ending == "stop":
            return                      # STOP_GO_MODE: the client did close it
        open_ms = t - trip.arrived_ms
        if open_ms < ARRIVED_NEVER_ENDED_MS:
            return
        trip.arrived_never_ended_fired = True
        since_fix_ms = t - trip.last_pos_ms
        # "Still arriving" means inside the gps-gap threshold: a fix a minute
        # old is not a stream, it is the last thing the phone said.
        fixes_live = since_fix_ms <= GPS_GAP_MS
        self._finding(
            trip, t, "arrived-never-ended", "warn",
            "arrived %s and the app never ended the trip (%dm%02ds open;"
            " %s)" % (fmt_hms(trip.arrived_ms), open_ms // 60000,
                      (open_ms // 1000) % 60,
                      ("position fixes still arriving, last %ds ago"
                       % (since_fix_ms // 1000)) if fixes_live
                      else ("no position fix for %ds — the client's dwell"
                            " timer is tick-driven and has nothing to tick"
                            " on (13.5)" % (since_fix_ms // 1000))),
            {"arrivedMs": trip.arrived_ms,
             "arrivedSource": trip.arrived_source,
             "openMs": open_ms,
             "thresholdMs": ARRIVED_NEVER_ENDED_MS,
             "clientAutoEndMs": 3 * 60 * 1000,   # otprr AUTO_END_AFTER_ARRIVAL_MS
             "lastFixMs": trip.last_pos_ms,
             "msSinceLastFix": since_fix_ms,
             "positionFixesStillArriving": fixes_live,
             # Named so a report cannot mistake our close for the app's.
             "watcherClosedAtMs": (trip.arrived_ms + ARRIVED_END_MS),
             "endedBy": ending})

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
        trip.arrived_source = None
        trip.arrived_never_ended_fired = False
        trip.arrived_far_fired = False
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
        for (ts, rule, severity, summary, ctx) in self.pending_onboard.pop(
                trip.session, []):
            if rule == "note-unverifiable":
                # Held before this trip existed, so the once-a-ride latch could
                # not be set then. Set it now, or ride B's 15:53:50 note and
                # its 15:57:02 note would both land on the 15:56 trip.
                trip.note_unverifiable_fired = True
            self._finding(trip, ts, rule, severity, summary, ctx)

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
            self._hold_onboard_finding(session, None, t,
                                       "stale-alight-candidate", "warn",
                                       summary, ctx)

    # -- the 2026-09-13 ride: an app that could not see the train under it ---

    def _note_session_fix(self, session, t, payload):
        """Remember where the rider was, per session id.

        Called from both sides of the trip chain on purpose: on 09-13 the fix
        that convicts the Union Depot anchor (11:38:35.033) arrived while a
        trip was open and was read 3.3 s later with none open.
        """
        coords = (payload or {}).get("coords")
        if not isinstance(coords, dict):
            return
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return
        if len(self.session_fix) > SESSION_CACHE_MAX and \
                session not in self.session_fix:
            self._prune_session_caches()
        prev = self.session_fix.get(session)
        if prev is not None and prev[2] > t:
            # A phone coming back onto the network replays buffered fixes,
            # each stamped with its own old time (the 8/27 gps-gap storm).
            # The newest fix is the one a rule may reason from.
            return
        # ...and the fix's own ground speed, which is the number the client
        # feeds speedAdjustedRadius (`userPos.coords.speed`,
        # lib/actions/go-mode.ts:6102). It is the same float UPDATE_PROGRESS
        # republishes as riderSpeedMps — checked tick for tick across the
        # 09-13 ride — but reading it here means the radius is rebuilt from
        # the record the app itself used, and works on a stream with no
        # progress tick beside the fix.
        speed = coords.get("speed")
        self.session_fix[session] = (
            float(lat), float(lon), int(t),
            float(speed) if isinstance(speed, (int, float)) and speed > 0
            else None)

    def _prune_session_caches(self):
        """Keep the per-session boarding caches to the newest few sessions.

        Every one of these is keyed by a session id the app mints fresh on each
        mount, and the daemon is meant to run for days. The fixes carry their
        own timestamps, so "newest" is exact; the other three follow the fix,
        because a session with no fix in memory can be judged by none of them.
        """
        keep = set(sorted(self.session_fix,
                          key=lambda k: self.session_fix[k][2]
                          )[-SESSION_CACHE_KEEP:])
        self.session_fix = dict((k, v) for k, v in self.session_fix.items()
                                if k in keep)
        for cache in (self.route_vehicles, self.nearby_vehicles_ms,
                      self.onboard_anchor, self.session_last_gesture,
                      self.session_timeouts, self.session_href):
            for key in [k for k in cache if k not in keep]:
                del cache[key]

    def _hold_onboard_finding(self, session, trip, ts, rule, severity,
                              summary, ctx):
        """File an onboard-flow finding, or hold it for the trip that follows.

        The flow runs between rides — before the first START_GO_MODE (8/9) or,
        on 09-13, in the seconds after a STOP — so half of these have no trip
        to hang on. One held finding per rule per session: two identical lines
        say nothing the first did, but two different rules are two findings.
        """
        if trip is not None:
            self._finding(trip, ts, rule, severity, summary, ctx)
            return
        held = self.pending_onboard.setdefault(session, [])
        if any(h[1] == rule for h in held):
            return
        held.append((ts, rule, severity, summary, ctx))
        self.log.info("held onboard finding for session %s: %s"
                      % (session, summary))

    def _note_onboard_anchor(self, session, t, payload):
        """Record the candidate list's anchor and try to place it.

        The anchor is the FIRST candidate, in list order: that is the stop
        `findAnchorIndex` decided the rider was at. Its coordinates are not in
        this record, so unless some earlier snapshot has already placed the
        stop this waits for the one that will.
        """
        candidates = (payload or {}).get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return
        first = candidates[0]
        if not isinstance(first, dict):
            return
        fix = self.session_fix.get(session)
        self.onboard_anchor[session] = {
            "tMs": int(t),
            "stopId": first.get("stopId"),
            "stopName": first.get("stopName"),
            "busArrivalEpoch": first.get("busArrivalEpoch"),
            "realtime": first.get("realtime"),
            "candidates": len(candidates),
            "fix": fix,
            # Every candidate AFTER the anchor, in list order — which is trip
            # order, which is the only thing in this stream that can say
            # whether the anchor is ahead of the rider or behind them (17.9c).
            # Coordinates arrive later, with each candidate's snapshot; these
            # are the joins that will claim them.
            "rest": [{"stopId": c.get("stopId"),
                      "stopName": c.get("stopName"),
                      "busArrivalEpoch": c.get("busArrivalEpoch")}
                     for c in candidates[1:] if isinstance(c, dict)],
        }
        self._check_onboard_anchor(session, None)

    def _on_candidate_snapshot(self, session, t, payload, trip):
        """Learn where a candidate stop is, then judge the pending anchor.

        `request.from` is the stop the plan was asked from, and
        `request.busArrivalEpoch` is the same float the candidate carried, so
        the two records join exactly. The name is the fallback join for a
        stream where the epoch was rounded away.
        """
        req = (payload or {}).get("request")
        if not isinstance(req, dict):
            return
        frm = req.get("from")
        if not isinstance(frm, dict):
            return
        lat, lon = frm.get("lat"), frm.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return
        pending = self.onboard_anchor.get(session)
        if not pending:
            return
        coords = (float(lat), float(lon))

        def joins(cand):
            if (cand.get("busArrivalEpoch") is not None
                    and req.get("busArrivalEpoch") == cand["busArrivalEpoch"]):
                return True
            return (bool(cand.get("stopName"))
                    and frm.get("name") == cand["stopName"])

        # Every candidate is placed, not only the anchor. The direction test
        # (17.9c) asks where the stops AFTER the anchor are, and until this
        # change a snapshot that did not join the anchor was thrown away — so
        # the rest of the list had no coordinates anywhere in the daemon.
        if joins(pending):
            if pending.get("stopId"):
                self.stop_coords[pending["stopId"]] = coords
            pending["coords"] = coords
            self._check_onboard_anchor(session, trip, detected_ms=t)
            return
        for cand in pending.get("rest") or []:
            if not joins(cand):
                continue
            if cand.get("stopId"):
                self.stop_coords[cand["stopId"]] = coords
            cand["coords"] = coords
            # A later candidate can be the record that decides the anchor, so
            # the check runs on it too. Both 09-13 and 09-15 resolved this way:
            # the snapshots arrive in whatever order the plans came back, and
            # on 09-13 Capitol / Rice St (11:38:44.885) beat Union Depot
            # (11:38:45.234) by 349 ms.
            self._check_onboard_anchor(session, trip, detected_ms=t)
            return

    def _check_onboard_anchor(self, session, trip, detected_ms=None):
        """onboard-anchor-behind-rider: the alight list built from elsewhere.

        Fires once per optimize, stamped at the optimize it is about rather
        than at the snapshot that happened to resolve it — the defect is the
        list the rider was shown, and `detectedMs` in the context says when
        the daemon could first prove it.
        """
        pending = self.onboard_anchor.get(session)
        if not pending or pending.get("fired"):
            return
        coords = pending.get("coords")
        if coords is None and pending.get("stopId"):
            coords = self.stop_coords.get(pending["stopId"])
        fix = pending.get("fix")
        if coords is None or fix is None:
            return
        age = pending["tMs"] - fix[2]
        if age > ONBOARD_ANCHOR_FIX_MAX_AGE_MS:
            # Too long since the rider was last seen to say the anchor is
            # behind them rather than the daemon being behind the rider.
            pending["fired"] = True
            return
        dist = meters_between((fix[0], fix[1]), coords)
        if dist <= ONBOARD_ANCHOR_FAR_M:
            return
        # Far is not behind. The direction test: is any stop further down the
        # trip nearer the rider than this one? If it is, the list starts behind
        # the rider and walks back toward them; if every later stop is further
        # out, the anchor is simply the next stop ahead on a long leg and the
        # list is right. Fails closed — an unplaced tail convicts nobody.
        # See ONBOARD_ANCHOR_AHEAD_MARGIN_M for both recorded flows.
        nearer = None
        for cand in pending.get("rest") or []:
            c = cand.get("coords")
            if c is None and cand.get("stopId"):
                c = self.stop_coords.get(cand["stopId"])
            if c is None:
                continue
            d = meters_between((fix[0], fix[1]), c)
            if nearer is None or d < nearer[0]:
                nearer = (d, cand.get("stopName") or cand.get("stopId"))
        if nearer is None:
            return                        # tail not placed yet: wait, or drop
        if nearer[0] + ONBOARD_ANCHOR_AHEAD_MARGIN_M >= dist:
            # Deliberately NOT latched. Snapshots come back in whatever order
            # the plans finish, not in list order — on 09-13 Capitol / Rice St
            # beat Union Depot by 349 ms and Prospect Park trailed by five
            # seconds — so "no later candidate is nearer YET" is not a verdict.
            # More coordinates can only make `nearer` smaller, so the decision
            # is monotone toward firing and re-running it is always safe.
            if not pending.get("aheadLogged"):
                pending["aheadLogged"] = True
                self.log.info(
                    "onboard anchor %s is %.0fm out but the nearest placed"
                    " candidate after it (%s) is %.0fm: the list runs away"
                    " from the rider, so the anchor is ahead. Not filing."
                    % (pending.get("stopName") or pending.get("stopId"),
                       dist, nearer[1], nearer[0]))
            return
        pending["fired"] = True
        name = pending.get("stopName") or pending.get("stopId") or "a stop"
        summary = ("onboard alight list anchored at %s, %.1f km from the "
                   "rider's last fix %s earlier, with %s only %.1f km away "
                   "further down the trip — the candidates are built from the "
                   "wrong end of the line"
                   % (name, dist / 1000.0, fmt_ms_span(age),
                      nearer[1], nearer[0] / 1000.0))
        ctx = {"stopId": pending.get("stopId"), "stopName": name,
               "distanceM": round(dist, 1),
               "candidates": pending.get("candidates"),
               "realtime": pending.get("realtime"),
               "fixMs": fix[2], "fixAgeMs": int(age),
               "fix": [fix[0], fix[1]], "anchor": [coords[0], coords[1]],
               # The direction test's own evidence: the nearest stop AFTER the
               # anchor, which is what makes "behind" a measurement rather
               # than an assumption (17.9c).
               "nearestLaterStop": nearer[1],
               "nearestLaterDistanceM": round(nearer[0], 1),
               "aheadMarginM": ONBOARD_ANCHOR_AHEAD_MARGIN_M,
               "optimizeMs": pending["tMs"]}
        if detected_ms is not None:
            ctx["detectedMs"] = int(detected_ms)
        self._hold_onboard_finding(session, trip, pending["tMs"],
                                   "onboard-anchor-behind-rider", "warn",
                                   summary, ctx)

    def _on_boarding_evidence(self, session, t, typ, payload, trip):
        """The three records boarding-prompt-empty reads."""
        if typ == "UPDATE_NEARBY_VEHICLES":
            self.nearby_vehicles_ms[session] = int(t)
            return
        if typ == "REALTIME_VEHICLE_POSITIONS_RESPONSE":
            route = (payload or {}).get("routeId")
            vehicles = (payload or {}).get("vehicles")
            if not route or not isinstance(vehicles, list):
                return
            kept = []
            for v in vehicles:
                if not isinstance(v, dict):
                    continue
                lat, lon = v.get("lat"), v.get("lon")
                if not isinstance(lat, (int, float)) or \
                        not isinstance(lon, (int, float)):
                    continue
                kept.append({"lat": float(lat), "lon": float(lon),
                             "vehicleId": v.get("vehicleId"),
                             "tripId": v.get("tripId"),
                             "label": v.get("label")})
            if not kept:
                return
            feeds = self.route_vehicles.setdefault(session, {})
            feeds[route] = {"tMs": int(t), "vehicles": kept}
            # One trip sheet polls one or two routes; a session that has seen
            # more than this has moved on from whatever the oldest was.
            for stale in sorted(feeds, key=lambda r: feeds[r]["tMs"]
                                )[:-ROUTE_FEED_KEEP]:
                del feeds[stale]
            return
        self._rule_boarding_prompt_empty(session, t, trip)

    def _rule_boarding_prompt_empty(self, session, t, trip):
        """"I'm on the bus" searched nothing while the feed held the bus.

        INFO, and once per ride. The rider is looking at the screen — they
        just tapped the button — so it asks nothing of them in the next
        minute; what it is for is the report, where on 09-13 the whole machine
        record of this defect was the rider typing it out by hand at 11:37:11.
        """
        if trip is None or trip.boarding_prompt_fired:
            return
        last_nearby = self.nearby_vehicles_ms.get(session, 0)
        if last_nearby and (t - last_nearby) <= BOARDING_PROMPT_NEARBY_WINDOW_MS:
            # The matcher did run: whatever the prompt showed, it showed the
            # result of a search. (11:37:38 and 11:39:44 on 09-13, both from
            # the onboard flow, which runs its own.)
            return
        fix = self.session_fix.get(session)
        if fix is None:
            return
        speed = fix[3]
        if speed is None and trip.progress and isinstance(
                trip.progress.get("riderSpeedMps"), (int, float)):
            speed = trip.progress["riderSpeedMps"]
        speed = max(0.0, float(speed or 0.0))
        # speedAdjustedRadius(200, speed), cap included (vehicle-matching.ts
        # :133-139, FEED_LAG_SECONDS 45, MAX_ADJUSTED_RADIUS_METERS 2500).
        radius = min(BOARDING_PROMPT_BASE_RADIUS_M
                     + BOARDING_PROMPT_SPEED_SECONDS * speed,
                     BOARDING_PROMPT_MAX_RADIUS_M)
        feeds = self.route_vehicles.get(session) or {}
        best = None
        for leg in (trip.itinerary or {}).get("legs") or []:
            if not leg.get("transit"):
                continue
            feed = feeds.get(leg.get("routeId"))
            if not feed or (t - feed["tMs"]) > BOARDING_PROMPT_FEED_MAX_AGE_MS:
                continue
            for v in feed["vehicles"]:
                dist = meters_between((fix[0], fix[1]), (v["lat"], v["lon"]))
                if best is None or dist < best[0]:
                    best = (dist, v, leg, feed)
        if best is None or best[0] > radius:
            return
        dist, vehicle, leg, feed = best
        trip.boarding_prompt_fired = True
        label = vehicle.get("label") or vehicle.get("vehicleId") or "a vehicle"
        summary = ("boarding prompt with no vehicle search in the %ds before "
                   "it, while the %s feed put %s %.0fm away (inside the %.0fm "
                   "the matcher would have used)"
                   % (BOARDING_PROMPT_NEARBY_WINDOW_MS / 1000,
                      leg.get("route") or leg.get("routeId") or "route",
                      label, dist, radius))
        self._finding(trip, t, "boarding-prompt-empty", "info", summary, {
            "routeId": leg.get("routeId"),
            "route": leg.get("route"),
            "vehicleId": vehicle.get("vehicleId"),
            "vehicleTripId": vehicle.get("tripId"),
            "distanceM": round(dist, 1),
            "radiusM": round(radius, 1),
            "riderSpeedMps": round(speed, 2),
            "feedMs": feed["tMs"],
            "feedAgeMs": int(t - feed["tMs"]),
            "fixMs": fix[2],
            "lastNearbyMs": last_nearby or None,
        })

    def _rule_access_leg_transit_speed(self, trip, t, p):
        """A bike leg doing 15 m/s: the rider is on a vehicle, silently.

        WARN, once per leg. The streak is kept on the leg index and the riding
        fact, not on the itinerary identity — the 09-13 ride swapped its
        itinerary three times inside the same run of speed (11:35:52,
        11:36:18, 11:36:44), each time re-planning the same bike leg 0, and a
        streak reset on swap would have watched the rider do 21 m/s and said
        nothing.
        """
        speed = p.get("riderSpeedMps")
        leg = p.get("currentLegIndex")
        if (not isinstance(speed, (int, float))
                or speed < ACCESS_TRANSIT_SPEED_MPS
                or trip.riding is not None
                # False = the itinerary says this leg is WALK/BICYCLE. None is
                # "cannot tell" (summarized payload, adopted trip) and is not
                # evidence of anything.
                or self._leg_is_transit(trip, leg) is not False):
            trip.fast_access = None
            return
        run = trip.fast_access
        if run is None or run["leg"] != leg:
            run = trip.fast_access = {"fromMs": int(t), "leg": leg,
                                      "minMps": float(speed),
                                      "maxMps": float(speed), "ticks": 1}
            return
        run["ticks"] += 1
        run["minMps"] = min(run["minMps"], float(speed))
        run["maxMps"] = max(run["maxMps"], float(speed))
        held = t - run["fromMs"]
        if held < ACCESS_TRANSIT_SPEED_MS or leg in trip.fast_access_legs:
            return
        trip.fast_access_legs.add(leg)
        legs = (trip.itinerary or {}).get("legs") or []
        mode = (legs[leg].get("mode")
                if isinstance(leg, int) and 0 <= leg < len(legs) else None)
        summary = ("leg %s (%s) has been doing %.1f-%.1f m/s for %s with no "
                   "riding fact — the rider is on a vehicle the app has not "
                   "noticed" % (leg, mode or "access", run["minMps"],
                                run["maxMps"], fmt_ms_span(held)))
        self._finding(trip, t, "access-leg-transit-speed", "warn", summary, {
            "legIndex": leg, "legMode": mode,
            "sinceMs": run["fromMs"], "heldMs": int(held),
            "ticks": run["ticks"],
            "minMps": round(run["minMps"], 2),
            "maxMps": round(run["maxMps"], 2),
            "thresholdMps": ACCESS_TRANSIT_SPEED_MPS,
        })

    def _rule_same_route_transfer(self, trip, t):
        """Two consecutive legs on one route, two different trips.

        The rider is told to get off their own vehicle and wait on the
        platform for the next one of the same route. It is never a transfer
        and the ranker cannot see it (15.4). WARN: by the time the itinerary
        is installed the rider has already chosen it, and the fix is in the
        app, not in anything they can do in the next minute.
        """
        legs = (trip.itinerary or {}).get("legs") or []
        for i in range(len(legs) - 1):
            a, b = legs[i], legs[i + 1]
            if not (a.get("transit") and b.get("transit")):
                continue
            route = a.get("routeId")
            if not route or route != b.get("routeId"):
                continue
            if not a.get("tripId") or not b.get("tripId") \
                    or a["tripId"] == b["tripId"]:
                # Same trip across two legs is the app splitting one ride, not
                # a transfer at all.
                continue
            key = (route, a["tripId"], b["tripId"])
            if key in trip.same_route_pairs:
                continue
            trip.same_route_pairs.add(key)
            wait = None
            if isinstance(a.get("endTime"), (int, float)) and \
                    isinstance(b.get("startTime"), (int, float)):
                wait = int(b["startTime"] - a["endTime"])
            summary = ("itinerary transfers %s to itself at %s: trip %s then "
                       "trip %s%s" % (a.get("route") or route,
                                      b.get("from") or "a stop",
                                      a["tripId"], b["tripId"],
                                      (" — %s on the platform"
                                       % fmt_ms_span(wait)) if wait else ""))
            self._finding(trip, t, "same-route-transfer", "warn", summary, {
                "routeId": route, "route": a.get("route"),
                "fromTripId": a["tripId"], "toTripId": b["tripId"],
                "atStop": b.get("from"), "legIndex": i,
                "waitMs": wait,
                "alightMs": a.get("endTime"), "boardMs": b.get("startTime"),
            })

    def _end_trip(self, trip, t, reason):
        # Whole-leg verdicts, before anything counts the findings: a rule whose
        # question is "did this ever happen across the leg?" can only be
        # answered once the ride is over, and the report request quotes
        # len(trip.findings).
        self._rule_vehicle_match_never(trip, t)
        # ...and the arrival that the app never closed. Reaching here with an
        # arrival latched and a reason other than `stop` is itself the evidence
        # (18.3b); in replay this is usually the path that fires, because the
        # clock only advances on events.
        self._rule_arrived_never_ended(trip, t, ending=reason)
        # ...and a refusal burst still inside its quiet window: the ride ending
        # is the end of that launch by definition.
        self._flush_wake_lock(trip, t, force=True)
        # ...and any panel the rider was fighting with. Before the deletion
        # loop below, so the burst is still filed on this ride rather than
        # landing trip-less in the ledger after the report request quoted
        # len(trip.findings). Every session key this trip answers to, because
        # an adopted continuation carries the burst under its own id.
        for key in [s for s, tr in self.trips.items() if tr is trip]:
            self._flush_panel_teardown(key, t, force=True)
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
            # "report FIRST" is in the typed line, not only in the sysprompt,
            # because the sysprompt is read once at spawn and this line lands
            # an hour later on a thread in the middle of something. 09-15 ride
            # 1: the thread took the wrap-up at 09:30:41 and spent it auditing
            # the daemon's (wrong) `resumed-trip` finding, stalled on a
            # permission prompt at 09:31:21, and the report was never written.
            # An audit that dies with the pane costs the ride its record; one
            # that runs after the report is written costs nothing.
            # ...then the promotion, which is what the ride is FOR, and the
            # line says so because before 2026-09-17 the pane went two minutes
            # after the report file appeared and two rides' findings reached no
            # backlog (15.8). Kept to a clause: THREAD_LINE_MAX is 400 and it
            # truncates from the right, which would take the request path with
            # it. The long form is sysprompt step 3.
            line += (" — wrap-up now: WRITE THE REPORT FIRST, then PROMOTE its"
                     " real bugs to the backlog (console held %d min for that),"
                     " investigate anything else after; request: %s"
                     % (PROMOTION_DEADLINE_MS // 60000, req_path))
        self._thread_event(trip, t, line)
        # The one line that must land. Everything else is a milestone the
        # digest repeats anyway; this one is the whole wrap-up, so it waits
        # for the pane rather than being typed over whatever is on it.
        self._thread_push(trip, line,
                          hold_ms=(THREAD_PUSH_WRAP_UP_HOLD_MS if req_path
                                   else None))
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
            # The backlog as it stood when the promise was made. A wrap-up is
            # not done until this has changed; see PROMOTION_DEADLINE_MS and
            # _check_report_deadlines. Digested rather than stat'ed because an
            # mtime can be bumped by anything, and because the digest is the
            # one comparison that survives a daemon restart inside the window.
            "planDigests": self._plan_digests(),
            "requestPath": self._report_request_path(trip),
            # Which pane was asked. _kill_previous_threads reads this: the
            # next ride's thread must not kill the one still writing.
            "tmux": (trip.thread or {}).get("tmux"),
        })
        self.log.info("wrap-up expected at %s by %s" % (path, fmt_hms(due)))
        self._save_state()

    def _plan_digests(self):
        """sha256 of each plan file, or None where there is no file.

        None is a real value and is compared like any other: a backlog that
        did not exist and now does has moved.
        """
        out = {}
        for path in self.plan_paths:
            try:
                with open(path, "rb") as f:
                    out[path] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                out[path] = None
        return out

    def _plans_moved(self, entry):
        """Has the backlog changed since this wrap-up was asked for?

        The whole of 15.8's second mechanism. Either file counts: a wrap-up
        whose findings all dedupe onto existing rows edits the backlog, and one
        that also closes a row moves it into the record. Both are the promotion
        step doing its job.
        """
        before = entry.get("planDigests")
        if not isinstance(before, dict) or not before:
            # Nothing was recorded (an entry from a daemon older than this
            # code, restored out of state.json). There is no baseline to
            # compare against, so the gate cannot be applied and the old
            # behaviour — the report file is the whole test — stands.
            return None
        after = self._plan_digests()
        for path, digest in before.items():
            if after.get(path) != digest:
                return path
        return False

    def _check_report_deadlines(self, now):
        """Has each promised wrap-up appeared, and has it been promoted?

        Deliberately checks files rather than the thread: the question the
        rider cares about is whether the record exists, and a pane that looks
        alive has already been shown to prove nothing. reportPath is the exact
        string handed to the thread in the request file, so this cannot drift
        into watching for a name nobody was asked to write.

        Two gates, in series, because the first one alone was 15.8's second
        mechanism. Until 2026-09-17 this method took `os.path.exists(reportPath)`
        as the wrap-up being over and scheduled the reap in the same tick, so
        the promotion step — which both prompts put AFTER the report, and which
        the daemon's own wrap-up line puts after it too ("WRITE THE REPORT
        FIRST") — had THREAD_REAP_GRACE_MS to run in: 120 seconds. On 09-15
        ng2uqc's report landed 15:53:04 and the pane went at 15:55:05;
        8lyyq1's landed ~16:00 and went the same way; neither plan file was
        touched after 13:42. Eleven rows' worth of evidence sat unpromoted for
        two days, the eighth miss in this family (12.4).

        So: report exists -> the promotion window opens rather than closes.
        See PROMOTION_DEADLINE_MS for the terminating condition, which is the
        part that keeps a pane from living forever.
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
                if entry.get("reportLandedMs") is None:
                    entry["reportLandedMs"] = int(now)
                    entry["realBugs"] = self._report_real_bugs(path)
                    changed = True
                    self.log.info(
                        "wrap-up landed for %s: %s (%s real bug(s) named)"
                        % (entry.get("session"), path, entry["realBugs"]))
                if self._settle_promotion(entry, now):
                    changed = True
                    continue
                keep.append(entry)
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

    def _report_real_bugs(self, path):
        """How many findings this report calls real bugs. 0 = nothing to promote.

        Section headings and the triage table, then the whole file if neither
        carries a verdict at all. Both 09-15 reports put it in a heading — `##
        1. Alight ranking is scored on a phantom arrival time — REAL BUG` and
        `## 1. "Just viewing switched..." — **real-bug**` — as does the
        prompt's own per-finding template.

        The table scan is 18.5. `report-prompt.md` also asks for a triage
        table, and 2026-09-17's ride 1 put its verdicts ONLY there — three rows
        of `| 3 | 18:28:04 | daemon ... | **real-bug** (rule correct) | ... |`
        with no verdict in any heading. The heading scan found 0, the arrow
        scan found 0, and the fallback returned 1: the 18:44:09 page told the
        rider "1 real bug(s)" about a report naming three. The gate itself
        behaved (any non-zero count holds the console), but the count is the
        only number the rider is given and it was a third of the truth.

        A cell counts only when the verdict STARTS it, bold markers and a
        trailing qualifier allowed — `**real-bug** (UX)` yes, `... already open
        as a real-bug on 17.9` no. That is what keeps the "what decided it"
        column from voting, and the row is counted once however many of its
        cells match. The larger of the two scans wins rather than their sum, so
        a report that carries its verdicts in BOTH places is not counted twice.

        The whole-file fallback stays exactly as it was: a report with an
        unexpected layout must be judged as having something to promote rather
        than nothing, because the failure this gate must never have is letting
        a real report out of the window silently.
        """
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as exc:
            self.log.error("could not read %s to count real bugs: %r"
                           % (path, exc))
            return 0
        heads = [ln for ln in lines if ln.lstrip().startswith("#")]
        n = sum(1 for ln in heads if REAL_BUG_RE.search(ln))
        n = max(n, sum(1 for ln in lines if self._is_real_bug_table_row(ln)))
        if n:
            return n
        # Neither headings nor the table name a verdict. Count the arrow form
        # the template uses anywhere, then fall back to the bare phrase.
        arrows = [ln for ln in lines if "->" in ln and REAL_BUG_RE.search(ln)]
        if arrows:
            return len(arrows)
        return 1 if any(REAL_BUG_RE.search(ln) for ln in lines) else 0

    @staticmethod
    def _is_real_bug_table_row(line):
        """One Markdown table row whose verdict cell says real-bug. (18.5)

        Three pipes minimum, so the `|---|---|` separator and a one-cell line
        cannot qualify, and the match must begin the cell — see
        _report_real_bugs for why.
        """
        row = line.strip()
        if not row.startswith("|") or row.count("|") < 3:
            return False
        cells = [c.strip() for c in row.strip("|").split("|")]
        return any(VERDICT_CELL_RE.match(c) for c in cells)

    def _settle_promotion(self, entry, now):
        """Is this wrap-up over? True when the deadline entry can be dropped.

        Three ways out, and no fourth — this is PROMOTION_DEADLINE_MS's
        terminating condition in code:

        1. The report named no real bugs. There is nothing to promote, so the
           wrap-up is complete the moment the file exists and the pane is
           reaped exactly as it was before this gate existed. A clean ride must
           never hold a console open, and a report the agent triaged down to
           "app behaved correctly" three times over is a clean ride.
        2. A plan file changed. That is the promotion, done. Reap, no page.
        3. The window ran out. Reap either way — the pane cannot be held
           indefinitely on the strength of a step that is evidently not
           happening — and page ONCE, because a report naming real bugs whose
           rows reached no backlog is exactly the failure nobody noticed twice
           on 09-15. Never page about a pane this daemon killed itself: the
           thread never had the window the deadline claims to have given it,
           and that protection is the same one the report deadline already
           carries below.
        """
        moved = self._plans_moved(entry)
        if moved is None:
            # No baseline (an entry restored from an older daemon's state).
            self.log.info("wrap-up for %s has no backlog baseline; settling on"
                          " the report alone" % entry.get("session"))
            self._schedule_thread_reap(entry.get("tmux"), now,
                                       "wrap-up landed")
            return True
        if not entry.get("realBugs"):
            self.log.info(
                "wrap-up for %s named no real bugs: nothing to promote"
                % entry.get("session"))
            self._schedule_thread_reap(entry.get("tmux"), now,
                                       "wrap-up landed, nothing to promote")
            return True
        if moved:
            self.log.info(
                "wrap-up for %s promoted: %s changed since %s"
                % (entry.get("session"), os.path.basename(moved),
                   fmt_hms(entry.get("armedMs"))))
            self._schedule_thread_reap(entry.get("tmux"), now,
                                       "wrap-up landed and promoted")
            return True
        due = entry.get("reportLandedMs", now) + PROMOTION_DEADLINE_MS
        if now < due:
            if not entry.get("promotionLogged"):
                entry["promotionLogged"] = True
                self.log.info(
                    "wrap-up for %s wrote its report (%d real bug(s)) but the"
                    " backlog has not moved; holding its console until %s"
                    % (entry.get("session"), entry.get("realBugs"),
                       fmt_hms(due)))
            return False
        killed = self._panes_killed.get(entry.get("tmux"))
        if killed is not None and killed >= entry.get("armedMs", 0):
            self.log.error(
                "nothing promoted for %s and none was possible: this daemon"
                " killed its pane %s at %s. Not paging the rider about a"
                " promotion it prevented."
                % (entry.get("session"), entry.get("tmux"), fmt_hms(killed)))
        else:
            self.log.warn(
                "no promotion for %s %d min after the report landed with %d"
                " real bug(s); paging (backlog unchanged since %s)"
                % (entry.get("session"), PROMOTION_DEADLINE_MS // 60000,
                   entry.get("realBugs"), fmt_hms(entry.get("armedMs"))))
            self._promotion_fallback_push(entry.get("realBugs") or 0)
        self._schedule_thread_reap(entry.get("tmux"), now,
                                   "promotion deadline expired")
        return True

    def _promotion_fallback_push(self, real_bugs):
        """One page: the report is written and its rows reached no backlog.

        Charged to the same budget as the missing-report page and for the same
        reason — it is the only notice the rider gets that a ride's evidence
        did not make it into the one place open work is tracked, and it fires
        long after the ride itself is over.
        """
        now = self.now_ms()
        if (self.last_report_page_ms
                and now - self.last_report_page_ms
                < REPORT_PAGE_MIN_INTERVAL_MS):
            self.log.info(
                "promotion page suppressed (one per %d min): %d real bug(s)"
                % (REPORT_PAGE_MIN_INTERVAL_MS // 60000, real_bugs))
            self.push_log.append({
                "tsMs": now, "title": "Ride watch",
                "body": "Report written — %d real bug(s) not in the backlog."
                        % real_bugs,
                "sent": False, "kind": "promotion",
                "suppressed": "report-page-budget"})
            return False
        self.last_report_page_ms = now
        return self._send_push(
            "Ride watch",
            "Report written, backlog not updated — %d real bug(s). Open Claude"
            " and say 'promote the ride report'." % real_bugs,
            kind="promotion", bypass_rate_limit=True)

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
            # Before the close below, and at a lower threshold, on purpose:
            # our own close is what has been masking the client's failure to
            # close, so it must not also be what suppresses the finding about
            # it (18.3b).
            self._rule_arrived_never_ended(trip, now)
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
            # A refusal burst matures the same way: the last warn of a ladder
            # is followed by silence, not by another event, so nothing but the
            # 5 s tick can tell us the launch is done being denied.
            self._flush_wake_lock(trip, now)
            self._maybe_heartbeat(trip, now)
        # Outside the loop on purpose: a promised wrap-up outlives its trip,
        # which was deleted from self.trips the moment the ride ended. The
        # live loop ticks every 5s whether or not telemetry is arriving, so a
        # phone that has gone home and stopped talking still gets its deadline
        # checked.
        self._check_report_deadlines(now)
        # Same shape, same reason: a note whose verdict is waiting on an error
        # that may never come has no trip to be ticked by, and on 2026-09-15
        # the note that needed this arrived two minutes before its ride began.
        self._check_pending_notes(now)
        # Outside the loop as well, and for a stronger reason: a panel
        # teardown burst is keyed by session and can be open with no trip
        # behind it at all (the rider on the settings tab from the search
        # form). The 5 s live tick is the only thing that can tell us the
        # rider has stopped fighting the screen.
        self._flush_panel_teardowns(now)
        # ...and the same is true of a console whose ride is over: the reap it
        # is waiting out is not attached to any live trip either.
        self._reap_due_threads(now)

    # -- rules --------------------------------------------------------------

    def _on_progress(self, trip, t, p):
        prev = trip.progress
        trip.progress = {
            "currentLegIndex": p.get("currentLegIndex"),
            "currentLegProgress": p.get("currentLegProgress"),
            # Progress over the WHOLE itinerary. This is the quantity the
            # client's arrival branch actually tests (>= 99.5 grants arrival),
            # so a finding about an early arrival has to be able to quote it
            # next to the leg percentage that disagrees with it (21.2).
            "overallProgress": p.get("overallProgress"),
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
            # The client's own smoothed ground speed. Read by
            # access-leg-transit-speed and by boarding-prompt-empty, which
            # needs it to rebuild the radius the app's matcher would have
            # used (speedAdjustedRadius).
            "riderSpeedMps": p.get("riderSpeedMps"),
            "tMs": t,
        }
        self._note_destination_distance(trip, p.get("distanceToDestination"))
        self._rule_access_leg_transit_speed(trip, t, p)
        # Per-leg last progress. early-leg-transition asks about the leg the
        # rider is LEAVING, and by the time TRANSITION_LEG arrives
        # trip.progress has already been overwritten by the new leg's first
        # tick on some streams — so the answer has to be kept per leg.
        if isinstance(p.get("currentLegProgress"), (int, float)) and \
                isinstance(p.get("currentLegIndex"), int):
            trip.leg_progress_last[p["currentLegIndex"]] = \
                p["currentLegProgress"]
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
                        and trip.current_leg_transit()
                        and trip.swap_seq not in trip.collapse_fired_seq):
                    verdict = self._stop_collapse_verdict(
                        trip, p.get("currentLegIndex"), int(prev_stops),
                        progress)
                    if verdict["wrong"]:
                        trip.collapse_fired_seq.add(trip.swap_seq)
                        ctx = {"prevStops": prev_stops, "stops": stops,
                               "legProgressPct": progress,
                               "nextStop": p.get("nextStopName")}
                        ctx.update(verdict["context"])
                        self._finding(
                            trip, t, "stop-count-collapse", "page",
                            "stopsRemaining %d -> 1 %s"
                            % (prev_stops, verdict["summary"]),
                            ctx,
                            push_body="Stop count wrong — app says 1 left %s. Ignore the banner."
                                      % verdict["push"])
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

    def _on_position(self, trip, t, p):
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
            self._check_position_teleport(trip, t, lat, lon,
                                          coords.get("accuracy"))
            trip.last_fix = (lat, lon)
            # Stall anchor: the oldest fix the rider has not meaningfully left.
            anchor = trip.stall_anchor
            if anchor is None or meters_between(anchor[0], (lat, lon)) > STALL_RADIUS_M:
                trip.stall_anchor = ((lat, lon), trip.last_pos_ms)
                trip.fixes_since_anchor = 1
            else:
                trip.fixes_since_anchor += 1

    def _check_position_teleport(self, trip, t, lat, lon, accuracy):
        """Two consecutive fixes that cannot both be true. (18.3a)

        Called BEFORE trip.last_fix is overwritten, so the pair being judged is
        the pair the app itself saw back to back.

        What this is looking for is not a bad fix — the matcher absorbs those —
        but a position STREAM that is not one stream. On 2026-09-17 ride 1 the
        fixes alternated between two tracks 200-310 m apart, each advancing at
        bike speed and each stopping at the same red light; whichever one
        arrived is what the deviation check, the replan, the progress bar and
        the turn card were computed off. The rate is the signal, so the finding
        matures on the count in a rolling minute, not on a single jump.

        Deliberately NOT gated on the rider being on a particular leg or mode:
        the defect is in the phone, and it fired on a bike leg on 09-17 and on
        a bus leg on 09-15.
        """
        prev = trip.last_fix_meta
        trip.last_fix_meta = (t, lat, lon, accuracy)
        if prev is None:
            return
        gap = t - prev[0]
        if gap < 0 or gap > TELEPORT_MAX_GAP_MS:
            return
        # Both ends must claim to be sure of themselves. A missing accuracy is
        # not a claim, so it is judged as unusable rather than as good: the
        # rule fails closed, which is right for one whose whole case rests on
        # "neither fix admits to being uncertain".
        for acc in (prev[3], accuracy):
            if not isinstance(acc, (int, float)) or acc >= TELEPORT_MAX_ACCURACY_M:
                return
        meters = meters_between((prev[1], prev[2]), (lat, lon))
        if meters <= TELEPORT_MIN_M:
            return
        trip.teleports.append((t, meters, gap, prev[3], accuracy))
        while trip.teleports and t - trip.teleports[0][0] > TELEPORT_WINDOW_MS:
            trip.teleports.popleft()
        n = len(trip.teleports)
        if n < TELEPORT_WARN_COUNT:
            return
        page = n >= TELEPORT_PAGE_COUNT
        # The cooldown holds a second warn about the same episode, but never
        # holds the escalation: a stream that has got worse since the warn is
        # news, and it gets through once.
        if trip.teleport_fired_ms and \
                t - trip.teleport_fired_ms <= TELEPORT_COOLDOWN_MS and \
                not (page and not trip.teleport_paged):
            return
        trip.teleport_fired_ms = t
        biggest = max(trip.teleports, key=lambda j: j[1])
        summary = ("position jumped %.0fm in %.1fs, %d times in the last"
                   " minute (accuracy %.0f/%.0fm — both fixes trusted)"
                   % (biggest[1], biggest[2] / 1000.0, n,
                      biggest[3], biggest[4]))
        context = {"jumps": [{"tMs": j[0], "meters": round(j[1], 1),
                              "gapMs": j[2],
                              "accuracyM": [round(j[3], 1), round(j[4], 1)]}
                             for j in trip.teleports],
                   "countInWindow": n,
                   "windowMs": TELEPORT_WINDOW_MS,
                   "minMeters": TELEPORT_MIN_M,
                   "maxGapMs": TELEPORT_MAX_GAP_MS,
                   "maxAccuracyM": TELEPORT_MAX_ACCURACY_M}
        if page:
            trip.teleport_paged = True
            self._finding(
                trip, t, "position-teleport", "page", summary, context,
                push_body=("Position tracking is jumping %d times a minute."
                           " Distances and turns on screen may be wrong."
                           % n))
        else:
            self._finding(trip, t, "position-teleport", "warn", summary, context)

    def _stop_collapse_verdict(self, trip, leg_index, prev_stops, progress):
        """Is a `stopsRemaining` that just fell to 1 actually WRONG? (22.3)

        The old test was a percentage: below STOP_COLLAPSE_MAX_PROGRESS of the
        leg, "one stop left" cannot be true. That is a proxy for the leg's
        geometry and it assumes the hops are roughly even. On 2026-09-21 the
        Orange Line's I-35W & 66th St -> I-35W & 98th St leg is 7885 m with its
        LONGEST hop last (3775 m of it after Knox Ave & American Blvd), so the
        penultimate stop sits at 41 % by construction; the count fell to 1 at
        09:32:47 while the rider was 31 m from that platform at 8.4 m/s, and
        the daemon spent the ride's one page telling them to ignore a banner
        that was right.

        So ask the leg instead. `stopsRemaining` counts the calls still ahead,
        the last of them being the alight stop, so the drop from N to 1 means
        the vehicle has just consumed stops[-N] — American Blvd here, and
        76th St for the 3 -> 2 drop 2m13s earlier (29 m away, same shape).
        Within STOP_COLLAPSE_NEAR_STOP_M of that stop the count is simply
        true and there is nothing to say.

        A leg with no stop coordinates falls back to the percentage, which is
        what the 7/29 incident this rule exists for needed: the count there
        collapsed to 1 at the very start of the leg, nowhere near any stop it
        could have meant.
        """
        legs = (trip.itinerary or {}).get("legs") or []
        stops = None
        if isinstance(leg_index, int) and 0 <= leg_index < len(legs):
            leg = legs[leg_index]
            if isinstance(leg, dict):
                stops = leg.get("stops")
        implied = None
        if isinstance(stops, list) and 1 <= prev_stops <= len(stops):
            implied = stops[-prev_stops]
        if implied is None or trip.last_fix is None:
            wrong = progress < STOP_COLLAPSE_MAX_PROGRESS
            return {
                "wrong": wrong,
                "summary": ("at %.0f%% of transit leg %s (the leg carries no"
                            " stop coordinates, so the percentage decided)"
                            % (progress, leg_index)),
                "push": "at %.0f%% of the leg" % progress,
                "context": {"stopGeometry": "unavailable",
                            "maxProgressPct": STOP_COLLAPSE_MAX_PROGRESS},
            }
        gap = meters_between(trip.last_fix,
                             (implied["lat"], implied["lon"]))
        name = implied.get("name") or "the stop it counted off"
        context = {"stopGeometry": "leg",
                   "impliedStop": name,
                   "impliedStopMeters": round(gap, 1),
                   "nearStopM": STOP_COLLAPSE_NEAR_STOP_M,
                   "maxProgressPct": STOP_COLLAPSE_MAX_PROGRESS,
                   "legStopCount": len(stops)}
        return {
            # Both tests, and the geometry only ever NARROWS the rule. The
            # percentage on its own pages on a correct count whose last hop is
            # the long one (22.3); the geometry on its own would page on a
            # count that is late rather than early — 2026-08-27 13:36:17 and
            # 2026-08-28 17:07:38 both drop to 1 at 97-98 % of the leg, 177 m
            # and 102 m past the stop the count named, which is a stale
            # `stopsRemaining` and not the "one stop left" lie this rule pages
            # about. Nothing that fired before this change fires only because
            # of it.
            "wrong": (progress < STOP_COLLAPSE_MAX_PROGRESS
                      and gap > STOP_COLLAPSE_NEAR_STOP_M),
            "summary": ("at %.0f%% of transit leg %s, %.0f m from %s — the"
                        " stop the count says was just passed"
                        % (progress, leg_index, gap, name)),
            "push": "but you are %.0f m from %s" % (gap, name),
            "context": context,
        }

    def _check_progress_without_motion(self, trip, t, p):
        """Leg progress advancing faster than the rider physically moved.

        The anchor is the last place progress was believed. It is reset when
        the rider genuinely travels (past MOTION_DISPLACEMENT_M — a real move,
        not GPS jitter), when the leg changes, or when the ITINERARY changes
        under it, so the question the rule actually asks is *physical*: did the
        progress bar gain more than MOTION_PROGRESS_PCT points in the time it
        took the rider to cover 15m?

        That third reset is 21.6 and it was missing. currentLegProgress is a
        percentage of whatever leg the current itinerary calls `currentLegIndex`,
        so an itinerary swap changes the denominator without moving the rider:
        on 2026-09-20 12:55:24 the rider's own onboard pick re-based the bar
        11 % -> 66 % in 9 ms at the identical fix and this rule reported it as
        the app teleporting them up the leg. `trip.swap_seq` is bumped in
        _on_start_go_mode and, until now, was never read here.

        That window is adaptive, and both of its ends are real defects:
        - Stationary: the window is minutes wide. This is the 7/31 shape —
          map-matching noise reported to a standing rider as travel.
        - Moving: the window is a couple of seconds. This is the 7/29 shape —
          on the Orange Line the bar went 35% -> 71% in ONE second while the
          bus covered 6.7m, i.e. the app teleported the rider a kilometre up
          the leg. Nothing else in the engine noticed.

        The finding reports the FROZEN SPAN, not the release tick. On
        2026-09-09 09:35:12 it said "moved 3.4 m" — true of that one second
        and useless: the episode was 09:34:27 to 09:35:11, 45 ticks pinned at
        0 %, 74 m of GPS travel, and then one tick of +8.36 points. The 3.4 m
        is the release; the span is the defect (12.17's fourth sighting, where
        the continuity gate in position-matching.ts held the old projection
        verbatim until the jump budget let go).
        """
        prog = p.get("currentLegProgress")
        leg = p.get("currentLegIndex")
        if not isinstance(prog, (int, float)) or trip.last_fix is None:
            return
        span = self._note_progress_freeze(trip, t, leg, prog)
        anchor = trip.motion_anchor
        fresh = {"fix": trip.last_fix, "progress": prog, "leg": leg, "tMs": t,
                 "swapSeq": trip.swap_seq, "afterSwap": None}
        if anchor is None or anchor["leg"] != leg:
            trip.motion_anchor = fresh
            return
        if anchor.get("swapSeq") != trip.swap_seq:
            # The itinerary was replaced under the anchor, so its percentage
            # is measured against a different leg 0 than this tick's and the
            # two are not comparable (21.6). REBASE rather than drop: the
            # anchor's fix and timestamp are still the physical window the
            # rule is asking about, and only the percentage changed basis, so
            # the new basis is read off this tick and everything below runs as
            # usual. Dropping the anchor outright would also work for 21.6 and
            # is what this was written as first — but it costs a tick, and at
            # 8 m/s an anchor survives exactly two ticks, so a one-tick phase
            # shift moves which tick a real jump lands on: it silently lost
            # 2026-08-31 15:37:52 (11 % -> 50 % in one second on an 8801 m
            # leg, the 7/29 shape). Rebasing preserves the phase exactly.
            anchor = dict(anchor)
            anchor["progress"] = prog
            anchor["swapSeq"] = trip.swap_seq
            anchor["afterSwap"] = trip.swap_seq
            trip.motion_anchor = anchor
        moved = meters_between(anchor["fix"], trip.last_fix)
        if moved > MOTION_DISPLACEMENT_M:
            trip.motion_anchor = fresh          # they really went somewhere
            return
        if prog - anchor["progress"] <= MOTION_PROGRESS_PCT:
            return
        if trip.motion_fired_ms and t - trip.motion_fired_ms <= MOTION_COOLDOWN_MS:
            return
        trip.motion_fired_ms = t
        summary = "leg progress %s -> %s while the fix moved %.0fm" % (
            fmt_pct(anchor["progress"]), fmt_pct(prog), moved)
        context = {"fromPct": anchor["progress"], "toPct": prog,
                   "movedMeters": round(moved, 1), "legIndex": leg,
                   "sinceMs": anchor["tMs"]}
        if anchor.get("afterSwap") is not None:
            summary += " (measured from the anchor re-based at itinerary" \
                       " swap #%d)" % anchor["afterSwap"]
            context["anchorAfterSwap"] = anchor["afterSwap"]
        if span:
            summary += " (frozen at %s for %ds, %.0fm travelled, %d ticks)" % (
                fmt_pct(span["atPct"]), span["seconds"], span["meters"],
                span["ticks"])
            context["frozenSpan"] = span
        self._finding(trip, t, "progress-without-motion", "warn",
                      summary, context)
        # Re-anchor: a drift that keeps drifting is one finding, not a stream.
        trip.motion_anchor = fresh

    @staticmethod
    def _note_progress_freeze(trip, t, leg, prog):
        """Hold the span over which currentLegProgress did not move.

        Returns the span that THIS tick released, or None. A span is the
        interval between two distinct progress values on one leg, plus the
        GPS path length the rider actually covered inside it — which is the
        number the report wants and the release tick can never supply.

        Exact equality is the right test: a frozen projection repeats the
        float bit for bit (09-09: `progressAlongLeg` pinned at 0.2426 for 19
        ticks, `distanceFromRoute` at 31.24200447554046 for 14). Ordinary
        motion never does.
        """
        freeze = trip.progress_freeze
        if freeze is not None and freeze["leg"] == leg and \
                freeze["pct"] == prog:
            if trip.last_fix is not None:
                if freeze["fix"] is not None:
                    freeze["meters"] += meters_between(freeze["fix"],
                                                       trip.last_fix)
                freeze["fix"] = trip.last_fix
            freeze["ticks"] += 1
            freeze["lastMs"] = t
            return None
        released = None
        if freeze is not None and freeze["leg"] == leg and freeze["ticks"] > 1:
            released = {"fromMs": freeze["tMs"], "toMs": t,
                        "atPct": freeze["pct"],
                        "meters": round(freeze["meters"], 1),
                        "seconds": max(0, (t - freeze["tMs"]) // 1000),
                        "ticks": freeze["ticks"]}
        trip.progress_freeze = {"tMs": t, "lastMs": t, "pct": prog, "leg": leg,
                                "fix": trip.last_fix, "meters": 0.0,
                                "ticks": 1}
        return released

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

    def _rule_riding_fact_dropped(self, trip, t):
        """CLEAR_RIDING after a confirmed vehicle, with nothing to explain it.

        The app does not dispatch CLEAR_RIDING on alighting — TRANSITION_LEG's
        reducer does that (see _on_transition_leg) — and it does not dispatch
        it on STOP_GO_MODE either. So a bare CLEAR_RIDING after a
        CONFIRM_VEHICLE is the app throwing away a boarding it had just
        confirmed, which is exactly what the rider was typing about on
        2026-09-21: "I used already on the bus flow but it's showing like I'm
        not!" (09:24:56).

        Measured on that ride (`mubbbiy9-6zjoq9`): CONFIRM_VEHICLE 09:22:10.536
        (vehicle 8228, trip 1:1268952, confidence confirmed) -> SET_RIDING
        09:22:10.538 -> CLEAR_RIDING 09:23:42.054, 91 s later, with no
        TRANSITION_LEG and no STOP_GO_MODE between them (STOP_GO_MODE is
        09:23:45.789, three seconds AFTER). It happened again at 09:25:52.081,
        91 s after the 09:24:21.372 confirm. Nothing in the daemon remarked on
        either.

        Warn and once a ride: the same defect twice in four minutes is one
        thing to read, and the rider cannot act on it in the next minute —
        they are already re-doing the onboard flow by hand.
        """
        if trip.riding_dropped_fired or trip.confirm_vehicle is None:
            return
        confirmed_ms, vehicle, trip_id, label = trip.confirm_vehicle
        trip.confirm_vehicle = None
        if trip.riding is None:
            return
        trip.riding_dropped_fired = True
        held = max(0, t - confirmed_ms)
        self._finding(
            trip, t, "riding-fact-dropped", "warn",
            "CLEAR_RIDING %ds after CONFIRM_VEHICLE %s with no leg change"
            " or stop — the app dropped a boarding it had confirmed"
            % (held // 1000, label or vehicle or trip_id or "?"),
            {"confirmedMs": confirmed_ms, "heldMs": held,
             "vehicleId": vehicle, "tripId": trip_id, "label": label,
             "legIndex": (trip.riding or {}).get("legIndex")})

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
        # Before the alight-clear, because the rule is about the case this
        # method returns on: riding is None, so there is nothing to clear.
        self._rule_early_leg_transition(trip, t, leg_index)
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

    def _leg_is_transit(self, trip, idx):
        """Does the itinerary call leg `idx` a transit leg? None = cannot tell.

        Deliberately not `current_leg_transit()`: that one answers "is the
        rider on a transit leg NOW" and falls back to stopsRemaining and the
        riding fact, both of which are the very things early-leg-transition is
        asking about. This reads the itinerary or admits it cannot.
        """
        legs = (trip.itinerary or {}).get("legs") or []
        if not isinstance(idx, int) or not (0 <= idx < len(legs)):
            return None
        return bool(legs[idx].get("transit"))

    def _rule_early_leg_transition(self, trip, t, leg_index):
        """The app stepped onto the bus leg before the rider got to the bus.

        2026-09-09 08:24:55: TRANSITION_LEG to leg 1 (METRO Orange Line) with
        the bike leg it was leaving at 71.88 % and riderSpeedMps 5.9 — the
        rider was still riding to the station, and SET_RIDING did not arrive
        for another 2m36s. From that moment the trip sheet, the banner and the
        stop count were all describing a bus the rider was not on, and no rule
        in this engine watched leg transitions at all: 13.1 reached the
        backlog because the rider typed it in by hand, 97 s later.

        Ride 2 of the same morning is the control and the reason the threshold
        is on the leg being LEFT rather than on SET_RIDING's absence: at
        09:14:39 leg 0 read 100 %, the transition was correct, and SET_RIDING
        landed 2 s afterwards. A rule keyed on "riding is unset" alone would
        have fired on both.

        warn, not page. It is real, but the rider is looking at the screen it
        is about — they are on a bike approaching a station — and a ride has
        two interrupts to spend on things they cannot already see. The finding
        reaches the thread and the report, which is where the fix comes from.
        """
        if trip.arrived_ms is not None:
            return
        if trip.riding is not None:
            return                        # aboard already: an ordinary advance
        if leg_index in trip.early_transition_legs:
            return
        if self._leg_is_transit(trip, leg_index) is not True:
            return
        prior = trip.leg_progress_last.get(leg_index - 1)
        if not isinstance(prior, (int, float)):
            return                        # never saw the leg being left
        if prior >= EARLY_TRANSITION_PROGRESS_PCT:
            return
        trip.early_transition_legs.add(leg_index)
        self._finding(
            trip, t, "early-leg-transition", "warn",
            "advanced to transit leg %d with leg %d at %s and no riding fact"
            % (leg_index, leg_index - 1, fmt_pct(prior)),
            {"legIndex": leg_index, "priorLegIndex": leg_index - 1,
             "priorLegProgressPct": prior,
             "thresholdPct": EARLY_TRANSITION_PROGRESS_PCT,
             "leg": self._leg_label(trip, leg_index)})

    def _rule_session_restart_while_aboard(self, session, t, obj, trip):
        """The app relaunched itself under a rider who was on the bus. (17.7)

        2026-09-15 ride A did it twice. At 15:49:45: `record-mode`, `start`,
        `resumed-session`, RESUME_GO_MODE (duration 2439.981, end 16:28:33),
        `bundle_hold` — then `bundle_health` / `bundle_apply` five seconds
        later, which is the crash-recovery path and not a normal resume.
        STOP_GO_MODE came at 15:50:09, 24 s afterwards, so the relaunch is
        plausibly why the rider quit. The same thing had happened at 15:47:11.
        `riding` was set across both, on Orange Line trip 1:1346665 with a
        confirmed vehicle match.

        No rule covered it and `resumed-trip` never could: that one is about a
        ride which arrives with no START_GO_MODE anywhere, reached only from
        _maybe_adopt, and both of these arrived inside a trip this daemon had
        opened itself. Nothing here can make it fire twice — different rule,
        different door.

        A page, not a warn: the rider is aboard and the screen they were
        navigating by has just gone away, so "the app restarted" is the one
        sentence that explains what they are looking at. Once per ride,
        though. The second relaunch is news for the report and is filed, but
        it is not worth the ride's other interrupt.

        Known gap, stated rather than guessed at: if a relaunch mints a NEW
        session id, RESUME_GO_MODE arrives before _maybe_adopt has aliased the
        old trip onto it, `self.trips.get(session)` is None and this says
        nothing. Both recorded relaunches kept the id
        (`mu346i5y-ng2uqc` throughout), so there is no evidence for how to
        bridge that and no rule written on a guess.
        """
        if trip is None or trip.riding is None:
            return
        if trip.arrived_ms is not None:
            return
        # `resumed-session` and RESUME_GO_MODE are one relaunch reported twice,
        # 3 ms apart on the real stream.
        if (trip.restart_aboard_ms
                and t - trip.restart_aboard_ms <= SESSION_RESTART_DEDUP_MS):
            return
        trip.restart_aboard_ms = t
        trip.restart_aboard_count += 1
        first = trip.restart_aboard_count == 1
        route = (trip.riding.get("headsign") or trip.riding.get("routeId")
                 or "bus")
        marker = ("RESUME_GO_MODE" if obj.get("type") == "RESUME_GO_MODE"
                  else "resumed-session")
        nth = "" if first else " (%d in this ride)" % trip.restart_aboard_count
        summary = ("the app relaunched mid-ride while aboard %s (trip %s):"
                   " %s%s" % (route, trip.riding.get("tripId"), marker, nth))
        ctx = {"marker": marker,
               "riding": {k: trip.riding.get(k) for k in
                          ("tripId", "vehicleId", "routeId", "legIndex")},
               "restartCount": trip.restart_aboard_count,
               "legIndex": (trip.progress or {}).get("currentLegIndex"),
               "legProgressPct": (trip.progress or {}).get("currentLegProgress"),
               "bundle": (self.device_bundles.get(trip.device) or {}).get("version"),
               "swapSeq": trip.swap_seq}
        self._finding(
            trip, t, "session-restart-while-aboard",
            "page" if first else "warn", summary, ctx,
            push_body=("The app restarted while you were on %s. Check the trip"
                       " sheet still shows your bus." % route) if first else None)

    # -- what the rider had touched, and what was hanging while they did ----
    #
    # Both ledgers are per session and fed from every record (see _process).
    # note-unverifiable is the only reader; it exists because on 2026-09-15 the
    # note "Clicking does nothing" could be neither confirmed nor contradicted.

    def _note_rider_gesture(self, session, t, kind, typ, obj):
        """Remember the last record only a rider's finger could have produced.

        Two vocabularies, on purpose. RIDER_GESTURE_TYPES is today's stream,
        every entry checked against its otprr call site. TAP_RECORD_RE /
        TAP_RECORD_KINDS are the tap records the client does not emit yet: a
        parallel change is adding them, and when they land they satisfy this by
        themselves and note-unverifiable stops firing — which is the whole
        point of the rule rather than a hole in it.
        """
        gesture = None
        if kind in TAP_RECORD_KINDS:
            gesture = kind
        elif isinstance(typ, str):
            if typ in RIDER_GESTURE_TYPES:
                gesture = typ
            elif TAP_RECORD_RE.search(typ):
                gesture = typ
            elif typ == "START_REROUTE" and \
                    (obj.get("payload") or {}).get("autoApply") is False:
                # The reroute BUTTON. autoApply true is the app's own re-plan
                # and says nothing about the rider (17.9d, same distinction).
                gesture = "START_REROUTE"
        if gesture is None:
            return
        if len(self.session_last_gesture) > SESSION_CACHE_MAX and \
                session not in self.session_last_gesture:
            self._prune_session_caches()
        prior = self.session_last_gesture.get(session)
        if prior is not None and prior[0] > t:
            return                        # buffered replay of an older record
        self.session_last_gesture[session] = (int(t), gesture)

    def _note_href(self, session, obj):
        """Remember which screen this session's records are coming from.

        Every beacon record carries the full href; the note the rider types
        carries none. The 2026-09-20 12:54:48 note ("the tap to return is
        still overlapping ... this feedback page for example") was surrounded
        on both sides by records reading `capacitor://localhost#/feedback`,
        and the finding could not say so (17.11).
        """
        page = href_page(obj.get("href"))
        if page is None:
            return
        if len(self.session_href) > SESSION_CACHE_MAX and \
                session not in self.session_href:
            self._prune_session_caches()
        self.session_href[session] = page

    def _note_request_timeout(self, session, t, typ, obj):
        """Reconstruct the window of a request that came back timed out.

        Only the ERROR carries the timeout, so the window is worked backwards
        from it: [errorMs - timeoutMs, errorMs]. Two shapes in the stream, both
        from 2026-09-15 — `{"error": {"timedOut": true, "timeoutMs": 20000,
        "url": ...}}` on ROUTING_ERROR, and `{"__error": true, "message":
        "Request timed out after 20000 ms"}` on FIND_FEEDS_ERROR,
        FIND_TRIP_ERROR and REALTIME_VEHICLE_POSITIONS_ERROR.
        """
        if not isinstance(typ, str) or not typ.endswith("_ERROR"):
            return
        p = obj.get("payload")
        if not isinstance(p, dict):
            return
        ms = None
        err = p.get("error")
        if isinstance(err, dict) and err.get("timedOut"):
            cand = err.get("timeoutMs")
            if isinstance(cand, (int, float)) and cand > 0:
                ms = int(cand)
        if ms is None:
            m = TIMEOUT_MESSAGE_RE.search(p.get("message") or "")
            if m:
                ms = int(m.group(1))
        if not ms or ms > TIMEOUT_WINDOW_MAX_MS:
            return
        if len(self.session_timeouts) > SESSION_CACHE_MAX and \
                session not in self.session_timeouts:
            self._prune_session_caches()
        ring = self.session_timeouts.setdefault(
            session, collections.deque(maxlen=64))
        ring.append((int(t) - ms, int(t), typ, ms))

    def _arm_note_evidence(self, session, t, text, image):
        """Hold the "could anyone check this?" verdict until the errors land.

        The gesture half is decidable now; the timeout half is not, because the
        request that swallowed the tap only reports itself when it gives up —
        15:53:59.330 for a note at 15:53:50.965. So the whole decision waits
        NOTE_EVIDENCE_GRACE_MS and is taken on the 5 s tick.
        """
        if not NOTE_NO_RESPONSE_RE.search(text):
            # Not a claim about a control, so "nothing records that a tap
            # happened" says nothing about it (17.11). Dropped here rather
            # than at resolve time so the pending list stays the notes the
            # rule might actually file.
            return
        last = self.session_last_gesture.get(session)
        self.pending_notes.append({
            "session": session,
            "tMs": int(t),
            "dueMs": int(t) + NOTE_EVIDENCE_GRACE_MS,
            "text": text,
            "image": image,
            "page": self.session_href.get(session),
            "control": bool(NOTE_CONTROL_RE.search(text)),
            "lastGestureMs": last[0] if last else None,
            "lastGesture": last[1] if last else None,
        })

    def _check_pending_notes(self, now, force=False):
        """File note-unverifiable for the notes nothing can corroborate. (17.11)"""
        if not self.pending_notes:
            return
        keep, changed = [], False
        for entry in self.pending_notes:
            if not force and now < entry["dueMs"]:
                keep.append(entry)
                continue
            changed = True
            self._resolve_pending_note(entry)
        if changed:
            self.pending_notes = keep

    def _resolve_pending_note(self, entry):
        session, t = entry["session"], entry["tMs"]
        gesture_ms = entry.get("lastGestureMs")
        blind = (gesture_ms is None
                 or t - gesture_ms > NOTE_GESTURE_LOOKBACK_MS)
        hung = None
        for (start, end, typ, ms) in self.session_timeouts.get(session, ()):
            if start <= t <= end:
                hung = (typ, ms, end)
                break
        if not blind and hung is None:
            return
        trip = self.trips.get(session) or self._recently_ended_trip(session, t)
        if trip is not None and trip.note_unverifiable_fired:
            return
        if trip is not None:
            trip.note_unverifiable_fired = True
        reasons = []
        if blind:
            reasons.append(
                "no rider-gesture record in the %d s before it (last was %s)"
                % (NOTE_GESTURE_LOOKBACK_MS // 1000,
                   ("%s at %s" % (entry.get("lastGesture"),
                                  fmt_hms(gesture_ms)))
                   if gesture_ms else "none in this session"))
        if hung is not None:
            reasons.append(
                "a request that timed out after %d ms was in flight across it"
                " (%s at %s)" % (hung[1], hung[0], fmt_hms(hung[2])))
        page = entry.get("page")
        summary = ("rider note \"%s\"%s cannot be checked against the"
                   " telemetry: %s"
                   % (entry["text"][:80],
                      (" (on %s)" % page) if page else "",
                      "; and ".join(reasons)))
        ctx = {"text": entry["text"], "noteMs": t,
               # Which screen the rider was on when they typed it, from the
               # nearest record that carried an href (17.11). A report cannot
               # place a control complaint without it.
               "page": page,
               "controlNamed": entry.get("control", False),
               "lastGestureMs": gesture_ms,
               "lastGesture": entry.get("lastGesture"),
               "gestureLookbackMs": NOTE_GESTURE_LOOKBACK_MS,
               "noGestureRecord": blind,
               "inFlightTimeout": ({"type": hung[0], "timeoutMs": hung[1],
                                    "errorMs": hung[2]} if hung else None),
               # Named so a report can say what would fix it: the client emits
               # no tap or gesture record anywhere, which is the defect the
               # rule is really about.
               "tapInstrumentation": "absent"}
        if entry.get("image"):
            ctx["image"] = entry["image"]
        self._hold_onboard_finding(session, trip, t, "note-unverifiable",
                                   "warn", summary, ctx)

    @staticmethod
    def _transit_route_signature(summary):
        """The route ids of a plan's transit legs, in order. None = unknown.

        Route ids, not trip ids, and not the walk/bike legs between them: this
        is "the journey the rider agreed to", which is exactly what a
        route-preserving swap keeps and a real swap does not. See
        ONBOARD_ANCHOR_AHEAD_MARGIN_M's block for why tripId is the wrong test
        — a `boarded-earlier` splice changes it by design.
        """
        if not summary:
            return None
        legs = summary.get("legs")
        if not isinstance(legs, list):
            return None
        sig = []
        for leg in legs:
            if not isinstance(leg, dict) or not leg.get("transit"):
                continue
            sig.append(leg.get("routeId") or leg.get("route")
                       or leg.get("headsign"))
        return tuple(sig)

    def _swap_preserves_the_plan(self, prev_summary, new_summary):
        """Same transit routes in the same order, same arrival. (17.9a)

        Fails closed on a summarized payload: if either plan is unavailable —
        `__summary: true` over the debug-log size cap — there is nothing to
        compare and the swap is judged as before rather than excused.
        """
        if not prev_summary or not new_summary:
            return False
        old_sig = self._transit_route_signature(prev_summary)
        new_sig = self._transit_route_signature(new_summary)
        if old_sig is None or new_sig is None or old_sig != new_sig:
            return False
        # A plan with no transit leg at all is not a route the rule protects,
        # and two of them would compare equal on an empty tuple.
        if not old_sig:
            return False
        old_end, new_end = prev_summary.get("endTime"), new_summary.get("endTime")
        if not isinstance(old_end, (int, float)) or \
                not isinstance(new_end, (int, float)):
            return False
        return int(old_end) == int(new_end)

    @staticmethod
    def _swap_lands_on_the_ridden_trip(trip, new_summary):
        """Does the incoming plan put the rider on the trip they are ON? (17.9a2)

        The stronger half of the boarded-earlier exemption, and the one that
        needs no arrival-time comparison: if a transit leg of the new plan
        carries `riding.tripId`, the app has replanned around the vehicle the
        rider is physically sitting in. Improving the arrival is what that
        replan is FOR, so an "arrival unchanged" test fails open on it — which
        is exactly what happened on 2026-09-17 17:59:24.

        Any transit leg, not just leg 0: the daemon should not care whether the
        splice made the ridden trip the first leg or left an access leg in
        front of it, only that the plan still contains the rider's vehicle.

        Fails closed on a summarized payload and on a plan with no tripIds:
        with nothing to compare, the swap is judged as before.

        It trusts `riding.tripId`, which the client is known to carry stale
        across a leg change (2026-08-31 17:35:57: TRANSITION_LEG 2 and
        SET_RIDING still saying trip 1:1268645 while the leg's route went
        1:904 -> 1:539 -> 1:546). That costs nothing here, because the plan
        coming in carries the same stale id: the app is replanning around the
        vehicle it believes the rider is on, which is the only thing this rule
        can be asked about. The stale id itself is a separate defect.
        """
        riding = trip.riding or {}
        trip_id = riding.get("tripId")
        if not trip_id or not new_summary:
            return False
        legs = new_summary.get("legs")
        if not isinstance(legs, list):
            return False
        for leg in legs:
            if isinstance(leg, dict) and leg.get("transit") \
                    and leg.get("tripId") == trip_id:
                return True
        return False

    def _rule_aboard_swap(self, trip, t, prev_summary=None, new_summary=None):
        if trip.riding is None:
            return
        # A swap that kept every route id and the arrival time is the designed
        # boarded-earlier splice, not the on-screen route walking away from the
        # rider's bus (17.9a). Checked first: it is the cheapest gate and the
        # one that was wrong on 2026-09-15 15:36:33.
        if self._swap_preserves_the_plan(prev_summary, new_summary):
            self.log.info(
                "itinerary swap #%d preserved the plan (routes %s, arrival"
                " %s): not an aboard-swap"
                % (trip.swap_seq,
                   " > ".join(str(r) for r in
                              self._transit_route_signature(new_summary)),
                   fmt_hms(new_summary.get("endTime"))))
            return
        # ...and a swap that lands the rider on the trip they are already
        # riding is the same splice with a better arrival (17.9a2). Checked
        # second because it is the one that cost 2026-09-17 its only page.
        if self._swap_lands_on_the_ridden_trip(trip, new_summary):
            self.log.info(
                "itinerary swap #%d kept the rider on trip %s (arrival %s):"
                " not an aboard-swap"
                % (trip.swap_seq, trip.riding.get("tripId"),
                   fmt_hms((new_summary or {}).get("endTime"))))
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
        # Only the app's own re-plans count toward a storm (17.9d). The rule is
        # "the app is re-planning in circles"; a rider pressing the reroute
        # button four times is a rider fighting the answer they were given,
        # which is a different finding and not this one.
        #
        # 2026-09-15 15:47:30 fired "4 reroutes within 5 min" on
        # 15:42:54.408, 15:43:49.052, 15:46:02.060 and 15:47:30.719 — every
        # one of them `autoApply: false`, `reason: "rider-reroute"`. The
        # ride's only automatic reroute, 15:36:33's `boarded-earlier`, had
        # already aged out of the window, so the storm was 100% the rider.
        # (The ride report attributed three of the four to the onboard-picker
        # commits at 15:43:28 and 15:47:53 instead; those are START_GO_MODE
        # records and never reached this counter at all.)
        if p.get("autoApply") is not True:
            self._note_replan(trip, t, p.get("reason") or "reroute")
            return
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
            trip.snapshots_since_gain = 0
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

    def _on_reroute_snapshot(self, trip, t, p):
        """Fold in the periodic "alternatives to finish the trip" capture.

        REROUTE_SNAPSHOT is a RECORDING, on a fixed ~90 s cadence
        (REROUTE_SNAPSHOT_INTERVAL_MS in otprr actions/go-mode.ts; measured
        80-101 s across 53 captures on 2026-09-09). It is emphatically NOT a
        re-plan and must never be counted as one — a rider who waits fifteen
        minutes for a bus produces ten of them and has re-planned nothing.

        What it IS is the only observable in this stream that says whether the
        graph can still reach the destination: each capture holds a full
        request/response pair from the rider's position to the trip's
        destination. Reduced here to one number — how far the best itinerary
        ENDS from the `toPlace` that was asked for — which is what
        unreachable-but-routable reads.
        """
        if not isinstance(p, dict) or p.get("__summary"):
            return
        req = p.get("request") if isinstance(p.get("request"), dict) else {}
        to = req.get("to") if isinstance(req.get("to"), dict) else {}
        lat, lon = to.get("lat"), to.get("lon")
        # The cadence is worth counting even when the payload cannot be read:
        # it is the evidence that the client was still planning at all.
        trip.snapshots_since_gain += 1
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
            return
        ends = self._snapshot_plan_ends(p.get("response"))
        if not ends:
            return
        gap = min(meters_between((lat, lon), end) for end in ends)
        trip.reroute_snaps.append({
            "tMs": int(t), "gapM": round(gap, 1),
            "itineraries": len(ends),
            "toName": to.get("name")})

    @staticmethod
    def _snapshot_plan_ends(response):
        """Where each itinerary in a snapshot response actually ends.

        The raw OTP2 payload, exactly as the reroute path's responseAction
        consumes it: `data.plan.itineraries[].legs[-1].to`. A summarised or
        errored capture yields nothing, which is a different fact from "the
        plans stopped short" and is why this returns a list rather than a
        distance.
        """
        if not isinstance(response, dict):
            return []
        plan = ((response.get("data") or {}).get("plan")
                if isinstance(response.get("data"), dict) else None)
        itineraries = (plan or {}).get("itineraries")
        if not isinstance(itineraries, list):
            return []
        ends = []
        for itin in itineraries:
            legs = itin.get("legs") if isinstance(itin, dict) else None
            if not isinstance(legs, list) or not legs:
                continue
            last = legs[-1].get("to") if isinstance(legs[-1], dict) else None
            if not isinstance(last, dict):
                continue
            lat, lon = last.get("lat"), last.get("lon")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                ends.append((lat, lon))
        return ends

    def _rule_unreachable_but_routable(self, trip, t, p):
        """The app gave up on a destination its own plans were reaching.

        2026-09-09 09:41:35: DESTINATION_UNREACHABLE, "Still 1670m from 2345
        Old Shakopee Road West and re-planning isn't closing the gap" — while
        the two REROUTE_SNAPSHOTs of the preceding three minutes (09:38:43 and
        09:40:13, and all 27 of the ride) came back with itineraries ending
        0.5 m from that exact address. The graph could reach it throughout;
        what had stalled was the rider's own approach, not the routing.

        This is the inverse of the 8/28 Fairgrounds ride the client's guard
        was built for, where the snapshots genuinely stopped at the fence —
        and the snapshots are the whole discriminator, which is why the daemon
        now reads them. A page, because "ask for it again" is an instruction
        the rider can act on in the next minute and the notification they are
        looking at says the opposite.
        """
        if trip.unreachable_routable_fired:
            return
        recent = [s for s in trip.reroute_snaps
                  if 0 <= t - s["tMs"] <= UNREACHABLE_SNAPSHOT_WINDOW_MS]
        arriving = [s for s in recent if s["gapM"] <= UNREACHABLE_GAP_M]
        if not arriving:
            return
        trip.unreachable_routable_fired = True
        best = min(arriving, key=lambda s: s["gapM"])
        self._finding(
            trip, t, "unreachable-but-routable", "page",
            "app gave up, but %d of %d plan(s) in the last %dm end %.0fm from"
            " the destination"
            % (len(arriving), len(recent),
               UNREACHABLE_SNAPSHOT_WINDOW_MS // 60000, best["gapM"]),
            {"notificationId": p.get("id"),
             "bestGapM": best["gapM"],
             "snapshotMs": best["tMs"],
             "snapshotsInWindow": len(recent),
             "snapshotsArriving": len(arriving),
             "gapMaxM": UNREACHABLE_GAP_M,
             "bestDistanceM": trip.dest_best_m,
             "toName": best.get("toName")},
            push_body="App says it cannot reach the destination, but its own"
                      " plan gets there. Ask for the route again.")

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
             # The count above is a LOWER BOUND and this is why: the client's
             # quiet access re-plans go through fetchOnboardCandidatePlan,
             # which dispatches nothing unless it produces a swap. Measured on
             # 2026-09-09 ride 2 (09:05:18-09:46:38), the two candidate
             # signals both come up empty — ZERO ROUTING_REQUEST records in
             # the whole ride (the isolated fetch bypasses the shared search
             # machinery by design) and console.log is not forwarded at all
             # (debug-log.js wraps error and warn only). What IS observable is
             # the snapshot cadence: how many ~90 s REROUTE_SNAPSHOTs have
             # gone by since the distance last improved. It is NOT a re-plan
             # count and is never used as one — it does not move the firing
             # threshold, it goes in the evidence so a report can see how long
             # the client has been planning without getting anywhere.
             "snapshotsSinceGain": trip.snapshots_since_gain,
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
             # A lower bound, and now labelled as one — see
             # _rule_replan_not_converging. The snapshot count is the
             # observable beside it.
             "snapshotsSinceGain": trip.snapshots_since_gain,
             "bestDistanceM": trip.dest_best_m})
        # ...and then ask whether the app was right to give up. It is the
        # cheapest question in the file and nothing was asking it.
        self._rule_unreachable_but_routable(trip, t, p)

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

    def _rule_wake_lock_denied(self, trip, t, obj):
        """The phone refused to stay awake while a trip was running.

        Why `_rule_console` did not catch it: that rule opens with
        `if obj.get("level") != "error": return`, and this is a
        `console.warn`. The client logs the refusal at warn level on purpose —
        it is recoverable and it retries — so the one rule that reads the
        console dropped every one of them on the floor. Deliberately NOT fixed
        by widening `_rule_console` to warns: the stream carries warns by the
        hundred (deprecations, tile misses, upstream chatter) and one
        catch-all `console-warn` finding per distinct string would drown a
        ride's findings budget the way the map-sprite burst did to
        `console-error` (see CONSOLE_ERROR_IGNORE). This is a named rule for a
        known symptom instead.

        `warn`, not a page. A sleeping screen is bad, but the rider cannot act
        on it in the next minute the way they can act on the wrong stop count,
        and a ride has only MAX_PAGES_PER_TRIP interrupts to spend. It also
        fires on EVERY launch on iOS, which is precisely the kind of rule that
        would burn both pages before the bus arrives.

        Deduped to one finding per launch/resume: see WAKE_LOCK_BURST_QUIET_MS.
        Accumulates here and is emitted by _flush_wake_lock, because the count
        the finding reports is not known until the burst is over.
        """
        if obj.get("level") != "warn":
            return
        args = obj.get("args") or []
        if not args:
            return
        head = args[0]
        if not isinstance(head, str) or WAKE_LOCK_WARN_PREFIX not in head:
            return
        # The DOMException rides as the second arg, already serialised by the
        # client's console shim. Its `name` is the whole diagnosis:
        # NotAllowedError is WKWebView refusing the API outright, and no
        # amount of retrying from the web layer will turn that into a lock.
        name, message = None, None
        if len(args) > 1 and isinstance(args[1], dict):
            name = args[1].get("name")
            message = args[1].get("message")
        elif len(args) > 1 and isinstance(args[1], str):
            message = args[1]
        burst = trip.wake_lock_burst
        if burst is not None and t - burst["lastMs"] <= WAKE_LOCK_BURST_QUIET_MS:
            burst["lastMs"] = t
            burst["count"] += 1
            burst["errorName"] = burst["errorName"] or name
            burst["errorMessage"] = burst["errorMessage"] or message
            self._mark_dirty()
            return
        # A burst that was still open belongs to an earlier launch: close it
        # before opening this one, or its count would be folded into this one.
        self._flush_wake_lock(trip, t, force=True)
        trip.wake_lock_burst = {"firstMs": t, "lastMs": t, "count": 1,
                                "errorName": name, "errorMessage": message}
        self._mark_dirty()

    def _flush_wake_lock(self, trip, now, force=False):
        """Emit the accumulated refusal burst once it is over."""
        burst = trip.wake_lock_burst
        if burst is None:
            return
        if not force and now - burst["lastMs"] <= WAKE_LOCK_BURST_QUIET_MS:
            return
        trip.wake_lock_burst = None
        trip.wake_lock_bursts += 1
        name = burst["errorName"] or "unknown error"
        detail = name
        if burst["errorMessage"]:
            detail = "%s: %s" % (name, burst["errorMessage"])
        self._finding(
            trip, burst["firstMs"], "wake-lock-denied", "warn",
            "screen wake lock denied %dx on this launch (%s) — the screen "
            "will sleep mid-ride" % (burst["count"], detail),
            {"count": burst["count"],
             "errorName": burst["errorName"],
             "errorMessage": burst["errorMessage"],
             "firstMs": burst["firstMs"],
             "lastMs": burst["lastMs"],
             "launchesDeniedThisRide": trip.wake_lock_bursts,
             "bundle": trip.bundle,
             "native": trip.bundle_native})

    # -- the screen under the rider's thumb ---------------------------------

    @staticmethod
    def _query_param_keys(payload):
        """Which query keys a SET_QUERY_PARAM touched.

        Handles the sink's oversized-payload form,
        `{"__summary": true, "chars": N, "keys": [...]}` — the key NAMES
        survive summarisation and the key names are all this rule reads. Every
        other rule in this file bails on `__summary` because it needs values;
        this one does not have to.
        """
        if not isinstance(payload, dict):
            return set()
        if payload.get("__summary"):
            keys = payload.get("keys")
            if not isinstance(keys, list):
                return set()
            return set(k for k in keys if isinstance(k, str))
        return set(k for k in payload.keys() if isinstance(k, str))

    def _note_query_param(self, session, t, obj):
        """Remember the last query change the RIDER made, per session."""
        hit = sorted(self._query_param_keys(obj.get("payload"))
                     & RIDER_QUERY_KEYS)
        if not hit:
            return
        self.rider_query[session] = {"atMs": int(t), "keys": hit}
        self._mark_dirty()

    def _rule_panel_torn_down(self, session, t, obj, trip):
        """A settings/detail screen unmounted by the rider's own query change.

        `warn`, never a page. The rider is looking straight at it — they do
        not need to be told, and a ride has MAX_PAGES_PER_TRIP interrupts to
        spend on things they cannot already see. What they need is for it to
        reach the ledger, which on 2026-09-04 it did not: the ride's report
        carried five records and every one of them was a rider note.

        Deduped to one finding per episode (PANEL_TEARDOWN_QUIET_MS), with the
        count in the text, on the wake-lock pattern: the number of times the
        screen threw the rider out is the measurement, and it is not known
        until the rider gives up.
        """
        payload = obj.get("payload") or {}
        location = payload.get("location") or {}
        path = location.get("pathname")
        if not isinstance(path, str) or not path:
            return
        prev = self.panel_route.get(session)
        self.panel_route[session] = {"pathname": path, "atMs": int(t)}
        # The mount's own LOCATION_CHANGE describes where the app started, not
        # a navigation, and there is nothing before it to have been left.
        if payload.get("isFirstRendering") or prev is None:
            return
        if not is_panel_route(prev["pathname"]):
            return
        # /places -> /places/new is the rider going deeper into the same
        # surface, not losing it.
        if is_panel_route(path):
            return
        q = self.rider_query.get(session)
        if q is None:
            return
        gap = int(t) - q["atMs"]
        if gap < 0 or gap > PANEL_TEARDOWN_WINDOW_MS:
            return
        burst = self.panel_teardowns.get(session)
        if (burst is not None
                and int(t) - burst["lastMs"] <= PANEL_TEARDOWN_QUIET_MS):
            burst["lastMs"] = int(t)
            burst["count"] += 1
        else:
            # An episode that was still open belongs to an earlier sitting;
            # close it before opening this one or its count folds into this.
            self._flush_panel_teardown(session, t, force=True)
            burst = {"firstMs": int(t), "lastMs": int(t), "count": 1,
                     "panels": [], "dests": [], "keys": [], "gapsMs": [],
                     "device": None, "duringTrip": False,
                     "outsideTrip": False}
            self.panel_teardowns[session] = burst
        for bucket, value in (("panels", prev["pathname"]),
                              ("dests", path)):
            if value not in burst[bucket]:
                burst[bucket].append(value)
        # One entry per occurrence, not a set: the gaps ARE the evidence that
        # the navigation is synchronous with the query change, and collapsing
        # three identical 8 ms gaps into one would hide two of them.
        burst["gapsMs"].append(gap)
        for key in q["keys"]:
            if key not in burst["keys"]:
                burst["keys"].append(key)
        burst["device"] = burst["device"] or obj.get("device")
        if trip is None:
            burst["outsideTrip"] = True
        else:
            burst["duringTrip"] = True
        self._mark_dirty()

    def _flush_panel_teardowns(self, now):
        for session in list(self.panel_teardowns):
            self._flush_panel_teardown(session, now)

    def _flush_panel_teardown(self, session, now, force=False):
        """Emit the episode once the rider has stopped being thrown out."""
        burst = self.panel_teardowns.get(session)
        if burst is None:
            return
        if not force and now - burst["lastMs"] <= PANEL_TEARDOWN_QUIET_MS:
            return
        del self.panel_teardowns[session]
        trip = self.trips.get(session)
        panels = ", ".join(burst["panels"])
        summary = ("%s closed itself %dx under the rider — a %s change made on "
                   "the screen navigated to %s within %d ms"
                   % (panels, burst["count"], "/".join(burst["keys"]),
                      ", ".join(burst["dests"]), max(burst["gapsMs"])))
        self._device_finding(
            session, burst["device"], burst["firstMs"], "panel-torn-down",
            "warn", summary,
            {"count": burst["count"],
             "panelPaths": burst["panels"],
             "toPaths": burst["dests"],
             "queryKeys": burst["keys"],
             "gapsMs": burst["gapsMs"],
             "firstMs": burst["firstMs"],
             "lastMs": burst["lastMs"],
             # Which side of START_GO_MODE this happened on. Both, on 09-04 —
             # and that is the fact that says the defect is the screen's, not
             # Go Mode's.
             "duringTrip": burst["duringTrip"],
             "outsideTrip": burst["outsideTrip"]},
            trip=trip, boot_health=False)

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
        image = obj.get("image")
        # Armed here, before any of the trip-guessing below: 17.11's note is
        # the case where every one of those guesses comes back empty. The
        # 15:53:50 "Clicking does nothing" landed 2m23s before its ride's
        # START_GO_MODE, so `trip` is None, _recently_ended_trip finds nothing
        # and the note itself is only logged — and until now that was the end
        # of it. The verdict resolves on the tick and is held for whichever
        # trip this session opens next.
        self._arm_note_evidence(
            session, t, text,
            image.strip()[:512] if isinstance(image, str) and image.strip()
            else None)
        if trip is None:
            # This session's ride has just ended. A note typed in the minutes
            # after a ride is about that ride — on 2026-09-09 the 09:03:42
            # note was a sentence about the trip that had closed 53 s earlier
            # — and the session id says whose ride it was, which is stronger
            # evidence than "only one ride happens to be running". Tried
            # before the single-active-trip guess for exactly that reason.
            trip = self._recently_ended_trip(session, t)
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
        # A screenshot from the in-app "Share feedback" screen. The sidecar
        # writes the bytes under ~/otp-debug-logs/feedback/ and puts only the
        # path in the record (preferences_api.py _store_feedback_image), so this
        # is a filename to cite, never an attachment to carry. It has to reach
        # `riderNotes` explicitly: the report agent reads that list, and a path
        # left in the raw log line is a path no report ever mentions — which is
        # the whole of backlog 9.3, where the one screenshot of the 2026-09-04
        # ride reached the record only because it was handed over by hand.
        image = obj.get("image")
        if isinstance(image, str) and image.strip():
            note["image"] = image.strip()[:512]
            context["image"] = note["image"]
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
        if trip.end_ms is not None and trip.report_path:
            # The wrap-up's inputs were written when the ride closed, and the
            # note is the half of a ride report that cannot be re-derived from
            # telemetry. Rewrite the request so the thread reads it. Only when
            # one was asked for: a ride that ended with nothing to report does
            # not grow a wrap-up because a note arrived after it.
            self._write_report_request(trip)
            self.log.info("report request refreshed with a late note "
                          "(%s, %s after the ride closed)"
                          % (trip.session, fmt_ms_span(t - trip.end_ms)))

    def _recently_ended_trip(self, session, t):
        """The ride this session just finished, if it finished just now.

        Newest first, and only a trip that really is over and really is this
        session's: `sessions` rather than `session` because a ride the app
        re-mounted mid-way answers to more than one id (_adopt_continuation),
        and a note typed under the second one is still about that ride.
        """
        for trip in reversed(self.ended_trips):
            if trip.end_ms is None or session not in trip.sessions:
                continue
            if 0 <= t - trip.end_ms <= NOTE_ATTACH_GRACE_MS:
                self.log.info("note attached to the ride that ended %s "
                              "earlier (session=%s)"
                              % (fmt_ms_span(t - trip.end_ms), session))
                return trip
            # Older than the window, and ended_trips is in end order: nothing
            # before this one can be closer.
            if t - trip.end_ms > NOTE_ATTACH_GRACE_MS:
                return None
        return None

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
        self._append_finding(self._findings_path(trip), finding)

    def _append_finding(self, path, finding):
        try:
            with open(path, "a") as f:
                f.write(json.dumps(finding) + "\n")
        except OSError as exc:
            self.log.error("could not append finding: %r" % exc)

    def _findings_path(self, trip):
        return self._findings_path_for(fmt_date(trip.start_ms), trip.session)

    def _findings_path_for(self, date, session):
        """The per-day, per-session findings ledger.

        Split out from _findings_path so a finding with no trip behind it — a
        boot crash, which by construction happens before any ride — lands in
        the file the ride under that session id would use, rather than in a
        second one nobody's report reads.
        """
        return os.path.join(self.watch_dir,
                            "%s-%s.findings.jsonl" % (date, session))

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

    def _send_push(self, title, body, kind="page", bypass_rate_limit=False):
        """Send a Pushover message. Returns True if sent (or dry-run-logged).

        The global 120s rate limit applies to every send but one: the
        "report pending" page carries `bypass_rate_limit`, because it is the
        only page about the ride's RECORD rather than the ride, and on
        2026-09-15 09:40:42 a deviated-streak page 80 s earlier ate it and
        nobody was told ride 1's report was missing. It spends its own
        REPORT_PAGE_MIN_INTERVAL_MS budget in _report_fallback_push instead.
        It still stamps last_push_ms, so it does not turn into a way to send
        two pages in a second.
        """
        now = self.now_ms()
        if (not bypass_rate_limit and self.last_push_ms
                and now - self.last_push_ms < PUSH_MIN_INTERVAL_MS):
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

        The suffix is keyed on the RIDE, not on the earlier report existing.
        It was the other way round until 2026-09-09, and 12.4 walked straight
        through the hole: ride 1 of `mtssjvee-mtc2dx` never got its report
        written (the thread stalled on a permission prompt), so forty minutes
        later ride 2 found no file, was handed ride 1's name, and both request
        files carried `2026-09-08-mtc2dx.md`. Had both threads worked the
        second would have silently overwritten the first — the exact 08-27 /
        08-28 failure this method was built to end, re-entered through the
        back door. `_report_request_path` writes one file per ride and is the
        only per-ride artifact that survives a daemon restart, so counting
        those is how the daemon knows which ride this is.

        The "does the file already exist" walk stays, one step below, as a
        backstop: a report written by hand (and 2026-09-09's ride 2 was) has
        no request file behind it, and nothing here may ever hand back a name
        that is already somebody's report.
        """
        base = os.path.join(self.report_dir, self._ride_slug(trip))
        n = self._ride_ordinal(trip)
        candidate = base + ".md" if n < 2 else "%s-ride%d.md" % (base, n)
        while os.path.exists(candidate):
            n = max(n, 1) + 1
            candidate = "%s-ride%d.md" % (base, n)
        return candidate

    def _ride_ordinal(self, trip):
        """Which ride of this session, on this date, this is — 1-based.

        Counted off the per-ride request files, which are written at the end
        of every ride that had anything to report. A clean ride writes none
        and so does not consume an ordinal: it also wrote no report, so there
        is nothing for the next ride to collide with.

        The date filter matters because the request file's name carries only
        `HHMM` and the phone keeps one session id overnight; the ride slug is
        dated, so only the same date's rides can collide.
        """
        date = fmt_date(trip.start_ms)
        earlier = 0
        pattern = os.path.join(
            self.watch_dir, "report-request-%s-*.json" % trip.session)
        for path in glob.glob(pattern):
            try:
                with open(path) as f:
                    req = json.load(f)
            except (OSError, ValueError):
                continue
            start = req.get("startMs")
            if not isinstance(start, (int, float)):
                continue
            if req.get("date") == date and start < trip.start_ms:
                earlier += 1
        return earlier + 1

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
            # Which web bundle this ride ran on. Since the OTA lane shipped
            # the store version no longer implies it, so a report that omits
            # it cannot say which build a defect belongs to. None when the
            # phone never reported one (a browser, or an app start this daemon
            # did not see).
            "bundle": trip.bundle,
            "bundleNative": trip.bundle_native,
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
        #
        # Exempt from the global 120 s limit and rate-limited on its own,
        # longer budget. See REPORT_PAGE_MIN_INTERVAL_MS: this page is the
        # only notice the rider gets that a ride's findings have no report,
        # it fires ten minutes after the ride already ended, and a page about
        # the ride itself must not be allowed to eat it.
        now = self.now_ms()
        if (self.last_report_page_ms
                and now - self.last_report_page_ms
                < REPORT_PAGE_MIN_INTERVAL_MS):
            self.log.info(
                "report-pending page suppressed (one per %d min): %d findings"
                % (REPORT_PAGE_MIN_INTERVAL_MS // 60000, findings_n))
            self.push_log.append({
                "tsMs": now, "title": "Ride watch",
                "body": "Ride ended — %d findings. Report pending." % findings_n,
                "sent": False, "kind": "fallback",
                "suppressed": "report-page-budget"})
            return False
        self.last_report_page_ms = now
        return self._send_push(
            "Ride watch",
            "Ride ended — %d findings. Report pending; open Claude and say 'ride report'."
            % findings_n,
            kind="fallback", bypass_rate_limit=True)

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

    def _thread_push(self, trip, line, hold_ms=None):
        """Rewrite the digest, then type one line into the thread.

        `hold_ms` is how long the pusher may wait for a pane that is mid-turn
        or sitting on a permission prompt. It reaches only the real tmux
        pusher — the test stubs take (name, line) and a ride must not depend
        on a stub growing a third parameter.
        """
        if not self._thread_ok(trip):
            return False
        try:
            digest = self._write_digest(trip)
        except OSError as exc:
            self.log.error("digest write failed: %r" % exc)
            digest = self._digest_path(trip)
        # Detail lives in the file; the line says only what changed.
        #
        # The digest path is bounded separately, because one_line() cuts from
        # the RIGHT and the path is the rightmost thing here — so a long
        # message used to eat it, the pusher then handed the thread a line
        # ending in "…", and in the test harness (which opens the path it is
        # given) the push was dropped outright with only a log line to show
        # for it. RIDER_NOTE_MAX_CHARS is 500 against a THREAD_LINE_MAX of
        # 400, so the rider only had to type a paragraph; adding a clause to
        # the wrap-up line on 2026-09-17 came within 21 characters of the same
        # thing. Whatever gets cut, it is never the two paths the thread needs
        # in order to go and read anything.
        suffix = " — digest: %s" % digest
        text = one_line("[ride-watch] %s" % line,
                        limit=max(1, THREAD_LINE_MAX - len(suffix))) + suffix
        trip.thread_cursor = len(trip.thread_events)
        trip.last_thread_push_ms = self.now_ms()
        trip.thread_pushes += 1
        push = self.push_line
        if push is None:
            if self.replay:
                return False
            push = self._tmux_push
        try:
            if hold_ms is not None and push is self._tmux_push:
                push(trip.thread["tmux"], text, hold_ms=hold_ms)
            else:
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

    def _daemon_source_drift(self):
        """Which of DAEMON_SOURCE_FILES no longer match what is running.

        The half of the version question that _repo_head_now cannot answer.
        A commit to `deployment/nginx/otp-common.conf.tmpl` moves HEAD and
        changes nothing this process does; an edit to `ride_watch.py` changes
        everything and need not be committed at all. Comparing content
        digests answers the second question and ignores the first.

        Same TTL as the head check, and no subprocess at all — this is the
        one provenance question that still works when git is unavailable.
        """
        now = int(time.time() * 1000)
        if (self._source_drift_checked_ms
                and now - self._source_drift_checked_ms < HEAD_RECHECK_MS):
            return self._source_drift_cached
        self._source_drift_checked_ms = now
        current = _source_digests()
        self._source_drift_cached = [
            rel for rel in DAEMON_SOURCE_FILES
            if current.get(rel) != DAEMON_SOURCE_DIGESTS.get(rel)]
        return self._source_drift_cached

    def _daemon_lines(self):
        """Who is running, in two lines, at the top of everything a human reads.

        On 2026-08-28 a daemon five days stale produced five false
        stalled-progress findings, missed the arrival event, and nearly
        overwrote a report — and no artifact it wrote said which version it
        was. The ride thread sat reading source on disk that the process in
        memory had never loaded.

        STALE is now keyed on the daemon's OWN files, not on HEAD. Keying it
        on HEAD made it fire for every commit anywhere in this repo: on
        2026-09-08 the running daemon was twelve commits "behind" with an
        empty `git diff` over ride-watch/, and two rides opened by telling the
        rider their watcher might be reporting code that no longer exists.
        The 8/28 daemon still trips it — that one's own source HAD changed —
        and a tree that moved without touching ride-watch/ now gets a plain
        statement of fact with no claim about the findings.
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
        drift = self._daemon_source_drift()
        moved = bool(head and base != "unknown" and head != base)
        if drift:
            out.append(
                "Tree now: %s  ** STALE — this daemon's own source has changed"
                " since it started (%s). Findings may come from code that no"
                " longer exists. Restart:"
                " `systemctl --user restart ride-watch` **"
                % (head or "unknown",
                   ", ".join(os.path.basename(r) for r in drift)))
        elif moved:
            # Not a warning. The tree moving is the ordinary state of a repo
            # four agents commit into; it says nothing about this process.
            out.append(
                "Tree now: %s (%sthis daemon is %s) — the tree moved, this"
                " daemon did not: none of ride-watch/ changed."
                % (head, "%d commit(s) back, " % behind if behind else "",
                   base))
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
            if note.get("image"):
                L.append("  - screenshot: `%s`" % note["image"])
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

    def _tmux_push(self, name, line, hold_ms=None):
        self._thread_enqueue(("push", name, line,
                              THREAD_PUSH_HOLD_MS if hold_ms is None
                              else hold_ms))
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
                        self._tmux_push_blocking(
                            job[1], job[2],
                            job[3] if len(job) > 3 else THREAD_PUSH_HOLD_MS)
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

    def _pane_state(self, name):
        """What the pane is doing, from its own screen: the check 12.4 wanted.

        "ready"   at the ❯ prompt, nothing pending — safe to type.
        "busy"    running a turn. The tty buffers; safe to type, better to wait.
        "blocked" a permission dialog is up. Typing answers the DIALOG and the
                  line is lost, which is how ride-1040's wrap-up became the
                  answer to a 418-second-old prompt.
        "unknown" capture failed or the screen says nothing we recognise —
                  type, exactly as this did before there was a check at all.
        """
        res = self._tmux(["capture-pane", "-p", "-t", name])
        if res.returncode != 0:
            return "unknown"
        pane = res.stdout or ""
        if any(m in pane for m in THREAD_BLOCKED_MARKERS):
            return "blocked"
        if any(m in pane for m in THREAD_BUSY_MARKERS):
            return "busy"
        if THREAD_READY_MARKER in pane:
            return "ready"
        return "unknown"

    def _wait_for_pane(self, name, hold_ms):
        """Hold the push until the pane is listening, or the hold runs out.

        Returns the state it gave up in. Serialising this on the worker thread
        is deliberate: a pane that cannot take this line cannot take the next
        one either, and letting a heartbeat overtake a wrap-up is the ordering
        bug _thread_worker_loop exists to prevent.
        """
        now = time.time()
        hold = max(0, hold_ms) / 1000.0
        deadlines = {"blocked": now + hold,
                     "busy": now + min(hold, THREAD_PUSH_BUSY_HOLD_MS / 1000.0)}
        state = self._pane_state(name)
        logged = False
        while state in deadlines and time.time() < deadlines[state]:
            if not logged:
                self.log.info("ride thread %s is %s; holding the push"
                              % (name, state))
                logged = True
            time.sleep(THREAD_PUSH_POLL_S)
            state = self._pane_state(name)
        return state

    def _page_blocked_thread(self, name):
        """The rider can clear this one themselves, and only they can.

        A pane stuck on a permission prompt is not a daemon problem: the
        dialog is sitting in the rider's Claude app waiting for a tap. Paged
        off the ride budget, like the missing-report fallback, and once per
        pane — a second buzz about the same dialog tells them nothing new.
        """
        if name in self._thread_blocked_paged:
            return
        self._thread_blocked_paged.add(name)
        self._send_push(
            "Ride watch",
            "Ride thread is waiting on a permission prompt — open Claude and"
            " answer it.",
            kind="thread-blocked")

    def _tmux_push_blocking(self, name, line, hold_ms=THREAD_PUSH_HOLD_MS):
        # Is the pane actually listening? Before this it was never asked, and
        # five consecutive rides lost their wrap-up to the answer (12.4).
        state = self._wait_for_pane(name, hold_ms)
        if state == "blocked":
            # Never type. Enter here answers the dialog and the line is gone.
            self.log.error("ride thread %s still blocked on a permission"
                           " prompt after %ds; push NOT delivered: %s"
                           % (name, hold_ms // 1000, one_line(line, 120)))
            self._thread_pushes_undelivered += 1
            self._page_blocked_thread(name)
            return
        if state == "busy":
            # The tty buffers it. Say so, so the log shows a late line rather
            # than a lost one.
            self.log.warn("ride thread %s still busy after %ds; typing anyway"
                          % (name, hold_ms // 1000))
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

        Emptied by _check_report_deadlines when the wrap-up settles or its
        deadline expires, so a pane is protected for at most
        REPORT_DEADLINE_MS + PROMOTION_DEADLINE_MS and a dead one cannot pin
        the namespace forever. The promotion half of that bound is deliberate:
        the backlog write needs a live console, and the pane it needs is the
        one this set spares (15.8).
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
            # Named on every ride, "unknown" included: an OTA bundle is what a
            # defect belongs to now, and a wrap-up that has to go and guess it
            # from the log gets it wrong.
            "- Bundle: %s%s" % (
                trip.bundle or "unknown",
                " (native %s)" % trip.bundle_native if trip.bundle_native
                else ""),
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
        # Above the rides, because a phone that will not start is the one
        # thing here that no ride can be running through. These findings have
        # no trip section to live in — that is the whole point of them.
        if self.boot_events:
            lines.append("## App boot health (%d, newest first)"
                         % len(self.boot_events))
            lines.append("")
            for ev in reversed(self.boot_events):
                paged = {True: " [paged]", "dry-run": " [paged]",
                         "pending": " [paging]"}.get(ev.get("paged"), "")
                lines.append("- %s [%s] %s on %s: %s%s" % (
                    ev["time"], ev["severity"], ev["rule"],
                    short_session(ev.get("device")),
                    one_line(ev["summary"], 200), paged))
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
                    if note.get("image"):
                        section.append("  - screenshot: `%s`" % note["image"])
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
        # Before the trips close, so a note still inside its grace window can
        # still be filed on the ride it belongs to. EOF is the end of every
        # window there is; an error that has not arrived by now never will.
        self._check_pending_notes(self.now_ms(), force=True)
        for trip in self._active_trips():
            self._end_trip(trip, trip.last_event_ms, "replay-eof")
        # Bursts that never had a trip to be closed by. EOF is the end of the
        # quiet window by definition; without this a replay of a rider who
        # only ever touched the settings tab would emit nothing.
        for session in list(self.panel_teardowns):
            self._flush_panel_teardown(session, self.now_ms(), force=True)
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

    def __init__(self, log, drains=None):
        self.log = log
        self.path = None
        self.fh = None
        self.offset = 0
        self.buf = b""
        # Diagnostics for the 09-15 miss (see ADOPT_START_LOOKBACK_MS). The
        # daemon acted on none of 260 consecutive lines and there was nothing
        # in daemon.log or the journal between "opened ... from start" at
        # 05:00:03 and the adopt at 09:26:38 to say what the follower had
        # done with them -- not a byte count, not an offset, nothing. So every
        # drain that delivers lines now leaves a trace.
        # Shared with the RideWatch that consumes this stream, so the rule
        # engine can print the follower's recent history at the one moment it
        # matters (a trip opening or being adopted) without the follower
        # knowing anything about trips.
        self.drains = (drains if drains is not None
                       else collections.deque(maxlen=TAILER_DRAIN_RING))
        self.lines_out = 0
        self._log_window_lines = 0
        self._log_window_polls = 0
        self._last_drain_log = 0.0

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
        start_offset = self.offset
        self.fh.seek(self.offset)
        chunk = self.fh.read(size - self.offset)
        self.offset = self.fh.tell()
        data = self.buf + chunk
        lines = data.split(b"\n")
        self.buf = lines.pop()  # possibly-partial tail
        delivered = [raw for raw in lines if raw.strip()]
        for raw in delivered:
            on_line(raw.decode("utf-8", "replace"))
        self._note_drain(start_offset, delivered)

    def _note_drain(self, start_offset, delivered):
        """Record — and, at a rate limit, log — what one poll handed over.

        The whole point is that this fires on the poll ITSELF, before any rule
        sees the lines: on 09-15 the question that could not be answered was
        whether the follower ever read records 69-329 at all, and no evidence
        either way existed. Now a drain that delivers lines always lands in the
        ring (dumped when a trip opens or is adopted) and lands in daemon.log
        at most once a TAILER_DRAIN_LOG_INTERVAL_S, so a 1 Hz ride writes one
        line a minute rather than one a second.
        """
        if not delivered:
            return
        self.lines_out += len(delivered)
        summary = {
            "atMs": int(time.time() * 1000),
            "lines": len(delivered),
            "bytes": self.offset - start_offset,
            "from": start_offset,
            "to": self.offset,
            "firstT": peek_record_ms(delivered[0]),
            "lastT": peek_record_ms(delivered[-1]),
        }
        self.drains.append(summary)
        self._log_window_lines += len(delivered)
        self._log_window_polls += 1
        now = time.time()
        if now - self._last_drain_log < TAILER_DRAIN_LOG_INTERVAL_S:
            return
        self._last_drain_log = now
        self.log.info(
            "stream: %d line(s) this poll (%d byte(s), offset %d->%d, t %s..%s)"
            "; %d line(s) over %d poll(s) since the last note, %d total"
            % (summary["lines"], summary["bytes"], summary["from"],
               summary["to"], fmt_hms(summary["firstT"]),
               fmt_hms(summary["lastT"]), self._log_window_lines,
               self._log_window_polls, self.lines_out))
        self._log_window_lines = 0
        self._log_window_polls = 0


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
    # So the adoption path's look-back reads the file being replayed, not
    # today's live one. Without this a replay of an old log would go looking
    # for its START_GO_MODE in today's telemetry.
    watch.stream_path = path
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
    # One ring, shared: the follower fills it, the rule engine prints it when
    # a trip opens. See Tailer._note_drain.
    tailer = Tailer(log, drains=watch.stream_drains)
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
