#!/usr/bin/env python3
"""check-debug-log-payload.py — prove the record path can actually carry a ride.

check-config-ladder.py compares four numbers. This one sends a real payload the
size of the ones real rides produce and looks at what came out the far end,
because every failure this family has had was invisible to the numbers:

  - nginx 413s an oversized body. fetch() treats 413 as a RESOLVED response, so
    the client cheerfully splices the batch away. Nothing is red anywhere.
  - Flask stubs an oversized line into `__truncated_chars`. HTTP 200, `accepted`
    counts it, and the payload is gone.

So: POST one entry whose serialised line is ~900 KB — larger than the biggest
payload a real ride has yet produced (865,300 chars, the 2026-08-28 evening
ride) and inside the client's MAX_FULL_PAYLOAD_CHARS — and require

    1. HTTP 200 with accepted >= 1                (nginx did not 413 it), and
    2. the line on disk is the full payload       (Flask did not stub it).

(2) needs ssh, because there is no endpoint that reads the log back and there
should not be one. `--no-disk-check` drops it and keeps (1).

This writes ~900 KB into the day's debug log, tagged session `config-probe`, so
it is trivially greppable and prune-debug-logs.sh ages it out like everything
else. Do not run it in a loop.

Exit: 0 the payload survived, 1 it did not, 75 SKIP (could not run the probe).
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

SKIP = 75

DEFAULT_SSH = os.environ.get("LADDER_SSH", "rwt@100.126.171.72")
DEFAULT_HOST = "api.transit-nav.com"
DEFAULT_PORT = 9966
DEFAULT_RESOLVE = os.environ.get("LADDER_RESOLVE", "100.126.171.72")

# Bigger than the largest payload any real ride has produced, smaller than the
# client's own MAX_FULL_PAYLOAD_CHARS (1,000,000). If those move, move this.
PROBE_CHARS = 900_000


def die_skip(msg):
    print(f"SKIP: {msg}", file=sys.stderr)
    print("SKIP: the record path was NOT exercised.", file=sys.stderr)
    sys.exit(SKIP)


def build_body(probe_id, size):
    # A single entry, one long string. Random-ish but cheap: the point is the
    # size, and an incompressible-looking blob keeps any future gzip honest.
    filler = (uuid.uuid4().hex * ((size // 32) + 1))[:size]
    return {
        "sessionId": "config-probe",
        "deviceId": "config-probe",
        "entries": [
            {
                "id": probe_id,
                "kind": "config-probe",
                "type": "LADDER_PROBE",
                "t": int(time.time() * 1000),
                "payload": filler,
            }
        ],
    }


def post(args, body):
    url = f"https://{args.host}:{args.port}/api/debug-log"
    cmd = [
        "curl", "-sS", "--max-time", str(args.timeout),
        "-o", "-", "-w", "\n%{http_code}",
        "-X", "POST", "-H", "content-type: application/json",
        "--data-binary", "@-",
    ]
    if args.resolve:
        cmd += ["--resolve", f"{args.host}:{args.port}:{args.resolve}"]
    cmd.append(url)
    try:
        out = subprocess.run(
            cmd, input=json.dumps(body), text=True, capture_output=True,
            timeout=args.timeout + 30, check=True).stdout
    except FileNotFoundError:
        die_skip("curl not found on PATH")
    except subprocess.TimeoutExpired:
        die_skip(f"POST to {url} timed out after {args.timeout}s")
    except subprocess.CalledProcessError as e:
        die_skip(f"curl failed against {url}: {(e.stderr or '').strip()[:300]}")
    text, _, code = out.rpartition("\n")
    return code.strip(), text


def disk_check(ssh, probe_id):
    """Find the probe line in today's log and report its shape.

    Daily rollover is by UTC date (preferences_api._debug_log_path uses gmtime),
    and a probe sent seconds before midnight can land in tomorrow's file, so look
    at both.
    """
    script = f"""
set -u
for d in $(date -u +%Y-%m-%d) $(date -u -d '-1 day' +%Y-%m-%d 2>/dev/null); do
  f="$HOME/otp-debug-logs/debug-$d.jsonl"
  [ -f "$f" ] || continue
  grep -h -- '{probe_id}' "$f" 2>/dev/null | tail -n1
