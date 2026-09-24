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
#   $2  the kickoff prompt: the session's first message, which tells it to arm
#       its Monitor on the ride's events file. Passed as claude's positional
#       prompt — since 2026-09-21 nothing is ever typed into this pane.
set -u

NAME="${1:-ride}"
PROMPT="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# cwd = the repo, so the session inherits CLAUDE.md and the auto-memory index
# and can answer "which branch is Go Mode on" without being told.
cd "$REPO" || exit 1

# --permission-mode is pinned rather than inherited: the mode a Claude session
# starts in comes from whatever the project was last left in (auto, plan, …),
# and a ride thread's permissions must not depend on that. `dontAsk` + the
# allowlist in ride-thread-settings.json means the routine job runs and anything
# outside it is REFUSED, and the turn goes on. It used to be `manual`, where
# anything outside the list asked the rider: mid-ride on a phone an ask is a
# hang, and three wrap-ups were lost that way after 12.4 first closed (the
# latest a multi-line `python3 -c` at 2026-09-23 16:06:54; backlog 12.4).
ARGS=(--remote-control "$NAME"
      --permission-mode dontAsk
      --settings "$HERE/ride-thread-settings.json"
      --append-system-prompt "$(cat "$HERE/ride-thread-sysprompt.md")")
if [ -n "$PROMPT" ]; then ARGS+=("$PROMPT"); fi
exec claude "${ARGS[@]}"
