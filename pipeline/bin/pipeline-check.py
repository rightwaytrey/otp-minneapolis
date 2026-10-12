#!/usr/bin/env python3
"""The deterministic half of a pipeline thread's self-check (2026-10-10).

Run by pipeline-wait.sh once a minute. Prints one `CHECK <key> :: <what>` line
per condition that needs the thread, and nothing at all otherwise. Each key is
printed ONCE (recorded in <dir>/checks.seen), so a thread that looked at a
condition and decided to wait is not woken again for the same thing.

  pipeline-check.py <N>       cycle N: the skill's self-check rules 1, 2 and 4
  pipeline-check.py board     the router: triage requested and never started

Rule 3 (a `running` step no live agent of this session owns) only arises after
a relaunch, and rule 5 (a lost RIDE line) is what the waiter's own RIDE line
covers; the thread runs the full self-check once at startup for both.
"""
import json
import os
import subprocess
import sys
import time

who = sys.argv[1].lstrip("c") if len(sys.argv) > 1 else sys.exit("usage: pipeline-check.py <N|board>")
base = os.path.expanduser("~/.claude/pipeline")
dirp = os.path.join(base, "board" if who == "board" else "c" + who)
seen_path = os.path.join(dirp, "checks.seen")
now = int(time.time() * 1000)
STALE_MS = 5 * 60 * 1000


def get(path):
    try:
        out = subprocess.run([os.path.expanduser("~/bin/pipeline-db"), "get", path],
                             capture_output=True, text=True, timeout=20).stdout
        return (json.loads(out) or {}).get("data") or {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None   # store down: say nothing, try again next minute


found = []   # (key, text)
if who == "board":
    b = get("pipeline/board")
    if b is None:
        sys.exit(0)
    i = b.get("intake") or {}
    if i.get("status") == "requested" and not i.get("startedAt") and now - (i.get("requestedAt") or now) > STALE_MS:
        found.append(("triage-requested:%s" % i.get("requestedAt"),
                      "intake.status is requested with no startedAt — run triage"))
else:
    d = get("cycles/c" + who)
    if d is None or not d:
        sys.exit(0)
    steps = d.get("steps") or {}
    if d.get("retiredAt"):
        found.append(("rule1:%s" % d["retiredAt"], "retiredAt is set — close the thread"))
    for name, s in steps.items():
        if s.get("status") == "requested" and not s.get("startedAt") and now - (s.get("requestedAt") or now) > STALE_MS:
            found.append(("rule2:%s:%s" % (name, s.get("requestedAt")),
                          "%s is requested with no startedAt — run it (self-check rule 2)" % name))
    build, ship = steps.get("build") or {}, steps.get("ship-dev") or {}
    if build.get("status") == "done" and ship.get("status", "idle") == "idle":
        found.append(("rule4:idle:%s" % build.get("finishedAt"), "build is done and ship-dev idle — run ship-dev (rule 4)"))
    if ship.get("status") == "done":
        for br in d.get("branches") or []:
            if (br.get("decision") == "approve" and br.get("shippable") is not False
                    and (br.get("decidedAt") or 0) > (ship.get("finishedAt") or 0)):
                found.append(("rule4:release:%s:%s" % (br.get("name"), br.get("decidedAt")),
                              "branch %s released after ship-dev — ship it if it is not on main (rule 4)" % br.get("name")))

try:
    seen = set(open(seen_path).read().split("\n"))
except OSError:
    seen = set()
new = [(k, t) for k, t in found if k not in seen]
if new:
    with open(seen_path, "a") as fh:
        for k, _ in new:
            fh.write(k + "\n")
    for k, t in new:
        print("CHECK %s :: %s" % (k, t))
