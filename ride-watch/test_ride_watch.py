#!/usr/bin/env python3
"""Tests for ride-watch. Stdlib only:  python3 ride-watch/test_ride_watch.py

Three layers:
  * synthetic streams that exercise each rule and the state machine in
    isolation,
  * a replay of the real 2026-07-29 telemetry, which asserts that the
    afternoon's incident is actually caught and that the rider would not
    have been paged more than twice, and
  * the ride thread: lifecycle and push cadence, replayed against both real
    logs, with tmux and `claude` behind stubs.

Nothing here spawns a process, touches tmux, or reaches the network.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ride_watch  # noqa: E402
from ride_watch import (  # noqa: E402
    Log, RideWatch, Trip, read_pushover_creds, ride_thread_sessions,
    run_replay)


def quiet_watch(watch_dir, replay=True, spawn_thread=None, push_line=None,
                thread_enabled=None):
    """A watcher whose daemon log does not spam the test runner.

    report_dir is pinned under the temp dir: _report_path stats the vault to
    pick a non-colliding name, and a test must neither read the rider's real
    notes nor hand back a path pointing into them.
    """
    log = Log(os.path.join(watch_dir, "daemon.log"), echo=False)
    reports = os.path.join(watch_dir, "reports")
    os.makedirs(reports, exist_ok=True)
    return RideWatch(dry_run=True, replay=replay, watch_dir=watch_dir, log=log,
                     spawn_thread=spawn_thread, push_line=push_line,
                     thread_enabled=thread_enabled, report_dir=reports)


def read_text(path):
    with open(path) as f:
        return f.read()


def report_requests(watch_dir):
    """Every request file in the dir. The name carries the ride's start time
    (one file per ride, not per session), so tests match on the session stem."""
    return sorted(glob.glob(os.path.join(
        watch_dir, "report-request-%s-*.json" % SESSION)))

REAL_LOG = os.path.join(
    os.path.expanduser("~"), "otp-debug-logs", "debug-2026-07-29.jsonl")

# The 2026-07-29 incident: the app swapped the itinerary out from under the
# rider while they were aboard the Orange Line, flipped the trip id, and then
# claimed one stop remaining at the very start of the leg.
INCIDENT_START_MS = 1785364020000
INCIDENT_END_MS = 1785364400000

T0 = 1785360000000  # arbitrary base for synthetic streams
SESSION = "test-session"


def transit_itinerary():
    """A walk -> bus -> walk itinerary, shaped like a real START_GO_MODE."""
    return {
        "itinerary": {
            "startTime": T0,
            "endTime": T0 + 1800000,
            "duration": 1800,
            "legs": [
                {"mode": "WALK", "transitLeg": False,
                 "from": {"name": "Home"}, "to": {"name": "Stop A"}},
                {"mode": "BUS", "transitLeg": True,
                 "route": {"shortName": "5", "longName": "Route 5"},
                 "headsign": "Downtown",
                 "from": {"name": "Stop A"}, "to": {"name": "Stop B"}},
                {"mode": "WALK", "transitLeg": False,
                 "from": {"name": "Stop B"}, "to": {"name": "Work"}},
            ],
        }
    }


class StreamBuilder:
    """Builds a synthetic JSONL-shaped event stream with a moving clock."""

    def __init__(self, session=SESSION, t=T0, device=None):
        self.session = session
        # The real records carry the phone's id. It is what tells a remount
        # (new session id, same device) from a second phone.
        self.device = device
        self.t = t
        self.events = []

    def at(self, offset_ms):
        self.t = T0 + offset_ms
        return self

    def advance(self, ms):
        self.t += ms
        return self

    def action(self, typ, payload=None, **kw):
        ev = {"type": typ, "payload": payload or {}, "t": self.t,
              "session": self.session, "recv": self.t / 1000.0, "kind": "action"}
        if self.device:
            ev["device"] = self.device
        ev.update(kw)
        self.events.append(ev)
        return self

    def console(self, level, args):
        self.events.append({"kind": "console", "level": level, "args": args,
                            "t": self.t, "session": self.session,
                            "recv": self.t / 1000.0})
        return self

    def start(self, payload=None):
        return self.action("START_GO_MODE", payload or transit_itinerary())

    def stop(self):
        return self.action("STOP_GO_MODE")

    def progress(self, leg=1, prog=10.0, status="on_track", stops=None,
                 next_stop="Stop B", dest=None):
        p = {"currentLegIndex": leg, "currentLegProgress": prog,
             "status": status, "nextStopName": next_stop}
        if stops is not None:
            p["stopsRemaining"] = stops
        if dest is not None:
            # Straight-line metres to the destination. In the real stream
            # since 2026-08-28; it is what tells a re-plan that converges from
            # one that does not.
            p["distanceToDestination"] = dest
        return self.action("UPDATE_PROGRESS", p)

    # Downtown Minneapolis, and a metre is ~9e-6 degrees of latitude — enough
    # to express "jittered in place" and "actually went somewhere" without a
    # geo library. The shape matches the real payload (coords at the top of
    # the payload, not nested under `position`).
    LAT, LON = 44.97780, -93.26500

    def position(self, lat=None, lon=None, accuracy=5.0):
        return self.action("UPDATE_POSITION", {
            "coords": {"latitude": self.LAT if lat is None else lat,
                       "longitude": self.LON if lon is None else lon,
                       "accuracy": accuracy},
            "timestamp": self.t})

    def position_metres_north(self, metres, **kw):
        return self.position(lat=self.LAT + metres * 9.0e-6, **kw)

    def notification(self, title="Turn right on Village Lane",
                     message="In 173 ft, then bear right on Village Terrace",
                     ntype="TURN_ALERT", nid=None):
        """An ADD_NOTIFICATION shaped like the real ones, id and all.

        The id carries a fresh Date.now() on every fire — that is the app bug
        that produced the 7/31 storm, so the fixture reproduces it.
        """
        return self.action("ADD_NOTIFICATION", {
            "id": nid or "UPCOMING_TURN_1785518021000_0_prepare_%d" % self.t,
            "title": title, "message": message, "type": ntype,
            "priority": "medium"})

    def riding(self, trip_id="1:100", vehicle="1:900", leg=1):
        return self.action("SET_RIDING", {
            "tripId": trip_id, "vehicleId": vehicle, "legIndex": leg,
            "routeId": "1:5", "headsign": "Downtown", "boardedAt": self.t})

    def vehicle_match(self, vehicle="1:900", trip_id="1:100",
                      confidence="confirmed", distance=40.0, consecutive=1):
        """A live-vehicle match, as UPDATE_VEHICLE_MATCH carries it.

        A rider who is genuinely aboard produces these continuously — which is
        why aboard-swap now asks for a recent one before believing the sticky
        riding fact.
        """
        return self.action("UPDATE_VEHICLE_MATCH", {
            "consecutiveMatches": consecutive, "emptyPolls": 0,
            "match": {"confidence": confidence, "vehicleId": vehicle,
                      "tripId": trip_id, "label": "8140",
                      "distanceMeters": distance}})

    def transition_leg(self, leg):
        """The app advancing to a new leg. Its reducer — not any action —
        is what clears the riding fact on alighting."""
        return self.action("TRANSITION_LEG", {"legIndex": leg})

    def route_match(self, dist, leg=1, on_route=True):
        return self.action("UPDATE_ROUTE_MATCH", {
            "legIndex": leg, "distanceFromRoute": dist,
            "progressAlongLeg": 0.1, "isOnRoute": on_route})

    def note(self, text, session=None, source=None):
        """A rider note exactly as the Flask sidecar writes it to the JSONL."""
        rec = {
            "kind": "rider-note", "event": "RIDER_NOTE", "text": text,
            "t": self.t, "recv": self.t / 1000.0,
            "session": self.session if session is None else session,
            "ip": "10.0.0.5"}
        if source is not None:
            rec["source"] = source
        self.events.append(rec)
        return self


# --- the ride thread stub ----------------------------------------------------
# The suite must never start a tmux session or a real `claude`: it costs money,
# it takes ten seconds per spawn, and the rider's own threads live in the same
# namespace. RideWatch takes `spawn_thread`/`push_line` for exactly this, so the
# tests exercise the real lifecycle, cadence and digest code against a thread
# that is only pretending.

PUSH_KINDS = (
    ("trip started", "start"),
    ("leg ", "leg"),
    ("finding [", "finding"),
    ("rider note:", "note"),
    ("trip ended", "end"),
    ("still riding", "heartbeat"),
)


class StubThread:
    """Records what would have been typed, and the digest as it was then.

    Reading the digest at push time is the point: asserting on the file after
    the ride would only prove the last write, and the promise is that the
    thread is never handed a stale picture.
    """

    def __init__(self, ok=True):
        self.ok = ok
        self.spawns = []               # [(tmux_name, display_name)]
        self.pushes = []               # [{name, line, digest}]

    def spawn(self, name, display):
        self.spawns.append((name, display))
        return self.ok

    def push(self, name, line):
        path = line.rsplit(" — digest: ", 1)[-1]
        with open(path) as f:
            digest = f.read()
        self.pushes.append({"name": name, "line": line, "digest": digest})
        return True

    def lines(self):
        return [p["line"] for p in self.pushes]

    def kinds(self):
        """Each push classified by its milestone, or 'other' if it is not one."""
        out = []
        for line in self.lines():
            body = line.split("[ride-watch] ", 1)[-1]
            out.append(next((kind for prefix, kind in PUSH_KINDS
                             if body.startswith(prefix)), "other"))
        return out

    def of_kind(self, kind):
        return [p for p, k in zip(self.pushes, self.kinds()) if k == kind]


class NoProcesses:
    """Fails the test if anything tries to start a process.

    The retired reply path spawned `claude -p` from the note handler; this is
    how the suite proves a note is now just a digest push.
    """

    def __enter__(self):
        self._popen = ride_watch.subprocess.Popen
        self._run = ride_watch.subprocess.run

        def forbidden(*a, **kw):
            raise AssertionError("a process was spawned: %r" % (a,))

        ride_watch.subprocess.Popen = forbidden
        ride_watch.subprocess.run = forbidden
        return self

    def __exit__(self, *exc):
        ride_watch.subprocess.Popen = self._popen
        ride_watch.subprocess.run = self._run
        return False


class RuleTestCase(unittest.TestCase):
    """Base: runs a synthetic stream through the real RideWatch code path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ride-watch-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_stream(self, builder, finalize=True, thread=None):
        watch = quiet_watch(
            self.tmp,
            spawn_thread=thread.spawn if thread else None,
            push_line=thread.push if thread else None)
        for ev in builder.events:
            watch.process(ev)
        if finalize:
            watch.finalize_replay()
        return watch

    def rules(self, watch):
        return [f["rule"] for f in watch.all_findings]

    def find(self, watch, rule):
        return [f for f in watch.all_findings if f["rule"] == rule]


class TestStateMachine(RuleTestCase):
    def test_start_go_mode_opens_a_trip_with_itinerary(self):
        b = StreamBuilder().start().advance(1000).progress()
        watch = self.run_stream(b, finalize=False)
        self.assertIn(SESSION, watch.trips)
        trip = watch.trips[SESSION]
        self.assertEqual(len(trip.itinerary["legs"]), 3)
        self.assertTrue(trip.itinerary["legs"][1]["transit"])
        self.assertEqual(trip.itinerary["legs"][1]["route"], "5")

    def test_stop_go_mode_ends_the_trip(self):
        b = StreamBuilder().start().advance(1000).progress().advance(1000).stop()
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.trips, {})
        self.assertEqual(len(watch.ended_trips), 1)
        self.assertEqual(watch.ended_trips[0].end_reason, "stop")

    def test_silence_ends_the_trip(self):
        b = StreamBuilder().start().advance(1000).progress()
        # A later event from another session advances the clock past the
        # 15-minute silence window.
        b.advance(ride_watch.SESSION_TIMEOUT_MS + 60000)
        b.session = "other-session"
        b.action("UPDATE_POSITION", {})
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(watch.ended_trips), 1)
        self.assertEqual(watch.ended_trips[0].end_reason, "timeout")

    def test_second_start_go_mode_is_a_swap_not_a_new_trip(self):
        b = StreamBuilder().start().advance(1000).progress()
        b.advance(1000).start()
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(watch.trips), 1)
        self.assertEqual(watch.trips[SESSION].swap_seq, 1)

    def test_trip_adopted_when_daemon_starts_mid_ride(self):
        b = StreamBuilder().advance(1000).progress(stops=5)
        watch = self.run_stream(b, finalize=False)
        self.assertIn(SESSION, watch.trips)
        self.assertTrue(watch.trips[SESSION].adopted)

    def test_malformed_lines_never_crash(self):
        watch = quiet_watch(self.tmp)
        for bad in ['{"not json', '', 'null', '[]', '{"t": "nope"}',
                    '{"type": "UPDATE_PROGRESS"}',
                    '{"type":"SET_RIDING","t":%d,"session":"s","payload":null}' % T0]:
            watch.process_line(bad)
        self.assertEqual(watch.all_findings, [])


