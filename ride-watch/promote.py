#!/usr/bin/env python3
"""Apply anchored edits to the TransitNav backlog: the ride thread's write path.

    python3 /home/rwt/projects/otp-minneapolis/ride-watch/promote.py EDITS.json [--dry-run]

Why this exists (backlog 34.2): the ride thread runs under
`--permission-mode dontAsk`, and Claude Code refuses the Edit tool anywhere
under `~/.claude/` even with `Edit(//home/rwt/.claude/plans/**)` on the allow
list — measured headless on CLI 2.1.285, 2026-09-30, while the same Edit
under `~/obsidian-vault/Claude/` landed. `python3` IS allowlisted, so the
wrap-up writes its edits to a JSON file (under `~/otp-debug-logs/ride-watch/`,
where it may write) and runs this.

EDITS.json is a list of edits (or `{"edits": [...]}`). Each edit is one of

    {"file": "backlog", "anchor": "<exact text>", "insert_after": "<text>"}
    {"file": "backlog", "anchor": "<exact text>", "replace": "<text>"}
    {"file": "backlog", "row": "16.7", "append": "<text>"}

`file` is "backlog" (the default), "record", or the full path of one of those
two files — nothing else can be written. An `anchor` must occur exactly once
in the file; a missing or ambiguous anchor aborts the whole run with nothing
written to any file. `append` adds " <text>" to the end of the table row
`| <row> | ...`, before its closing ` |`.

Every edit is idempotent, so a wrap-up that re-runs is safe:
  * insert_after is a no-op when `anchor + insert_after` is already present;
  * replace is a no-op when the replacement is present and the anchor no
    longer occurs outside it;
  * append is a no-op when the row already contains the text.

Edits apply in order, each to the result of the previous one. The file is
read, edited and written back with the read repeated immediately before the
write — other sessions edit the backlog too — and if it changed in between,
the edits are recomputed on the fresh text. A unified diff is printed.

Exit status: 0 applied (or nothing to do), 1 bad input, 2 aborted on an anchor.
"""

import argparse
import difflib
import json
import os
import sys
import tempfile

PLANS_DIR = "/home/rwt/.claude/plans"
FILES = {
    "backlog": "please-make-a-centralized-sharded-petal.md",
    "record": "transitnav-backlog-record.md",
}


class Abort(Exception):
    """An edit cannot be applied safely; nothing is written."""


class BadInput(Exception):
    pass


def resolve_file(name, plans_dir):
    allowed = {k: os.path.join(plans_dir, v) for k, v in FILES.items()}
    if name in (None, ""):
        name = "backlog"
    if name in allowed:
        return allowed[name]
    path = os.path.realpath(os.path.expanduser(name))
    for p in allowed.values():
        if path == os.path.realpath(p):
            return p
    raise BadInput("file %r is not the backlog or the record (%s)"
                   % (name, ", ".join(sorted(allowed.values()))))


def load_edits(path, plans_dir):
    if path == "-":
        data = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    if isinstance(data, dict):
        data = data.get("edits")
    if not isinstance(data, list) or not data:
        raise BadInput("expected a non-empty list of edits")
    edits = []
    for i, e in enumerate(data):
        if not isinstance(e, dict):
            raise BadInput("edit %d is not an object" % i)
        ops = [k for k in ("insert_after", "replace", "append") if k in e]
        if len(ops) != 1:
            raise BadInput("edit %d needs exactly one of insert_after, "
                           "replace, append" % i)
        op = ops[0]
        text = e[op]
        if not isinstance(text, str) or (op != "replace" and not text):
            raise BadInput("edit %d: %s must be a non-empty string" % (i, op))
        if op == "append":
            key = e.get("row")
            if not isinstance(key, str) or not key.strip():
                raise BadInput("edit %d: append needs a row number" % i)
        else:
            key = e.get("anchor")
            if not isinstance(key, str) or not key:
                raise BadInput("edit %d: %s needs a non-empty anchor" % (i, op))
        edits.append({"n": i, "file": resolve_file(e.get("file"), plans_dir),
                      "op": op, "key": key, "text": text})
    return edits


