#!/usr/bin/env python3
"""check-transit-not-hidden.py — a short trip must still be offered a bus.

On 2026-09-02 a Current Location -> Southdale Center search (about 800 m) came
back as ONE 9-minute walk card under "Transit isn't the fastest way to make this
trip". Nine bus itineraries existed; OTP's filter chain deleted every one of
them and answered with the routing error alone. The rider's rule since:
speed is not the only metric a trip is measured by, so transit is shown
alongside the walk with the difference visible -- never suppressed by it.

Two OTP filters do the deleting, and BOTH have to be off for a bus to survive:

  transit-vs-street-filter  RemoveTransitIfStreetOnlyIsBetter, limited by
                            `removeTransitWithHigherCostThanBestOnStreetOnly`
                            (default `1m + 1.3x`). Configurable.
  transit-vs-walk-filter    RemoveTransitIfWalkingIsBetter, which deletes any
                            transit itinerary costing at least as much as the
                            walk-all-the-way one, with NO cost function to
                            relax. In stock OTP 2.9 it is wired on with a
                            literal `true` in RouteRequestToFilterChainMapper,
                            so relaxing the first knob alone is inert.

The fork adds `itineraryFilters.removeTransitIfWalkingIsBetter` so the second
one can be turned off, which means this check needs BOTH the config (deployed by
`deploy-app.sh <host> --only graph`) and the jar built from the patched fork
(`--only jar otp`). Ship one without the other and the search silently goes back
to a lone walk card with no error anywhere -- which is exactly why this asks the
running server a real question instead of reading a file.

Exit: 0 a transit itinerary came back, 1 none did, 75 SKIP (server unreachable).
"""

import argparse
import json
import subprocess
import sys

SKIP = 75

DEFAULT_HOST = "api.transit-nav.com"
DEFAULT_PORT = 9966

# Same shape as check-debug-log-payload.py: the box is named, never inferred.
# /etc/hosts on rwtpc4 points api.transit-nav.com at the Linode, so a probe sent
# by name from the house grades the Linode no matter which one you meant.
TARGETS = {
    "prod": "100.126.171.72",
    "house": "127.0.0.1",
}
DEFAULT_TARGET = "prod"

# The rider's 2026-09-02 16:37 pair. Origin is W 66th St & France Ave S, which
# is where the walk leg in their screenshot starts; destination is Southdale
# Center. ~800 m apart: a 10-minute walk, and a 13-minute bus.
ORIGIN = (44.8814, -93.3290)
DESTINATION = (44.8795, -93.3226)

QUERY = """
query P($from: InputCoordinates!, $to: InputCoordinates!) {
  plan(
    from: $from
    to: $to
    numItineraries: 10
    transportModes: [{mode: TRANSIT}, {mode: WALK}]
  ) {
    routingErrors { code }
    itineraries {
      duration
      generalizedCost
      legs { mode route { shortName } }
    }
  }
}
"""


def skip(message):
    print(f"SKIP: {message}")
    sys.exit(SKIP)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", choices=sorted(TARGETS), default=DEFAULT_TARGET,
                    help="which deployment to ask (default: %(default)s)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--resolve", default=None,
                    help="override the address --target pins the host to")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    address = args.resolve or TARGETS[args.target]
    url = f"https://{args.host}:{args.port}/otp/gtfs/v1"
    payload = json.dumps({
        "query": QUERY,
        "variables": {
            "from": {"lat": ORIGIN[0], "lon": ORIGIN[1]},
            "to": {"lat": DESTINATION[0], "lon": DESTINATION[1]},
        },
    })

    cmd = [
        "curl", "-s", "--max-time", str(args.timeout),
        "--resolve", f"{args.host}:{args.port}:{address}",
        "-H", "Content-Type: application/json",
        "-X", "POST", "--data-binary", payload, url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        skip(f"could not run curl: {exc}")
    if proc.returncode != 0 or not proc.stdout.strip():
        skip(f"{args.target} ({address}) did not answer: rc={proc.returncode} "
             f"{proc.stderr.strip()[:200]}")

    try:
        body = json.loads(proc.stdout)
    except ValueError:
        skip(f"{args.target} ({address}) answered with non-JSON: "
             f"{proc.stdout[:200]}")

    if body.get("errors"):
        skip(f"GraphQL refused the query: {json.dumps(body['errors'])[:300]}")

    plan = (body.get("data") or {}).get("plan") or {}
    itineraries = plan.get("itineraries") or []
    errors = [e.get("code") for e in plan.get("routingErrors") or []]

    def is_transit(itinerary):
        return any(leg.get("route") for leg in itinerary.get("legs") or [])

    transit = [i for i in itineraries if is_transit(i)]
    street = [i for i in itineraries if not is_transit(i)]

    if not transit:
        print(f"FAIL: {args.target} ({address}) returned {len(itineraries)} "
              f"itinerary/ies for the Southdale pair and not one of them is "
              f"transit. routingErrors={errors or 'none'}.")
        print("      The transit-vs-walk and transit-vs-street filters are "
              "still deleting them. Needs BOTH halves shipped: "
              "`deploy-app.sh <host> --only graph` for router-config.json and "
              "`--only jar otp` for the patched OTP jar.")
        return 1

    best_transit = min(i["duration"] for i in transit)
    line = (f"OK: {args.target} ({address}) offers {len(transit)} transit "
            f"option(s) for the Southdale pair, best {best_transit // 60} min")
    if street:
        best_street = min(i["duration"] for i in street)
        line += f" against a {best_street // 60} min street-only option"
    print(line + f". routingErrors={errors or 'none'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
