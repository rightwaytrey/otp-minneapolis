#!/usr/bin/env python3
"""check-config-ladder.py — the Go Mode debug-log payload ceiling is FOUR caps
in THREE repos, and it only works if they stay strictly increasing.

    client MAX_FULL_PAYLOAD_CHARS   (otprr    lib/util/debug-log.js)
      < Flask DEBUG_LOG_MAX_LINE_CHARS (transitnav preferences_api.py)
        < client MAX_BODY_BYTES       (otprr    lib/util/debug-log.js)
          < nginx client_max_body_size on /api/debug-log
            (otp-minneapolis deployment/nginx/otp-common.conf.tmpl)

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

TWO MODES, AND THE SECOND IS THE ONE THAT MATTERS
    (no flag)   read the four rungs out of the SOURCE in the three repos.
    --deployed  read the two rungs that live on a HOST out of that host, and
                compare them against the client rungs the app ships with.

The repo mode is not enough on its own, and 2026-09-01 is the proof: it printed
"OK: all four rungs are strictly increasing" while production was serving
DEBUG_LOG_MAX_LINE_CHARS 393216 (repo: 1179648) and client_max_body_size 512k
(repo: 1536k). Both rungs had been raised in git and neither had been deployed.
Every health check was green and rides were still being stubbed.

Only two of the four rungs exist on a host: the Flask cap and the nginx cap.
The client rungs ship inside the app bundle on the rider's phone, so --deployed
takes them from the repo and says so — it is checking that what is ON THE BOX
can carry what the app will send it.

Exit: 0 ladder holds, 1 ladder broken, 75 SKIP (a repo or host could not be
resolved — loudly, because a check that quietly no-ops is worse than no check).
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP = 75

OTPMIN = Path(__file__).resolve().parent.parent
SIBLINGS = OTPMIN.parent
OTPRR = Path(os.environ.get("OTPRR_DIR", SIBLINGS / "otprr" / "otp-react-redux"))
TRANSITNAV = Path(os.environ.get("TRANSITNAV_DIR", SIBLINGS / "transitnav"))

DEBUG_LOG_JS = OTPRR / "lib" / "util" / "debug-log.js"
PREFS_API = TRANSITNAV / "preferences_api.py"
# One template, both hosts. There used to be two forked copies here and the
# check had to assert they agreed on this cap; they cannot disagree any more,
# because deployment/render-nginx.py renders both environments from these bytes
# and /api/debug-log is not inside any per-environment region.
NGINX_TMPL = OTPMIN / "deployment" / "nginx" / "otp-common.conf.tmpl"

DEBUG_LOG_LOCATION = "/api/debug-log"

# The Linode, over the tailnet. The public IP as root is deliberately blocked;
# this is the address every read-only inspection of production uses.
DEFAULT_SSH = os.environ.get("LADDER_SSH", "rwt@100.126.171.72")
LIVE_NGINX_SNIPPET = "/etc/nginx/snippets/otp-common.conf"
LIVE_PREFS_API = "~/projects/transitnav/preferences_api.py"
PREFS_UNIT = "prefs-api"


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


# --------------------------------------------------------------------------
# --deployed: read the rungs that exist on a host, out of that host


SECTION = "----8<----%s----"


def read_host(ssh):
    """One round trip: the live nginx snippet, the live preferences_api.py,
    and enough about the systemd unit to tell whether gunicorn is even running
    the file we just read.

    That last part is not paranoia. gunicorn has no --reload, so a
    preferences_api.py newer than the running service is code that exists on the
    box and is not being served — the exact shape of the 2026-09-01 finding
    where the deployed API sat 179 lines behind.
    """
    script = f"""
