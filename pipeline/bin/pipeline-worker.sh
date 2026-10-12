#!/usr/bin/env bash
# The TransitNav Pipeline board's ROUTER: the ONE Claude session (no Remote Control since 2026-10-09)
# that runs Triage. Since 2026-10-09 the board is self-hosted (~/pipeline-board,
# unit pipeline-board) and the router is woken by a one-shot background waiter,
# ~/bin/pipeline-wait.sh board (no Monitor since 2026-10-10; the 15-minute
# ledger scan runs inside that waiter, with no agent). Since 2026-09-24 it no longer runs cycle
# work itself — each cycle has its own thread (pipeline-cycle-thread.sh, window
# cN of the `transitnav` tmux session), and the router hands a press to it with
# pipeline-route.sh. The router runs Triage (it belongs to no cycle), the
# 15-minute ledger scan, and opens new cycles. Since 2026-09-24 a press acts on
# its own cycle only — Verify no longer fans out (the rider: "a very serious bug").
# Since 2026-10-08 it manages no cycle at all: each cycle thread drives itself,
# catches its own stuck steps (a 30-min self-check replaced the sweep) and
# closes its own window after ship-prod.
#
# Why one watcher (2026-09-21): every session watching the artifact wakes on
# every comment. Why threads per cycle (2026-09-24): the rider, "an agent per
# cycle — then context is not polluted together and follows the flow of each
# loop"; and at 08:15 that day a Verify press was lost behind two others with
# nothing to catch it.
#     tmux new-session -d -s transitnav -n board -c ~/projects/otp-minneapolis ~/bin/pipeline-worker.sh
#     tmux attach -t transitnav                        # look at it
#     touch ~/.claude/pipeline-worker-stop; then /exit  # stop the loop for good
# /exit relaunches a fresh session under the same name, and the kickoff prompt
# starts the waiter every time.

NAME="${1:-pipeline board}"
STOP="$HOME/.claude/pipeline-worker-stop"
BOARD="https://rwtpc4.tail9d6464.ts.net:9600/"
cd "$HOME/projects/otp-minneapolis" || exit 1
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

KICKOFF="You are the TransitNav Pipeline board's ROUTER session, and nothing else. \
The board is self-hosted since 2026-10-09 ($BOARD); the old Claude Artifact is retired — \
never use ArtifactComments or ArtifactData. HOW YOU ARE WOKEN: run \
\`$HOME/bin/pipeline-wait.sh board\` with the Bash tool, run_in_background true. It prints \
nothing and stays running until there is work, then exits with a batch of lines and you \
are notified; read its output, handle every line, then start it again the same way. \
Never use the Monitor tool, never poll, never set a timer. The 15-minute ledger scan \
runs inside the waiter (pipeline-scan-apply.py) — you do not run it on a clock. Start \
the waiter now and say in one line that it is running. Then load the transitnav-pipeline \
skill and read 'The board's store and presses' and 'Routing' — they are your brief. The \
board's server routes every press that names a cycle straight to that cycle's inbox; \
your inbox gets START triage (and any press naming no cycle). An 'INBOX #K …' line is a \
press: read its text= file, act, then append K to ~/.claude/pipeline/board/handled. A \
'CHECK triage-requested:… ' line means triage was requested and never started: run it. \
You do exactly two things: (a) run Triage yourself; (b) on 'then plan cycle N', open the \
cycle doc and route its plan with ~/bin/pipeline-route.sh N --thread - --key \
plan:<requestedAt>. Read and write the store only with ~/bin/pipeline-db (pin \
--if-version). You make no decision about any cycle: no sweep, no re-routing from the \
db, no stopping or retiring of threads, never write a live cycle's doc after opening it \
(Triage closing in-prod rows of RETIRED cycles is the one exception). Never fan a press \
out to other cycles. \
Ignore any '[ride-watch]' lines completely — they are not for you."

rm -f "$STOP"
trap 'echo; echo "pipeline-worker: interrupted — exiting loop."; exit 0' INT
while true; do
  # A new session gets every unhandled press; no leftover waiter holds one.
  pkill -f "pipeline-wait.sh board\$" 2>/dev/null; : > "$HOME/.claude/pipeline/board/delivered" 2>/dev/null
  claude --model opus --name "$NAME" --permission-mode auto "$KICKOFF"   # opus: the rider, 2026-10-09 ("all agents are opening in fable now, it should be opus")
  if [ -f "$STOP" ]; then rm -f "$STOP"; echo "pipeline-worker: stop requested."; break; fi
  echo "pipeline-worker: session ended — relaunching in 5s (Ctrl-C to stop)"; sleep 5
done