class TestRules(RuleTestCase):
    def test_a_stop_count_collapse_pages(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        watch = self.run_stream(b)
        hits = self.find(watch, "stop-count-collapse")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")
        self.assertEqual(hits[0]["context"]["prevStops"], 6)

    def test_a_no_collapse_late_in_the_leg(self):
        """One stop left at 80% of the leg is just arrival."""
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=79.0)
        b.advance(1000).progress(stops=1, prog=80.0)
        watch = self.run_stream(b)
        self.assertNotIn("stop-count-collapse", self.rules(watch))

    def test_a_collapse_survives_an_itinerary_swap(self):
        """The real 7/29 shape: the swap is what collapses the count."""
        b = StreamBuilder().start().advance(1000).progress(leg=1, stops=4, prog=5.0)
        b.advance(1000).start()                       # itinerary replaced
        b.advance(1000).progress(leg=1, stops=1, prog=0.0)
        watch = self.run_stream(b)
        self.assertIn("stop-count-collapse", self.rules(watch))

    def test_b_stop_count_increase_warns(self):
        b = StreamBuilder().start().advance(1000).progress(stops=3, prog=30.0)
        b.advance(1000).progress(stops=7, prog=31.0)
        watch = self.run_stream(b)
        hits = self.find(watch, "stop-count-increase")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")

    def test_b_increase_excused_by_an_itinerary_swap(self):
        b = StreamBuilder().start().advance(1000).progress(stops=3, prog=30.0)
        b.advance(1000).start()
        b.advance(1000).progress(stops=7, prog=5.0)
        watch = self.run_stream(b)
        self.assertNotIn("stop-count-increase", self.rules(watch))

    def test_c_aboard_swap_pages(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding()
        b.advance(59000).vehicle_match()
        b.advance(1000).start()
        watch = self.run_stream(b)
        hits = self.find(watch, "aboard-swap")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")

    def test_c_no_aboard_swap_without_a_recent_sighting(self):
        """The riding fact alone is not evidence the rider is aboard NOW.

        A confirmed match keeps its confidence long after its vehicle leaves
        the feed, so "aboard" has to be backed by the app actually having seen
        the bus recently.
        """
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding().vehicle_match()
        b.advance(5 * 60000).start()   # last sighting is now 5 min stale
        watch = self.run_stream(b)
        self.assertNotIn("aboard-swap", self.rules(watch))

    def test_c_aboard_swap_still_fires_when_the_swap_lands_on_a_walk_leg(self):
        """The starkest form of the defect must not be the quiet one.

        A swap that puts the rider on a non-transit leg while they are
        physically on a bus is exactly what this rule is for. Requiring a
        transit current leg would have silenced it — measured against the 7/29
        and 8/2 recordings it suppressed two genuine detections and prevented
        no false positive, so it is deliberately not required.
        """
        b = StreamBuilder().start().advance(1000).progress(leg=0, stops=5)
        b.advance(1000).riding(leg=0).vehicle_match()
        # Progress now says leg 1, which the itinerary summary calls a walk.
        b.advance(1000).progress(leg=1, stops=None)
        b.advance(30000).start()
        watch = self.run_stream(b)
        self.assertIn("aboard-swap", self.rules(watch))

    def test_transition_leg_clears_the_riding_fact_on_alight(self):
        """The app never dispatches CLEAR_RIDING — its TRANSITION_LEG reducer
        does the clearing. Mirroring only action types left the daemon holding
        the fact for the whole 8/2 ride, 53 minutes after the rider got off."""
        b = StreamBuilder().start().advance(1000).progress(leg=1, stops=5)
        b.advance(1000).riding(leg=1).vehicle_match()
        b.advance(1000).transition_leg(2)          # advanced past the bus leg
        b.advance(30000).start()                   # a bike-leg replan
        watch = self.run_stream(b)
        # No longer aboard, so an ordinary replan is not an aboard-swap.
        self.assertNotIn("aboard-swap", self.rules(watch))

    def test_transition_leg_keeps_the_fact_when_not_past_the_bus_leg(self):
        b = StreamBuilder().start().advance(1000).progress(leg=1, stops=5)
        b.advance(1000).riding(leg=2).vehicle_match()
        b.advance(1000).transition_leg(2)   # onto the bus leg, not past it
        b.advance(30000).start()
        watch = self.run_stream(b)
        self.assertIn("aboard-swap", self.rules(watch))

    def test_transition_leg_never_clears_an_unanchored_fact(self):
        """legIndex -1 means aboard but not yet tied to a leg. The app
        deliberately keeps it (asserted in its own riding.ts) — so do we."""
        b = StreamBuilder().start().advance(1000).progress(leg=1, stops=5)
        b.advance(1000).riding(leg=-1).vehicle_match()
        b.advance(1000).transition_leg(3)
        b.advance(30000).start()
        watch = self.run_stream(b)
        self.assertIn("aboard-swap", self.rules(watch))

    def test_c_no_aboard_swap_after_explicit_rider_action(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding()
        b.advance(60000).action("SET_ACTIVE_ITINERARY", {"index": 2})
        b.advance(2000).start()
        watch = self.run_stream(b)
        self.assertNotIn("aboard-swap", self.rules(watch))

    def test_c_no_aboard_swap_when_not_riding(self):
        b = StreamBuilder().start().advance(1000).progress()
        b.advance(60000).start()
        watch = self.run_stream(b)
        self.assertNotIn("aboard-swap", self.rules(watch))

    def test_d_riding_flip_pages(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100", vehicle="1:900")
        b.advance(30000).riding(trip_id="1:222", vehicle="1:901")
        watch = self.run_stream(b)
        hits = self.find(watch, "riding-flip")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")
        self.assertEqual(hits[0]["context"]["newTripId"], "1:222")

    def test_d_no_flip_when_the_trip_id_is_unchanged(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(30000).riding(trip_id="1:100")
        watch = self.run_stream(b)
        self.assertNotIn("riding-flip", self.rules(watch))

    def test_d_no_flip_across_different_legs(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100", leg=1)
        b.advance(30000).riding(trip_id="1:222", leg=3)
        watch = self.run_stream(b)
        self.assertNotIn("riding-flip", self.rules(watch))

    def test_e_missed_bus_while_riding_pages(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding()
        b.advance(30000).action("ADD_NOTIFICATION", {
            "id": "MISSED_BUS_Route5_StopA_123", "message": "Missed the bus"})
        watch = self.run_stream(b)
        hits = self.find(watch, "missed-bus-while-riding")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")

    def test_e_missed_bus_before_boarding_is_normal(self):
        b = StreamBuilder().start().advance(1000).progress()
        b.advance(30000).action("ADD_NOTIFICATION", {
            "id": "MISSED_BUS_Route5_StopA_123", "message": "Missed the bus"})
        watch = self.run_stream(b)
        self.assertNotIn("missed-bus-while-riding", self.rules(watch))

    def test_f_deviated_streak_pages_on_a_transit_leg(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        for i in range(12):
            b.advance(10000).progress(stops=5, status="deviated")
        watch = self.run_stream(b)
        hits = self.find(watch, "deviated-streak")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")

    def test_f_short_deviation_is_ignored(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        for i in range(3):
            b.advance(10000).progress(stops=5, status="deviated")
        b.advance(10000).progress(stops=5, status="on_track")
        watch = self.run_stream(b)
        self.assertNotIn("deviated-streak", self.rules(watch))

    def test_f_deviated_streak_only_warns_off_transit(self):
        walk_only = {"itinerary": {"legs": [
            {"mode": "WALK", "transitLeg": False,
             "from": {"name": "A"}, "to": {"name": "B"}}]}}
        b = StreamBuilder().start(walk_only).advance(1000).progress(leg=0)
        for i in range(12):
            b.advance(10000).progress(leg=0, status="deviated")
        watch = self.run_stream(b)
        hits = self.find(watch, "deviated-streak")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")

    def test_g_gps_gap_warns(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress()
        b.advance(ride_watch.GPS_GAP_MS + 15000).progress()
        watch = self.run_stream(b)
        hits = self.find(watch, "gps-gap")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")

    def test_g_gps_gap_is_one_finding_per_gap_not_per_replayed_fix(self):
        # 2026-08-27: the phone came back onto the network and replayed its
        # buffered fixes. Each was NEWER than the last one the daemon had seen
        # (so last_pos_ms advanced) but still far behind the clock, and each
        # used to clear gps_gap_open — so check_timers re-opened the gap on the
        # very next event. Sixteen findings inside one second, the reported gap
        # shrinking 108s -> 60s as the backlog drained.
        b = StreamBuilder().start().advance(1000).position()
        first_fix_t = b.t
        # The clock runs on while no fix arrives.
        b.advance(ride_watch.GPS_GAP_MS + 60000).progress()
        # Now the backlog lands: fixes stamped between the last one and now,
        # each newer than the last but all still stale against the clock.
        for i in range(6):
            b.action("UPDATE_POSITION", {
                "coords": {"latitude": b.LAT, "longitude": b.LON,
                           "accuracy": 5.0},
                "timestamp": first_fix_t + (i + 1) * 5000},
                t=first_fix_t + (i + 1) * 5000)
        watch = self.run_stream(b)
        self.assertEqual(len(self.find(watch, "gps-gap")), 1)

    def test_g_gps_gap_reopens_for_a_genuinely_new_gap(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(ride_watch.GPS_GAP_MS + 15000).progress()
        b.advance(1000).position()           # fresh fix: gap closes
        b.advance(ride_watch.GPS_GAP_MS + 15000).progress()   # and a new one
        watch = self.run_stream(b)
        self.assertEqual(len(self.find(watch, "gps-gap")), 2)

    def test_g_no_gps_gap_after_arrival(self):
        # A phone idle in the rider's pocket at the destination is not a
        # diagnostic event. On 2026-08-27 the first of these fired two minutes
        # after the rider reached 4Front, and the wording said "mid-trip".
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).action("SET_ARRIVED", 1)
        b.advance(ride_watch.GPS_GAP_MS + 15000).progress()
        watch = self.run_stream(b)
        self.assertNotIn("gps-gap", self.rules(watch))

    def test_g_no_deviated_streak_or_stall_after_arrival(self):
        # ~25 of that ride's 42 findings were these two rules judging a trip
        # that had ended: nine deviated-streaks 15:11-17:11 and stalled-progress
        # counting up to 75 minutes, all after arrival at 15:10.
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).action("SET_ARRIVED", 1)
        b.advance(1000).progress(status="deviated")
        b.advance(ride_watch.DEVIATED_STREAK_MS + 5000).progress(
            status="deviated")
        b.advance(ride_watch.STALL_MS + 5000).progress(status="deviated")
        watch = self.run_stream(b)
        rules = self.rules(watch)
        self.assertNotIn("deviated-streak", rules)
        self.assertNotIn("stalled-progress", rules)

    def test_g_deviated_streak_still_fires_before_arrival(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress(status="deviated")
        b.advance(ride_watch.DEVIATED_STREAK_MS + 5000).progress(
            status="deviated")
        watch = self.run_stream(b)
        self.assertIn("deviated-streak", self.rules(watch))

    def test_g_no_gap_while_fixes_keep_arriving(self):
        b = StreamBuilder().start()
        for i in range(20):
            b.advance(5000).position().progress()
        watch = self.run_stream(b)
        self.assertNotIn("gps-gap", self.rules(watch))

    def test_h_reroute_storm_warns(self):
        b = StreamBuilder().start().advance(1000).progress()
        for i in range(4):
            b.advance(20000).action("START_REROUTE",
                                    {"autoApply": True, "reason": "missed-bus"})
        watch = self.run_stream(b)
        hits = self.find(watch, "reroute-storm")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")

    def test_h_paced_reroutes_are_not_a_storm(self):
        b = StreamBuilder().start().advance(1000).progress()
        for i in range(4):
            b.advance(4 * 60 * 1000).action("START_REROUTE", {"autoApply": True})
        watch = self.run_stream(b)
        self.assertNotIn("reroute-storm", self.rules(watch))

    def test_i_console_errors_are_info_and_deduped(self):
        b = StreamBuilder().start().advance(1000).progress()
        for i in range(3):
            b.advance(1000).console("error", ["boom happened"])
        b.advance(1000).console("warn", ["just a warning"])
        b.advance(1000).console("error", ["a different boom"])
        watch = self.run_stream(b)
        hits = self.find(watch, "console-error")
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["severity"], "info")

    def test_k_notification_repeat_pages(self):
        """The 7/31 storm, in miniature: identical buzzes inside five minutes."""
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        for _ in range(3):
            b.advance(30 * 1000).notification()
        watch = self.run_stream(b, finalize=False)
        hits = self.find(watch, "notification-repeat")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")
        # Two, not three, since 2026-08-31: the rule reported storms only
        # after they were over. It fires on the second and latches.
        self.assertEqual(hits[0]["context"]["count"], 2)
        self.assertIn("Turn right on Village Lane", hits[0]["summary"])

    def test_k_two_of_the_same_alert_in_the_window_is_a_storm(self):
        """8/28 evening is why. Five "Off Route" pushes, and the only pair
        inside any five-minute window was 17:12:57 / 17:14:45 — at a threshold
        of three the rule watched the whole storm and said nothing."""
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        b.advance(30 * 1000).notification()
        b.advance(30 * 1000).notification()
        watch = self.run_stream(b, finalize=False)
        hits = self.find(watch, "notification-repeat")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["context"]["count"], 2)

    def test_k_two_of_the_same_alert_far_apart_is_not_a_storm(self):
        """Lowering the count did not widen the window. The same alert twice
        in an hour is an app telling a rider two true things."""
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        b.advance(30 * 1000).notification()
        b.advance(30 * 60 * 1000).notification()
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("notification-repeat", self.rules(watch))

    def test_k_a_drifting_distance_is_still_the_same_alert(self):
        """The 8/28 defeat, exactly: "You are 121m…" and "You are 124m…" are
        one alert about one fault, and the byte key made them two."""
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        for metres in (121, 124):
            b.advance(60 * 1000).notification(
                title="Off Route", ntype="ROUTE_DEVIATION",
                message="You are %dm from the planned route" % metres,
                nid="ROUTE_DEVIATION_deviation_%d" % b.t)
        watch = self.run_stream(b, finalize=False)
        hits = self.find(watch, "notification-repeat")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["context"]["count"], 2)
        self.assertEqual(hits[0]["context"]["key"], "ROUTE_DEVIATION_deviation")

    def test_k_the_turn_stage_is_part_of_the_alert_identity(self):
        """`_prepare` and `_act` are two different things said about one turn
        ("in 300 ft" then "now"), so stripping the timestamp must not collapse
        them — the id stem keeps the cue index and the stage."""
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        for stage in ("prepare", "act"):
            b.advance(30 * 1000).notification(
                nid="UPCOMING_TURN_1785518021000_0_%s_%d" % (stage, b.t))
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("notification-repeat", self.rules(watch))

    def test_k_a_re_posted_notification_is_not_a_second_buzz(self):
        """8/27 13:10:42: the debug-log client re-POSTed a batch, so one
        ROUTE_DEVIATION arrived twice — same `t`, same id, `recv` 208 ms
        apart. It went into the ride notes as an app defect. At a threshold of
        two, counting it would page the rider about a telemetry retry."""
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        b.advance(30 * 1000).notification(
            nid="ROUTE_DEVIATION_deviation_%d" % b.t)
        dup = dict(b.events[-1])
        dup["recv"] = dup["recv"] + 0.208      # the only field that differed
        b.events.append(dup)
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("notification-repeat", self.rules(watch))
        self.assertEqual(watch.duplicate_records, 1)

    def test_k_three_different_alerts_are_not_a_storm(self):
        # Three turns in a row on a fast route is the app working.
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        for turn in ("Turn right on Village Lane", "Turn left on 5th",
                     "Bear right on Hiawatha"):
            b.advance(30 * 1000).notification(title=turn, message=turn)
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("notification-repeat", self.rules(watch))

    def test_k_the_same_alert_spread_over_an_hour_is_not_a_storm(self):
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        for _ in range(3):
            b.advance(20 * 60 * 1000).notification()
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("notification-repeat", self.rules(watch))

    def test_k_a_continuing_storm_is_one_finding_not_fourteen(self):
        # The real ride buzzed 14 times; the rider gets told once.
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        for _ in range(14):
            b.advance(30 * 1000).notification()
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(self.find(watch, "notification-repeat")), 1)

    def test_l_progress_without_motion_warns(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress(leg=1, prog=10.0, stops=5)
        for _ in range(6):                       # jitter inside a few metres
            b.advance(10 * 1000).position_metres_north(1.0)
        b.advance(1000).progress(leg=1, prog=25.0, stops=5)
        watch = self.run_stream(b, finalize=False)
        hits = self.find(watch, "progress-without-motion")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")
        self.assertEqual(hits[0]["context"]["fromPct"], 10.0)
        self.assertEqual(hits[0]["context"]["toPct"], 25.0)
        self.assertLess(hits[0]["context"]["movedMeters"], 15.0)

    def test_l_progress_with_real_motion_is_just_travel(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress(leg=1, prog=10.0, stops=5)
        for i in range(6):                       # 60m per tick: a moving bus
            b.advance(10 * 1000).position_metres_north(60.0 * (i + 1))
        b.advance(1000).progress(leg=1, prog=25.0, stops=5)
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("progress-without-motion", self.rules(watch))

    def test_l_standing_still_with_steady_progress_is_quiet(self):
        b = StreamBuilder().start().advance(1000).position()
        for _ in range(8):
            b.advance(10 * 1000).position_metres_north(1.0)
            b.progress(leg=1, prog=10.0, stops=5)
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("progress-without-motion", self.rules(watch))

    def test_l_a_progress_teleport_on_a_moving_bus_still_warns(self):
        """The 7/29 shape: 35% -> 71% in one second, 6.7m of actual travel.

        A moving rider re-anchors every couple of seconds, so this can only
        fire when the bar outruns the bus inside that window — which is
        exactly what a map-matching relocation looks like.
        """
        b = StreamBuilder().start()
        # 6.5m per second — an Orange Line bus. The anchor resets every third
        # tick (past 15m), so the teleport has to land inside one of those
        # windows to be caught, which is precisely what happened at 17:20:11.
        b.advance(1000).position_metres_north(0.0).progress(
            leg=0, prog=35.0, stops=None)
        b.advance(1000).position_metres_north(6.5).progress(
            leg=0, prog=35.1, stops=None)
        b.advance(1000).position_metres_north(13.0).progress(
            leg=0, prog=70.6, stops=None)
        watch = self.run_stream(b, finalize=False)
        hits = self.find(watch, "progress-without-motion")
        self.assertEqual(len(hits), 1)
        self.assertIn("71%", hits[0]["summary"])
        self.assertLess(hits[0]["context"]["movedMeters"], 15.0)

    def test_l_a_tenth_of_a_point_of_jitter_is_not_a_finding(self):
        """The actual 7/31 numbers: 0.31 -> 0.21 -> 0.31 inside a 7m circle."""
        b = StreamBuilder().start().advance(1000).position()
        for prog in (0.3077, 0.2100, 0.3077):
            b.advance(60 * 1000).position_metres_north(3.0)
            b.progress(leg=0, prog=prog, stops=None)
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("progress-without-motion", self.rules(watch))

    def test_j_distance_spike_warns(self):
        b = StreamBuilder().start().advance(1000).progress()
        b.advance(1000).route_match(50.0)
        b.advance(1000).route_match(3400.0, on_route=False)
        watch = self.run_stream(b)
        hits = self.find(watch, "distance-spike")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")

    def test_j_gradual_drift_is_not_a_spike(self):
        b = StreamBuilder().start().advance(1000).progress()
        for d in (50.0, 400.0, 900.0, 1600.0, 2400.0):
            b.advance(1000).route_match(d)
        watch = self.run_stream(b)
        self.assertNotIn("distance-spike", self.rules(watch))

    def test_k_absurd_match_distance_warns(self):
        """8/2: ~10,268 km to the bus the rider was sitting on — a real
        haversine against a null-island coordinate the feed published."""
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding()
        b.advance(1000).vehicle_match(distance=10267729.06)
        watch = self.run_stream(b)
        hits = self.find(watch, "match-distance-absurd")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")
        self.assertGreater(hits[0]["context"]["distanceMeters"], 1e6)

    def test_k_absurd_distance_is_reported_once_per_episode(self):
        """It held for the whole 8/2 ride: 582 identical findings, unlatched."""
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding()
        for _ in range(50):
            b.advance(15000).vehicle_match(distance=10267729.06)
        watch = self.run_stream(b)
        self.assertEqual(len(self.find(watch, "match-distance-absurd")), 1)

    def test_k_a_second_episode_is_reported_again(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding()
        b.advance(1000).vehicle_match(distance=10267729.06)
        b.advance(15000).vehicle_match(distance=40.0)      # recovers
        b.advance(15000).vehicle_match(distance=9900000.0)  # and breaks again
        watch = self.run_stream(b)
        self.assertEqual(len(self.find(watch, "match-distance-absurd")), 2)

    def test_k_ordinary_feed_lag_is_not_absurd(self):
        """A bus outrunning its own feed position is normal, not a fault."""
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding()
        b.advance(1000).vehicle_match(distance=1200.0)
        watch = self.run_stream(b)
        self.assertNotIn("match-distance-absurd", self.rules(watch))

    def test_k_sustained_trip_disagreement_warns(self):
        """8/2: the match sat on the ghost trip 1:1191630 while the rider was
        confirmed on 1:1201789 — the disagreement that armed the replan loop."""
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:1201789")
        b.advance(1000).vehicle_match(trip_id="1:1191630")
        b.advance(ride_watch.MATCH_TRIP_DISAGREE_MS + 5000)
        b.vehicle_match(trip_id="1:1191630")
        watch = self.run_stream(b)
        hits = self.find(watch, "match-trip-disagrees")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")
        self.assertEqual(hits[0]["context"]["matchTripId"], "1:1191630")
        self.assertEqual(hits[0]["context"]["ridingTripId"], "1:1201789")

    def test_k_one_tick_of_disagreement_is_a_rebind_not_a_fault(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:1201789")
        b.advance(1000).vehicle_match(trip_id="1:1191630")
        b.advance(2000).vehicle_match(trip_id="1:1201789")
        b.advance(ride_watch.MATCH_TRIP_DISAGREE_MS + 5000)
        b.vehicle_match(trip_id="1:1201789")
        watch = self.run_stream(b)
        self.assertNotIn("match-trip-disagrees", self.rules(watch))

    def test_k_disagreement_fires_once_not_per_tick(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:1201789")
        for _ in range(12):
            b.advance(ride_watch.MATCH_TRIP_DISAGREE_MS // 2)
            b.vehicle_match(trip_id="1:1191630")
        watch = self.run_stream(b)
        self.assertEqual(len(self.find(watch, "match-trip-disagrees")), 1)

    def test_l_stalled_progress_warns(self):
        """8/2 §12: 34 minutes stationary inside a bike leg, 640 m short of the
        destination, every number internally consistent."""
        b = StreamBuilder().start().advance(1000).progress(leg=2, prog=0.0)
        b.advance(1000).position()
        # Sit still, still sending fixes, well past the stall threshold.
        for _ in range(20):
            b.advance(60 * 1000).position_metres_north(1)
        watch = self.run_stream(b)
        hits = self.find(watch, "stalled-progress")
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")
        self.assertGreaterEqual(hits[0]["context"]["heldMs"],
                                ride_watch.STALL_MS)

    def test_l_a_rider_who_is_moving_never_stalls(self):
        b = StreamBuilder().start().advance(1000).progress(leg=2, prog=0.0)
        for i in range(30):
            b.advance(60 * 1000).position_metres_north(200 * (i + 1))
        watch = self.run_stream(b)
        self.assertNotIn("stalled-progress", self.rules(watch))

    def test_l_a_gps_gap_is_not_reported_as_a_stall(self):
        """Two different faults. A rider who stopped sending fixes has not
        been shown to have stopped moving — gps-gap already covers that."""
        b = StreamBuilder().start().advance(1000).progress(leg=2, prog=0.0)
        b.advance(1000).position()
        b.advance(40 * 60 * 1000).progress(leg=2, prog=0.0)
        watch = self.run_stream(b)
        self.assertNotIn("stalled-progress", self.rules(watch))
        self.assertIn("gps-gap", self.rules(watch))


class TestPaging(RuleTestCase):
    def test_at_most_two_pages_per_trip(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        # Six page-worthy riding flips, spaced past the 120s rate limit.
        for i in range(6):
            b.advance(ride_watch.PUSH_MIN_INTERVAL_MS + 10000)
            b.riding(trip_id="1:%d" % (200 + i))
        watch = self.run_stream(b)
        self.assertGreater(len(self.find(watch, "riding-flip")), 2)
        sent = [p for p in watch.push_log if p.get("sent")]
        self.assertEqual(len(sent), ride_watch.MAX_PAGES_PER_TRIP)

    def test_rate_limit_suppresses_a_close_second_page(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(5000).riding(trip_id="1:200")   # page
        # Past the coalescing window (so this is a second page, not a rival
        # candidate for the first one) but still inside the 120s rate limit.
        b.advance(ride_watch.PAGE_COALESCE_MS + 20000).riding(trip_id="1:300")
        watch = self.run_stream(b)
        sent = [p for p in watch.push_log if p.get("sent")]
        self.assertEqual(len(sent), 1)
        suppressed = [p for p in watch.push_log
                      if p.get("suppressed") == "rate-limit"]
        self.assertTrue(suppressed)

    def test_warnings_never_page(self):
        b = StreamBuilder().start().advance(1000).progress()
        b.advance(1000).route_match(50.0)
        b.advance(1000).route_match(3400.0)
        watch = self.run_stream(b)
        self.assertIn("distance-spike", self.rules(watch))
        self.assertEqual(watch.push_log, [])

    def test_dry_run_never_touches_the_network(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(5000).riding(trip_id="1:200")
        watch = self.run_stream(b)
        sent = [p for p in watch.push_log if p.get("sent")]
        self.assertEqual([p["sent"] for p in sent], ["dry-run"])

    def test_push_bodies_follow_the_rider_copy_rules(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(5000).riding(trip_id="1:200")
        b.advance(ride_watch.PUSH_MIN_INTERVAL_MS + 5000).progress(
            stops=6, prog=10.0)
        b.advance(1000).progress(stops=1, prog=11.0)
        watch = self.run_stream(b)
        self.assertTrue(watch.push_log)
        for p in watch.push_log:
            self.assertLess(len(p["body"]), 120, p["body"])
            self.assertNotIn("!", p["body"])


class TestPageRanking(RuleTestCase):
    """The rider gets the most actionable page in the window, not the first.

    Shaped after the real 17:28 cascade: riding-flip fires first, the stop
    counter collapses eight seconds later, and only one of them can be sent.
    """

    def sent_bodies(self, watch):
        return [p["body"] for p in watch.push_log if p.get("sent")]

    def _flip_then_collapse(self, gap_ms):
        b = StreamBuilder().start().advance(1000).progress(leg=1, stops=6, prog=20.0)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).riding(trip_id="1:222")            # riding-flip (rank 20)
        b.advance(gap_ms).progress(leg=1, stops=1, prog=21.0)  # collapse (rank 50)
        return b

    def test_a_later_higher_ranked_page_supersedes_the_first(self):
        watch = self.run_stream(self._flip_then_collapse(8000))
        self.assertEqual(
            {"riding-flip", "stop-count-collapse"},
            set(self.rules(watch)) & {"riding-flip", "stop-count-collapse"})
        sent = self.sent_bodies(watch)
        self.assertEqual(len(sent), 1)
        self.assertIn("Stop count wrong", sent[0])

    def test_the_superseded_page_is_recorded_not_silently_dropped(self):
        watch = self.run_stream(self._flip_then_collapse(8000))
        dropped = [p for p in watch.push_log
                   if p.get("suppressed", "").startswith("superseded-by")]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["suppressed"],
                         "superseded-by-stop-count-collapse")
        self.assertIn("Trip id flipped", dropped[0]["body"])
        flip = self.find(watch, "riding-flip")[0]
        self.assertEqual(flip["paged"], False)
        self.assertEqual(flip["supersededBy"], "stop-count-collapse")

    def test_a_page_outside_the_window_is_not_superseded(self):
        """Same cascade, but slow enough that both are separate decisions."""
        watch = self.run_stream(
            self._flip_then_collapse(ride_watch.PAGE_COALESCE_MS + 20000))
        sent = self.sent_bodies(watch)
        self.assertEqual(len(sent), 1)
        self.assertIn("Trip id flipped", sent[0])   # the flip won its own window
        rate_limited = [p for p in watch.push_log
                        if p.get("suppressed") == "rate-limit"]
        self.assertEqual(len(rate_limited), 1)

    def test_a_lower_ranked_page_arriving_later_does_not_win(self):
        b = StreamBuilder().start().advance(1000).progress(leg=1, stops=6, prog=20.0)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).progress(leg=1, stops=1, prog=21.0)   # collapse (50)
        b.advance(5000).riding(trip_id="1:222")               # flip (20)
        watch = self.run_stream(b)
        sent = self.sent_bodies(watch)
        self.assertEqual(len(sent), 1)
        self.assertIn("Stop count wrong", sent[0])

    def test_ties_go_to_the_earlier_page(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).riding(trip_id="1:222")   # riding-flip
        b.advance(3000).riding(trip_id="1:333")   # riding-flip, same rank
        watch = self.run_stream(b)
        sent = self.sent_bodies(watch)
        self.assertEqual(len(sent), 1)
        self.assertIn("1:222", sent[0])

    def test_the_ranking_covers_every_page_rule(self):
        """A page rule with no rank would silently fall back to mid-pack."""
        page_rules = {"stop-count-collapse", "itinerary-backwards",
                      "missed-bus-while-riding", "replan-not-converging",
                      "notification-repeat", "aboard-swap", "riding-flip",
                      "deviated-streak"}
        self.assertEqual(page_rules, set(ride_watch.PAGE_RANK))
        self.assertEqual(
            ["stop-count-collapse", "itinerary-backwards",
             "missed-bus-while-riding", "replan-not-converging",
             "notification-repeat", "aboard-swap", "riding-flip",
             "deviated-streak"],
            sorted(ride_watch.PAGE_RANK, key=ride_watch.PAGE_RANK.get,
                   reverse=True))

    def test_a_buffered_page_is_held_until_the_window_closes(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).riding(trip_id="1:222")
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(self.sent_bodies(watch), [])
        trip = watch.trips[SESSION]
        self.assertEqual(len(trip.pending_pages), 1)

    def test_a_buffered_page_flushes_when_the_log_goes_quiet(self):
        """No further telemetry — the idle tick must still send it."""
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).riding(trip_id="1:222")
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(self.sent_bodies(watch), [])
        # The live loop's 5s tick, with the clock past the window.
        watch.clock_ms += ride_watch.PAGE_COALESCE_MS + 1000
        watch.check_timers()
        self.assertEqual(len(self.sent_bodies(watch)), 1)
        self.assertEqual(watch.trips[SESSION].pending_pages, [])

    def test_a_buffered_page_flushes_when_the_trip_ends(self):
        """Trip ends 3s into the window: the page must not be lost."""
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).riding(trip_id="1:222")
        b.advance(3000).stop()
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(self.sent_bodies(watch)), 1)
        self.assertEqual(len(watch.ended_trips), 1)
        self.assertEqual(watch.ended_trips[0].pages_sent, 1)

    def test_a_buffered_page_flushes_on_clean_shutdown(self):
        """A restart mid-window must not eat the page."""
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).riding(trip_id="1:222")
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(self.sent_bodies(watch), [])
        watch.flush_pending_pages()
        self.assertEqual(len(self.sent_bodies(watch)), 1)
        # ...and the trip is still active, so a restart re-adopts it.
        self.assertIn(SESSION, watch.trips)

    def test_a_page_is_persisted_with_its_final_paging_verdict(self):
        watch = self.run_stream(self._flip_then_collapse(8000))
        path = [f for f in os.listdir(self.tmp) if f.endswith(".findings.jsonl")]
        rows = [json.loads(l) for l in
                read_text(os.path.join(self.tmp, path[0])).splitlines()
                if l.strip()]
        by_rule = {r["rule"]: r for r in rows}
        self.assertEqual(by_rule["stop-count-collapse"]["paged"], True)
        self.assertEqual(by_rule["riding-flip"]["paged"], False)
        for row in rows:
            self.assertNotEqual(row.get("paged"), "pending")


class TestSurfaces(RuleTestCase):
    def test_status_file_shows_idle_text_with_no_trip(self):
        watch = quiet_watch(self.tmp, replay=False)
        watch.write_status(force=True)
        text = read_text(os.path.join(self.tmp, "current-ride.md"))
        self.assertIn("No active trip", text)

    def test_status_file_describes_the_active_trip(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).progress(stops=4, prog=42.0)
        watch = self.run_stream(b, finalize=False)
        watch.write_status(force=True)
        text = read_text(os.path.join(self.tmp, "current-ride.md"))
        self.assertIn("Active trip", text)
        self.assertIn("42%", text)
        self.assertIn("1:100", text)
        self.assertIn("4 stops left", text)

    def test_each_trip_also_gets_a_status_file_of_its_own(self):
        """The combined file is the operator's view; a rider must not read it.

        current-ride.md describes every trip on the server at once, so handing
        it to a rider's /ride console would show them somebody else's live
        position. The per-session file is what the console reads once it can
        say whose it is.
        """
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress(stops=4, prog=42.0)
        watch = self.run_stream(b, finalize=False)
        watch.write_status(force=True)

        sessions = list(watch.trips.keys())
        self.assertEqual(len(sessions), 1)
        own = read_text(os.path.join(self.tmp, "%s.current-ride.md" % sessions[0]))
        self.assertIn("Active trip", own)
        self.assertIn("42%", own)
        # It opens like a document, not like a fragment.
        self.assertTrue(own.startswith("# Ride watch"), own[:40])

    def test_one_riders_status_file_never_mentions_another_trip(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress(stops=4, prog=42.0)
        watch = self.run_stream(b, finalize=False)
        # A second rider appears on the same server.
        other = ride_watch.Trip("sess-someone-else", watch.now_ms(), None)
        watch.trips["sess-someone-else"] = other
        watch.write_status(force=True)

        mine = [s for s in watch.trips if s != "sess-someone-else"][0]
        own = read_text(os.path.join(self.tmp, "%s.current-ride.md" % mine))
        self.assertNotIn("sess-someone-else", own)
        # The combined file still carries both, for the operator.
        combined = read_text(os.path.join(self.tmp, "current-ride.md"))
        self.assertIn("sess-someone-else", combined)
        self.assertIn(mine, combined)

    def test_findings_are_appended_as_jsonl(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        watch = self.run_stream(b)
        path = [f for f in os.listdir(self.tmp) if f.endswith(".findings.jsonl")]
        self.assertEqual(len(path), 1)
        rows = [json.loads(l) for l in
                read_text(os.path.join(self.tmp, path[0])).splitlines()
                if l.strip()]
        self.assertEqual(rows[0]["rule"], "stop-count-collapse")
        for key in ("tsMs", "session", "rule", "severity", "summary", "context"):
            self.assertIn(key, rows[0])

    def test_report_request_written_when_a_ride_has_findings(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        b.advance(1000).stop()
        watch = self.run_stream(b, finalize=False)
        paths = report_requests(self.tmp)
        self.assertEqual(len(paths), 1)
        req = json.loads(read_text(paths[0]))
        for key in ("session", "date", "startMs", "endMs", "findingsPath",
                    "reportPath", "findingsFrom", "itinerarySummary"):
            self.assertIn(key, req)
        self.assertEqual(req["session"], SESSION)
        self.assertEqual(req["findingsFrom"], req["startMs"])

    def test_two_rides_on_one_session_do_not_share_a_report_path(self):
        """The phone keeps one session id for as long as the app stays loaded.

        Both 2026-08-27 and 2026-08-28 ran two trips under one id; the wrap-up
        path was derived from that id alone, so ride 2 resolved to the file
        ride 1's report was already in. The daemon now hands the thread a
        non-colliding name instead of hoping it notices.
        """
        watch = quiet_watch(self.tmp)
        session = "mtdh67f3-0z5p24"
        first = Trip(session, 1787953260021, None)     # 16:41
        second = Trip(session, 1787968604000, None)    # 20:56, same session

        p1 = watch._report_path(first)
        self.assertTrue(p1.endswith("-0z5p24.md"), p1)
        # Nothing on disk yet, so both rides want the same name.
        self.assertEqual(watch._report_path(second), p1)

        # Once ride 1's report exists, ride 2 is handed the next free name.
        with open(p1, "w") as f:
            f.write("# ride 1\n")
        p2 = watch._report_path(second)
        self.assertTrue(p2.endswith("-0z5p24-ride2.md"), p2)
        self.assertNotEqual(p1, p2)

        with open(p2, "w") as f:
            f.write("# ride 2\n")
        self.assertTrue(watch._report_path(second).endswith("-ride3.md"))
        # ride 1's report is untouched.
        self.assertEqual(read_text(p1), "# ride 1\n")

        # The request files are per-ride too, so ride 2 cannot destroy ride 1's
        # inputs before anyone has read them.
        self.assertNotEqual(watch._report_request_path(first),
                            watch._report_request_path(second))

    def test_clean_ride_requests_no_report(self):
        b = StreamBuilder().start()
        for i in range(10):
            b.advance(5000).position().progress(stops=5)
        b.advance(1000).stop()
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.all_findings, [])
        self.assertEqual(report_requests(self.tmp), [])
        watch.write_status(force=True)
        self.assertIn("clean ride",
                      read_text(os.path.join(self.tmp, "current-ride.md")))


class TestRiderNotes(RuleTestCase):
    """Notes typed on the /ride console land in the stream like any event.

    They are the rider's own account of the ride — first-class input to the
    post-ride report — but an observation, never an alarm.
    """

    def test_a_note_attaches_to_the_active_trip(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5, prog=30.0)
        b.advance(1000).note("driver blew past my stop")
        watch = self.run_stream(b, finalize=False)
        trip = watch.trips[SESSION]
        self.assertEqual(len(trip.notes), 1)
        self.assertEqual(trip.notes[0]["text"], "driver blew past my stop")
        self.assertIn("rider-note", self.rules(watch))
        fnd = self.find(watch, "rider-note")[0]
        self.assertEqual(fnd["severity"], "info")
        self.assertIn("driver blew past my stop", fnd["summary"])

    def test_a_note_captures_the_trip_state_at_that_moment(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).progress(stops=3, prog=45.0, status="on_track")
        b.advance(1000).note("bus is crawling")
        watch = self.run_stream(b, finalize=False)
        ctx = watch.trips[SESSION].notes[0]["context"]
        self.assertEqual(ctx["legIndex"], 1)
        self.assertEqual(ctx["legProgressPct"], 45.0)
        self.assertEqual(ctx["stopsRemaining"], 3)
        self.assertEqual(ctx["status"], "on_track")
        self.assertTrue(ctx["onTransitLeg"])
        self.assertEqual(ctx["riding"]["tripId"], "1:100")
        self.assertEqual(ctx["text"], "bus is crawling")

    def test_a_note_never_pages(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        for text in ("this is broken", "PAGE ME", "stop count collapsed"):
            b.advance(1000).note(text)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(watch.trips[SESSION].notes), 3)
        self.assertEqual(watch.push_log, [])
        self.assertEqual(watch.trips[SESSION].pages_sent, 0)
        for fnd in self.find(watch, "rider-note"):
            self.assertEqual(fnd["severity"], "info")
            self.assertNotIn("paged", fnd)

    def test_notes_render_in_the_status_file(self):
        b = StreamBuilder().start().advance(1000).progress(stops=3, prog=45.0)
        b.advance(1000).note("wrong stop announced")
        b.advance(1000).note("still wrong")
        watch = self.run_stream(b, finalize=False)
        watch.write_status(force=True)
        text = read_text(os.path.join(self.tmp, "current-ride.md"))
        self.assertIn("Rider notes (2, newest first)", text)
        self.assertIn("wrong stop announced", text)
        # Newest first, and each note says where in the trip it was written.
        self.assertLess(text.index("still wrong"), text.index("wrong stop"))
        self.assertIn("leg 1 at 45%, 3 stops left", text)

    def test_notes_are_persisted_as_findings(self):
        b = StreamBuilder().start().advance(1000).progress(stops=3)
        b.advance(1000).note("late again")
        watch = self.run_stream(b, finalize=False)
        path = [f for f in os.listdir(self.tmp) if f.endswith(".findings.jsonl")]
        rows = [json.loads(l) for l in
                read_text(os.path.join(self.tmp, path[0])).splitlines()
                if l.strip()]
        self.assertEqual(rows[0]["rule"], "rider-note")
        self.assertEqual(rows[0]["severity"], "info")
        self.assertEqual(rows[0]["context"]["text"], "late again")

    def test_a_note_with_a_stale_session_id_still_finds_the_trip(self):
        # The sidecar guesses the session from the log tail; when it guesses
        # wrong there is still only one ride happening.
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).note("guessed wrong", session="some-other-session")
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(watch.trips[SESSION].notes), 1)

    def test_a_note_outside_any_trip_is_dropped(self):
        b = StreamBuilder().at(0).note("thinking out loud")
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.trips, {})
        self.assertEqual(watch.all_findings, [])

    def test_an_empty_note_is_ignored(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).note("   ")
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.trips[SESSION].notes, [])

    def test_the_report_request_carries_the_riders_notes(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).note("app said 1 stop left, it was 6")
        b.advance(1000).stop()
        watch = self.run_stream(b, finalize=False)
        req = json.loads(read_text(report_requests(self.tmp)[0]))
        self.assertEqual(req["notesCount"], 1)
        self.assertEqual(req["riderNotes"][0]["text"],
                         "app said 1 stop left, it was 6")



class TestRideThread(RuleTestCase):
    """One conversation per ride, pinged only at milestones.

    What has to hold: the thread exists from the first second of the ride, it
    hears about everything that matters and nothing that does not, the digest
    it is pointed at is current at the moment of every ping, and none of it can
    take the daemon down or interfere with paging.
    """

    def ride(self, builder, ok=True, finalize=False):
        thread = StubThread(ok=ok)
        watch = self.run_stream(builder, finalize=finalize, thread=thread)
        return watch, thread

    # -- lifecycle ---------------------------------------------------------

    def test_a_trip_start_spawns_exactly_one_thread(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        watch, thread = self.ride(b)
        self.assertEqual(len(thread.spawns), 1)
        name, display = thread.spawns[0]
        # tmux name is machine-facing; the display name is what the rider
        # picks out of a list of conversations on their phone.
        self.assertRegex(name, r"^ride-\d{4}$")
        self.assertRegex(display, r"^ride \d\d-\d\d \d\d:\d\d$")
        self.assertEqual(watch.trips[SESSION].thread["tmux"], name)

    def test_an_itinerary_swap_is_not_a_second_thread(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).start()          # auto-reroute swap, same ride
        watch, thread = self.ride(b)
        self.assertEqual(len(thread.spawns), 1)

    def test_a_trip_adopted_mid_stream_still_gets_a_thread(self):
        # Daemon restarted under a rider who is already on the bus.
        b = StreamBuilder().advance(1000).progress(stops=5)
        watch, thread = self.ride(b)
        self.assertEqual(len(thread.spawns), 1)
        self.assertIn("adopted", thread.lines()[0])

    def test_the_kill_switch_leaves_the_ride_untouched(self):
        thread = StubThread()
        watch = quiet_watch(self.tmp, spawn_thread=thread.spawn,
                            push_line=thread.push, thread_enabled=False)
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        for ev in b.events:
            watch.process(ev)
        self.assertEqual(thread.spawns, [])
        self.assertEqual(thread.pushes, [])
        # The rule engine and the page are exactly as they were.
        self.assertIn("stop-count-collapse", self.rules(watch))

    def test_a_failed_spawn_never_stops_the_ride(self):
        def explode(name, display):
            raise OSError("tmux: command not found")

        watch = quiet_watch(self.tmp, spawn_thread=explode,
                            push_line=lambda n, l: True)
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0).advance(1000).stop()
        for ev in b.events:
            watch.process(ev)
        self.assertEqual(len(watch.ended_trips), 1)
        self.assertIn("stop-count-collapse", self.rules(watch))

    def test_a_push_failure_never_stops_the_ride(self):
        def explode(name, line):
            raise OSError("no such pane")

        watch = quiet_watch(self.tmp, spawn_thread=lambda n, d: True,
                            push_line=explode)
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0).advance(1000).stop()
        for ev in b.events:
            watch.process(ev)
        self.assertEqual(len(watch.ended_trips), 1)
        self.assertEqual(len(watch.ended_trips[0].findings), 1)

    # -- which tmux sessions are ours to kill -------------------------------

    def test_only_our_own_ride_sessions_are_cleaned_up(self):
        names = ["0", "rc-1785516790", "ride-1432", "ride-0907",
                 "ride-test-smoke", "ride-impl-test-1432", "rider-1432",
                 "ride-143", "ride-14322"]
        self.assertEqual(ride_thread_sessions(names), ["ride-1432", "ride-0907"])

    def test_the_riders_own_test_thread_is_never_killed(self):
        # `ride-test-smoke` was live in the rider's app while this was written.
        self.assertEqual(ride_thread_sessions(["ride-test-smoke"]), [])

    def test_a_test_run_under_its_own_prefix_ignores_real_rides(self):
        names = ["ride-1432", "ride-impl-test-1432", "ride-test-smoke"]
        self.assertEqual(
            ride_thread_sessions(names, prefix="ride-impl-test"),
            ["ride-impl-test-1432"])

    # -- cadence -------------------------------------------------------------

    def test_the_kickoff_names_the_itinerary(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        watch, thread = self.ride(b)
        first = thread.lines()[0]
        self.assertIn("[ride-watch] trip started", first)
        self.assertIn("BUS 5", first)
        self.assertIn(".digest.md", first)

    def test_every_push_is_one_line(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5, prog=10.0)
        b.advance(1000).note("driver\nblew\npast my stop")
        b.advance(1000).progress(leg=2, stops=None, prog=5.0)
        b.advance(1000).stop()
        watch, thread = self.ride(b)
        for line in thread.lines():
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line), ride_watch.THREAD_LINE_MAX)

    def test_routine_telemetry_never_reaches_the_thread(self):
        b = StreamBuilder().start()
        for _ in range(60):                 # a minute of ~1 Hz noise
            b.advance(1000).position().progress(stops=5, prog=30.0)
        watch, thread = self.ride(b)
        self.assertEqual(thread.kinds(), ["start"])

    def test_a_leg_transition_is_a_milestone(self):
        b = StreamBuilder().start().advance(1000).progress(leg=1, stops=5)
        b.advance(1000).progress(leg=2, prog=5.0)
        watch, thread = self.ride(b)
        self.assertEqual(thread.kinds(), ["start", "leg"])
        self.assertIn("leg 1 -> 2", thread.of_kind("leg")[0]["line"])

    def test_a_finding_is_a_milestone(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        watch, thread = self.ride(b)
        self.assertEqual(thread.kinds(), ["start", "finding"])
        self.assertIn("stop-count-collapse", thread.of_kind("finding")[0]["line"])

    def test_a_rider_note_is_a_milestone_and_costs_one_push(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5, prog=30.0)
        b.advance(1000).note("driver blew past my stop")
        watch, thread = self.ride(b)
        self.assertEqual(thread.kinds(), ["start", "note"])
        self.assertIn("driver blew past my stop", thread.of_kind("note")[0]["line"])

    def test_a_thread_recorded_note_is_kept_but_not_echoed_back(self):
        """The 8/2 gap: notes typed straight into the thread reached nothing.

        The thread can now put them in the stream itself, which is what gets
        them into the ledger, the digest and the wrap-up request. What it must
        NOT do is bounce the rider's own words back at the conversation they
        just typed them into.
        """
        b = StreamBuilder().start().advance(1000).progress(stops=5, prog=30.0)
        b.advance(1000).note("2 bus legs, I'm only on one bus",
                             source="ride-thread")
        watch, thread = self.ride(b)
        trip = watch.trips[SESSION]
        # Recorded everywhere the report will look...
        self.assertEqual(len(trip.notes), 1)
        self.assertEqual(trip.notes[0]["source"], "ride-thread")
        self.assertIn("rider-note", [f["rule"] for f in watch.all_findings])
        # ...but never pushed back at the thread.
        self.assertEqual(thread.kinds(), ["start"])

    def test_a_console_note_is_still_pushed_to_the_thread(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5, prog=30.0)
        b.advance(1000).note("driver blew past my stop", source="console")
        watch, thread = self.ride(b)
        self.assertIn("note", thread.kinds())

    def test_a_note_with_no_source_is_treated_as_a_console_note(self):
        """Older sidecars send no `source` — the console is the default."""
        b = StreamBuilder().start().advance(1000).progress(stops=5, prog=30.0)
        b.advance(1000).note("still wrong")
        watch, thread = self.ride(b)
        self.assertIn("note", thread.kinds())
        self.assertEqual(watch.trips[SESSION].notes[0]["source"], "console")

    def test_a_note_no_longer_spawns_a_process(self):
        """The whole reason this feature exists — no more `claude -p` per note."""
        thread = StubThread()
        watch = quiet_watch(self.tmp, spawn_thread=thread.spawn,
                            push_line=thread.push)
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        for text in ("how many stops", "is it following me", "still wrong"):
            b.advance(1000).note(text)
        with NoProcesses():
            for ev in b.events:
                watch.process(ev)
        self.assertEqual(len(watch.trips[SESSION].notes), 3)
        self.assertEqual(thread.kinds(), ["start", "note", "note", "note"])
        self.assertFalse(hasattr(watch, "replies"))
        self.assertFalse(hasattr(watch, "reply_queue"))

    def test_trip_end_hands_over_the_wrap_up(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        b.advance(1000).stop()
        watch, thread = self.ride(b)
        end = thread.of_kind("end")
        self.assertEqual(len(end), 1)
        self.assertIn("1 finding(s)", end[0]["line"])
        self.assertIn("report-request-%s-" % SESSION, end[0]["line"])

    def test_a_clean_ride_ends_without_a_request_file(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).stop()
        watch, thread = self.ride(b)
        end = thread.of_kind("end")
        self.assertEqual(len(end), 1)
        self.assertNotIn("report-request", end[0]["line"])

    def test_a_heartbeat_only_fires_while_still_moving(self):
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        # 11 minutes of position fixes with nothing else happening.
        for _ in range(11):
            b.advance(60 * 1000).position().progress(stops=5, prog=30.0)
        watch, thread = self.ride(b)
        self.assertEqual(len(thread.of_kind("heartbeat")), 1)
        self.assertIn("leg 1 at 30%", thread.of_kind("heartbeat")[0]["line"])

    def test_no_heartbeat_when_the_fixes_stopped(self):
        # gps-gap still fires (that is a finding); the heartbeat does not,
        # because "still riding" would be a lie.
        b = StreamBuilder().start().advance(1000).position().progress(stops=5)
        b.advance(20 * 60 * 1000).progress(stops=5, prog=30.0)
        watch, thread = self.ride(b)
        self.assertEqual(thread.of_kind("heartbeat"), [])

    # -- the digest ----------------------------------------------------------

    def test_the_digest_is_current_at_every_push(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).riding(trip_id="1:100")
        b.advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)     # collapse
        b.advance(1000).note("it says one stop and we just left")
        b.advance(1000).stop()
        watch, thread = self.ride(b)
        seen = 0
        for push in thread.pushes:
            digest = push["digest"]
            # Trip state, every time.
            self.assertIn("- Started:", digest)
            self.assertIn("- Itinerary: WALK > BUS 5", digest)
            self.assertIn("- Last fix:", digest)
            # The findings section never goes backwards.
            count = int(re.search(r"## Findings \((\d+)\)", digest).group(1))
            self.assertGreaterEqual(count, seen)
            seen = count
        self.assertEqual(seen, len(watch.ended_trips[0].findings))

    def test_a_finding_push_carries_that_finding_in_the_digest(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        watch, thread = self.ride(b)
        push = thread.of_kind("finding")[0]
        summary = push["line"].split(": ", 1)[1].rsplit(" — digest: ", 1)[0]
        self.assertIn(summary, push["digest"])
        self.assertIn("## New since the last push (1)", push["digest"])

    def test_the_digest_carries_the_riders_own_words(self):
        b = StreamBuilder().start().advance(1000).progress(stops=3, prog=45.0)
        b.advance(1000).note("wrong stop announced")
        watch, thread = self.ride(b)
        digest = thread.pushes[-1]["digest"]
        self.assertIn("## Rider notes (1)", digest)
        self.assertIn("wrong stop announced", digest)
        self.assertIn("leg 1", digest)

    def test_a_fraction_of_a_percent_is_never_shown_as_a_third_of_the_leg(self):
        """The 7/31 bug, in one test.

        currentLegProgress 0.3077 means 0.31% of the leg — a rider 4m into a
        1326m bike leg. A reply agent read the unitless number as a fraction
        and told them "31% along"; rounding it to "0%" would be the opposite
        lie. It must render 0.3%, and the digest must say what the unit is.
        """
        b = StreamBuilder().start().advance(1000).progress(leg=0, prog=0.3077,
                                                           stops=None)
        b.advance(1000).note("am I moving")
        watch, thread = self.ride(b)
        digest = thread.pushes[-1]["digest"]
        self.assertIn("0.3%", digest)
        self.assertNotIn("31%", digest)
        self.assertNotIn("at 0% of leg", digest)
        self.assertIn("currentLegProgress is a percentage on 0-100", digest)
        self.assertIn("progressAlongLeg", digest)
        ctx = watch.trips[SESSION].notes[0]["context"]
        self.assertEqual(ctx["legProgressPct"], 0.3077)
        self.assertNotIn("legProgress", ctx)

    def test_a_whole_percentage_still_reads_as_a_whole_number(self):
        self.assertEqual(ride_watch.fmt_pct(42.0), "42%")
        self.assertEqual(ride_watch.fmt_pct(0.3077), "0.3%")
        self.assertEqual(ride_watch.fmt_pct(9.94), "9.9%")
        self.assertEqual(ride_watch.fmt_pct(None), "?")

    def test_the_digest_points_at_the_evidence(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        watch, thread = self.ride(b)
        digest = thread.pushes[0]["digest"]
        self.assertIn("debug-", digest)               # raw telemetry
        self.assertIn(".findings.jsonl", digest)
        self.assertIn("current-ride.md", digest)

    # -- the fallback page ---------------------------------------------------

    def test_a_ride_with_no_thread_and_findings_still_reaches_the_rider(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        b.advance(1000).stop()
        watch, thread = self.ride(b, ok=False)
        fallback = [p for p in watch.push_log if p["kind"] == "fallback"]
        self.assertEqual(len(fallback), 1)
        self.assertIn("Report pending", fallback[0]["body"])

    def test_a_working_thread_means_no_fallback_page(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        b.advance(1000).stop()
        watch, thread = self.ride(b, ok=True)
        self.assertEqual([p for p in watch.push_log if p["kind"] == "fallback"], [])

    def test_a_clean_ride_never_falls_back(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).stop()
        watch, thread = self.ride(b, ok=False)
        self.assertEqual(watch.push_log, [])


class TestThreadCadenceOnRealRides(unittest.TestCase):
    """Replay both real logs and count what the rider's thread would have heard.

    The 7/29 file is the incident ride; the 7/31 file is the one whose reply
    agents re-diagnosed the same bug twice and prompted this whole design. If
    milestone-only pushing is right, these two rides are the proof: a full
    ride of ~1 Hz telemetry must come out as a handful of lines.
    """

    LOGS = [os.path.join(os.path.expanduser("~"), "otp-debug-logs", name)
            for name in ("debug-2026-07-29.jsonl", "debug-2026-07-31.jsonl")]

    def replay(self, path):
        tmp = tempfile.mkdtemp(prefix="ride-watch-thread-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        thread = StubThread()
        watch = quiet_watch(tmp, spawn_thread=thread.spawn,
                            push_line=thread.push)
        run_replay(path, watch=watch)
        return watch, thread

    def test_every_push_is_a_milestone(self):
        for path in self.LOGS:
            if not os.path.exists(path):
                continue
            watch, thread = self.replay(path)
            self.assertNotIn("other", thread.kinds(),
                             "%s pushed a non-milestone: %s"
                             % (path, thread.lines()))

    def test_one_thread_and_one_kickoff_per_ride(self):
        for path in self.LOGS:
            if not os.path.exists(path):
                continue
            watch, thread = self.replay(path)
            rides = len(watch.ended_trips)
            self.assertEqual(len(thread.spawns), rides, path)
            self.assertEqual(len(thread.of_kind("start")), rides, path)
            self.assertEqual(len(thread.of_kind("end")), rides, path)

    def test_a_push_per_finding_and_nothing_extra(self):
        for path in self.LOGS:
            if not os.path.exists(path):
                continue
            watch, thread = self.replay(path)
            findings = [f for f in watch.all_findings
                        if f["rule"] != "rider-note"]
            notes = sum(len(t.notes) for t in watch.ended_trips)
            self.assertEqual(len(thread.of_kind("finding")), len(findings), path)
            self.assertEqual(len(thread.of_kind("note")), notes, path)

    def test_heartbeats_are_bounded_by_the_length_of_the_ride(self):
        for path in self.LOGS:
            if not os.path.exists(path):
                continue
            watch, thread = self.replay(path)
            minutes = sum((t.end_ms - t.start_ms) / 60000.0
                          for t in watch.ended_trips)
            self.assertLessEqual(
                len(thread.of_kind("heartbeat")),
                int(minutes / 10) + len(watch.ended_trips), path)

    def test_the_whole_ride_fits_in_a_conversation(self):
        """A ride is a handful of lines, not a feed."""
        for path in self.LOGS:
            if not os.path.exists(path):
                continue
            watch, thread = self.replay(path)
            with open(path) as f:
                events = sum(1 for _ in f)
            self.assertLess(len(thread.pushes), max(40, events // 100), path)

    def test_the_incident_ride_tells_the_thread_about_the_stop_count(self):
        path = self.LOGS[0]
        if not os.path.exists(path):
            self.skipTest("%s not present" % path)
        watch, thread = self.replay(path)
        self.assertTrue(
            any("stop-count-collapse" in p["line"]
                for p in thread.of_kind("finding")),
            "the thread must hear about the 17:28 collapse: %s" % thread.lines())


class TestPushoverCreds(unittest.TestCase):
    """The send path is verified by parsing only — never by sending."""

    def _write(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_parses_the_riders_key_value_format(self):
        path = self._write("USER_KEY=uuuu1111\nAPI_TOKEN=aaaa2222\n")
        self.assertEqual(read_pushover_creds(path), ("uuuu1111", "aaaa2222"))

    def test_parses_a_bare_two_line_file(self):
        path = self._write("uuuu1111\naaaa2222\n")
        self.assertEqual(read_pushover_creds(path), ("uuuu1111", "aaaa2222"))

    def test_rejects_an_unparseable_file(self):
        path = self._write("nothing useful here\n")
        with self.assertRaises(ValueError):
            read_pushover_creds(path)

    def test_the_real_credentials_file_parses(self):
        real = os.path.join(os.path.expanduser("~"),
                            ".config", "pushover", "credentials")
        if not os.path.exists(real):
            self.skipTest("no credentials installed")
        user, token = read_pushover_creds(real)
        self.assertTrue(user and "=" not in user)
        self.assertTrue(token and "=" not in token)


@unittest.skipUnless(os.path.exists(REAL_LOG),
                     "real telemetry %s not present" % REAL_LOG)
class TestRealIncidentReplay(unittest.TestCase):
    """Replay the real 2026-07-29 ride and assert the incident is caught."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ride-watch-replay-")
        cls.watch = run_replay(REAL_LOG, watch=quiet_watch(cls.tmp))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def incident_rules(self):
        return {f["rule"] for f in self.watch.all_findings
                if INCIDENT_START_MS <= f["tsMs"] <= INCIDENT_END_MS}

    def test_the_ride_is_recognised_as_one_trip(self):
        self.assertEqual(len(self.watch.ended_trips), 1)
        self.assertEqual(self.watch.ended_trips[0].session, "ms6m3bgy-0j7v94")

    def test_board_state_anomaly_is_flagged(self):
        hit = self.incident_rules()
        self.assertTrue(
            {"aboard-swap", "riding-flip"} & hit,
            "expected aboard-swap and/or riding-flip in the incident window, "
            "got %s" % sorted(hit))

    def test_stop_count_collapse_is_flagged(self):
        self.assertIn("stop-count-collapse", self.incident_rules())

    def test_rider_would_not_be_paged_more_than_twice(self):
        sent = [p for p in self.watch.push_log if p.get("sent")]
        self.assertLessEqual(len(sent), 2,
                             "sent %d pages: %s" % (len(sent), sent))

    def test_the_stop_count_collapse_is_the_page_the_rider_gets(self):
        """The whole point of the ranking.

        Before coalescing, riding-flip (17:28:45) won the window and the stop
        count collapse (17:28:53) died to the 120s rate limit — so the rider
        was told about a trip id and not about the counter that would have put
        them off at the wrong stop.
        """
        sent = [p for p in self.watch.push_log if p.get("sent")]
        self.assertTrue(
            any("Stop count wrong" in p["body"] for p in sent),
            "stop-count-collapse must be one of the <=2 pages; got %s"
            % [p["body"] for p in sent])
        collapse = [f for f in self.watch.all_findings
                    if f["rule"] == "stop-count-collapse"]
        self.assertEqual(collapse[0]["paged"], True)

    def test_the_17_28_cascade_costs_the_rider_one_interrupt(self):
        """Five page-worthy findings in eight seconds, one page.

        Four until 2026-08-31, when notification-repeat gained a stable key
        and started seeing the two "Off Route" pushes at 17:28:49 / 17:28:53
        that its byte key had counted as separate alerts. One more finding in
        the window, still one interrupt — which is what the ranking is for.
        """
        cascade = [p for p in self.watch.push_log
                   if INCIDENT_START_MS <= p["tsMs"] <= INCIDENT_END_MS]
        sent = [p for p in cascade if p.get("sent")]
        superseded = [p for p in cascade
                      if p.get("suppressed", "").startswith("superseded-by")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(superseded), 4)
        for p in superseded:
            self.assertEqual(p["suppressed"], "superseded-by-stop-count-collapse")

    def test_replay_never_sends_a_real_push(self):
        for p in self.watch.push_log:
            self.assertIn(p["sent"], ("dry-run", False))

    def test_page_worthy_findings_carry_rider_ready_copy(self):
        for p in self.watch.push_log:
            self.assertLess(len(p["body"]), 120, p["body"])
            self.assertNotIn("!", p["body"])

    def test_the_progress_teleports_are_caught(self):
        """Twice on this ride the bar outran the bus; nothing used to notice.

        17:05:12 (0% -> 13%) and 17:20:11 (35% -> 71%), each in one second
        while the Orange Line covered about 6.5m.
        """
        hits = [f for f in self.watch.all_findings
                if f["rule"] == "progress-without-motion"]
        self.assertEqual(len(hits), 2, [f["summary"] for f in hits])
        for f in hits:
            self.assertEqual(f["severity"], "warn")   # diagnostic, not a page
            self.assertLess(f["context"]["movedMeters"], 15.0)
        self.assertEqual(hits[0]["time"][:5], "17:05")
        self.assertEqual(hits[1]["time"][:5], "17:20")


class TestNotificationStormReplay(unittest.TestCase):
    """Replay the 7/31 ride whose 14-buzz storm nothing caught at the time.

    Every finding that ride produced was a rider note: the rider typed the
    complaint out by hand on a bike because the engine had no rule for a phone
    misbehaving at them. This is that rule, against that ride.
    """

    LOG = os.path.join(os.path.expanduser("~"), "otp-debug-logs",
                       "debug-2026-07-31.jsonl")

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(cls.LOG):
            raise unittest.SkipTest("%s not present" % cls.LOG)
        cls.tmp = tempfile.mkdtemp(prefix="ride-watch-storm-")
        cls.watch = run_replay(cls.LOG, watch=quiet_watch(cls.tmp))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def hits(self):
        return [f for f in self.watch.all_findings
                if f["rule"] == "notification-repeat"]

    def test_the_turn_alert_storm_is_caught_at_the_second_buzz(self):
        # Was 11:53:38, the 3rd buzz, until 2026-08-31. Thirty-one seconds
        # earlier is not the point — the point is that the same change is what
        # lets the rule see the 8/28 storms at all, and this ride proves it
        # costs nothing on the storm it was originally written for.
        first = self.hits()[0]
        self.assertEqual(first["time"], "11:53:07")
        self.assertEqual(first["severity"], "page")
        self.assertEqual(first["context"]["count"], 2)
        self.assertEqual(first["context"]["title"],
                         "Turn right on Village Lane")
        self.assertEqual(first["context"]["key"],
                         "UPCOMING_TURN_1785518021000_0_prepare")

    def test_it_fires_six_minutes_before_the_rider_had_to_type_it(self):
        note = next(f for f in self.watch.all_findings
                    if f["rule"] == "rider-note"
                    and "notifications" in f["summary"])
        self.assertLess(self.hits()[0]["tsMs"], note["tsMs"])
        gap = (note["tsMs"] - self.hits()[0]["tsMs"]) / 60000.0
        self.assertGreater(gap, 1.0)

    def test_fourteen_buzzes_cost_the_rider_one_finding(self):
        storm = [f for f in self.hits()
                 if f["context"]["title"] == "Turn right on Village Lane"
                 and f["time"].startswith("11:")]
        self.assertEqual(len(storm), 1)

    def test_the_page_copy_follows_the_rider_rules(self):
        for p in self.watch.push_log:
            self.assertLess(len(p["body"]), 120, p["body"])
            self.assertNotIn("!", p["body"])
        sent = [p for p in self.watch.push_log if p.get("sent")]
        self.assertTrue(any("Ignore the buzzing" in p["body"] for p in sent))


# The 2026-08-09 backwards itinerary: the rider photographed a trip sheet
# reading 7:29 PM above 7:18 PM. The onboard optimizer anchored an onward plan
# to stop 1:53313's realtime arrival, which the feed published as UPDATED with
# delay 0 while its neighbours ran ~11 min late — 9m13.9s behind the clock.
# The daemon watched the whole ride and raised nothing.
LOG_0809 = os.path.join(
    os.path.expanduser("~"), "otp-debug-logs", "debug-2026-08-10.jsonl")
SESSION_0809 = "msmhi3j5-lnt6uw"


def backwards_itinerary(inversion_ms):
    """A walk -> bus -> walk trip whose bus leg starts before the walk ends."""
    payload = transit_itinerary()
    legs = payload["itinerary"]["legs"]
    legs[0]["startTime"] = T0
    legs[0]["endTime"] = T0 + 600000
    legs[1]["startTime"] = T0 + 600000 - inversion_ms
    legs[1]["endTime"] = T0 + 1200000
    legs[2]["startTime"] = T0 + 1200000
    legs[2]["endTime"] = T0 + 1500000
    return payload


class TestBackwardsItineraryRules(RuleTestCase):
    """The two 8/9 rules, on synthetic streams."""

    def test_a_backwards_itinerary_pages(self):
        b = StreamBuilder().start(backwards_itinerary(680170))
        hits = self.find(self.run_stream(b), "itinerary-backwards")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")
        self.assertEqual(hits[0]["context"]["leg"], 1)
        self.assertEqual(hits[0]["context"]["byMs"], 680170)

    def test_a_forward_itinerary_is_silent(self):
        b = StreamBuilder().start()
        self.assertEqual(self.find(self.run_stream(b), "itinerary-backwards"), [])

    def test_a_sub_threshold_overlap_is_not_an_inversion(self):
        """Clocks and rounding differ across the wire; a rider sees neither."""
        b = StreamBuilder().start(backwards_itinerary(1500))
        self.assertEqual(self.find(self.run_stream(b), "itinerary-backwards"), [])

    def test_a_a_mid_trip_swap_is_checked_too(self):
        b = StreamBuilder().start().advance(60000)
        b.start(backwards_itinerary(680170))
        self.assertEqual(
            len(self.find(self.run_stream(b), "itinerary-backwards")), 1)

    def test_b_a_stale_alight_candidate_pages(self):
        # The onboard flow runs BEFORE the trip exists, so this finding is held
        # and flushed when START_GO_MODE opens the trip.
        b = StreamBuilder()
        b.action("START_ONBOARD_OPTIMIZE", {"candidates": [
            {"busArrivalEpoch": T0 - 553857, "realtime": True,
             "stopId": "1:53313", "stopName": "2nd Ave S & 7th St"}]})
        b.advance(20000).start()
        hits = self.find(self.run_stream(b), "stale-alight-candidate")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["context"]["stopId"], "1:53313")
        self.assertEqual(hits[0]["context"]["behindMs"], 553857)
        # WARN, not page: as a page it spent the 2/trip budget on the precursor
        # and suppressed itinerary-backwards, the actionable one.
        self.assertEqual(hits[0]["severity"], "warn")

    def test_b_a_candidate_in_the_future_is_silent(self):
        b = StreamBuilder()
        b.action("START_ONBOARD_OPTIMIZE", {"candidates": [
            {"busArrivalEpoch": T0 + 600000, "stopId": "1:53314"}]})
        b.advance(20000).start()
        self.assertEqual(
            self.find(self.run_stream(b), "stale-alight-candidate"), [])

    def test_b_the_offered_results_are_checked_as_well(self):
        b = StreamBuilder()
        b.action("SET_ONBOARD_RESULT", [
            {"busArrivalEpoch": T0 - 553857, "stopId": "1:53313",
             "stopName": "2nd Ave S & 7th St"}])
        b.advance(20000).start()
        self.assertEqual(
            len(self.find(self.run_stream(b), "stale-alight-candidate")), 1)

    def test_b_a_repeat_optimize_does_not_say_it_twice(self):
        """One bad feed reading, one line — 8/9 produced four optimizes."""
        b = StreamBuilder()
        for _ in range(3):
            b.action("START_ONBOARD_OPTIMIZE", {"candidates": [
                {"busArrivalEpoch": T0 - 553857, "stopId": "1:53313"}]})
            b.advance(5000)
        b.start().advance(5000)
        b.action("START_ONBOARD_OPTIMIZE", {"candidates": [
            {"busArrivalEpoch": T0 - 553857, "stopId": "1:53313"}]})
        self.assertEqual(
            len(self.find(self.run_stream(b), "stale-alight-candidate")), 1)

    def test_b_only_the_worst_candidate_of_a_batch_is_reported(self):
        """One optimize is one anomaly, not five."""
        b = StreamBuilder()
        b.action("START_ONBOARD_OPTIMIZE", {"candidates": [
            {"busArrivalEpoch": T0 - 100000, "stopId": "1:a"},
            {"busArrivalEpoch": T0 - 553857, "stopId": "1:b"},
            {"busArrivalEpoch": T0 - 200000, "stopId": "1:c"}]})
        b.advance(20000).start()
        hits = self.find(self.run_stream(b), "stale-alight-candidate")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["context"]["stopId"], "1:b")


@unittest.skipUnless(os.path.exists(LOG_0809), "%s not present" % LOG_0809)
class TestBackwardsItineraryReplay(unittest.TestCase):
    """Replay the real 8/9 ride: would these rules have caught it?"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ride-watch-0809-")
        cls.watch = run_replay(LOG_0809, watch=quiet_watch(cls.tmp))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def hits(self, rule):
        return [f for f in self.watch.all_findings
                if f["rule"] == rule and f["session"] == SESSION_0809]

    def test_the_backwards_itinerary_is_caught(self):
        hits = self.hits("itinerary-backwards")
        self.assertTrue(hits, "the 8/9 START_GO_MODE ran backwards and was "
                              "not flagged")
        # Leg 1 started 692,303 ms before leg 0 ended.
        self.assertEqual(hits[0]["context"]["leg"], 1)
        self.assertGreater(hits[0]["context"]["byMs"], 600000)

    def test_the_stale_candidate_is_caught_before_the_rider_is_shown_it(self):
        hits = self.hits("stale-alight-candidate")
        self.assertTrue(hits, "every onboard optimize fell through the "
                              "dispatch before this rule existed")
        # The first one fires at 19:24:38, five minutes before the rider was
        # sent to a route 22 that had already gone.
        self.assertLess(hits[0]["tsMs"], 1786321773307)

    def test_the_backwards_trip_sheet_is_what_reaches_the_rider(self):
        # Pages are capped at 2/trip, first come. The precursor rule fires
        # four times and would have spent the whole budget before the trip
        # sheet ever ran backwards, which is why it warns instead.
        paged = [p for p in self.watch.push_log if p.get("sent")]
        self.assertTrue(paged, "the whole incident raised nothing to the rider")
        self.assertTrue(
            any("backwards" in p["body"] for p in paged),
            "the rider was paged, but not about the thing they photographed: "
            "%s" % [p["body"] for p in paged])


class TestDuplicateRecords(RuleTestCase):
    """The debug-log client re-POSTs a batch it is not sure landed.

    ~1,000-1,700 records a day arrive twice across 8/27-8/29 — 3.7% of the
    8/27 stream — carrying the app's original `t` and payload id, differing
    only in the sidecar's `recv`. Every counting rule in the daemon read that
    stream as truth.
    """

    def test_a_re_posted_record_is_processed_once(self):
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        dup = dict(b.events[-1])
        dup["recv"] = dup["recv"] + 0.208
        b.events.append(dup)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(self.find(watch, "stop-count-collapse")), 1)
        self.assertEqual(watch.duplicate_records, 1)

    def test_a_genuine_second_event_is_not_a_duplicate(self):
        """Identity is (session, kind, type, t, payload id) — the app's own
        clock, not arrival. Two real events have two timestamps."""
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).position()
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.duplicate_records, 0)

    def test_a_burst_inside_one_delivery_is_not_a_duplicate(self):
        """The correction of 2026-08-31. Identity alone dropped genuine records.

        8/27 13:35:02 carries 197 POSITION_RESPONSE actions inside 584 ms, one
        per in-flight request settling, and many share a millisecond. Keyed on
        identity alone the daemon discarded 492 real records that day, 461 of
        them POSITION_RESPONSE, to catch 1,207 true re-POSTs. Records written
        by one POST share a `recv`, so a repeat inside one delivery cannot be
        a re-send.
        """
        b = StreamBuilder().start().advance(1000).position()
        twin = dict(b.events[-1])          # same t, same payload, same delivery
        b.events.append(twin)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.duplicate_records, 0)

    def test_the_same_record_in_a_later_delivery_still_is(self):
        """The other half: differing `recv` is what makes it a re-POST."""
        b = StreamBuilder().start().advance(1000).position()
        again = dict(b.events[-1])
        again["recv"] = again["recv"] + 2.096   # the real 8/27 gap
        b.events.append(again)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.duplicate_records, 1)

    def test_a_client_minted_entry_id_is_preferred_over_the_heuristic(self):
        """Once entries carry ids the inference is unnecessary: a re-send
        carries the original's id, a burst member carries its own."""
        b = StreamBuilder().start().advance(1000).position()
        b.events[-1]["id"] = "sess-1a"
        again = dict(b.events[-1])
        again["recv"] = again["recv"] + 1.0
        again["t"] = again["t"] + 5          # heuristic would MISS this
        b.events.append(again)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.duplicate_records, 1)

    def test_the_dedup_ring_is_bounded(self):
        b = StreamBuilder().start()
        for _ in range(ride_watch.RECORD_DEDUP_RING + 500):
            b.advance(1000).position()
        watch = self.run_stream(b, finalize=False)
        self.assertLessEqual(len(watch._seen_records),
                             ride_watch.RECORD_DEDUP_RING)
        self.assertEqual(len(watch._seen_record_keys),
                         len(watch._seen_records))


class TestStalledProgressContext(RuleTestCase):
    """8/28: five stalled-progress findings read as a dead GPS receiver.

    The finding reported `anchor[0]` — where the rider FIRST stopped, up to 15
    minutes and 60 m ago — as `lat`/`lon`, and carried no fix time and no fix
    count at all. The receiver was healthy throughout: 2,168 distinct fixes,
    ~4.1 m apart.
    """

    def stall(self):
        b = StreamBuilder().start().advance(1000).progress(leg=2, prog=0.0)
        b.advance(1000).position()
        for i in range(20):
            # Drifting slowly, well inside STALL_RADIUS_M: stationary, but
            # emphatically still receiving.
            b.advance(60 * 1000).position_metres_north(1 + i % 3)
        watch = self.run_stream(b)
        hits = self.find(watch, "stalled-progress")
        self.assertTrue(hits)
        return hits[0]

    def test_the_finding_reports_where_the_rider_is_now(self):
        ctx = self.stall()["context"]
        self.assertEqual(ctx["lat"], StreamBuilder.LAT + 3 * 9.0e-6)
        self.assertNotEqual(ctx["lat"], ctx["anchorLat"])

    def test_the_anchor_is_still_reported_under_its_own_name(self):
        ctx = self.stall()["context"]
        self.assertEqual(ctx["anchorLat"], StreamBuilder.LAT)
        self.assertIsNotNone(ctx["anchorSetMs"])
        self.assertLess(ctx["movedFromAnchorM"], ride_watch.STALL_RADIUS_M)

    def test_the_finding_says_the_gps_is_alive(self):
        """The one fact that would have stopped the 8/28 misreading."""
        hit = self.stall()
        ctx = hit["context"]
        self.assertGreater(ctx["fixesSinceAnchor"], 10)
        self.assertLess(ctx["sinceLastFixMs"], ride_watch.GPS_GAP_MS)
        self.assertIsNotNone(ctx["lastFixMs"])
        self.assertIn("GPS live", hit["summary"])


class TestReplanNotConverging(RuleTestCase):
    """8/28 afternoon: 32 minutes re-planning into the State Fairgrounds.

    The destination was inside the fence, where the street graph stops. The
    distance never dropped below 427 m. reroute-storm counts reroute events
    and never looks at whether they work, so it had nothing to say.
    """

    def circling(self, replans=4, dest=430.0, unreachable_at=None):
        """A rider who keeps being re-planned at without getting closer."""
        b = StreamBuilder().start().advance(1000).progress(leg=2, dest=1200.0)
        b.advance(60 * 1000).progress(leg=2, dest=dest)
        for i in range(replans):
            b.advance(3 * 60 * 1000)
            if unreachable_at is not None and i == unreachable_at:
                b.notification(
                    title="This is as close as routing gets",
                    ntype="DESTINATION_UNREACHABLE",
                    message="Still 430m away and re-planning isn't closing "
                            "the gap.",
                    nid="DESTINATION_UNREACHABLE_destination_%d" % b.t)
                b.advance(1000)
            b.start()                     # the applied re-plan
            b.advance(30 * 1000).progress(leg=2, dest=dest + i)
        return self.run_stream(b, finalize=False)

    def test_re_planning_in_circles_pages(self):
        watch = self.circling()
        hits = self.find(watch, "replan-not-converging")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")
        self.assertEqual(hits[0]["context"]["appSaidUnreachable"], False)
        self.assertEqual(hits[0]["context"]["bestDistanceM"], 430.0)

    def test_it_waits_for_the_client_to_speak_first(self):
        """The client retires the mode on re-plan 3 and tells the rider on
        re-plan 4. Firing at 3 would page about a defect the app was in the
        middle of reporting itself."""
        self.assertNotIn("replan-not-converging",
                         self.rules(self.circling(replans=3)))

    def test_the_app_saying_it_first_means_the_daemon_stays_quiet(self):
        """DESTINATION_UNREACHABLE is the cheaper signal and the rider already
        has it on their phone at high priority. Repeating it would spend one
        of two interrupts saying nothing new."""
        watch = self.circling(unreachable_at=1)
        self.assertNotIn("replan-not-converging", self.rules(watch))
        hits = self.find(watch, "destination-unreachable")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "info")

    def test_re_planning_that_is_working_is_not_a_finding(self):
        """A rider on a changing bus network gets re-planned at constantly.
        Closing the distance is the whole difference."""
        b = StreamBuilder().start().advance(1000).progress(leg=2, dest=4000.0)
        for i in range(6):
            b.advance(3 * 60 * 1000).start()
            b.advance(30 * 1000).progress(leg=2, dest=4000.0 - 400 * (i + 1))
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("replan-not-converging", self.rules(watch))

    def test_gps_scatter_is_not_a_gain(self):
        """DEST_GAIN_MIN_M mirrors the client's 50 m for a reason: 8/28's
        427 m floor wandered by tens of metres for half an hour."""
        b = StreamBuilder().start().advance(1000).progress(leg=2, dest=430.0)
        for i in range(5):
            b.advance(3 * 60 * 1000).start()
            b.advance(30 * 1000).progress(leg=2, dest=430.0 - 20 * (i % 2))
        watch = self.run_stream(b, finalize=False)
        self.assertIn("replan-not-converging", self.rules(watch))

    def test_a_real_gain_clears_the_count(self):
        """Whatever changed, the rider is moving again and gets the machinery
        back — same as the client resetting stalledModes."""
        b = StreamBuilder().start().advance(1000).progress(leg=2, dest=900.0)
        for _ in range(3):
            b.advance(3 * 60 * 1000).start()
            b.advance(30 * 1000).progress(leg=2, dest=900.0)
        b.advance(30 * 1000).progress(leg=2, dest=700.0)     # 200 m of real gain
        b.advance(3 * 60 * 1000).start()
        b.advance(30 * 1000).progress(leg=2, dest=700.0)
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("replan-not-converging", self.rules(watch))

    def test_one_replan_logged_twice_counts_once(self):
        """8/28 16:44:06: a START_REROUTE and the START_GO_MODE that applied
        its result, in the same second. Counting both retires a converging
        trip on half the evidence the client used."""
        b = StreamBuilder().start().advance(1000).progress(leg=2, dest=430.0)
        for _ in range(3):
            b.advance(3 * 60 * 1000)
            b.action("START_REROUTE", {"autoApply": True,
                                       "reason": "boarded-earlier"})
            b.start()                     # same second, one re-plan
            b.advance(30 * 1000).progress(leg=2, dest=430.0)
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("replan-not-converging", self.rules(watch))

    def test_a_trip_with_no_measured_distance_never_fires(self):
        """"No net reduction" is not a fact you can hold about a distance
        nobody has measured — the client's own null-state guard."""
        b = StreamBuilder().start().advance(1000).progress(leg=2)
        for _ in range(8):
            b.advance(3 * 60 * 1000).start()
            b.advance(30 * 1000).progress(leg=2)
        watch = self.run_stream(b, finalize=False)
        self.assertNotIn("replan-not-converging", self.rules(watch))

    def test_the_page_copy_follows_the_rider_rules(self):
        watch = self.circling()
        body = [p for p in watch.push_log
                if "Re-planning" in p["body"]][0]["body"]
        self.assertLess(len(body), 120, body)
        self.assertNotIn("!", body)
        self.assertIn("Finish from here", body)


class TestReportDeadline(RuleTestCase):
    """8/28: a wrap-up that never appeared paged nobody, ever.

    _report_fallback_push had one call site, guarded by _thread_missing, which
    is true only when the tmux spawn failed or the pane is dead. That evening's
    thread spawned fine, took the wrap-up line, and sat at a permission prompt
    for about three hours.
    """

    def ended_ride(self, thread_ok=True):
        thread = StubThread(ok=thread_ok)
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        b.advance(1000).stop()
        watch = self.run_stream(b, finalize=False, thread=thread)
        return watch

    def fallbacks(self, watch):
        return [p for p in watch.push_log if p["kind"] == "fallback"]

    def test_a_deadline_is_armed_when_a_wrap_up_is_asked_for(self):
        watch = self.ended_ride()
        self.assertEqual(len(watch.report_deadlines), 1)
        # The exact path the thread was handed, not a second guess at it.
        req = json.loads(read_text(report_requests(self.tmp)[0]))
        self.assertEqual(watch.report_deadlines[0]["reportPath"],
                         req["reportPath"])

    def test_a_healthy_thread_that_never_writes_still_pages(self):
        """The 8/28 hole. Nothing is wrong with the pane; the report simply
        does not exist."""
        watch = self.ended_ride()
        self.assertEqual(self.fallbacks(watch), [])
        watch.clock_ms += ride_watch.REPORT_DEADLINE_MS + 1000
        watch.check_timers()
        self.assertEqual(len(self.fallbacks(watch)), 1)
        self.assertIn("Report pending", self.fallbacks(watch)[0]["body"])

    def test_the_deadline_survives_the_trip_being_deleted(self):
        """_end_trip does `del self.trips[session]`, so check_timers' own loop
        can never see an ended ride. The deadline is not on the trip."""
        watch = self.ended_ride()
        self.assertEqual(watch.trips, {})
        watch.clock_ms += ride_watch.REPORT_DEADLINE_MS + 1000
        watch.check_timers()
        self.assertEqual(len(self.fallbacks(watch)), 1)

    def test_a_wrap_up_that_lands_in_time_pages_nobody(self):
        watch = self.ended_ride()
        path = watch.report_deadlines[0]["reportPath"]
        with open(path, "w") as f:
            f.write("# ride report\n")
        watch.clock_ms += ride_watch.REPORT_DEADLINE_MS + 1000
        watch.check_timers()
        self.assertEqual(self.fallbacks(watch), [])
        self.assertEqual(watch.report_deadlines, [])

    def test_the_rider_is_paged_once_not_every_tick(self):
        watch = self.ended_ride()
        watch.clock_ms += ride_watch.REPORT_DEADLINE_MS + 1000
        for _ in range(5):
            watch.clock_ms += 60 * 1000
            watch.check_timers()
        self.assertEqual(len(self.fallbacks(watch)), 1)

    def test_a_clean_ride_arms_nothing(self):
        """No findings, no request file, nothing to wait for."""
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        b.advance(1000).stop()
        watch = self.run_stream(b, finalize=False, thread=StubThread())
        self.assertEqual(watch.report_deadlines, [])

    def test_a_failed_spawn_pages_now_and_does_not_also_arm(self):
        """The old path still fires immediately when there is provably no
        thread; the deadline is for the case it cannot see."""
        watch = self.ended_ride(thread_ok=False)
        self.assertEqual(len(self.fallbacks(watch)), 1)
        self.assertEqual(watch.report_deadlines, [])

    def test_the_deadline_survives_a_daemon_restart(self):
        """Restart-on-commit is now a thing that happens to this process
        mid-evening, and the promise must outlive it."""
        watch = self.ended_ride()
        restarted = quiet_watch(self.tmp)
        self.assertEqual(len(restarted.report_deadlines), 1)
        restarted.clock_ms = watch.clock_ms + ride_watch.REPORT_DEADLINE_MS + 1
        restarted.check_timers()
        self.assertEqual(len(self.fallbacks(restarted)), 1)


class TestArrivalEndsTheRide(RuleTestCase):
    """8/31: the rider arrived at 18:52 and the app talked until 20:36.

    Every trip-end this daemon had was a silence — STOP_GO_MODE, the
    15-minute timeout, replay EOF — and that evening the stream never fell
    silent: 18,105 records of `status: "completed"` at 42 m from the door,
    across a UTC day rollover, still arriving two hours later. So the ride was
    never closed, no report request was ever written, and nobody was asked to
    write it up.
    """

    def arrived_ride(self, thread=None, minutes=6, arrive=True,
                     findings=True, note_after_ms=None):
        b = StreamBuilder().start().advance(1000).position()
        if findings:
            b.advance(1000).progress(leg=1, stops=6, prog=20.0)
            b.advance(1000).progress(leg=1, stops=1, prog=21.0)
        else:
            b.advance(1000).progress(leg=1, stops=6, prog=20.0)
        if arrive:
            b.advance(1000).action("SET_ARRIVED", 1)
        if note_after_ms is not None:
            b.advance(note_after_ms).note("thanks, made it")
        # ...and then the app goes on ticking, exactly as it did that evening.
        for _ in range(minutes * 2):
            b.advance(30000).position().progress(
                leg=2, prog=76.4, status="completed", next_stop=None)
        return self.run_stream(b, finalize=False, thread=thread)

    def test_a_ride_that_keeps_ticking_after_arrival_still_ends(self):
        watch = self.arrived_ride()
        self.assertEqual([t.end_reason for t in watch.ended_trips], ["arrived"])
        self.assertEqual(watch.trips, {})

    def test_the_closed_ride_asks_for_its_report(self):
        watch = self.arrived_ride()
        paths = report_requests(self.tmp)
        self.assertEqual(len(paths), 1)
        req = json.loads(read_text(paths[0]))
        self.assertEqual(req["endReason"], "arrived")
        self.assertGreaterEqual(req["findingsCount"], 1)

    def test_the_ride_is_not_closed_inside_the_grace_window(self):
        """A rider who is still typing at the destination is still on the
        ride; five minutes is the window their note has to arrive in."""
        watch = self.arrived_ride(minutes=2)
        self.assertEqual(watch.ended_trips, [])
        self.assertEqual(len(watch._active_trips()), 1)

    def test_a_note_typed_at_the_destination_is_inside_the_ride(self):
        watch = self.arrived_ride(note_after_ms=60 * 1000)
        req = json.loads(read_text(report_requests(self.tmp)[0]))
        self.assertEqual([n["text"] for n in req["riderNotes"]],
                         ["thanks, made it"])

    def test_completed_status_closes_a_ride_that_never_said_set_arrived(self):
        """SET_ARRIVED fires once per mount, so a trip adopted afterwards can
        never see it. `status: "completed"` is the same fact, every tick."""
        watch = self.arrived_ride(arrive=False)
        self.assertEqual([t.end_reason for t in watch.ended_trips], ["arrived"])

    def test_boarding_after_an_arrival_puts_the_ride_back_in_progress(self):
        """Arrival is an inference and this one acts on it, so the ride has to
        be able to say it is not over."""
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress(leg=1, stops=6, prog=20.0)
        b.advance(1000).action("SET_ARRIVED", 1)
        b.advance(1000).riding(leg=1)
        for _ in range(20):
            b.advance(30000).position().progress(leg=1, stops=5, prog=30.0)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.ended_trips, [])
        trip = watch._active_trips()[0]
        self.assertIsNone(trip.arrived_ms)

    def test_a_later_leg_after_an_arrival_puts_the_ride_back_in_progress(self):
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress(leg=1, stops=6, prog=20.0)
        b.advance(1000).action("SET_ARRIVED", 1)
        for _ in range(20):
            b.advance(30000).position().progress(leg=2, stops=None, prog=30.0)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.ended_trips, [])
        self.assertIsNone(watch._active_trips()[0].arrived_ms)

    def test_a_trip_the_app_calls_completed_is_never_adopted(self):
        """Both of 8/31's phantom rides were adopted off post-arrival ticks of
        a trip that was already finished: 37 findings about a rider standing
        still, two threads, two reports."""
        thread = StubThread()
        b = StreamBuilder()
        for _ in range(12):
            b.advance(1000).position().progress(
                leg=3, prog=76.4, status="completed", next_stop=None)
        watch = self.run_stream(b, finalize=False, thread=thread)
        self.assertEqual(watch.trips, {})
        self.assertEqual(watch.ended_trips, [])
        self.assertEqual(thread.spawns, [])
        self.assertEqual(watch.all_findings, [])

    def test_a_closed_ride_is_not_re_adopted_off_the_next_tick(self):
        """8/27 replayed with the arrival rule and without this guard turned
        one 4.5-hour ride into nine: close at arrival, re-adopt sixty seconds
        later, arrive again five minutes on. Post-arrival ticks do not all say
        `completed` — that afternoon the app went on map-matching a stationary
        rider and calling it `deviated`."""
        b = StreamBuilder().start().advance(1000).position()
        b.advance(1000).progress(leg=1, stops=6, prog=20.0)
        b.advance(1000).action("SET_ARRIVED", 1)
        for _ in range(60):        # half an hour of post-arrival noise
            b.advance(30000).position().progress(leg=1, prog=76.4,
                                                 status="deviated")
        watch = self.run_stream(b, finalize=False)
        self.assertEqual([t.end_reason for t in watch.ended_trips], ["arrived"])
        self.assertEqual(watch.trips, {})

    def test_a_restart_does_not_re_adopt_a_ride_closed_at_arrival(self):
        """The app is still streaming; restart-on-commit happens mid-evening."""
        watch = self.arrived_ride()
        restarted = quiet_watch(self.tmp)
        self.assertEqual(restarted.ended_arrived, watch.ended_arrived)
        b = StreamBuilder()
        b.advance(1000).progress(leg=2, prog=76.4, status="deviated")
        for ev in b.events:
            restarted.process(ev)
        self.assertEqual(restarted.ended_trips, [])
        self.assertEqual(restarted.trips, {})

    def test_a_new_start_go_mode_reopens_a_session_closed_at_arrival(self):
        """The rider asking for another ride under the same app load."""
        watch = self.arrived_ride()
        b = StreamBuilder(t=watch.clock_ms + 60000).start()
        b.advance(1000).progress(leg=0, stops=5, prog=1.0)
        for ev in b.events:
            watch.process(ev)
        self.assertEqual(len(watch._active_trips()), 1)

    def test_a_live_trip_is_still_adopted(self):
        """The guard is about `completed`, not about adoption."""
        b = StreamBuilder().advance(1000).progress(leg=1, stops=5, prog=20.0)
        watch = self.run_stream(b, finalize=False)
        self.assertTrue(watch.trips[SESSION].adopted)


class TestSessionChurn(RuleTestCase):
    """8/31 18:52:14 and 18:52:55: one situation, two session ids.

    Same phone, same frozen itinerary, 41 s apart, because the app re-mounted
    and the debug-log client minted a new id. The daemon read two rides.
    """

    def remounted_ride(self, thread=None, gap_ms=41000, device2="dev-1",
                       leg2=1, prog2=40.0, tail=None):
        b = StreamBuilder(device="dev-1")
        b.advance(1000).progress(leg=1, prog=40.0, stops=5)      # adopted
        b.advance(1000).position()
        b.session = "session-b"
        b.device = device2
        b.advance(gap_ms).progress(leg=leg2, prog=prog2, stops=5)
        b.advance(1000).position()
        if tail is not None:
            tail(b)
        return self.run_stream(b, finalize=False, thread=thread)

    def test_a_remount_mid_ride_is_one_ride_not_two(self):
        watch = self.remounted_ride()
        self.assertEqual(len(watch._active_trips()), 1)
        trip = watch.trips[SESSION]
        self.assertIs(watch.trips["session-b"], trip)
        self.assertEqual(trip.sessions, [SESSION, "session-b"])

    def test_the_split_is_recorded_rather_than_papered_over(self):
        """The app half of this is someone else's fix; the evidence for it has
        to be somewhere."""
        watch = self.remounted_ride()
        hits = self.find(watch, "session-churn")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "warn")
        self.assertEqual(hits[0]["context"]["newSession"], "session-b")
        self.assertEqual(hits[0]["context"]["priorSession"], SESSION)
        self.assertEqual(watch.push_log, [])          # not worth a buzz

    def test_the_continuation_gets_no_second_ride_thread(self):
        thread = StubThread()
        self.remounted_ride(thread=thread)
        self.assertEqual(len(thread.spawns), 1)
        self.assertEqual(thread.kinds().count("start"), 1)

    def test_one_ride_writes_one_report_request(self):
        def tail(b):
            b.advance(1000).progress(leg=1, prog=41.0, stops=1)   # collapse
            b.advance(1000).stop()

        watch = self.remounted_ride(tail=tail)
        self.assertEqual(len(watch.ended_trips), 1)
        paths = report_requests(self.tmp)
        self.assertEqual(len(paths), 1)
        req = json.loads(read_text(paths[0]))
        self.assertEqual(req["session"], SESSION)
        self.assertEqual(req["sessions"], [SESSION, "session-b"])
        self.assertEqual(watch.trips, {})

    def test_a_stop_under_the_new_id_ends_the_one_ride(self):
        watch = self.remounted_ride(tail=lambda b: b.advance(1000).stop())
        self.assertEqual(len(watch.ended_trips), 1)
        self.assertEqual(watch.ended_trips[0].session, SESSION)
        self.assertEqual(watch.trips, {})

    def test_the_timers_see_a_continued_ride_once(self):
        """Both ids are keys in self.trips; iterating .values() would tick the
        same ride twice and end it twice."""
        def tail(b):
            b.advance(ride_watch.SESSION_TIMEOUT_MS + 60000)
            b.session = "someone-else"
            b.device = None
            b.action("UPDATE_POSITION", {})

        watch = self.remounted_ride(tail=tail)
        self.assertEqual([t.end_reason for t in watch.ended_trips], ["timeout"])
        self.assertEqual(watch.trips, {})

    def test_a_second_phone_is_never_merged(self):
        watch = self.remounted_ride(device2="dev-2")
        self.assertEqual(len(watch._active_trips()), 2)
        self.assertEqual(self.find(watch, "session-churn"), [])

    def test_a_remount_the_daemon_cannot_vouch_for_is_never_merged(self):
        """No device id on the records: nothing anchors the two ids together."""
        b = StreamBuilder()
        b.advance(1000).progress(leg=1, prog=40.0, stops=5)
        b.session = "session-b"
        b.advance(41000).progress(leg=1, prog=40.0, stops=5)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(watch._active_trips()), 2)

    def test_a_late_new_session_is_its_own_ride(self):
        watch = self.remounted_ride(
            gap_ms=ride_watch.CONTINUATION_GAP_MS + 5000)
        self.assertEqual(len(watch._active_trips()), 2)

    def test_a_ride_that_starts_over_at_leg_zero_is_its_own_ride(self):
        """The gate that carries the weight: a genuinely new ride begins at
        the top of its first leg, not where the last one left off."""
        watch = self.remounted_ride(leg2=0, prog2=0.0)
        self.assertEqual(len(watch._active_trips()), 2)

    def test_a_different_place_in_the_same_leg_is_its_own_ride(self):
        watch = self.remounted_ride(prog2=70.0)
        self.assertEqual(len(watch._active_trips()), 2)

    def test_a_note_whose_session_was_guessed_still_finds_the_ride(self):
        """The sidecar guesses a note's session from the log tail and can
        miss; the daemon falls back to "the one ride that is running". A
        re-mount holds two session keys, and counting those as two rides
        would drop the note the rider just typed."""
        def tail(b):
            b.advance(1000).note("driver blew past my stop",
                                 session="who-knows")

        watch = self.remounted_ride(tail=tail)
        trip = watch.trips[SESSION]
        self.assertEqual([n["text"] for n in trip.notes],
                         ["driver blew past my stop"])

    def test_a_new_session_that_starts_go_mode_is_taken_at_its_word(self):
        """An explicit START_GO_MODE is the rider asking for a ride. Only the
        adoption path — a resume, which emits none — can be a continuation."""
        b = StreamBuilder(device="dev-1")
        b.advance(1000).progress(leg=1, prog=40.0, stops=5)
        b.session = "session-b"
        b.advance(20000).start()
        b.advance(1000).progress(leg=1, prog=40.0, stops=5)
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(len(watch._active_trips()), 2)


class TmuxResult:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class FakeTmux:
    """Answers `list-sessions` from a fixed list and records every argv."""

    def __init__(self, names):
        self.names = names
        self.calls = []

    def __call__(self, args, timeout=20):
        self.calls.append(args)
        if args[0] == "list-sessions":
            return TmuxResult(0, "\n".join(self.names))
        return TmuxResult(0, "")

    def killed(self):
        return [a[2] for a in self.calls if a[0] == "kill-session"]


class TestWrapUpPaneOutlivesTheNextRide(RuleTestCase):
    """Backlog 2.6, and the log says the mechanism plainly.

    15:52:31 "wrap-up now" to ride-1535; 15:52:48 the next ride starts and
    "previous ride thread(s) killed: ride-1535" — seventeen seconds. Again at
    17:07:50 -> 17:08:43 with ride-1700, and that report never existed; the
    deadline paged about it at 17:17:50. Not a daemon restart, which is what
    the backlog line says: it is _begin_ride_thread on the NEXT ride.
    """

    def ended_ride(self):
        thread = StubThread()
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        b.advance(1000).stop()
        watch = self.run_stream(b, finalize=False, thread=thread)
        return watch, thread

    def fallbacks(self, watch):
        return [p for p in watch.push_log if p["kind"] == "fallback"]

    def test_the_deadline_remembers_which_pane_was_asked(self):
        watch, thread = self.ended_ride()
        self.assertEqual(watch.report_deadlines[0].get("tmux"),
                         thread.spawns[0][0])

    def test_the_pane_still_writing_a_wrap_up_is_not_killed(self):
        watch, thread = self.ended_ride()
        pane = thread.spawns[0][0]
        fake = FakeTmux([pane, "ride-0101", "0", "ride-test-smoke"])
        watch._tmux = fake
        watch._kill_previous_threads(keep="ride-9999")
        self.assertEqual(fake.killed(), ["ride-0101"])

    def test_a_pane_whose_wrap_up_landed_is_killable_again(self):
        watch, thread = self.ended_ride()
        pane = thread.spawns[0][0]
        with open(watch.report_deadlines[0]["reportPath"], "w") as f:
            f.write("# ride report\n")
        watch.check_timers()
        fake = FakeTmux([pane])
        watch._tmux = fake
        watch._kill_previous_threads(keep="ride-9999")
        self.assertEqual(fake.killed(), [pane])

    def test_a_pane_whose_deadline_expired_is_killable_again(self):
        """Protection lasts REPORT_DEADLINE_MS, not forever: a pane that never
        writes must not pin the namespace for the rest of the evening."""
        watch, thread = self.ended_ride()
        pane = thread.spawns[0][0]
        watch.clock_ms += ride_watch.REPORT_DEADLINE_MS + 1000
        watch.check_timers()
        self.assertEqual(len(self.fallbacks(watch)), 1)
        fake = FakeTmux([pane])
        watch._tmux = fake
        watch._kill_previous_threads(keep="ride-9999")
        self.assertEqual(fake.killed(), [pane])

    def test_a_timeout_end_gives_the_thread_the_whole_window(self):
        """A timeout end is stamped with the ride's last event, fifteen
        minutes in the past. 8/31 18:00:34: "wrap-up expected ... by 17:55:33",
        and the missing-report page went out in the same second."""
        thread = StubThread()
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        b.advance(ride_watch.SESSION_TIMEOUT_MS + 60000)
        b.session = "other-session"
        b.action("UPDATE_POSITION", {})
        watch = self.run_stream(b, finalize=False, thread=thread)
        self.assertEqual([t.end_reason for t in watch.ended_trips], ["timeout"])
        self.assertEqual(self.fallbacks(watch), [])
        self.assertGreaterEqual(watch.report_deadlines[0]["dueMs"],
                                watch.clock_ms + ride_watch.REPORT_DEADLINE_MS)


class TestDaemonProvenance(RuleTestCase):
    """8/28: a five-day-stale daemon, and nothing it wrote said so.

    It produced five false stalled-progress findings, missed the arrival event
    (SET_ARRIVED handling did not exist in the loaded code), and nearly
    overwrote a report. The ride thread was reading source on disk that the
    process in memory had never loaded.
    """

    def moved_head(self, head="deadbee", behind="7"):
        """Pretend the tree moved on after this process started."""
        calls = []
        original = ride_watch._git_out

        def spy(args, **kw):
            calls.append(args)
            return behind if args[0] == "rev-list" else head

        ride_watch._git_out = spy
        self.addCleanup(setattr, ride_watch, "_git_out", original)
        return calls

    def test_a_head_that_moves_does_not_change_the_reported_running_sha(self):
        """The correctness pin, and the regression a naive implementation
        passes by accident.

        `git rev-parse HEAD` read at digest-write time reports the tree as it
        is NOW, so a five-day-stale daemon would stamp today's SHA and
        confidently claim to be current — the mismatch this exists to expose
        becomes invisible, and the header positively asserts something false.
        """
        stamped = ride_watch.DAEMON_GIT_SHA
        self.moved_head()
        lines = quiet_watch(self.tmp)._daemon_lines()
        self.assertEqual(ride_watch.DAEMON_GIT_SHA, stamped)
        self.assertIn(stamped, lines[0])
        self.assertNotIn("deadbee", lines[0])

    def test_a_head_that_moves_surfaces_the_stale_marker(self):
        """The feature. Printing one SHA still relies on somebody noticing it
        is old, and on 8/28 nobody did — for five days."""
        calls = self.moved_head()
        lines = quiet_watch(self.tmp)._daemon_lines()
        stale = [ln for ln in lines[1:] if "STALE" in ln]
        self.assertEqual(len(stale), 1, lines)
        self.assertIn("deadbee", stale[0])
        self.assertIn("7 commit(s) behind", stale[0])
        # Read-only git only: no `status`, which can take the index lock other
        # agents in this shared worktree are using.
        self.assertTrue(all(a[0] in ("rev-parse", "rev-list") for a in calls),
                        calls)

    def test_a_head_that_has_not_moved_raises_nothing(self):
        original = ride_watch._git_out
        ride_watch._git_out = lambda args, **kw: (
            ride_watch.DAEMON_GIT_SHA.split("-", 1)[0])
        self.addCleanup(setattr, ride_watch, "_git_out", original)
        watch = quiet_watch(self.tmp)
        self.assertFalse([ln for ln in watch._daemon_lines() if "STALE" in ln])

    def test_a_missing_head_never_claims_staleness(self):
        """git unavailable at write time is not evidence of anything."""
        original = ride_watch._git_out
        ride_watch._git_out = lambda args, **kw: None
        self.addCleanup(setattr, ride_watch, "_git_out", original)
        lines = quiet_watch(self.tmp)._daemon_lines()
        self.assertFalse([ln for ln in lines if "STALE" in ln], lines)
        self.assertIn(ride_watch.DAEMON_GIT_SHA, lines[0])

    def test_the_head_check_is_not_run_per_write(self):
        """Shelling out to git on every status write is both needless work on
        a hot path and the bug above waiting to happen."""
        calls = self.moved_head()
        watch = quiet_watch(self.tmp)
        for _ in range(50):
            watch._daemon_lines()
        self.assertEqual(len(calls), 2)   # one rev-parse, one rev-list

    def test_git_being_unavailable_stamps_unknown_and_never_raises(self):
        """The daemon must not die, or go quiet, because it could not
        introspect itself."""
        original = ride_watch.subprocess.run

        def boom(*a, **kw):
            raise OSError("no git here")

        ride_watch.subprocess.run = boom
        try:
            self.assertEqual(ride_watch._resolve_daemon_sha(), "unknown")
            self.assertIsNone(ride_watch._git_out(["rev-parse", "HEAD"]))
        finally:
            ride_watch.subprocess.run = original

    def test_the_status_file_names_the_running_daemon(self):
        watch = quiet_watch(self.tmp)
        watch.write_status(force=True)
        status = read_text(os.path.join(self.tmp, "current-ride.md"))
        self.assertIn("Daemon: ride_watch.py @ %s"
                      % ride_watch.DAEMON_GIT_SHA, status)
        self.assertIn("started", status)

    def test_the_digest_names_the_running_daemon(self):
        """The ride thread reads the digest before every reply; this is where
        it can see that the source it is about to read is not what is running."""
        thread = StubThread()
        b = StreamBuilder().start().advance(1000).progress(stops=6, prog=20.0)
        b.advance(1000).progress(stops=1, prog=21.0)
        self.run_stream(b, finalize=False, thread=thread)
        self.assertIn("Daemon: ride_watch.py @ %s" % ride_watch.DAEMON_GIT_SHA,
                      thread.pushes[-1]["digest"])

    def test_a_riders_own_status_file_carries_the_header_too(self):
        b = StreamBuilder().start().advance(1000).progress(stops=5)
        watch = self.run_stream(b, finalize=False)
        watch.write_status(force=True)
        own = read_text(os.path.join(self.tmp, "%s.current-ride.md" % SESSION))
        self.assertIn("Daemon: ride_watch.py @", own)
        self.assertIn("## Active trip", own)


if __name__ == "__main__":
    unittest.main(verbosity=2)
