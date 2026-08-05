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

# Rule thresholds (ms unless noted)
STARTUP_LOOKBACK_MS = 5 * 60 * 1000        # scan back this far at startup
LOOKBACK_TAIL_BYTES = 16 * 1024 * 1024     # ...reading at most this much tail
SESSION_TIMEOUT_MS = 15 * 60 * 1000        # trip ends after this much silence
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
NOTIFICATION_REPEAT_COUNT = 3              # fires on the 3rd
# progress-without-motion. Map-matching noise reported to the rider as travel.
# On 7/31 the swing was a tenth of a point (0.31 -> 0.21 -> 0.31) inside a 7m
# circle, which is below this threshold and should stay quiet; the same
# signature at 30 points would mean the app is inventing a journey.
MOTION_PROGRESS_PCT = 5.0                  # percentage points gained
MOTION_DISPLACEMENT_M = 15.0               # ...while the fix stayed this close
MOTION_COOLDOWN_MS = 5 * 60 * 1000
MAX_PAGES_PER_TRIP = 2
PUSH_MIN_INTERVAL_MS = 120 * 1000
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
    "missed-bus-while-riding": 40,
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
        self.last_rider_action_ms = 0
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
                 thread_enabled=None):
        self.dry_run = dry_run
        self.replay = replay
        self.watch_dir = watch_dir
        os.makedirs(watch_dir, exist_ok=True)
        self.log = log or Log(os.path.join(watch_dir, "daemon.log"))
        self.trips = {}               # session -> Trip (active)
        self.all_findings = []        # every finding this process has emitted
        self.ended_trips = []         # Trip objects, for replay/test inspection
        self.recently_ended = {}      # session -> end_ms (blocks re-adoption)
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
        self.thread_enabled = (THREAD_ENABLED if thread_enabled is None
                               else thread_enabled)
        self._thread_lock = threading.RLock()
        self._thread_jobs = []         # queued tmux work (worker thread)
        self._thread_wake = threading.Event()
        self._thread_worker = None
        self._thread_status = {}       # tmux name -> True/False once known

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
                return json.load(f).get("lastTrip")
        except (OSError, ValueError):
            return None

    def _save_state(self):
        try:
            with open(self._state_path(), "w") as f:
                json.dump({"lastTrip": self.last_trip_summary}, f)
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

        trip = self.trips.get(session)
        if trip:
            trip.last_event_ms = max(trip.last_event_ms, t)

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
        elif trip is not None:
            if kind == "console":
                self._rule_console(trip, t, obj)
            elif typ == "UPDATE_POSITION":
                trip.last_pos_ms = max(trip.last_pos_ms, t)
                trip.gps_gap_open = False
                self._on_position(trip, obj.get("payload") or {})
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
            elif typ == "SET_ACTIVE_ITINERARY":
                # Rider picked an itinerary from the list — explicit action.
                trip.last_rider_action_ms = t
        elif typ == "UPDATE_PROGRESS":
            # Go Mode is clearly active but we never saw START_GO_MODE
            # (daemon started mid-trip): adopt the trip.
            ended = self.recently_ended.get(session, 0)
            if t - ended > 60 * 1000:
                trip = Trip(session, t, None, adopted=True)
                self.trips[session] = trip
                self.log.info("adopted mid-stream trip for session %s" % session)
                # An adopted trip is a ride in progress — usually the daemon was
                # just restarted under a rider who is still on the bus — so it
                # gets a thread too, marked as adopted in the digest.
                self._begin_ride_thread(trip, t)
                self._on_progress(trip, t, obj.get("payload") or {})
                self._mark_dirty()

        # Time-based rules ride on the advancing clock.
        self.check_timers()

    # -- state machine ------------------------------------------------------

    def _on_start_go_mode(self, session, t, obj):
        payload = obj.get("payload") or {}
        summary = summarize_itinerary(payload)
        trip = self.trips.get(session)
        if trip is None:
            trip = Trip(session, t, summary)
            self.trips[session] = trip
            self.log.info("trip started: session=%s itinerary=%s" % (
                session, itinerary_one_liner(summary)))
            self._begin_ride_thread(trip, t)
        else:
            # Itinerary replacement mid-trip.
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
        self._mark_dirty()

    def _end_trip(self, trip, t, reason):
        # Flush first: a page must not be lost because the trip ended three
        # seconds into its coalescing window.
        self._flush_pages(trip, t, force=True)
        trip.end_ms = t
        trip.end_reason = reason
        del self.trips[trip.session]
        self.ended_trips.append(trip)
        self.recently_ended[trip.session] = t
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
            self._report_fallback_push(trip)
        self._mark_dirty()
        self.write_status(force=True)

    def check_timers(self):
        """Silence-based rules + trip timeout. Called per event and on ticks.

        This is also how a buffered page gets out when the log goes quiet: the
        live loop ticks every 5s regardless of traffic, so a closed coalescing
        window is never waiting on the next telemetry line.
        """
        now = self.now_ms()
        for trip in list(self.trips.values()):
            if now - trip.last_event_ms > SESSION_TIMEOUT_MS:
                self._end_trip(trip, trip.last_event_ms, "timeout")
                continue
            self._flush_pages(trip, now)
            # gps-gap: no position fix for >60s mid-trip
            if not trip.gps_gap_open and now - trip.last_pos_ms > GPS_GAP_MS:
                trip.gps_gap_open = True
                gap_s = (now - trip.last_pos_ms) // 1000
                self._finding(trip, now, "gps-gap", "warn",
                              "no GPS fix for %ds mid-trip" % gap_s,
                              {"lastFixMs": trip.last_pos_ms})
            # deviated-streak may mature between UPDATE_PROGRESS ticks
            self._check_deviated_streak(trip, now)
            self._check_stalled(trip, now)
            self._maybe_heartbeat(trip, now)

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
            "tMs": t,
        }
        self._mark_dirty()

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
        self._finding(
            trip, now, "stalled-progress", "warn",
            "stationary %dm inside leg %s with the trip still active" % (
                held_ms // 60000, leg),
            {"heldMs": held_ms, "legIndex": leg,
             "lat": anchor[0][0], "lon": anchor[0][1],
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
        self._rule_match_distance_absurd(trip, t, match)
        self._rule_match_trip_disagrees(trip, t, match)

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
        self._rule_notification_repeat(trip, t, p)

    def _rule_notification_repeat(self, trip, t, p):
        """The same alert, over and over, at a rider who cannot make it stop.

        Keyed on (title, message) rather than the notification id, because the
        id carries a fresh `Date.now()` on every fire — the very defect that
        let the 7/31 storm through. The id minus that suffix
        (`UPCOMING_TURN_<legStart>_<cue>_<stage>`) would also work and survives
        the message drifting "173 ft" -> "175 ft" on GPS jitter; the stricter
        key is the conservative choice, since a page costs the rider one of
        their two interrupts. On the 7/31 log this fires at 11:53:38 — the 3rd
        of 14 buzzes, six minutes before the rider gave up and typed it out.
        """
        title = (p.get("title") or p.get("type") or "").strip()
        message = (p.get("message") or "").strip()
        if not title and not message:
            return
        key = (title, message)
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
             "type": p.get("type")},
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
            active = list(self.trips.values())
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

    def _write_report_request(self, trip):
        req = {
            "session": trip.session,
            "date": fmt_date(trip.start_ms),
            "startMs": trip.start_ms,
            "endMs": trip.end_ms,
            "findingsPath": self._findings_path(trip),
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
        path = os.path.join(self.watch_dir, "report-request-%s.json" % trip.session)
        with open(path, "w") as f:
            json.dump(req, f, indent=2)
        self.log.info("report request written: %s" % path)
        return path

    def _report_fallback_push(self, trip):
        self._send_push(
            "Ride watch",
            "Ride ended — %d findings. Report pending; open Claude and say 'ride report'."
            % len(trip.findings),
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
        return "%s-%s" % (THREAD_NAME_PREFIX, datetime.datetime.fromtimestamp(
            trip.start_ms / 1000).strftime("%H%M"))

    def _thread_display(self, trip):
        """What the rider sees in their Claude app list."""
        return "%s %s" % (THREAD_NAME_PREFIX, datetime.datetime.fromtimestamp(
            trip.start_ms / 1000).strftime("%m-%d %H:%M"))

    def _begin_ride_thread(self, trip, t):
        """Spawn the ride's thread and send the kickoff line."""
        self._thread_event(trip, t, "trip started%s — %s" % (
            " (adopted mid-stream)" if trip.adopted else "",
            itinerary_one_liner(trip.itinerary)))
        if not self.thread_enabled:
            self.log.info("ride thread disabled (RIDE_THREAD_ENABLED=0)")
            return
        name, display = self._thread_name(trip), self._thread_display(trip)
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
                 "%Y-%m-%d %H:%M:%S"),
             "Pushes so far: %d" % trip.thread_pushes, ""]
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
        req = os.path.join(self.watch_dir,
                           "report-request-%s.json" % trip.session)
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

    def _kill_previous_threads(self, keep=None):
        """The new ride's thread is the rider's thread; retire the old ones."""
        res = self._tmux(["list-sessions", "-F", "#{session_name}"])
        if res.returncode != 0:
            return []          # no tmux server yet: nothing to clean up
        killed = []
        for name in ride_thread_sessions((res.stdout or "").split()):
            if name == keep:
                continue
            if self._tmux(["kill-session", "-t", name]).returncode == 0:
                killed.append(name)
        if killed:
            self.log.info("previous ride thread(s) killed: %s"
                          % ", ".join(killed))
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
        # list(): the tailer may be starting or ending a trip while this runs.
        for trip in list(self.trips.values()):
            now = self.now_ms()
            lines.append("## Active trip — session %s%s" % (
                trip.session, " (adopted mid-stream)" if trip.adopted else ""))
            lines.append("")
            lines.extend(self._trip_state_lines(trip, now))
            lines.append("")
            # The rider's own words go above the machine findings: when both
            # exist, the note is the one that says what actually went wrong.
            if trip.notes:
                lines.append("### Rider notes (%d, newest first)" % len(trip.notes))
                lines.append("")
                for note in reversed(trip.notes[-20:]):
                    c = note["context"]
                    where = "leg %s" % c.get("legIndex")
                    if isinstance(c.get("legProgressPct"), (int, float)):
                        where += " at %s" % fmt_pct(c["legProgressPct"])
                    if c.get("stopsRemaining") is not None:
                        where += ", %s stops left" % c["stopsRemaining"]
                    if c.get("status"):
                        where += ", %s" % c["status"]
                    lines.append("- %s — %s  _(%s)_" % (
                        note["time"], note["text"], where))
                lines.append("")
            if trip.findings:
                lines.append("### Findings (%d, newest first)" % len(trip.findings))
                lines.append("")
                for fnd in reversed(trip.findings[-30:]):
                    lines.append("- %s [%s] %s: %s" % (
                        fnd["time"], fnd["severity"], fnd["rule"], fnd["summary"]))
            else:
                lines.append("### Findings: none")
            lines.append("")
        path = os.path.join(self.watch_dir, "current-ride.md")
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, path)
        except OSError as exc:
            self.log.error("status write failed: %r" % exc)

    # -- finalize (replay EOF / shutdown) -----------------------------------

    def flush_pending_pages(self):
        """Close every open coalescing window now (shutdown path).

        A clean shutdown leaves active trips un-ended so a restart re-adopts
        them, but a page still inside its window has nowhere to be re-adopted
        from — send it before going away.
        """
        for trip in list(self.trips.values()):
            self._flush_pages(trip, self.now_ms(), force=True)

    def finalize_replay(self):
        """End any still-active trips at replay EOF."""
        for trip in list(self.trips.values()):
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
    replay can never clobber the live daemon's current-ride.md.
    """
    if watch is None:
        watch = RideWatch(dry_run=True, replay=True,
                          watch_dir=watch_dir or os.path.join(WATCH_DIR, "replay"))
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
    log.info("ride-watch starting (dry_run=%s, log_dir=%s, watch_dir=%s)"
             % (watch.dry_run, DEBUG_LOG_DIR, watch.watch_dir))
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
             % len(watch.trips))
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
