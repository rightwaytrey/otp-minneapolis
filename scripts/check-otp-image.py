#!/usr/bin/env python3
"""check-otp-image.py — is the OTP container running the image `docker-otp` names?

`docker ps` cannot answer this. It prints `Up 10 hours (healthy)` for a container
serving a JAR from three weeks ago exactly as happily as for one created a minute
ago from the image that was just built, and every health probe agrees with it:
OTP answers `/otp/`, the graph is queryable, the realtime updaters log. On
2026-09-02 that state stood for hours after a deploy whose `--only jar,otp,graph`
had put a new shaded JAR and a rewritten router-config.json on the box: compose
declined to recreate the container, so the new itinerary-filter key sat on disk
and was never read. Nothing anywhere was red. The only check that caught it was
`check-transit-not-hidden.py`, which asks a routing question whose answer happens
to depend on the JAR. (backlog 2.27)

deploy-app.sh now forces the recreate and asserts the match — but only on the
runs that deploy. `scripts/cron-gtfs-refresh.sh:165,169` runs `docker restart
$CONTAINER` on a schedule, which restarts the SAME container from the SAME image
and refreshes `Up …` to something reassuringly recent. So between deploys a box
can drift, look freshly started, and pass everything. This asks the question
directly, nightly, on both boxes. (backlog 2.28)

WHAT IT COMPARES
    docker image inspect docker-otp --format '{{.Id}}'
    docker inspect       --format '{{.Image}}' otp-minneapolis

Not the compose labels. On the Linode (Docker 29.7.2, containerd image store,
Compose v5.5.0) the container's `com.docker.compose.image` label and its `.Image`
are two DIFFERENT digests of the same image — measured 2026-09-02, label
2e4eb184… against image 1449ca73… on a container compose had just created from
that image. A check written against the label would fail every night on a
perfectly fresh box.

Verified against the live boxes before it was trusted (2026-09-02): on prod the
container created at 22:27:45Z from the image built at 22:27:21Z reports the two
ids EQUAL, so the plain comparison is the right one and needs no fallback there;
on the house (Docker 28.3.2, overlay2) likewise. Two fallbacks are kept anyway,
because a digest-shape disagreement on a healthy fresh recreate is a mismatch of
spelling and not of content, and crying wolf is how a nightly check gets ignored:
if the container's image id is one of the tag's own RepoDigests, or if the two
images have identical `RootFS.Layers`, that is the same image and the run passes
with the reason printed.

WHICH BOX DID IT GRADE?
    Named, never inferred, like every other check here — see
    check-debug-log-payload.py's docstring for why that rule exists. `--target
    prod` reads the Linode over the read-only tailnet ssh; `--target house` reads
    this machine's docker with no ssh at all (rwtpc4's sshd rejects a loopback
    connection). The address appears in the OK line.

It reads and it does not write: three `docker inspect` calls and a `docker
version`. It never builds, restarts, recreates or pulls anything.

Exit: 0 the container is the tagged image and running, 1 it is not, 75 SKIP
(could not ask -- the box is off, the tailnet is down, the daemon is not there).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SKIP = 75

# One entry per deployment, selected as a unit, the same shape
# check-debug-log-payload.py uses. `local` means this machine with no ssh.
TARGETS = {
    # The Linode. Reached over the tailnet; `rwt@` is the app user there.
    "prod": {"ssh": "rwt@100.126.171.72"},
    # rwtpc4 itself. Its sshd rejects a loopback connection, so read docker here.
    "house": {"ssh": "local"},
}
DEFAULT_TARGET = os.environ.get("LADDER_TARGET", "prod")
DEFAULT_SSH = os.environ.get("LADDER_SSH", "")

DEFAULT_IMAGE = "docker-otp"
DEFAULT_CONTAINER = "otp-minneapolis"

# Every value the verdict needs, in one round trip. `docker version` first: a
# daemon that cannot be reached is a SKIP (the box is off, the tailnet is down),
# while a daemon that answers and has no such image or container is a FAIL — that
# is the tag naming nothing, or OTP not running, and both are findings.
PROBE = r"""
set -u
IMAGE='@@IMAGE@@'
CONTAINER='@@CONTAINER@@'
if ! ver=$(docker version --format '{{.Server.Version}}' 2>&1); then
  echo "daemon_error=$(printf '%s' "$ver" | head -n1)"
  exit 0
