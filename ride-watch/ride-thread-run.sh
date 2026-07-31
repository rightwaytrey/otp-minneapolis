#!/usr/bin/env bash
# Inner runner for a ride thread: exec ONE Remote Control session that follows
# the ride the daemon just started. Modelled on ~/bin/_rc-run.sh (the rider's
# hand-spawn script) — same shape, no loop, no relaunch, so `/exit` from the
# phone behaves normally and the SessionEnd hook reaps the tmux pane.
#
# ride_watch.py runs this under `tmux new-session -d`, so everything the
# session needs must be set here rather than inherited: a tmux server started
# by some other client does NOT carry the daemon's PATH, and `claude` lives in
# ~/.local/bin.
#
#   $1  display name shown in the rider's Claude app list ("ride 07-31 14:32")
set -u

NAME="${1:-ride}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# cwd = the repo, so the session inherits CLAUDE.md and the auto-memory index
# and can answer "which branch is Go Mode on" without being told.
cd "$REPO" || exit 1

# --permission-mode is pinned rather than inherited: the mode a Claude session
# starts in comes from whatever the project was last left in (auto, plan, …),
# and a ride thread's permissions must not depend on that. `manual` + the
# allowlist in ride-thread-settings.json means the routine job never prompts and
# anything outside it prompts the rider, which is the intended fallback.
exec claude --remote-control "$NAME" \
  --permission-mode manual \
  --settings "$HERE/ride-thread-settings.json" \
  --append-system-prompt "$(cat "$HERE/ride-thread-sysprompt.md")"
