#!/usr/bin/env python3
"""Mirror the Linode's debug-log sink into the local one, line by line.

Why this exists (backlog 11.5, 2026-09-05): until the `transit-nav.com`
split-DNS route was removed, every phone on the tailnet posted telemetry
straight to this desktop, and the ride-watch daemon (which reads
~/otp-debug-logs/debug-<date>.jsonl here) saw every ride by accident. With
production genuinely on the Linode, the phone's records land in the Linode's
~/otp-debug-logs and the daemon would see nothing. This follows the Linode's
file for the current UTC-agnostic local date over ssh and appends new lines
to the local file of the same name, so the daemon, the nightly report and
`build-fixture.js` keep working unchanged.

Rules: append only, never rewrite; resume by byte offset per REMOTE file
(`.mirror-offset-<remote date>`), so a restart never duplicates; reconnect
forever on ssh failure. The Linode runs on UTC and rolls its day file at
19:00 CDT, so every remote file named for yesterday, today and tomorrow (this
box's local date) is followed, and whatever they yield is appended to the
LOCAL file for the CURRENT local date — the one ride-watch is tailing — never
to a file named after the Linode's clock (the first version did that and the
daemon would have missed everything posted after 19:00 CDT).
Records from the house lane and the dev app that post here directly are
untouched — their ids are per-session and cannot collide with the mirrored
ones.
"""
import datetime, os, pathlib, subprocess, sys, time

HOST = os.environ.get("MIRROR_HOST", "rwt@100.126.171.72")
REMOTE_DIR = os.environ.get("MIRROR_REMOTE_DIR", "otp-debug-logs")
LOCAL_DIR = pathlib.Path(os.environ.get("MIRROR_LOCAL_DIR", str(pathlib.Path.home() / "otp-debug-logs")))
POLL_S = float(os.environ.get("MIRROR_POLL_S", "5"))
# The in-app feedback screenshots (backlog 9.3) land on the Linode too; they are
# small and few, so an rsync once a minute keeps the daemon's report agent able
# to open them locally (backlog 11.5). --ignore-existing: an image is never
# rewritten, so this can never clobber one already mirrored.
FEEDBACK_EVERY_S = float(os.environ.get("MIRROR_FEEDBACK_EVERY_S", "60"))
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", HOST]


def today():
    return datetime.date.today().isoformat()


def offset_path(date):
    return LOCAL_DIR / f".mirror-offset-{date}"


def read_offset(date):
    try:
        return int(offset_path(date).read_text().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def fetch(date, offset):
    """Bytes of the remote day file from `offset`, or b'' if nothing new."""
    remote = f"{REMOTE_DIR}/debug-{date}.jsonl"
    cmd = SSH + [f"test -f {remote} && tail -c +{offset + 1} {remote} || true"]
    out = subprocess.run(cmd, capture_output=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode(errors="replace").strip()[:200])
    return out.stdout


def mirror_feedback():
    """rsync the remote feedback/ images into the local sink, if the dir exists."""
    local = LOCAL_DIR / "feedback"
    local.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--ignore-existing", "-e", " ".join(SSH[:-1]),
           f"{HOST}:{REMOTE_DIR}/feedback/", str(local) + "/"]
    res = subprocess.run(cmd, capture_output=True, timeout=120)
    if res.returncode == 0:
        return
    err = res.stderr.decode(errors="replace").strip()
    # A remote dir that does not exist yet is not an error worth a line per minute.
    if "No such file or directory" in err:
        return
    print(f"{time.strftime('%H:%M:%S')} feedback rsync rc={res.returncode}: {err[:200]}", flush=True)


def main():
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"mirror: {HOST}:{REMOTE_DIR} -> {LOCAL_DIR}", flush=True)
    last_feedback = 0.0
    while True:
        if time.monotonic() - last_feedback >= FEEDBACK_EVERY_S:
            last_feedback = time.monotonic()
            try:
                mirror_feedback()
            except Exception as exc:  # noqa: BLE001
                print(f"{time.strftime('%H:%M:%S')} feedback rsync error: {exc}", flush=True)
        local_date = today()
        base = datetime.date.fromisoformat(local_date)
        for delta in (-1, 0, 1):
            remote_date = (base + datetime.timedelta(days=delta)).isoformat()
            try:
                offset = read_offset(remote_date)
                chunk = fetch(remote_date, offset)
                if not chunk:
                    continue
                # Only commit whole lines; keep a partial tail for next time.
                cut = chunk.rfind(b"\n") + 1
                if not cut:
                    continue
                with open(LOCAL_DIR / f"debug-{local_date}.jsonl", "ab") as f:
                    f.write(chunk[:cut])
                offset_path(remote_date).write_text(str(offset + cut))
                added = chunk[:cut].count(b"\n")
                print(f"{time.strftime('%H:%M:%S')} +{added} lines (remote {remote_date} -> local {local_date})", flush=True)
            except Exception as exc:  # noqa: BLE001 — keep following no matter what
                print(f"{time.strftime('%H:%M:%S')} mirror error ({remote_date}): {exc}", flush=True)
                time.sleep(15)
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
