#!/usr/bin/env python3
"""Tests for ride-watch. Stdlib only:  python3 ride-watch/test_ride_watch.py

Two layers:
  * synthetic streams that exercise each rule and the state machine in
    isolation, and
  * a replay of the real 2026-07-29 telemetry, which asserts that the
    afternoon's incident is actually caught and that the rider would not
    have been paged more than twice.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ride_watch  # noqa: E402
from ride_watch import (  # noqa: E402
    Log, RideWatch, read_pushover_creds, run_replay)


def quiet_watch(watch_dir, replay=True):
    """A watcher whose daemon log does not spam the test runner."""
    log = Log(os.path.join(watch_dir, "daemon.log"), echo=False)
    return RideWatch(dry_run=True, replay=replay, watch_dir=watch_dir, log=log)


def read_text(path):
    with open(path) as f:
        return f.read()

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

    def __init__(self, session=SESSION, t=T0):
        self.session = session
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
                 next_stop="Stop B"):
        p = {"currentLegIndex": leg, "currentLegProgress": prog,
             "status": status, "nextStopName": next_stop}
        if stops is not None:
            p["stopsRemaining"] = stops
        return self.action("UPDATE_PROGRESS", p)

    def position(self):
        return self.action("UPDATE_POSITION", {"position": {"coords": {}}})

    def riding(self, trip_id="1:100", vehicle="1:900", leg=1):
        return self.action("SET_RIDING", {
            "tripId": trip_id, "vehicleId": vehicle, "legIndex": leg,
            "routeId": "1:5", "headsign": "Downtown", "boardedAt": self.t})

    def route_match(self, dist, leg=1, on_route=True):
        return self.action("UPDATE_ROUTE_MATCH", {
            "legIndex": leg, "distanceFromRoute": dist,
            "progressAlongLeg": 0.1, "isOnRoute": on_route})

    def note(self, text, session=None):
        """A rider note exactly as the Flask sidecar writes it to the JSONL."""
        self.events.append({
            "kind": "rider-note", "event": "RIDER_NOTE", "text": text,
            "t": self.t, "recv": self.t / 1000.0,
            "session": self.session if session is None else session,
            "ip": "10.0.0.5"})
        return self


class RuleTestCase(unittest.TestCase):
    """Base: runs a synthetic stream through the real RideWatch code path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ride-watch-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_stream(self, builder, finalize=True):
        watch = quiet_watch(self.tmp)
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
        b.advance(60000).start()
        watch = self.run_stream(b)
        hits = self.find(watch, "aboard-swap")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "page")

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
        page_rules = {"stop-count-collapse", "missed-bus-while-riding",
                      "aboard-swap", "riding-flip", "deviated-streak"}
        self.assertEqual(page_rules, set(ride_watch.PAGE_RANK))
        self.assertEqual(
            ["stop-count-collapse", "missed-bus-while-riding", "aboard-swap",
             "riding-flip", "deviated-streak"],
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
        path = os.path.join(self.tmp, "report-request-%s.json" % SESSION)
        self.assertTrue(os.path.exists(path))
        req = json.loads(read_text(path))
        for key in ("session", "date", "startMs", "endMs", "findingsPath",
                    "itinerarySummary"):
            self.assertIn(key, req)
        self.assertEqual(req["session"], SESSION)

    def test_clean_ride_requests_no_report(self):
        b = StreamBuilder().start()
        for i in range(10):
            b.advance(5000).position().progress(stops=5)
        b.advance(1000).stop()
        watch = self.run_stream(b, finalize=False)
        self.assertEqual(watch.all_findings, [])
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "report-request-%s.json" % SESSION)))
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
        self.assertEqual(ctx["legProgress"], 45.0)
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
        req = json.loads(read_text(
            os.path.join(self.tmp, "report-request-%s.json" % SESSION)))
        self.assertEqual(req["notesCount"], 1)
        self.assertEqual(req["riderNotes"][0]["text"],
                         "app said 1 stop left, it was 6")


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
        """Four page-worthy findings in eight seconds, one page."""
        cascade = [p for p in self.watch.push_log
                   if INCIDENT_START_MS <= p["tsMs"] <= INCIDENT_END_MS]
        sent = [p for p in cascade if p.get("sent")]
        superseded = [p for p in cascade
                      if p.get("suppressed", "").startswith("superseded-by")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(superseded), 3)
        for p in superseded:
            self.assertEqual(p["suppressed"], "superseded-by-stop-count-collapse")

    def test_replay_never_sends_a_real_push(self):
        for p in self.watch.push_log:
            self.assertIn(p["sent"], ("dry-run", False))

    def test_page_worthy_findings_carry_rider_ready_copy(self):
        for p in self.watch.push_log:
            self.assertLess(len(p["body"]), 120, p["body"])
            self.assertNotIn("!", p["body"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
