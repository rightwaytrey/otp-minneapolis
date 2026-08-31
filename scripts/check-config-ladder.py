#!/usr/bin/env python3
"""check-config-ladder.py — the Go Mode debug-log payload ceiling is FOUR caps
in THREE repos, and it only works if they stay strictly increasing.

    client MAX_FULL_PAYLOAD_CHARS   (otprr    lib/util/debug-log.js)
      < Flask DEBUG_LOG_MAX_LINE_CHARS (transitnav preferences_api.py)
        < client MAX_BODY_BYTES       (otprr    lib/util/debug-log.js)
          < nginx client_max_body_size on /api/debug-log
            (otp-minneapolis config/nginx/ AND deployment/nginx/otp-common.conf)

Why this script exists: raising one rung alone does not remove the loss, it
relocates it one hop. Raise only the client and the Flask cap swaps the
client's `__summary` stub for its own `__truncated_chars` stub; raise past
nginx and the body is 413'd, which fetch() treats as a resolved response, so
the client uploads the payload in full and then throws it away. Either way the
ride is unreplayable and NOTHING is red — the symptom is a stubbed payload
nobody notices for weeks. That is precisely what happened on 2026-08-27 and
again on 2026-08-28.

Every value is PARSED OUT OF THE REAL FILE. There is deliberately no second
copy of the numbers here; a check that restates them is just a fifth rung to
forget.

Exit: 0 ladder holds, 1 ladder broken, 75 SKIP (a repo could not be resolved —
loudly, because a check that quietly no-ops is worse than no check).
"""

import os
import re
import sys
from pathlib import Path

SKIP = 75

OTPMIN = Path(__file__).resolve().parent.parent
SIBLINGS = OTPMIN.parent
OTPRR = Path(os.environ.get("OTPRR_DIR", SIBLINGS / "otprr" / "otp-react-redux"))
TRANSITNAV = Path(os.environ.get("TRANSITNAV_DIR", SIBLINGS / "transitnav"))

DEBUG_LOG_JS = OTPRR / "lib" / "util" / "debug-log.js"
PREFS_API = TRANSITNAV / "preferences_api.py"
NGINX_CONFS = {
    "config/nginx (house)": OTPMIN / "config" / "nginx" / "otp-common.conf",
    "deployment/nginx (server)": OTPMIN / "deployment" / "nginx" / "otp-common.conf",
}

DEBUG_LOG_LOCATION = "/api/debug-log"


def die_skip(msg):
    print(f"SKIP: {msg}", file=sys.stderr)
    print(
        "SKIP: the payload ladder was NOT verified. Resolve the repo paths (or set "
        "OTPRR_DIR / TRANSITNAV_DIR) and re-run.",
        file=sys.stderr,
    )
    sys.exit(SKIP)


def read(path, label):
    if not path.is_file():
        die_skip(f"{label} not found at {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def scalar(text, name, path):
    """Read `name = <int>` from JS or Python source (assignment lines only)."""
    # Tolerate a trailing `// ...` or `# ...` comment on the assignment line.
    m = re.search(
        r"^(?:const\s+|let\s+|var\s+)?%s\s*=\s*([0-9_]+)\s*(?://|#|$)"
        % re.escape(name),
        text,
        re.M,
    )
    if not m:
        die_skip(f"could not find `{name} = <int>` in {path}")
    return int(m.group(1).replace("_", ""))


def nginx_size(raw):
    """nginx size units: bare bytes, k/K = KiB, m/M = MiB."""
    m = re.fullmatch(r"(\d+)([kKmMgG]?)", raw.strip())
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[
        m.group(2).lower()
    ]


def body_cap_for_location(text, location, path):
    """client_max_body_size inside `location <location> { ... }`, brace-matched."""
    m = re.search(r"location\s+%s\s*\{" % re.escape(location), text)
    if not m:
        die_skip(f"no `location {location}` block in {path}")
    depth, i = 1, m.end()
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    block = text[m.end() : i]
    caps = re.findall(r"client_max_body_size\s+([^;]+);", block)
    if len(caps) != 1:
        die_skip(
            f"expected exactly 1 client_max_body_size in `location {location}` of "
            f"{path}, found {len(caps)}"
        )
    size = nginx_size(caps[0])
    if size is None:
        die_skip(f"unparseable client_max_body_size `{caps[0]}` in {path}")
    return size


def main():
    js = read(DEBUG_LOG_JS, "otprr debug-log.js")
    py = read(PREFS_API, "transitnav preferences_api.py")

    rungs = [
        ("MAX_FULL_PAYLOAD_CHARS", scalar(js, "MAX_FULL_PAYLOAD_CHARS", DEBUG_LOG_JS),
         f"{DEBUG_LOG_JS}"),
        ("DEBUG_LOG_MAX_LINE_CHARS", scalar(py, "DEBUG_LOG_MAX_LINE_CHARS", PREFS_API),
         f"{PREFS_API}"),
        ("MAX_BODY_BYTES", scalar(js, "MAX_BODY_BYTES", DEBUG_LOG_JS),
         f"{DEBUG_LOG_JS}"),
    ]

    nginx_caps = {}
    for label, path in NGINX_CONFS.items():
        conf = read(path, f"otp-minneapolis {label}")
        nginx_caps[label] = body_cap_for_location(conf, DEBUG_LOG_LOCATION, path)

    failures = []

    # The two nginx copies are deliberately different files, but this cap is not
    # one of the things they are allowed to differ on.
    if len(set(nginx_caps.values())) != 1:
        failures.append(
            "the two nginx copies disagree on client_max_body_size for "
            f"{DEBUG_LOG_LOCATION}: "
            + ", ".join(f"{k} = {v:,}" for k, v in nginx_caps.items())
            + " — both are deployed to different hosts; they must match."
        )
    top = min(nginx_caps.values())
    rungs.append(("nginx client_max_body_size", top, " + ".join(NGINX_CONFS)))

    print("Go Mode debug-log payload ladder:")
    for name, value, src in rungs:
        print(f"  {value:>12,}  {name}")
        print(f"  {'':>12}  ({src})")

    for (lo_name, lo, _), (hi_name, hi, _) in zip(rungs, rungs[1:]):
        if lo >= hi:
            failures.append(
                f"{lo_name} ({lo:,}) must be strictly less than {hi_name} ({hi:,}) "
                "— as it stands the larger cap is unreachable and payloads are "
                "silently stubbed or 413'd at the lower one."
            )

    if failures:
        print("\nFAIL: the payload ladder is broken.", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nThe caps must move TOGETHER. See the comment blocks in "
            "debug-log.js, preferences_api.py and otp-common.conf.",
            file=sys.stderr,
        )
        return 1

    print("\nOK: all four rungs are strictly increasing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
