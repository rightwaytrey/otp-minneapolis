# pipeline — the TransitNav Pipeline board and its sessions

Self-hosted since 2026-10-09 (it left Claude Artifacts: an Artifact page cannot frame or
stream the agents' terminals). Live at https://rwtpc4.tail9d6464.ts.net:9600/ (tailnet only).

- `board/` — `server.py` (127.0.0.1:8098; store, SSE, presses, ttyd terminals), `shim.js`
  (the page's `claude.use(db|comments|user)` against that server), `build-index.py`
  (`index.html` from `artifact-source.html`), `ttyd-index.html` (ttyd's client + touch
  scrolling), `pipeline-board.service` (systemd --user unit; `~/pipeline-board` is a symlink here).
- `bin/` — the session wrappers and routers; `~/bin/pipeline-*` are symlinks here.
- `skill/transitnav-pipeline/` — the skill every session runs; `~/.claude/skills/transitnav-pipeline`
  is a symlink here.

Store: `~/.claude/pipeline/db` (not in git). Inboxes: `~/.claude/pipeline/cN`, `~/.claude/pipeline/board`.
