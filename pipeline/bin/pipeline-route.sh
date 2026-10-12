#!/usr/bin/env bash
# Hand one pipeline press to the cycle thread that owns it (2026-09-24).
#
# The board's router session (pipeline-worker.sh) is the ONLY session watching
# the board artifact. When a press names a cycle, it calls this; the cycle's own
# thread (pipeline-cycle-thread.sh, window cN of the `transitnav` tmux session)
# is woken by pipeline-wait.sh N when its inbox grows and runs the action; `--key` makes a repeat
# idempotent. Since 2026-10-08 there is no router sweep: a press whose comment
# never arrived is picked up by the cycle thread's own 30-minute self-check.
#
#   pipeline-route.sh <N|board> --thread <comment thread id|-> --key <dedupe key> [--file <press text>]
#   (press text on stdin when --file is not given)
#
# Inbox:  ~/.claude/pipeline/cN/inbox.log   one line per press:
#           #K key=<key> thread=<id> text=<path> :: <first line of the press>
#         ~/.claude/pipeline/cN/K.txt       the full press text
#         ~/.claude/pipeline/cN/handled     K per line, appended by the thread when done
set -u
N="${1:?usage: pipeline-route.sh <N> --thread ID --key KEY [--file PATH]}"; shift
N="${N#c}"
THREAD="-"; KEY=""; FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --thread) THREAD="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --file) FILE="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$KEY" ] || { echo "--key is required (e.g. verify:1790255731941)" >&2; exit 2; }

# "board" (2026-10-09, self-hosted board): a press that names no cycle — START
# triage — goes to the router's own inbox, which pipeline-wait.sh board follows.
if [ "$N" = "board" ]; then DIR="$HOME/.claude/pipeline/board"; else DIR="$HOME/.claude/pipeline/c$N"; fi
INBOX="$DIR/inbox.log"
mkdir -p "$DIR"; touch "$INBOX" "$DIR/handled"

exec 9>"$DIR/.route.lock"; flock 9
if grep -q " key=$KEY " "$INBOX"; then
  echo "already routed: $(basename "$DIR") $KEY ($(grep " key=$KEY " "$INBOX" | cut -c1-80))"
else
  K=$(( $(wc -l < "$INBOX") + 1 ))
  if [ -n "$FILE" ]; then cp "$FILE" "$DIR/$K.txt"; else cat > "$DIR/$K.txt"; fi
  FIRST="$(head -n1 "$DIR/$K.txt" | tr -d '\r')"
  echo "#$K key=$KEY thread=$THREAD text=$DIR/$K.txt :: $FIRST" >> "$INBOX"
  echo "routed $(basename "$DIR") #$K: $FIRST"
fi
flock -u 9

# Make sure the thread is alive. Its waiter resumes from `handled`, so a
# thread started now picks up everything routed but not yet handled. A cycle
# that retired closed its own thread (cN/retired); never start it again here.
if [ "$N" = "board" ]; then
  echo "routed to the router's inbox (no window started)"
elif [ -f "$DIR/retired" ]; then
  echo "cycle $N has retired — press written to its inbox, thread NOT started"
elif ! tmux list-windows -t transitnav -F '#W' 2>/dev/null | grep -qx "c$N"; then
  tmux new-window -d -t transitnav -n "c$N" -c "$HOME/projects/otp-minneapolis" \
    "$HOME/bin/pipeline-cycle-thread.sh $N"
  echo "started cycle thread c$N (tmux transitnav:c$N)"
fi