fi
echo "server_version=$ver"
echo "image_id=$(docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null || echo none)"
echo "image_created=$(docker image inspect "$IMAGE" --format '{{.Created}}' 2>/dev/null || echo none)"
echo "image_repodigests=$(docker image inspect "$IMAGE" --format '{{json .RepoDigests}}' 2>/dev/null || echo none)"
echo "image_layers=$(docker image inspect "$IMAGE" --format '{{json .RootFS.Layers}}' 2>/dev/null || echo none)"
cimg=$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || echo none)
echo "container_image=$cimg"
echo "container_created=$(docker inspect --format '{{.Created}}' "$CONTAINER" 2>/dev/null || echo none)"
echo "container_started=$(docker inspect --format '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null || echo none)"
echo "container_status=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo none)"
echo "container_health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null || echo none)"
echo "compose_image_label=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.image"}}' "$CONTAINER" 2>/dev/null || echo none)"
if [ "$cimg" != none ]; then
  echo "container_image_layers=$(docker image inspect "$cimg" --format '{{json .RootFS.Layers}}' 2>/dev/null || echo none)"
else
  echo "container_image_layers=none"
fi
"""


def die_skip(msg):
    print(f"SKIP: {msg}", file=sys.stderr)
    print("SKIP: the running container's image was NOT compared.", file=sys.stderr)
    sys.exit(SKIP)


def run_probe(ssh, image, container, timeout):
    script = PROBE.replace("@@IMAGE@@", image).replace("@@CONTAINER@@", container)
    # `local` reads this machine, with no ssh — the same spelling
    # check-debug-log-payload.py and deploy-manifest.py accept.
    cmd = (["bash", "-s"] if ssh in ("local", "-")
           else ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=accept-new", ssh, "bash", "-s"])
    try:
        out = subprocess.run(cmd, input=script, text=True, capture_output=True,
                             timeout=timeout, check=True).stdout
    except FileNotFoundError as e:
        die_skip(f"{cmd[0]} not found on PATH ({e})")
    except subprocess.TimeoutExpired:
        die_skip(f"the probe on {ssh} timed out after {timeout}s")
    except subprocess.CalledProcessError as e:
        die_skip(f"could not run docker on {ssh}: {(e.stderr or '').strip()[:300]}")
    facts = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k:
            facts[k.strip()] = v.strip()
    return facts


_FRAC = re.compile(r"(\.\d{1,9})")


def parse_ts(s):
    """docker prints RFC3339 with nanoseconds; fromisoformat wants at most 6."""
    if not s or s in ("none", "0001-01-01T00:00:00Z"):
        return None
    t = s.replace("Z", "+00:00")
    m = _FRAC.search(t)
    if m and len(m.group(1)) > 7:
        t = t[:m.start(1)] + m.group(1)[:7] + t[m.end(1):]
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def ago(ts, now=None):
    if ts is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    d = now - ts
    secs = int(abs(d.total_seconds()))
    if secs < 90:
        span = f"{secs}s"
    elif secs < 5400:
        span = f"{secs // 60}m"
    elif secs < 172800:
        span = f"{secs // 3600}h"
    else:
        span = f"{secs // 86400}d"
    return f"{span} ago" if d >= timedelta(0) else f"in {span}"


def short(digest):
    return (digest or "none").removeprefix("sha256:")[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", choices=sorted(TARGETS), default=DEFAULT_TARGET,
                    help="which deployment to grade (default: %(default)s). Sets "
                         "--ssh; still overridable.")
    ap.add_argument("--ssh", default=None,
                    help="host whose docker to ask; `local` reads this machine "
                         "directly. Defaults from --target.")
    ap.add_argument("--image", default=DEFAULT_IMAGE,
                    help="image tag the container should be running (default: %(default)s)")
    ap.add_argument("--container", default=DEFAULT_CONTAINER,
                    help="container name to inspect (default: %(default)s)")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    if args.ssh is None:
        args.ssh = DEFAULT_SSH or TARGETS[args.target]["ssh"]
    where = "this machine" if args.ssh in ("local", "-") else args.ssh

    facts = run_probe(args.ssh, args.image, args.container, args.timeout)
    if not facts:
        die_skip(f"the probe on {where} printed nothing")
    if facts.get("daemon_error"):
        die_skip(f"the docker daemon on {where} did not answer: "
                 f"{facts['daemon_error'][:200]}")

    img = facts.get("image_id", "none")
    run = facts.get("container_image", "none")
    img_created = parse_ts(facts.get("image_created"))
    c_created = parse_ts(facts.get("container_created"))
    c_started = parse_ts(facts.get("container_started"))

    print(f"target     : {args.target}  (docker on {where}, "
          f"engine {facts.get('server_version', '?')})")
    print(f"image      : {args.image}  {short(img)}  built {facts.get('image_created')} "
          f"({ago(img_created)})")
    print(f"container  : {args.container}  {short(run)}  "
          f"{facts.get('container_status')}/{facts.get('container_health')}")
    print(f"  created  : {facts.get('container_created')} ({ago(c_created)})")
    print(f"  started  : {facts.get('container_started')} ({ago(c_started)})")

    # The restart-without-rebuild case, said out loud. cron-gtfs-refresh.sh
    # restarts this container on a schedule; a restart makes `Up …` recent and
    # changes the image not at all, which is the whole reason 2.27 could hide.
    if c_created and c_started and (c_started - c_created) > timedelta(minutes=2):
        print(f"  note     : restarted {int((c_started - c_created).total_seconds() // 60)} "
              "min after it was created — a restart (cron-gtfs-refresh.sh does one "
              "on a schedule) reuses the image, so a recent `Up` proves nothing "
              "about the JAR.")

    failures = []
    verdict = None
    if img == "none":
        failures.append(
            f"there is no image tagged `{args.image}` on {where}. Whatever the "
            "container is running, nothing on the box says what it should be.")
    if run == "none":
        failures.append(
            f"there is no container named `{args.container}` on {where} — OTP is "
            "not running there.")
    elif facts.get("container_status") not in ("running", None, "none", ""):
        # `docker inspect` answers happily for a stopped container, and its
        # .Image still equals the tag's, so the id comparison alone would print
        # OK over a dead OTP. A green line for a box serving nothing is the
        # same class of lie this check was written to end.
        failures.append(
            f"`{args.container}` on {where} is {facts['container_status']}, not "
            "running. Its image id is beside the point: nothing is serving.")

    if not failures:
        if img == run:
            verdict = "the container's image id is the tag's image id"
        else:
            # Same image, different digest spelling. Both fallbacks print why
            # they fired: a pass whose reason is invisible is a pass nobody
            # believes the next time it matters.
            try:
                repodigests = json.loads(facts.get("image_repodigests") or "null") or []
            except json.JSONDecodeError:
                repodigests = []
            digests = {d.rpartition("@")[2] for d in repodigests}
            tag_layers = facts.get("image_layers", "none")
            run_layers = facts.get("container_image_layers", "none")
            if run in digests:
                verdict = (f"the ids differ in spelling but {short(run)} is one of "
                           f"{args.image}'s own RepoDigests")
            elif tag_layers != "none" and tag_layers == run_layers:
                verdict = ("the ids differ in spelling but the two images have "
                           "identical RootFS.Layers — the same content under two "
                           "digests (containerd image store)")
            else:
                failures.append(
                    f"`{args.container}` is running image {short(run)}, and the "
                    f"`{args.image}` tag names {short(img)}. Different content: "
                    "not one of the tag's RepoDigests and a different layer stack. "
                    "The JAR and router-config.json on the box are not what OTP "
                    "loaded.")

    # Reported, never asserted on: on the Linode this label is a different digest
    # of the same image by construction (Compose v5.5 + containerd image store).
    label = facts.get("compose_image_label", "none")
    if label not in ("none", "", "<no value>") and label != run:
        print(f"  (compose label {short(label)} != container image {short(run)} — "
              "expected on the containerd image store, not compared)")

    if failures:
        print("\nFAIL: the running OTP container is not the image the tag names.",
              file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(f"\nFix: deployment/deploy-app.sh {args.target} --only otp  (or, on the "
              "box, `docker compose -f deployment/docker-compose.server.yml up -d "
              "--force-recreate otp`).", file=sys.stderr)
        return 1

    print(f"\nOK: {args.container} on {args.target} is running the image `{args.image}` "
          f"names — {verdict} ({short(img)}, built {facts.get('image_created')}; "
          f"container created {facts.get('container_created')}, started "
          f"{facts.get('container_started')}; read from {where}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
