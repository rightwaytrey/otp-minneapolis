#!/usr/bin/env python3
"""
stopcount-sweep.py — measure what routingDefaults.accessEgress.maxStopCount
actually buys, across the service area.

Why: bike-as-access-to-transit is the dominant cost in a trip plan here.
Measured on the live graph, WALK+TRANSIT is ~361ms and adding BICYCLE takes it
to ~4356ms — 12x. The two knobs are the bike radius (maxDurationForMode BIKE,
currently 120m) and how many candidate boarding stops are weighed
(maxStopCount, currently 20000).

The radius is load-bearing: exurban origins genuinely use it (Ham Lake needs
72-78 min of bike access to reach transit at all). maxStopCount is the one
that may be oversized. This sweeps it against a spread of origin/destination
pairs and reports, per pair, whether a lower cap LOSES itineraries or makes the
best trip SLOWER — which is the only thing that matters. Speed that costs a
rider a better connection is not a win.

Run one config per invocation against a throwaway container; the live service
is never touched.

    ./stopcount-sweep.py --stop-count 20000 --port 8098 --out /tmp/base.json
    ./stopcount-sweep.py --stop-count 500   --port 8098 --out /tmp/500.json
    ./stopcount-sweep.py --compare /tmp/base.json /tmp/500.json
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = "/home/rwt/projects/otp-minneapolis"
CONTAINER = "otp-stopcount-probe"
TRANSIT = {"BUS", "RAIL", "TRAM", "SUBWAY", "FERRY", "GONDOLA", "FUNICULAR",
           "CABLE_CAR", "TROLLEYBUS", "MONORAIL", "COACH"}

# Spread deliberately across the service area: urban core, inner ring, outer
# suburbs, exurbs, and cross-town trips that do not touch downtown. The exurban
# pairs are the ones a smaller cap is most likely to break.
PAIRS = [
    ("downtown Mpls -> MOA",        44.9778, -93.2650, 44.8548, -93.2422),
    ("downtown Mpls -> downtown SP", 44.9778, -93.2650, 44.9537, -93.0900),
    ("U of M -> MSP airport",       44.9740, -93.2277, 44.8820, -93.2079),
    ("Eden Prairie -> downtown",    44.8547, -93.4708, 44.9778, -93.2650),
    ("Maple Grove -> U of M",       45.0725, -93.4557, 44.9740, -93.2277),
    ("Shakopee -> downtown SP",     44.7980, -93.5269, 44.9537, -93.0900),
    ("Ham Lake -> downtown",        45.2530, -93.2497, 44.9778, -93.2650),
    ("Woodbury -> downtown Mpls",   44.9239, -92.9594, 44.9778, -93.2650),
    ("Bloomington -> Roseville",    44.8408, -93.2983, 45.0061, -93.1567),
    ("Edina -> Northeast Mpls",     44.8897, -93.3499, 45.0000, -93.2500),
    ("Brooklyn Park -> Richfield",  45.0941, -93.3563, 44.8833, -93.2830),
    ("Coon Rapids -> Bloomington",  45.1732, -93.3030, 44.8408, -93.2983),
]

QUERY = """{plan(from:{lat:%f,lon:%f},to:{lat:%f,lon:%f},
 date:"%s",time:"08:30",numItineraries:5,
 transportModes:[{mode:WALK},{mode:TRANSIT},{mode:BICYCLE}])
 {itineraries{duration legs{mode duration route{shortName}}}}}"""


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def start_probe(stop_count, port, date):
    cfg = f"/tmp/rc-stopcount-{stop_count}.json"
    with open(f"{REPO}/config/router-config.json") as fh:
        rc = json.load(fh)
    # Only this one field moves. The bike radius stays at whatever the live
    # config says, because that is the knob we established must not shrink.
    rc["routingDefaults"]["accessEgress"]["maxStopCount"] = stop_count
    with open(cfg, "w") as fh:
        json.dump(rc, fh, indent=2)

    sh(f"sudo -n docker rm -f {CONTAINER}")
    r = sh(f"sudo -n docker run -d --name {CONTAINER} --memory=3g --cpus=2 "
           f"-p 127.0.0.1:{port}:8080 "
           f"-v {REPO}/data:/var/opentripplanner:ro "
           f"-v {cfg}:/var/opentripplanner/router-config.json:ro "
           f"-e JAVA_OPTS=-Xmx2G docker-otp")
    if r.returncode:
        sys.exit(f"could not start probe: {r.stderr.strip()}")
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/otp/", timeout=5)
            return cfg
        except Exception:                                   # noqa: BLE001
            time.sleep(5)
    sys.exit("probe never became ready")


def plan(port, pair, date):
    _, flat, flon, tlat, tlon = pair
    body = json.dumps({"query": QUERY % (flat, flon, tlat, tlon, date)}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/otp/gtfs/v1", data=body,
                                 headers={"content-type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=90) as r:
        doc = json.load(r)
    ms = int((time.time() - t0) * 1000)
    its = (doc.get("data") or {}).get("plan", {}).get("itineraries") or []
    multimodal = []
    for it in its:
        modes = [l["mode"] for l in it["legs"]]
        if not any(m in TRANSIT for m in modes):
            continue
        routes = sorted(l["route"]["shortName"] for l in it["legs"]
                        if l.get("route") and l["route"].get("shortName"))
        bike = sum(l["duration"] for l in it["legs"] if l["mode"] == "BICYCLE")
        multimodal.append({"duration": it["duration"], "routes": routes, "bike": bike})
    multimodal.sort(key=lambda x: x["duration"])
    return {
        "ms": ms,
        "n_transit": len(multimodal),
        "best": multimodal[0] if multimodal else None,
        "route_sets": [",".join(m["routes"]) for m in multimodal],
    }


def run(stop_count, port, date, out):
    cfg = start_probe(stop_count, port, date)
    results = {}
    for pair in PAIRS:
        try:
            results[pair[0]] = plan(port, pair, date)
        except Exception as exc:                            # noqa: BLE001
            results[pair[0]] = {"error": str(exc)}
        print(f"  {pair[0]:30s} {results[pair[0]].get('ms','--'):>6}ms  "
              f"{results[pair[0]].get('n_transit','?')} transit itin")
    sh(f"sudo -n docker rm -f {CONTAINER}")
    sh(f"rm -f {cfg}")
    payload = {"maxStopCount": stop_count, "date": date, "results": results}
    with open(out, "w") as fh:
        json.dump(payload, fh, indent=2)
    times = [r["ms"] for r in results.values() if "ms" in r]
    print(f"\n  maxStopCount={stop_count}: median {sorted(times)[len(times)//2]}ms "
          f"over {len(times)} pairs")


def compare(base_path, cand_path):
    base = json.load(open(base_path))
    cand = json.load(open(cand_path))
    print(f"baseline maxStopCount={base['maxStopCount']}  vs  "
          f"candidate maxStopCount={cand['maxStopCount']}\n")
    regressions = 0
    for name in base["results"]:
        b, c = base["results"][name], cand["results"].get(name, {})
        if "error" in b or "error" in c:
            print(f"  {name:30s} ERROR"); continue
        speed = f"{b['ms']:>5}ms -> {c['ms']:>5}ms"
        if b["best"] is None:
            print(f"  {name:30s} {speed}  (no transit itinerary either way)")
            continue
        if c["best"] is None:
            print(f"  {name:30s} {speed}  ** LOST ALL TRANSIT ITINERARIES **")
            regressions += 1
            continue
        d_best = c["best"]["duration"] - b["best"]["duration"]
        lost = b["n_transit"] - c["n_transit"]
        flag = ""
        if d_best > 60:
            flag = f"  ** BEST TRIP {d_best//60}min SLOWER **"; regressions += 1
        elif lost > 0:
            flag = f"  (lost {lost} alternative(s), best trip unchanged)"
        print(f"  {name:30s} {speed}  best {b['best']['duration']//60}min -> "
              f"{c['best']['duration']//60}min{flag}")
    print()
    if regressions:
        print(f"VERDICT: {regressions} regression(s). Do not adopt this value.")
        return 1
    print("VERDICT: no pair lost its best trip. Safe on this sample "
          f"({len(base['results'])} pairs) — not a proof for the whole service area.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-count", type=int)
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--date", default="2026-08-26")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("BASE", "CANDIDATE"))
    a = ap.parse_args()
    if a.compare:
        return compare(*a.compare)
    if a.stop_count is None or not a.out:
        ap.error("--stop-count and --out are required unless --compare is used")
    run(a.stop_count, a.port, a.date, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
