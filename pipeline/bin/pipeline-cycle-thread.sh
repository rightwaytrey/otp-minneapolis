#!/usr/bin/env bash
# One TransitNav pipeline cycle's own thread (2026-09-24, the rider: "an agent
# per cycle — then context is not polluted together and follows the flow of
# each loop"). A Claude session (no Remote Control, rider 2026-10-09) named "cycle N" that owns cycle N from
# its plan to its retirement. Every phase's WORK still goes to a fresh Opus
# agent; this thread only dispatches, checks and replies.
#
# Since 2026-10-08 it DRIVES its cycle: after the rider approves the plan it
# builds, ships to dev on its own, verifies on each dev-app ride on its own,
# and waits for the rider only at plan approval and ship-to-prod. It catches
# its own stuck steps (a self-check every 30 min, which replaced the router's
# sweep) and closes itself after ship-prod (touches cN/retired + cN/stop and
# kills its own window).
#
# It watches no board (since 2026-10-09 the self-hosted board's server routes
# presses itself, via pipeline-route.sh, into ~/.claude/pipeline/cN/
# inbox.log via pipeline-route.sh; this thread follows that and the ride-report
# folder with a one-shot background waiter, ~/bin/pipeline-wait.sh N, which
# exits only when there is work (no Monitor since 2026-10-10). Started by
# pipeline-route.sh as window cN of the `transitnav` tmux session; /exit
# relaunches it; `touch ~/.claude/pipeline/cN/stop` then /exit ends it.
set -u
N="${1:?usage: pipeline-cycle-thread.sh <N>}"; N="${N#c}"
DIR="$HOME/.claude/pipeline/c$N"
STOP="$DIR/stop"
BOARD="https://rwtpc4.tail9d6464.ts.net:9600/"
mkdir -p "$DIR"; touch "$DIR/inbox.log" "$DIR/handled"
cd "$HOME/projects/otp-minneapolis" || exit 1
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

if [ -f "$DIR/retired" ]; then
  echo "cycle-thread c$N: cycle $N has retired ($DIR/retired) — not starting."
  echo "Remove that file to run its thread again."; exit 0
fi

KICKOFF="You are the thread for TransitNav pipeline CYCLE $N (board $BOARD, \
db doc cycles/c$N), and nothing else. Load the transitnav-pipeline skill now and \
read its 'Cycle threads' section in full — it is your brief: you drive cycle $N \
yourself (build after plan approval, ship-dev right after build, verify on each \
dev-app ride) and wait for the rider only at plan approval and ship-to-prod. The \
board is self-hosted since 2026-10-09: read and write cycles/c$N and pipeline/board \
ONLY with ~/bin/pipeline-db (get / update --if-version / set; read 'The board's store \
and presses' in the skill) — never ArtifactData or ArtifactComments, the old Artifact \
is retired. There are no comment threads: a reply is the step summary plus one \
{by: \"claude\"} line in feedback[]. Do not write terminalSnapshot (the board embeds \
your live window now). HOW YOU ARE WOKEN: run \`$HOME/bin/pipeline-wait.sh $N\` with \
the Bash tool, run_in_background true. It prints nothing and stays running until there \
is work, then exits with a batch of lines and you are notified; read its output, handle \
every line, then start it again the same way. Never use the Monitor tool, never poll, \
never set a timer — an idle thread costs nothing. An 'INBOX #K …' line is a press: read \
its text= file and run that one action for cycle $N as the skill says. A 'RIDE …' line \
is a trip that ended: run the skill's 'Verify on a ride' for cycle $N only. A 'CHECK \
<key> :: <what>' line is a self-check rule that fired: act on it as the self-check \
says. Run the full self-check once now, at startup, then start the waiter. Never touch \
another cycle's doc, rows or windows. When the rider talks to you directly in this \
session, mirror the gist into cycles/c$N.feedback[] as the skill says. Ignore any \
'[ride-watch]' lines. END EVERY TURN with the status block in the skill's 'How every \
turn ends' section — Working on / Needs you (with the exact words to type) / Shipping / \
Waiting / Held / Next, every row as number plus plain words — as the last thing you write. \
Reply now with one line: whether the waiter is running, \
cycle $N's steps, and what the self-check found — then the status block."

rm -f "$STOP"
trap 'echo; echo "cycle-thread c$N: interrupted — exiting loop."; exit 0' INT
while true; do
  # A new session gets every press not yet marked handled, and no waiter left
  # over from the last one (it would print to nobody) is still holding a press.
  pkill -f "pipeline-wait.sh $N\$" 2>/dev/null; : > "$DIR/delivered"
  claude --model opus --name "cycle $N" --permission-mode auto "$KICKOFF"   # opus: the rider, 2026-10-09
  if [ -f "$STOP" ]; then rm -f "$STOP"; echo "cycle-thread c$N: stop requested."; break; fi
  if [ -f "$DIR/retired" ]; then echo "cycle-thread c$N: cycle retired."; break; fi
  echo "cycle-thread c$N: session ended — relaunching in 5s (Ctrl-C to stop)"; sleep 5
done
