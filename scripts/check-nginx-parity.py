#!/usr/bin/env python3
"""check-nginx-parity.py — the two nginx configs must not drift where it counts.

There are two copies of otp-common.conf and they are DELIBERATELY different:

    config/nginx/otp-common.conf      the house/desktop config (local Pelias on
                                      :4000, a Photon fallback, /delivery/api)
    deployment/nginx/otp-common.conf  what deploy-app.sh installs on the Linode
                                      (Stadia geocoding, no local Pelias, ride
                                      endpoints proxied back over Tailscale,
                                      __PLACEHOLDER__ tokens)

So a whole-file diff is useless — it is all signal-free noise, which is exactly
why nobody runs one, and why the 2026-08-27 CORS/429 fix was nearly regressed
when the server variant did not receive it. The OTA lane then sat undeployed
for three days with every health check green.

What this checks instead: for every location the two files SHARE, they must
agree on the things a request actually feels —

    client_max_body_size    (a too-small cap 413s the real traffic)
    limit_req               (zone + burst + nodelay: the rate limit)
    auth_basic              (whether the route is public at all)

and any location present in only one file must be named in EXPECTED_ONLY_IN
below, with a reason. An unexplained one-sided location is a fail: that is the
shape every drift so far has had.

Exit: 0 in parity, 1 drifted, 75 SKIP (a config could not be read — loudly).
"""

import re
import sys
from pathlib import Path

SKIP = 75
OTPMIN = Path(__file__).resolve().parent.parent

HOUSE = ("config/nginx (house)", OTPMIN / "config" / "nginx" / "otp-common.conf")
SERVER = ("deployment/nginx (server)", OTPMIN / "deployment" / "nginx" / "otp-common.conf")

# Locations that legitimately exist in only one copy. Add here WITH A REASON, or
# the check fails — that is the point.
EXPECTED_ONLY_IN = {
    "/photon/api": (
        HOUSE[0],
        "Photon geocoder fallback; the server geocodes via Stadia and never "
        "proxies komoot.io.",
    ),
    "/pelias/": (
        HOUSE[0],
        "prefix-match proxy to the desktop's LOCAL Pelias on :4000. The server "
        "has no local Pelias (Elasticsearch alone did not fit a 4 GB Linode) and "
        "replaces this with the regex form below.",
    ),
    "~ ^/pelias/(?<geo_path>.*)$": (
        SERVER[0],
        "SERVER VARIANT of /pelias/ — a regex capture is needed to rewrite the "
        "path onto Stadia while keeping the /pelias/v1/* contract "
        "(docs/API-COMPAT.md rules 1-3).",
    ),
    "/delivery/api": (
        HOUSE[0],
        "desktop-only delivery API; not part of the transit deployment.",
    ),
}

# Directives compared on shared locations, as (label, regex over the block).
COMPARED = [
    ("client_max_body_size", r"client_max_body_size\s+([^;]+);"),
    ("limit_req", r"limit_req\s+([^;]+);"),
    ("auth_basic", r"auth_basic\s+([^;]+);"),
]


def die_skip(msg):
    print(f"SKIP: {msg}", file=sys.stderr)
    print("SKIP: nginx parity was NOT verified.", file=sys.stderr)
    sys.exit(SKIP)


def strip_comments(text):
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


def parse_locations(path):
    """{location spec -> block body}, brace-matched, comments stripped."""
    if not path.is_file():
        die_skip(f"config not found at {path}")
    text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    out = {}
    for m in re.finditer(r"location\s+([^{]+?)\s*\{", text):
        spec = re.sub(r"\s+", " ", m.group(1).strip())
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        # A nested location (none today) would appear again on its own; keying by
        # spec means a duplicate spec is itself worth knowing about.
        if spec in out:
            die_skip(f"duplicate `location {spec}` in {path}")
        out[spec] = text[m.end() : i]
    if not out:
        die_skip(f"no location blocks parsed from {path} — parser or file changed")
    return out


def values(block, pattern):
    return [re.sub(r"\s+", " ", v.strip()) for v in re.findall(pattern, block)]


def main():
    (h_label, h_path), (s_label, s_path) = HOUSE, SERVER
    house = parse_locations(h_path)
    server = parse_locations(s_path)

    failures = []

    shared = sorted(set(house) & set(server))
    for spec in shared:
        for label, pattern in COMPARED:
            hv, sv = values(house[spec], pattern), values(server[spec], pattern)
            if hv != sv:
                failures.append(
                    f"location {spec}: {label} differs — "
                    f"{h_label} has {hv or '(none)'}, {s_label} has {sv or '(none)'}"
                )

    for spec in sorted(set(house) ^ set(server)):
        present = h_label if spec in house else s_label
        expected = EXPECTED_ONLY_IN.get(spec)
        if expected is None:
            failures.append(
                f"location {spec}: present only in {present}, and not listed in "
                "EXPECTED_ONLY_IN. Either port it to the other copy or add it "
                "there with a reason."
            )
        elif expected[0] != present:
            failures.append(
                f"location {spec}: EXPECTED_ONLY_IN says {expected[0]}, but it is "
                f"actually only in {present}."
            )

    # A stale exception is drift too: it hides a location that has since been
    # ported, and the next reader trusts it.
    for spec, (where, _) in EXPECTED_ONLY_IN.items():
        if spec in house and spec in server:
            failures.append(
                f"location {spec}: listed in EXPECTED_ONLY_IN as {where}-only, but "
                "it now exists in BOTH copies. Remove the exception."
            )
        elif spec not in house and spec not in server:
            failures.append(
                f"location {spec}: listed in EXPECTED_ONLY_IN but exists in NEITHER "
                "copy. Remove the exception."
            )

    print(f"{h_label}: {len(house)} locations")
    print(f"{s_label}: {len(server)} locations")
    print(f"shared: {len(shared)}; one-sided (all expected): {len(set(house) ^ set(server))}")

    if failures:
        print("\nFAIL: the two nginx copies have drifted.", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            f"\nBoth files are real: {h_path} serves the house, {s_path} is what "
            "deploy-app.sh installs on the Linode. A fix applied to one is not "
            "deployed until it is in the other.",
            file=sys.stderr,
        )
        return 1

    print("\nOK: shared locations agree on body cap, rate limit and auth posture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
