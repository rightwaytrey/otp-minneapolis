# CLAUDE.md — otp-minneapolis (OTP server, graph, deployment, ride-watch daemon)

The routing backend and its operations: the OTP2 graph and `router-config.json`, the
Linode deployment, and the ride-watch daemon that writes ride reports into the vault.

## The backlog protocol — read this before planning anything

**There is exactly one backlog for TransitNav:**
`~/.claude/plans/please-make-a-centralized-sharded-petal.md` — numbered tiers spanning
all four repos (`transitnav`, `otprr/otp-react-redux`, `transitnav-ios`,
`otp-minneapolis`) and the Obsidian vault. Read it before you plan or start anything,
and **verify the item against current source** — not against the note and not against
the commit message. In nine of the last ten sessions the plan or a ride note was wrong
about the *mechanism*.

**Anything you find that should be fixed later goes in that file and nowhere else.**
Dedupe against the existing tiers first (a recurrence is an observation added to the
existing row, never a new row). Then: a numbered row with a bolded one-line finding and
a Note carrying real evidence — `file:line`, actual numbers, timestamps, the measurement
you ran; a Session index row naming the repo; a Sequencing constraint if order matters;
and what you **ruled out**. Never delete a row — mark it `**DONE** <sha>` or move it to
"Closed — do not re-plan".

**Nothing floats anywhere else.** No `## Fix backlog` in a ride report, no new plan file
of open items, no `TODO(later)` in source, no scratch Markdown of things to fix.
Evidence documents stay put — ride reports in `~/obsidian-vault/Claude/ride-watch/` are
daemon-owned, and the rider's vault notes are the rider's — read them, cite them, link
them, but promote the actionable item into the plan. Notes are evidence; the plan is the
index. If you find something floating, promote it and say so.

The full protocol is in `~/.claude/CLAUDE.md`.

## Git

Ask before committing: show the proposed message and the file list, and wait for
explicit approval.

## Traps specific to this repo

- **`data/router-config.json` is what OTP loads**, and `config/router-config.json` is
  the source copy — change both. `routingDefaults` changes need an OTP restart, not a
  graph rebuild.
- **The ride-watch daemon owns its reports.** `ride-watch/report-prompt.md` defines
  their structure, and it now requires the report agent to promote findings into the
  central backlog rather than keep a private fix list. Reports themselves live in
  `~/obsidian-vault/Claude/ride-watch/` and are not to be edited by hand.
- **Root-level `GEOCODER_*.md` and `FIX_PORT_8001.md` are 2026-01 history**, banner-marked
  SUPERSEDED — they predate the Linode migration and Stadia geocoding. Not action lists.
- **`deploy-app.sh` has a `/var/www` landmine** — it could install an nginx location
  without publishing the desktop frontend to production. Check before deploying.