set -u
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
echo "{SECTION % 'nginx'}"
cat {LIVE_NGINX_SNIPPET} 2>/dev/null || echo "__UNREADABLE__"
echo "{SECTION % 'prefs'}"
cat {LIVE_PREFS_API} 2>/dev/null || echo "__UNREADABLE__"
echo "{SECTION % 'meta'}"
stat -c 'prefs_mtime=%Y' {LIVE_PREFS_API} 2>/dev/null || echo prefs_mtime=
started="$(systemctl --user show {PREFS_UNIT} -p ActiveEnterTimestamp --value 2>/dev/null || true)"
echo "unit_started=$started"
# Convert with `date -d`, not by parsing in Python. systemd prints the unit's
# start in the HOST's locale and timezone ("Fri 2026-08-28 22:03:51 CDT"), and
# the first version of this check assumed UTC, produced no timestamp on rwtpc4,
# and skipped the staleness test in silence -- a check that quietly no-ops,
# which is the exact failure mode this file exists to prevent.
echo "unit_started_epoch=$(date -d "$started" +%s 2>/dev/null || true)"
systemctl --user is-active {PREFS_UNIT} 2>/dev/null | sed 's/^/unit_state=/' || echo unit_state=
"""
    # `--ssh local` inspects THIS machine with no ssh at all. That is not a
    # convenience: rwtpc4 is a real environment with real secrets in its own
    # /etc/nginx (the unlock secret had to be rotated on both hosts), and its
    # sshd rejects a loopback connection, so "the house" would otherwise be
    # uncheckable.
    cmd = (["bash", "-s"] if ssh in ("local", "-")
           else ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=accept-new", ssh, "bash", "-s"])
    try:
        out = subprocess.run(
            cmd, input=script, text=True, capture_output=True, timeout=90, check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        die_skip(f"probing {ssh} failed: {(e.stderr or '').strip()[:400]}")
    except subprocess.TimeoutExpired:
        die_skip(f"probing {ssh} timed out")
    except FileNotFoundError:
        die_skip("ssh not found on PATH")

    parts = {}
    key = None
    for line in out.splitlines():
        if line.startswith("----8<----") and line.endswith("----"):
            key = line.strip("-").strip("8<").strip("-")
            parts[key] = []
        elif key is not None:
            parts[key].append(line)
    for k in ("nginx", "prefs", "meta"):
        if k not in parts:
            die_skip(f"{ssh} returned no `{k}` section — the remote probe did not run")
    text = {k: "\n".join(v) for k, v in parts.items()}
    meta = dict(
        line.split("=", 1) for line in text["meta"].splitlines() if "=" in line
    )
    for k in ("nginx", "prefs"):
        if "__UNREADABLE__" in text[k]:
            die_skip(
                f"could not read the live {k} file on {ssh}. Both are world-readable "
                "today; if that changed, this check has to be fixed rather than skipped."
            )
    return text["nginx"], text["prefs"], meta


def check_deployed(ssh):
    where = "this machine" if ssh in ("local", "-") else ssh
    js = read(DEBUG_LOG_JS, "otprr debug-log.js")
    nginx_text, prefs_text, meta = read_host(ssh)

    client_lo = scalar(js, "MAX_FULL_PAYLOAD_CHARS", DEBUG_LOG_JS)
    client_hi = scalar(js, "MAX_BODY_BYTES", DEBUG_LOG_JS)
    flask = scalar(prefs_text, "DEBUG_LOG_MAX_LINE_CHARS", f"{where}:{LIVE_PREFS_API}")
    nginx_cap = body_cap_for_location(
        nginx_text, DEBUG_LOG_LOCATION, f"{where}:{LIVE_NGINX_SNIPPET}"
    )

    rungs = [
        ("MAX_FULL_PAYLOAD_CHARS", client_lo, f"REPO {DEBUG_LOG_JS}"),
        ("DEBUG_LOG_MAX_LINE_CHARS", flask, f"LIVE {where}:{LIVE_PREFS_API}"),
        ("MAX_BODY_BYTES", client_hi, f"REPO {DEBUG_LOG_JS}"),
        ("nginx client_max_body_size", nginx_cap, f"LIVE {where}:{LIVE_NGINX_SNIPPET}"),
    ]

    print(f"Go Mode debug-log payload ladder, AS DEPLOYED on {where}:")
    for name, value, src in rungs:
        print(f"  {value:>12,}  {name}")
        print(f"  {'':>12}  ({src})")
    print(
        "\n  The two REPO rungs are the client's; they ship inside the app bundle "
        "on the\n  phone, not on this host. What is being checked is that the box "
        "can carry\n  what the app will send it."
    )

    failures = []
    for (lo_name, lo, _), (hi_name, hi, _) in zip(rungs, rungs[1:]):
        if lo >= hi:
            failures.append(
                f"{lo_name} ({lo:,}) must be strictly less than {hi_name} ({hi:,}). "
                "As deployed, payloads between those two sizes are silently stubbed "
                "or 413'd, and the ride cannot be replayed."
            )

    # Repo-vs-deployed drift on the two host rungs, reported even when the
    # deployed ladder happens to hold: a box a release behind is the thing this
    # mode exists to surface.
    repo_flask = scalar(read(PREFS_API, "transitnav preferences_api.py"),
                        "DEBUG_LOG_MAX_LINE_CHARS", PREFS_API)
    repo_nginx = body_cap_for_location(
        read(NGINX_TMPL, "otp-minneapolis nginx template"), DEBUG_LOG_LOCATION, NGINX_TMPL)
    if repo_flask != flask:
        failures.append(
            f"DEBUG_LOG_MAX_LINE_CHARS on the box is {flask:,} but the repo says "
            f"{repo_flask:,} — preferences_api.py has not been deployed."
        )
    if repo_nginx != nginx_cap:
        failures.append(
            f"nginx client_max_body_size on the box is {nginx_cap:,} but the repo "
            f"says {repo_nginx:,} — the nginx config has not been deployed."
        )

    # gunicorn has no --reload: a file newer than the unit is not being served.
    started = meta.get("unit_started", "")
    started_epoch = meta.get("unit_started_epoch", "")
    mtime = meta.get("prefs_mtime", "")
    state = meta.get("unit_state", "unknown")
    print(f"\n  prefs-api unit: {state}, started {started or 'unknown'}")
    if state != "active":
        failures.append(
            f"the prefs-api unit on {where} is `{state}`, not active — whatever "
            "DEBUG_LOG_MAX_LINE_CHARS says, nothing is serving /api/debug-log."
        )
    elif not (started_epoch.isdigit() and mtime.isdigit()):
        # Say so rather than passing quietly. The first version of this check
        # parsed the timestamp itself, got nothing on a non-UTC host, and
        # skipped in silence.
        print(
            "  WARNING: could not compare preferences_api.py's mtime to the unit "
            "start time, so 'is gunicorn running this code?' was NOT checked."
        )
    elif int(mtime) > int(started_epoch):
        failures.append(
            "preferences_api.py on the box is NEWER than the running prefs-api "
            f"service (file {int(mtime) - int(started_epoch)}s after the start). "
            "gunicorn has no --reload, so the value read above is on disk and is "
            "NOT what is serving requests. Fix: "
            + (f"systemctl --user restart {PREFS_UNIT}" if ssh in ("local", "-")
               else f"ssh {ssh} 'systemctl --user restart {PREFS_UNIT}'")
        )

    if failures:
        print("\nFAIL: the deployed payload ladder is broken.", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nThe caps must move TOGETHER, and they must be DEPLOYED together. "
            "nginx is installed only by deployment/deploy-app.sh --only nginx, "
            "which needs root on the target.",
            file=sys.stderr,
        )
        return 1

    print("\nOK: the box carries the full ladder, and matches the repo.")
    return 0


def check_repo():
    js = read(DEBUG_LOG_JS, "otprr debug-log.js")
    py = read(PREFS_API, "transitnav preferences_api.py")
    conf = read(NGINX_TMPL, "otp-minneapolis deployment/nginx/otp-common.conf.tmpl")

    rungs = [
        ("MAX_FULL_PAYLOAD_CHARS", scalar(js, "MAX_FULL_PAYLOAD_CHARS", DEBUG_LOG_JS),
         f"{DEBUG_LOG_JS}"),
        ("DEBUG_LOG_MAX_LINE_CHARS", scalar(py, "DEBUG_LOG_MAX_LINE_CHARS", PREFS_API),
         f"{PREFS_API}"),
        ("MAX_BODY_BYTES", scalar(js, "MAX_BODY_BYTES", DEBUG_LOG_JS),
         f"{DEBUG_LOG_JS}"),
        ("nginx client_max_body_size",
         body_cap_for_location(conf, DEBUG_LOG_LOCATION, NGINX_TMPL), f"{NGINX_TMPL}"),
    ]

    print("Go Mode debug-log payload ladder, IN THE REPO:")
    for name, value, src in rungs:
        print(f"  {value:>12,}  {name}")
        print(f"  {'':>12}  ({src})")

    failures = []
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
            "debug-log.js, preferences_api.py and otp-common.conf.tmpl.",
            file=sys.stderr,
        )
        return 1

    print(
        "\nOK: all four rungs are strictly increasing IN THE REPO. That is not a "
        "statement about production —\n    run with --deployed for that."
    )
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--deployed", action="store_true",
        help="read the two host rungs off a live host instead of out of the repo")
    ap.add_argument(
        "--ssh", default=DEFAULT_SSH,
        help=f"host to inspect with --deployed (default: {DEFAULT_SSH}); "
             "`local` inspects this machine directly, with no ssh")
    args = ap.parse_args()
    return check_deployed(args.ssh) if args.deployed else check_repo()


if __name__ == "__main__":
    sys.exit(main())