done
"""
    # `local` reads this machine, with no ssh -- rwtpc4 is a real environment
    # with its own live nginx, and its sshd rejects a loopback connection.
    cmd = (["bash", "-s"] if ssh in ("local", "-")
           else ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=accept-new", ssh, "bash", "-s"])
    try:
        out = subprocess.run(
            cmd, input=script, text=True, capture_output=True, timeout=120, check=True).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, f"could not read the log on {ssh}: {e}"
    line = out.strip().splitlines()[-1] if out.strip() else ""
    if not line:
        return None, (
            "the endpoint answered 200 but no line carrying the probe id reached "
            f"~/otp-debug-logs on {ssh}. The batch was accepted and dropped."
        )
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None, "the probe line on disk is not valid JSON"
    return rec, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--resolve", default=DEFAULT_RESOLVE,
                    help="IP to pin --host to (default: the Linode over the tailnet); "
                         "empty string uses normal DNS")
    ap.add_argument("--ssh", default=DEFAULT_SSH,
                    help="host to read the written line back from; `local` reads "
                         "this machine directly")
    ap.add_argument("--no-disk-check", action="store_true",
                    help="only assert the HTTP result; skip reading the line back")
    ap.add_argument("--chars", type=int, default=PROBE_CHARS,
                    help="payload size to send (default: %d). Lower it to find "
                         "where a broken ladder actually cuts off." % PROBE_CHARS)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    probe_id = "ladder-probe-" + uuid.uuid4().hex[:16]
    body = build_body(probe_id, args.chars)
    wire = len(json.dumps(body))
    print(f"probe id   : {probe_id}")
    print(f"body       : {wire:,} bytes to https://{args.host}:{args.port}/api/debug-log"
          + (f" (resolved to {args.resolve})" if args.resolve else ""))

    code, text = post(args, body)
    print(f"http       : {code}  {text.strip()[:200]}")

    failures = []
    if code != "200":
        detail = {
            "413": "nginx REFUSED the body: client_max_body_size on /api/debug-log "
                   "is below what the client sends. This is the top rung of the "
                   "ladder and the only hard one — fetch() resolves a 413, so the "
                   "app uploads the whole payload and then throws it away.",
            "429": "rate limited. The nginx otp_log zone is 2r/s; try again in a "
                   "moment rather than treating this as a ladder failure.",
            "401": "the route has been re-gated behind Basic Auth. The bundled app "
                   "runs at capacitor://localhost and can carry no credential, so "
                   "this breaks every tester's telemetry.",
            "404": "no /api/debug-log location in the live nginx snippet.",
        }.get(code, "unexpected status.")
        failures.append(f"POST returned {code}. {detail}")
    else:
        try:
            accepted = json.loads(text).get("accepted", 0)
        except json.JSONDecodeError:
            accepted = 0
            failures.append("200, but the response body is not the JSON this "
                            "endpoint returns — something else answered.")
        else:
            if accepted < 1:
                failures.append(
                    f"200 with accepted={accepted}: the batch was acknowledged and "
                    "no line was written.")

    if code == "200" and not args.no_disk_check and not failures:
        rec, err = disk_check(args.ssh, probe_id)
        if err:
            failures.append(err)
        else:
            if "__truncated_chars" in rec:
                failures.append(
                    f"the line was STUBBED on write: __truncated_chars="
                    f"{rec['__truncated_chars']:,}. DEBUG_LOG_MAX_LINE_CHARS on the "
                    "box is below the payload the client is allowed to send, so the "
                    "content is gone even though the upload succeeded.")
            else:
                got = len(rec.get("payload") or "")
                print(f"on disk    : payload {got:,} chars, not stubbed")
                if got != args.chars:
                    failures.append(
                        f"the payload on disk is {got:,} chars, expected "
                        f"{args.chars:,} — something truncated it silently.")

    if failures:
        print("\nFAIL: the record path cannot carry a real ride's payload.", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print("\nRun scripts/check-config-ladder.py --deployed to see which rung.",
              file=sys.stderr)
        return 1

    print(f"\nOK: a {args.chars:,}-char payload reached disk intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
