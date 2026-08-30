#!/usr/bin/env python3
"""
geocoder-parity.py — gate the Pelias -> Stadia swap on measured parity.

Verification step 1 of the Linode migration plan. The 4 GB box is only viable
if Stadia's hosted Pelias resolves house numbers as well as the local Pelias
index does, because the local index is OSM *plus* OpenAddresses parcel points
and that second layer is exactly what public Photon lacks (measured: 9/25
exact, with misses up to 4.8 km).

Samples real addresses out of the OpenAddresses source file rather than a
hand-picked list, so the result is a rate and not an anecdote.

Usage:
    STADIA_API_KEY=... python3 scripts/geocoder-parity.py [--n 25] [--seed 7]

Exit status is 0 only if Stadia's exact-match rate is >= Pelias's. Anything
else means stop and fall back to the 8 GB box with local Pelias.
"""
import argparse
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request

OA_SRC = "/mnt/tradingbot_data/pelias/openaddresses/us/mn/statewide.geojson"
PELIAS = "http://127.0.0.1:4000/v1"
STADIA = "https://api.stadiamaps.com/geocoding/v1"
# Twin Cities metro, matching the frontend's focus point neighbourhood.
LAT0, LAT1, LON0, LON1 = 44.75, 45.15, -93.55, -92.95
FOCUS_LAT, FOCUS_LON = 44.98, -93.27
MATCH_RADIUS_M = 200


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def sample_addresses(n, seed):
    """Reservoir-sample n metro address points from the OpenAddresses feed."""
    random.seed(seed)
    picks, seen = [], 0
    with open(OA_SRC) as fh:
        for line in fh:
            if '"MN"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            lon, lat = rec["geometry"]["coordinates"]
            if not (LAT0 <= lat <= LAT1 and LON0 <= lon <= LON1):
                continue
            p = rec["properties"]
            if not (p.get("number") and p.get("street") and p.get("city")):
                continue
            seen += 1
            if len(picks) < n:
                picks.append((p, lat, lon))
            else:
                j = random.randrange(seen)
                if j < n:
                    picks[j] = (p, lat, lon)
    return picks, seen


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "transitnav-parity"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def query(base, endpoint, params, api_key=None):
    if api_key:
        params = dict(params, api_key=api_key)
    return fetch(f"{base}/{endpoint}?" + urllib.parse.urlencode(params))


def top_feature(doc):
    feats = doc.get("features") or []
    return feats[0] if feats else None


def score(feat, want_number, lat, lon):
    """-> ('exact'|'wrong-number'|'street-only'|'miss', detail)"""
    if not feat:
        return "miss", "no results"
    props = feat["properties"]
    glon, glat = feat["geometry"]["coordinates"]
    dist = haversine(lat, lon, glat, glon)
    num = props.get("housenumber") or props.get("house_number")
    label = props.get("label") or props.get("name") or "?"
    if num == want_number and dist <= MATCH_RADIUS_M:
        return "exact", f"{label} ({dist:.0f}m)"
    if num:
        return "wrong-number", f"{label} ({dist:.0f}m)"
    return "street-only", f"{label} ({dist:.0f}m)"


def shape_check(doc, source):
    """The frontend renders properties.label, groups on .layer, uses .name."""
    feat = top_feature(doc)
    if not feat:
        return [f"{source}: no features to shape-check"]
    props = feat["properties"]
    missing = [f for f in ("label", "name", "layer") if not props.get(f)]
    if missing:
        return [f"{source}: MISSING properties.{f}" for f in missing]
    return [f"{source}: label/name/layer all present (layer={props['layer']!r})"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--delay", type=float, default=0.35, help="seconds between Stadia calls")
    args = ap.parse_args()

    key = os.environ.get("STADIA_API_KEY")
    if not key:
        sys.exit("STADIA_API_KEY is not set. Create a free key at https://client.stadiamaps.com/")

    if not os.path.exists(OA_SRC):
        sys.exit(f"OpenAddresses source not found: {OA_SRC}")

    picks, total = sample_addresses(args.n, args.seed)
    print(f"Sampled {len(picks)} of {total:,} metro address points (seed={args.seed})\n")

    tally = {"pelias": {}, "stadia": {}}
    for props, lat, lon in picks:
        text = f"{props['number']} {props['street'].title()}, {props['city']}, MN"
        row = {}
        for name, base, api_key in (
            ("pelias", PELIAS, None),
            ("stadia", STADIA, key),
        ):
            try:
                doc = query(base, "search", {"text": text, "size": 1}, api_key)
                verdict, detail = score(top_feature(doc), props["number"], lat, lon)
            except Exception as exc:                     # noqa: BLE001
                verdict, detail = "error", str(exc)
            row[name] = (verdict, detail)
            tally[name][verdict] = tally[name].get(verdict, 0) + 1
            if api_key:
                time.sleep(args.delay)

        flag = " " if row["pelias"][0] == row["stadia"][0] else "*"
        print(f"{flag} {text}")
        for name in ("pelias", "stadia"):
            v, d = row[name]
            print(f"    {name:7s} {v:13s} {d}")

    print("\n--- exact-match rate " + "-" * 40)
    for name in ("pelias", "stadia"):
        counts = tally[name]
        exact = counts.get("exact", 0)
        print(f"  {name:7s} exact {exact}/{len(picks)}   " +
              "  ".join(f"{k}={v}" for k, v in sorted(counts.items()) if k != "exact"))

    print("\n--- response shape (what the frontend renders) " + "-" * 14)
    notes = []
    try:
        notes += shape_check(query(PELIAS, "autocomplete",
                                   {"text": "target field",
                                    "focus.point.lat": FOCUS_LAT,
                                    "focus.point.lon": FOCUS_LON,
                                    "layers": "address,venue", "size": 3}), "pelias")
    except Exception as exc:                              # noqa: BLE001
        notes.append(f"pelias autocomplete FAILED: {exc}")
    try:
        notes += shape_check(query(STADIA, "autocomplete",
                                   {"text": "target field",
                                    "focus.point.lat": FOCUS_LAT,
                                    "focus.point.lon": FOCUS_LON,
                                    "layers": "address,venue", "size": 3}, key), "stadia")
    except Exception as exc:                              # noqa: BLE001
        notes.append(f"stadia autocomplete FAILED: {exc}")
    try:
        query(STADIA, "reverse", {"point.lat": FOCUS_LAT, "point.lon": FOCUS_LON, "size": 1}, key)
        notes.append("stadia: /reverse OK")
    except Exception as exc:                              # noqa: BLE001
        notes.append(f"stadia /reverse FAILED: {exc}")
    for n in notes:
        print("  " + n)

    p_exact = tally["pelias"].get("exact", 0)
    s_exact = tally["stadia"].get("exact", 0)
    print()
    if s_exact >= p_exact and not any("FAILED" in n or "MISSING" in n for n in notes):
        print(f"PASS — Stadia {s_exact} >= Pelias {p_exact}. The 4 GB plan holds.")
        return 0
    print(f"FAIL — Stadia {s_exact} vs Pelias {p_exact}. Fall back to the 8 GB box "
          "with local Pelias (plan: 'Later, if the third-party dependency ever bites').")
    return 1


if __name__ == "__main__":
    sys.exit(main())
