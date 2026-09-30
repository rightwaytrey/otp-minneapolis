#!/usr/bin/env python3
"""Tests for promote.py. Stdlib only:  python3 ride-watch/test_promote.py

Every test works in a temporary plans directory. The real backlog is only
ever COPIED into one (the dry-run case); nothing here writes under
~/.claude/plans/.
"""

import io
import json
import re
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import promote  # noqa: E402

REAL_BACKLOG = os.path.join(promote.PLANS_DIR, promote.FILES["backlog"])

BACKLOG = """# Backlog

## Open — 2 rows *(earlier text)*

## Tier 1 — things *(opened 2026-09-01, all OPEN)*

| # | Finding | Note |
|---|---|---|
| 1.1 | **first** | note one |
| 1.2 | **second** | note two |

**Sequencing (Tier 1):** none.
"""

RECORD = "# Record\n\n## Tier 1\n\n| 1.0 | **closed** | done |\n"


class PromoteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="promote-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.backlog = os.path.join(self.dir, promote.FILES["backlog"])
        self.record = os.path.join(self.dir, promote.FILES["record"])
        with open(self.backlog, "w", encoding="utf-8") as f:
            f.write(BACKLOG)
        with open(self.record, "w", encoding="utf-8") as f:
            f.write(RECORD)

    def edits(self, edits):
        p = os.path.join(self.dir, "edits-%d.json" % id(edits))
        with open(p, "w", encoding="utf-8") as f:
            json.dump(edits, f)
        return p

    def run_promote(self, edits, *extra):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = promote.main([self.edits(edits), "--plans-dir", self.dir]
                              + list(extra))
        finally:
            sys.stdout, sys.stderr = old
        return rc, out.getvalue(), err.getvalue()

    def text(self, path=None):
        with open(path or self.backlog, encoding="utf-8") as f:
            return f.read()

    TIER2 = "\n## Tier 2 — ride `abc-123` *(opened 2026-09-30, all OPEN)*\n"

    def test_anchored_insert(self):
        rc, out, _ = self.run_promote([
            {"anchor": "**Sequencing (Tier 1):** none.\n",
             "insert_after": self.TIER2}])
        self.assertEqual(rc, 0)
        self.assertTrue(self.text().endswith(
            "**Sequencing (Tier 1):** none.\n" + self.TIER2))
        self.assertIn("+## Tier 2", out)  # the diff is printed
        self.assertIn("applied", out)

    def test_replace_and_append_and_record(self):
        rc, _, _ = self.run_promote([
            {"anchor": "## Open — 2 rows *(",
             "replace": "## Open — 3 rows *(**abc-123 wrap-up**; "},
            {"row": "1.2", "append": "**Second sighting (abc-123).**"},
            {"file": "record", "anchor": "| done |",
             "replace": "| done, reopened by abc-123 |"}])
        self.assertEqual(rc, 0)
        t = self.text()
        self.assertIn("## Open — 3 rows *(**abc-123 wrap-up**; earlier", t)
        self.assertIn("| 1.2 | **second** | note two "
                      "**Second sighting (abc-123).** |", t)
        self.assertIn("reopened by abc-123", self.text(self.record))

    def test_ambiguous_anchor_aborts_with_no_write(self):
        # A good edit first, then an ambiguous one: neither may land.
        rc, _, err = self.run_promote([
            {"row": "1.1", "append": "sighting"},
            {"file": "record", "anchor": "# Record", "replace": "# R"},
            {"anchor": "| **", "insert_after": "x"}])
        self.assertEqual(rc, 2)
        self.assertIn("found 2 times", err)
        self.assertEqual(self.text(), BACKLOG)
        self.assertEqual(self.text(self.record), RECORD)

    def test_missing_anchor_aborts(self):
        rc, _, err = self.run_promote([
            {"row": "1.1", "append": "sighting"},
            {"anchor": "## Tier 9", "insert_after": "x"}])
        self.assertEqual(rc, 2)
        self.assertIn("found 0 times", err)
        self.assertEqual(self.text(), BACKLOG)

    def test_missing_row_aborts(self):
        rc, _, _ = self.run_promote([{"row": "7.7", "append": "x"}])
        self.assertEqual(rc, 2)
        self.assertEqual(self.text(), BACKLOG)

    def test_idempotent_on_rerun(self):
        edits = [
            {"anchor": "## Open — 2 rows *(",
             "replace": "## Open — 3 rows *(**abc-123**; "},
            {"anchor": "**Sequencing (Tier 1):** none.\n",
             "insert_after": self.TIER2},
            {"row": "1.1", "append": "**sighting abc-123**"},
            # a replacement that contains its own anchor
            {"anchor": "note two", "replace": "note two (checked abc-123)"}]
        rc, _, _ = self.run_promote(edits)
        self.assertEqual(rc, 0)
        once = self.text()
        mtime = os.stat(self.backlog).st_mtime_ns
        rc, out, _ = self.run_promote(edits)
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)
        self.assertEqual(self.text(), once)
        self.assertEqual(os.stat(self.backlog).st_mtime_ns, mtime)
        self.assertEqual(once.count("Tier 2"), 1)
        self.assertEqual(once.count("sighting abc-123"), 1)
        self.assertEqual(once.count("checked abc-123"), 1)

    def test_only_backlog_and_record_are_writable(self):
        other = os.path.join(self.dir, "other.md")
        with open(other, "w") as f:
            f.write("anchor")
        rc, _, err = self.run_promote(
            [{"file": other, "anchor": "anchor", "replace": "x"}])
        self.assertEqual(rc, 1)
        self.assertIn("not the backlog or the record", err)
        with open(other) as f:
            self.assertEqual(f.read(), "anchor")

    def test_full_path_names_the_file(self):
        rc, _, _ = self.run_promote(
            [{"file": self.backlog, "row": "1.1", "append": "p"}])
        self.assertEqual(rc, 0)
        self.assertIn("note one p |", self.text())

    def test_bad_input(self):
        for bad in ([], [{"anchor": "x"}],
                    [{"anchor": "x", "replace": "y", "insert_after": "z"}],
                    [{"anchor": "", "insert_after": "z"}],
                    [{"row": "1.1", "append": ""}]):
            rc, _, _ = self.run_promote(bad)
            self.assertEqual(rc, 1, bad)
        self.assertEqual(self.text(), BACKLOG)

    def test_recomputes_when_file_changes_before_write(self):
        real_read = promote.read
        calls = {"n": 0}

        def racing_read(path):
            calls["n"] += 1
            if calls["n"] == 2:  # the re-read right before the write
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\nanother session's line\n")
            return real_read(path)

        promote.read = racing_read
        self.addCleanup(setattr, promote, "read", real_read)
        rc, out, _ = self.run_promote([{"row": "1.1", "append": "mine"}])
        self.assertEqual(rc, 0)
        self.assertIn("recomputing", out)
        t = self.text()
        self.assertIn("another session's line", t)  # theirs kept
        self.assertIn("note one mine |", t)         # ours applied

    @unittest.skipUnless(os.path.exists(REAL_BACKLOG),
                         "%s not present" % REAL_BACKLOG)
    def test_dry_run_against_a_copy_of_the_real_backlog(self):
        shutil.copyfile(REAL_BACKLOG, self.backlog)
        before = self.text()
        real_mtime = os.stat(REAL_BACKLOG).st_mtime_ns
        header = next(l for l in before.split("\n")
                      if l.startswith("## Open — "))
        anchor = header[:header.index("*(") + 2]
        row = next(l.split(" | ")[0][2:] for l in before.split("\n")
                   if re.match(r"\| \d+\.\d+ \| ", l))
        rc, out, _ = self.run_promote(
            [{"anchor": anchor, "replace": anchor + "**dry-run probe**; "},
             {"row": row, "append": "**dry-run probe.**"}], "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("dry run: nothing written", out)
        self.assertIn("+" + anchor + "**dry-run probe**; ", out)
        self.assertEqual(self.text(), before)
        self.assertEqual(os.stat(REAL_BACKLOG).st_mtime_ns, real_mtime)


if __name__ == "__main__":
    unittest.main(verbosity=2)
