#!/usr/bin/env python3
"""List the pipeline's intake inputs — rider comments, ride reports, nightlies —
newer than a watermark, as JSON the board's `intake.inputs[]` entries are made
from. Reads only; the session that runs it merges the result into the board.

    scan.py [--since MS] [--days N]

Each entry: {id, kind: comment|report|nightly, at (ms), text, image, session,
source, path}. `became` is left for triage to fill (null = not looked at).
"""
import argparse, glob, json, os, re, time
from datetime import datetime

HOME = os.path.expanduser("~")
LOGS = f"{HOME}/otp-debug-logs"
REPORTS = f"{HOME}/obsidian-vault/Claude/ride-watch"
NIGHTLY = f"{HOME}/obsidian-vault/Claude/verify-nightly"

ap = argparse.ArgumentParser()
ap.add_argument("--since", type=int, default=0, help="only inputs newer than this epoch ms")
ap.add_argument("--days", type=int, default=14, help="how many day files to read")
a = ap.parse_args()
since = a.since
out = []

def clip(s, n=220):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= n else s[: n - 1] + "…"

# Rider comments: rider-note records in the day files (the mirror can lag ~30 min).
days = sorted(glob.glob(f"{LOGS}/debug-*.jsonl"))[-a.days:]
for f in days:
    with open(f, errors="replace") as fh:
        for line in fh:
            if '"rider-note"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("kind") != "rider-note":
                continue
            at = d.get("t") or d.get("time") or d.get("ts") or 0
            if not at and d.get("image"):
                m = re.search(r"-(\d{13})\.\w+$", d["image"])
                at = int(m.group(1)) if m else 0
            if at <= since:
                continue
            sid = d.get("session") or d.get("sessionId") or ""
            out.append({
                "id": f"c-{at}",
                "kind": "comment",
                "at": at,
                "text": clip(d.get("text") or d.get("note") or ""),
                "image": d.get("image") or None,
                "session": sid,
                "source": d.get("source") or "feedback",
                "path": f,
            })

# Ride reports: one file per ride, mtime = when it landed.
for f in glob.glob(f"{REPORTS}/20*.md"):
    at = int(os.path.getmtime(f) * 1000)
    if at <= since or at < (time.time() - a.days * 86400) * 1000:
        continue
    head = open(f, errors="replace").read(4000)
    m = re.search(r"session\s+`?([a-z0-9]+-[a-z0-9]+)`?", head)
    sid = m.group(1) if m else os.path.basename(f)[11:].split(".")[0]
    t = re.search(r"^# Ride report — (.+)$", head, re.M)
    title = t.group(1).strip() if t else os.path.basename(f)
    title = re.sub(r"\s*\(session [^)]*\)|,?\s*session\s+\S+", "", title).strip()
    fx = re.search(r"\*\*Findings:\*\*\s*(\d+)", head) or re.search(r"(\d+) findings", head)
    bugs = re.search(r"(\d+) real bug", head)
    text = f"Ride report {title}"
    if fx:
        text += f" · {fx.group(1)} findings"
    if bugs:
        text += f" · {bugs.group(1)} real bug{'s' if bugs.group(1) != '1' else ''}"
    out.append({"id": "r-" + os.path.basename(f)[:-3], "kind": "report", "at": at, "text": clip(text), "image": None, "session": sid, "source": "ride-watch", "path": f})

# Nightly: the newest report only.
nl = sorted(glob.glob(f"{NIGHTLY}/20*.md"))
if nl:
    f = nl[-1]
    at = int(os.path.getmtime(f) * 1000)
    if at > since:
        head = open(f, errors="replace").read(2000)
        m = re.search(r"\*\*(\d+) passed, (\d+) failed, (\d+) skipped", head)
        text = f"Nightly {os.path.basename(f)[:-3]}" + (f" · {m.group(1)} pass / {m.group(2)} fail / {m.group(3)} skip" if m else "")
        out.append({"id": f"n-{os.path.basename(f)[:-3]}", "kind": "nightly", "at": at, "text": text, "image": None, "session": "", "source": "nightly", "path": f})

out.sort(key=lambda e: e["at"])
print(json.dumps(out, ensure_ascii=False, indent=1))
