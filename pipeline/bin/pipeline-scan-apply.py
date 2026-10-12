#!/usr/bin/env python3
"""The skill's `scan` action, with no agent in the loop (2026-10-10).

Registers new intake inputs (rider comments, ride reports, the newest nightly)
in pipeline/board `intake.inputs[]` and stamps `intake.scannedAt`. It only
REGISTERS — `became: null` — triage still does the evaluating. Silent: no board
log, no reply. pipeline-wait.sh board runs it every 15 minutes; it used to be
the router session's Monitor-expiry chore, which woke an Opus session 96 times
a day to do this.

  pipeline-scan-apply.py            prints one line: what it added (or nothing)
"""
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DB = f"{HOME}/bin/pipeline-db"
SCAN = f"{HOME}/.claude/skills/transitnav-pipeline/scan.py"
CURRENT = f"{HOME}/otp-debug-logs/ride-watch/current-ride.md"
KEEP, AGE_MS = 80, 14 * 86400 * 1000
now = int(time.time() * 1000)


def run(*cmd, inp=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, input=inp)


got = json.loads(run(DB, "get", "pipeline/board").stdout)
board, version = got.get("data") or {}, got.get("version")
intake = board.get("intake") or {}
inputs = list(intake.get("inputs") or [])
since = max([e.get("at") or 0 for e in inputs] or [0])
cands = json.loads(run("python3", "-I", SCAN, "--since", str(since)).stdout or "[]")

# A ride in progress: its own notes wait for the ride to end (its report carries them).
live = ""
try:
    cur = open(CURRENT).read()
    if "No active trip" not in cur:
        m = re.search(r"session\s+`?([a-z0-9]+-[a-z0-9]+)", cur)
        live = m.group(1) if m else ""
except OSError:
    pass

journal = ""
if any(c.get("kind") == "report" for c in cands):
    journal = run("journalctl", "--user", "-u", "ride-watch", "--since", "-3 days", "--no-pager").stdout

have = {e.get("id") for e in inputs}
added = []
for c in cands:
    if c["id"] in have or (live and c.get("kind") == "comment" and c.get("session") == live):
        continue
    flag = ""
    if c.get("kind") == "report" and c.get("session"):
        m = re.findall(r"^(\w+ +\d+ [\d:]{5}).*no promotion for %s\b" % re.escape(c["session"]), journal, re.M)
        if m:
            flag = "report promoted nothing; the daemon paged you %s" % m[-1].split()[-1][:5]
    inputs.append(dict(c, seenAt=now, triagedAt=None, became=None, flag=flag))
    added.append(c["id"])

inputs = [e for e in inputs if not (e.get("became") and now - (e.get("at") or 0) > AGE_MS)]
inputs.sort(key=lambda e: e.get("at") or 0)
inputs = inputs[-KEEP:]

patch = {"intake": {"inputs": inputs, "scannedAt": now}}
r = run(DB, "update", "pipeline/board", "--data", json.dumps(patch), "--if-version", str(version))
if r.returncode != 0:
    print("scan: store refused the write (version moved) — next tick retries", file=sys.stderr)
    sys.exit(1)
if added:
    print("scan: registered %d input(s): %s" % (len(added), ", ".join(added)))
