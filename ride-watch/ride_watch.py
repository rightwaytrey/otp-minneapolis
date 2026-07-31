#!/usr/bin/env python3
"""ride-watch: live anomaly watcher for transit-navigation Go Mode telemetry.

Follows the day's debug JSONL stream (written by the Flask sidecar's
/api/debug-log endpoint), runs a per-trip rule engine over the redux action
stream, pages the rider via Pushover for at most 2 high-value findings per
ride, keeps a live status file any Claude session can read, and requests a
post-ride report from a headless `claude -p` run when a ride with findings
ends.

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
import os
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
CLAUDE_BIN = os.environ.get("RIDE_WATCH_CLAUDE", "claude")
REPO_DIR = os.environ.get(
    "RIDE_WATCH_REPO", os.path.join(HOME, "projects", "otp-minneapolis")
)
PROMPT_PATH = os.path.join(REPO_DIR, "ride-watch", "report-prompt.md")

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
MAX_PAGES_PER_TRIP = 2
PUSH_MIN_INTERVAL_MS = 120 * 1000
STATUS_DEBOUNCE_MS = 2000
STOP_INCREASE_COOLDOWN_MS = 60 * 1000

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


def short_session(session):
    if not session:
        return "unknown"
    return session.rsplit("-", 1)[-1]


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
        self.gps_gap_open = False
        self.prev_stops = None
        self.stops_swap_pending = False   # itinerary swapped since last count
        self.collapse_fired_seq = set()
        self.stop_increase_last_ms = 0
        self.deviated_since_ms = None
        self.deviated_fired = False
        self.reroute_times = collections.deque()
        self.reroute_storm_last_ms = 0
        self.prev_dist = None
        self.last_rider_action_ms = 0
        self.console_seen = set()
        self.findings = []
        self.pages_sent = 0
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
    def __init__(self, dry_run=DRY_RUN, replay=False, watch_dir=WATCH_DIR, log=None):
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
        self._report_threads = []

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
        elif trip is not None:
            if kind == "console":
                self._rule_console(trip, t, obj)
            elif typ == "UPDATE_POSITION":
                trip.last_pos_ms = max(trip.last_pos_ms, t)
                trip.gps_gap_open = False
            elif typ == "UPDATE_PROGRESS":
                self._on_progress(trip, t, obj.get("payload") or {})
            elif typ == "UPDATE_ROUTE_MATCH":
                self._rule_distance_spike(trip, t, obj.get("payload") or {})
            elif typ == "SET_RIDING":
                self._on_set_riding(trip, t, obj.get("payload") or {})
            elif typ == "CLEAR_RIDING":
                trip.riding = None
                self._mark_dirty()
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
            self._rule_aboard_swap(trip, t)
        self._mark_dirty()

    def _end_trip(self, trip, t, reason):
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
        if n > 0:
            req_path = self._write_report_request(trip)
            self._request_report(trip, req_path)
        self._mark_dirty()
        self.write_status(force=True)

    def check_timers(self):
        """Silence-based rules + trip timeout. Called per event and on ticks."""
        now = self.now_ms()
        for trip in list(self.trips.values()):
            if now - trip.last_event_ms > SESSION_TIMEOUT_MS:
                self._end_trip(trip, trip.last_event_ms, "timeout")
                continue
            # gps-gap: no position fix for >60s mid-trip
            if not trip.gps_gap_open and now - trip.last_pos_ms > GPS_GAP_MS:
                trip.gps_gap_open = True
                gap_s = (now - trip.last_pos_ms) // 1000
                self._finding(trip, now, "gps-gap", "warn",
                              "no GPS fix for %ds mid-trip" % gap_s,
                              {"lastFixMs": trip.last_pos_ms})
            # deviated-streak may mature between UPDATE_PROGRESS ticks
            self._check_deviated_streak(trip, now)

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
                         "legProgress": progress,
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

    def _rule_aboard_swap(self, trip, t):
        if trip.riding is None:
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

    # -- findings, paging, surfaces ----------------------------------------

    def _finding(self, trip, ts_ms, rule, severity, summary, context, push_body=None):
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
        # Page before persisting so the record says whether the rider was told.
        if severity == "page":
            finding["paged"] = (
                self._page(trip, push_body) if push_body else False)
        try:
            with open(self._findings_path(trip), "a") as f:
                f.write(json.dumps(finding) + "\n")
        except OSError as exc:
            self.log.error("could not append finding: %r" % exc)
        self._mark_dirty()

    def _findings_path(self, trip):
        return os.path.join(
            self.watch_dir,
            "%s-%s.findings.jsonl" % (fmt_date(trip.start_ms), trip.session))

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

    def _write_report_request(self, trip):
        req = {
            "session": trip.session,
            "date": fmt_date(trip.start_ms),
            "startMs": trip.start_ms,
            "endMs": trip.end_ms,
            "findingsPath": self._findings_path(trip),
            "itinerarySummary": trip.itinerary or {"unavailable": True},
            "findingsCount": len(trip.findings),
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

    def _request_report(self, trip, req_path):
        if self.dry_run:
            self.log.info("DRY-RUN: skipping claude report invocation for %s"
                          % trip.session)
            return
        try:
            with open(PROMPT_PATH) as f:
                prompt = f.read()
        except OSError as exc:
            self.log.error("report prompt unreadable: %r" % exc)
            self._report_fallback_push(trip)
            return
        env = dict(os.environ)
        env["RIDE_WATCH_REQUEST"] = req_path
        out_path = os.path.join(self.watch_dir,
                                "report-claude-%s.log" % trip.session)
        try:
            out = open(out_path, "a")
            proc = subprocess.Popen(
                [CLAUDE_BIN, "-p", prompt],
                cwd=REPO_DIR, env=env,
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.log.error("claude binary not found (%s)" % CLAUDE_BIN)
            self._report_fallback_push(trip)
            return
        except OSError as exc:
            self.log.error("claude spawn failed: %r" % exc)
            self._report_fallback_push(trip)
            return
        self.log.info("post-ride report agent started (pid %d, log %s)"
                      % (proc.pid, out_path))

        def waiter():
            rc = proc.wait()
            out.close()
            if rc != 0:
                self.log.error("report agent exited %d" % rc)
                self._report_fallback_push(trip)
            else:
                self.log.info("report agent finished ok for %s" % trip.session)

        th = threading.Thread(target=waiter, daemon=True)
        th.start()
        self._report_threads.append(th)

    # -- live status file ---------------------------------------------------

    def _mark_dirty(self):
        self._status_dirty = True

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
        for trip in self.trips.values():
            now = self.now_ms()
            lines.append("## Active trip — session %s%s" % (
                trip.session, " (adopted mid-stream)" if trip.adopted else ""))
            lines.append("")
            lines.append("- Started: %s %s" % (fmt_date(trip.start_ms), fmt_hms(trip.start_ms)))
            lines.append("- Itinerary: %s" % itinerary_one_liner(trip.itinerary))
            if trip.swap_seq:
                lines.append("- Itinerary swaps: %d (last %s)" % (
                    trip.swap_seq, fmt_hms(trip.swap_times[-1])))
            p = trip.progress
            if p:
                stops = ""
                if p.get("stopsRemaining") is not None:
                    stops = ", %s stops left (next: %s)" % (
                        p["stopsRemaining"], p.get("nextStopName"))
                prog = p.get("currentLegProgress")
                prog_s = "%.0f%%" % prog if isinstance(prog, (int, float)) else "?"
                lines.append("- Leg %s at %s, status %s%s" % (
                    p.get("currentLegIndex"), prog_s, p.get("status"), stops))
            if trip.riding:
                lines.append("- Riding: trip %s vehicle %s (%s) since %s" % (
                    trip.riding.get("tripId"), trip.riding.get("vehicleId"),
                    trip.riding.get("headsign"), fmt_hms(trip.riding.get("boardedAt"))))
            else:
                lines.append("- Riding: not aboard")
            lines.append("- Last fix: %ds ago" % max(0, (now - trip.last_pos_ms) // 1000))
            lines.append("- Pages sent: %d/%d" % (trip.pages_sent, MAX_PAGES_PER_TRIP))
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

    # Clean shutdown: keep active trips un-ended (a restart re-adopts them);
    # just leave a fresh status file behind.
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