def _short(s):
    s = s.replace("\n", "\\n")
    return s if len(s) <= 70 else s[:67] + "..."


def apply_edit(text, edit):
    """Return (new_text, status) or raise Abort. status: applied|present."""
    op, key, new = edit["op"], edit["key"], edit["text"]
    where = "edit %d (%s)" % (edit["n"], op)
    if op == "append":
        prefix = "| %s | " % key.strip()
        lines = text.split("\n")
        hits = [i for i, l in enumerate(lines) if l.startswith(prefix)]
        if len(hits) != 1:
            raise Abort("%s: row %s found %d times" % (where, key, len(hits)))
        line = lines[hits[0]]
        if new in line:
            return text, "present"
        if not line.rstrip().endswith(" |"):
            raise Abort("%s: row %s does not end with ' |'" % (where, key))
        body = line.rstrip()[:-2].rstrip()
        lines[hits[0]] = body + " " + new + " |"
        return "\n".join(lines), "applied"
    if op == "insert_after":
        if key + new in text:
            return text, "present"
        n = text.count(key)
        if n != 1:
            raise Abort("%s: anchor found %d times: %r" % (where, n, _short(key)))
        i = text.index(key) + len(key)
        return text[:i] + new + text[i:], "applied"
    # replace
    if new and new in text and key not in text.replace(new, ""):
        return text, "present"
    n = text.count(key)
    if n != 1:
        raise Abort("%s: anchor found %d times: %r" % (where, n, _short(key)))
    return text.replace(key, new, 1), "applied"


def apply_all(texts, edits):
    """Apply every edit in order; returns (new_texts, statuses)."""
    out = dict(texts)
    statuses = []
    for e in edits:
        out[e["file"]], st = apply_edit(out[e["file"]], e)
        statuses.append(st)
    return out, statuses


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_atomic(path, text):
    d = os.path.dirname(path)
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=".promote-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(edits_path, dry_run=False, plans_dir=PLANS_DIR, out=None):
    out = out or sys.stdout
    edits = load_edits(edits_path, plans_dir)
    files = sorted({e["file"] for e in edits})
    for attempt in range(3):
        before = {p: read(p) for p in files}
        after, statuses = apply_all(before, edits)
        for e, st in zip(edits, statuses):
            label = e["key"] if e["op"] == "append" else _short(e["key"])
            print("%-7s edit %d %s %s in %s" % (st, e["n"], e["op"], label,
                                               os.path.basename(e["file"])),
                  file=out)
        changed = [p for p in files if after[p] != before[p]]
        for p in changed:
            out.writelines(difflib.unified_diff(
                before[p].splitlines(True), after[p].splitlines(True),
                fromfile=p, tofile=p + (" (dry run)" if dry_run else "")))
        if not changed:
            print("nothing to do: every edit is already in place", file=out)
            return 0
        if dry_run:
            print("dry run: nothing written", file=out)
            return 0
        # The re-read right before the write: another session may have
        # edited the backlog while we worked. Recompute on the fresh text.
        if any(read(p) != before[p] for p in changed):
            print("file changed under us; recomputing", file=out)
            continue
        for p in changed:
            write_atomic(p, after[p])
        print("wrote %s" % ", ".join(changed), file=out)
        return 0
    raise Abort("the file kept changing between read and write; nothing written")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("edits", help="JSON file of edits, or - for stdin")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the diff, write nothing")
    ap.add_argument("--plans-dir", default=PLANS_DIR,
                    help="directory holding the backlog and record "
                         "(tests point this at a copy)")
    args = ap.parse_args(argv)
    try:
        return run(args.edits, dry_run=args.dry_run, plans_dir=args.plans_dir)
    except BadInput as e:
        print("promote: bad input: %s" % e, file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print("promote: %s" % e, file=sys.stderr)
        return 1
    except Abort as e:
        print("promote: ABORTED, nothing written: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
