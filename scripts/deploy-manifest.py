#!/usr/bin/env python3
"""deploy-manifest.py — record, and later re-check, what is actually deployed.

WHY A MANIFEST AND NOT A GIT CHECKOUT ON THE BOX
The obvious alternative was to make the server paths real git checkouts and read
`git rev-parse` there. Measured on 2026-09-01, that cannot answer the question:

  - Nothing on the server is a checkout today. `git rev-parse` fails in both
    ~/projects/transitnav and ~/projects/otp-minneapolis on the Linode.
  - The nginx config that runs is RENDERED, not tracked. /etc/nginx/snippets/
    otp-common.conf holds substituted secrets; the tracked file holds
    `__PLACEHOLDER__` tokens. A checkout could never contain the running bytes,
    and committing them is precisely how the unlock secret reached a public repo.
  - What ships is a SUBSET, and sometimes a mutation. deploy-app.sh sends five
    files out of the transitnav repo, and rewrites routingDefaults.accessEgress.
    maxStopCount in router-config.json on the way (SERVER_MAX_STOP_COUNT) so the
    server does not inherit the desktop's 20000. A clean checkout would report a
    sha whose content is not what OTP loaded.
  - The big inputs are gitignored build artifacts: data/graph.obj and the shaded
    JAR are what actually determine routing behaviour and neither is in git.

So: a checkout sha would be a confident answer to a different question. The
manifest records the bytes, on the box, plus the shas they were built from.

USAGE
    # on the target host, at the end of a deploy
    deploy-manifest.py record --target prod --steps repo,nginx \\
        --provenance '<json>' --file /etc/nginx/snippets/otp-common.conf ...

    # from anywhere
    deploy-manifest.py show   [--ssh rwt@100.126.171.72]
    deploy-manifest.py verify [--ssh rwt@100.126.171.72]

`verify` re-checksums every recorded file where it now lives and reports drift —
which is the case this whole thing exists for: a file hand-edited on the box
after the deploy that wrote the manifest. Exit 0 clean, 1 drifted, 75 SKIP.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SKIP = 75
SCHEMA = 1

# Default manifest location on either host. Under the repo root rather than
# /var/lib so that writing it needs no root — `rwt` has no passwordless sudo on
# the Linode, and a manifest that only root can write is a manifest nobody
# writes.
DEFAULT_PATH = Path.home() / "projects" / "otp-minneapolis" / "deployment" / "deploy-manifest.json"

# Above this, record size+mtime instead of a digest. graph.obj is hundreds of MB
# and its identity is already pinned by the sha of the repo that built it.
DIGEST_MAX_BYTES = 32 * 1024 * 1024


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def die_skip(msg):
    print(f"SKIP: {msg}", file=sys.stderr)
    sys.exit(SKIP)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def describe(path):
    # expanduser: a `~/...` argument would otherwise be recorded as ABSENT and
    # then agree with itself on every later verify -- a hole that reads as a pass.
    p = Path(path).expanduser()
    if not p.is_file():
        return {"present": False}
    st = p.stat()
    out = {
        "present": True,
        "bytes": st.st_size,
        "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
    }
    if st.st_size <= DIGEST_MAX_BYTES:
        out["sha256"] = sha256(p)
    else:
        out["sha256"] = None
        out["note"] = f"larger than {DIGEST_MAX_BYTES} bytes; size+mtime only"
    return out


def repo_provenance(repos):
    """{name: {sha, dirty, branch}} for the repos this deploy was built from.

    Run on the DEPLOYING machine — the target has no checkouts, which is the
    whole point of the manifest.
    """
    out = {}
    for name, path in repos.items():
        p = Path(path).expanduser()
        entry = {"path": str(p)}
        try:
            entry["sha"] = subprocess.check_output(
                ["git", "-C", str(p), "rev-parse", "HEAD"], text=True,
                stderr=subprocess.DEVNULL).strip()
            entry["branch"] = subprocess.check_output(
                ["git", "-C", str(p), "rev-parse", "--abbrev-ref", "HEAD"], text=True,
                stderr=subprocess.DEVNULL).strip()
            dirty = subprocess.check_output(
                ["git", "-C", str(p), "status", "--porcelain"], text=True,
                stderr=subprocess.DEVNULL).strip()
            entry["dirty"] = bool(dirty)
            # A dirty tree means the sha does not describe what shipped. Say so
            # in the manifest rather than implying a clean provenance.
            if dirty:
                entry["dirty_paths"] = len(dirty.splitlines())
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            entry["sha"] = None
            entry["dirty"] = None
            entry["note"] = "not a git checkout on the deploying machine"
        out[name] = entry
    return out


# --------------------------------------------------------------------------


def cmd_record(args):
    try:
        provenance = json.loads(args.provenance) if args.provenance else {}
    except json.JSONDecodeError as e:
        die(f"--provenance is not valid JSON: {e}")

    files = {f: describe(f) for f in args.file}
    missing = [f for f, d in files.items() if not d["present"]]

    manifest = {
        "schema": SCHEMA,
        "target": args.target,
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recorded_on": socket.gethostname(),
        "steps": args.steps.split(",") if args.steps else [],
        "provenance": provenance,
        "files": files,
    }
    out = Path(args.out or DEFAULT_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"manifest written: {out}")
    if missing:
        # Not fatal: a partial deploy (--only nginx) legitimately leaves other
        # paths untouched, and a manifest that refuses to record the truth is
        # worse than one that records an absence.
        print(
            "  NOTE: recorded as ABSENT: " + ", ".join(missing),
            file=sys.stderr,
        )
    return 0


def _load(args):
    if args.ssh:
        path = args.path or str(DEFAULT_PATH).replace(str(Path.home()), "~")
        try:
            raw = subprocess.check_output(
                ["ssh", "-o", "ConnectTimeout=15", args.ssh, f"cat {path}"],
                text=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            die_skip(
                f"no manifest at {path} on {args.ssh} ({e.stderr.strip()}). "
                "That host has not been deployed to since manifests existed — "
                "run deploy-app.sh, or read it as 'unknown', not as 'current'.")
        return json.loads(raw), f"{args.ssh}:{path}"
    p = Path(args.path or DEFAULT_PATH)
    if not p.is_file():
        die_skip(f"no manifest at {p}")
    return json.loads(p.read_text()), str(p)


def cmd_show(args):
    manifest, where = _load(args)
    print(f"# {where}")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def cmd_verify(args):
    manifest, where = _load(args)
    files = manifest.get("files", {})
    if not files:
        die_skip(f"manifest at {where} records no files")

    if args.ssh:
        # Re-checksum remotely with a self-contained snippet, so verify needs
        # nothing installed on the far side but python3.
        script = (
            "import hashlib,json,os,sys,time\n"
            "out={}\n"
            "for a in sys.argv[1:]:\n"
            "    p=os.path.expanduser(a)\n"
            "    if not os.path.isfile(p): out[a]={'present':False}; continue\n"
            "    st=os.stat(p); d={'present':True,'bytes':st.st_size,"
            "'mtime_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(st.st_mtime))}\n"
            f"    if st.st_size <= {DIGEST_MAX_BYTES}:\n"
            "        h=hashlib.sha256()\n"
            "        f=open(p,'rb')\n"
            "        while True:\n"
            "            c=f.read(1<<20)\n"
            "            if not c: break\n"
            "            h.update(c)\n"
            "        f.close(); d['sha256']=h.hexdigest()\n"
            "    else: d['sha256']=None\n"
            "    out[a]=d\n"
            "print(json.dumps(out))\n"
        )
        try:
            raw = subprocess.check_output(
                ["ssh", "-o", "ConnectTimeout=15", args.ssh, "python3", "-", *files],
                input=script, text=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            die_skip(f"could not re-checksum on {args.ssh}: {e.stderr.strip()}")
        now = json.loads(raw)
    else:
        now = {f: describe(f) for f in files}

    drift = []
    for path, was in sorted(files.items()):
        is_ = now.get(path, {"present": False})
        if was.get("present") and not is_.get("present"):
            drift.append(f"{path}: recorded as deployed, now MISSING")
        elif not was.get("present") and is_.get("present"):
            drift.append(f"{path}: recorded as absent, now PRESENT (deployed by hand?)")
        elif was.get("sha256") and is_.get("sha256") and was["sha256"] != is_["sha256"]:
            drift.append(
                f"{path}: content changed since the deploy that wrote this "
                f"manifest\n      recorded {was['sha256'][:16]}… at {was['mtime_utc']}"
                f"\n      now      {is_['sha256'][:16]}… at {is_.get('mtime_utc')}")
        elif was.get("sha256") is None and was.get("present") and (
                was.get("bytes") != is_.get("bytes")
                or was.get("mtime_utc") != is_.get("mtime_utc")):
            drift.append(
                f"{path}: size/mtime changed (too large to digest): "
                f"{was.get('bytes')}@{was.get('mtime_utc')} -> "
                f"{is_.get('bytes')}@{is_.get('mtime_utc')}")

    print(f"manifest: {where}")
    print(f"  target      : {manifest.get('target')}")
    print(f"  recorded    : {manifest.get('recorded_utc')} on {manifest.get('recorded_on')}")
    print(f"  steps       : {', '.join(manifest.get('steps') or ['(all)'])}")
    for name, p in sorted((manifest.get("provenance") or {}).items()):
        sha = (p.get("sha") or "unknown")[:12]
        flag = " +DIRTY" if p.get("dirty") else ""
        print(f"  {name:<16}: {sha}{flag}")
    print(f"  files       : {len(files)} recorded, {len(drift)} drifted")

    if drift:
        print("\nFAIL: the box no longer matches its own deploy manifest.", file=sys.stderr)
        for d in drift:
            print(f"  - {d}", file=sys.stderr)
        print(
            "\nSomething changed these outside a deploy. Re-run deploy-app.sh "
            "for the affected step, or find out who edited the box.",
            file=sys.stderr,
        )
        return 1
    print("\nOK: every recorded file is byte-for-byte what the deploy installed.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="write a manifest (run ON the target host)")
    r.add_argument("--target", required=True, help="house | prod")
    r.add_argument("--steps", default="", help="comma-separated deploy steps that ran")
    r.add_argument("--provenance", default="", help="JSON from --emit-provenance")
    r.add_argument("--out", default="")
    r.add_argument("--file", action="append", default=[], help="path to record (repeatable)")
    r.set_defaults(func=cmd_record)

    p = sub.add_parser("provenance", help="emit repo shas as JSON (run on the DEPLOYING host)")
    p.add_argument("--repo", action="append", default=[], metavar="NAME=PATH")
    p.set_defaults(func=lambda a: (
        print(json.dumps(repo_provenance(dict(x.split("=", 1) for x in a.repo))), end=""), 0)[1])

    for name, fn, helptext in (
        ("show", cmd_show, "print a manifest"),
        ("verify", cmd_verify, "re-checksum the recorded files and report drift"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--ssh", default=os.environ.get("DEPLOY_MANIFEST_SSH", ""),
                       help="user@host to read/verify remotely")
        s.add_argument("--path", default="", help="manifest path (default: %s)" % DEFAULT_PATH)
        s.set_defaults(func=fn)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
