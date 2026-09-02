#!/usr/bin/env bash
#
# install-house-nginx.sh — render and install the :9966 site on THIS desktop.
#
# The house half of what deploy-app.sh does for the Linode, calling the same
# renderer with the same templates and the house value set. There is no second
# copy of the config any more; parity between the two hosts is a property of
# deployment/nginx/*.tmpl, and `render-nginx.py --check` proves it.
#
# Nothing here `cp`s a repo file onto a live nginx path. It renders, diffs, and
# only then installs. README.md used to tell you to
# `sudo cp config/nginx/otp.conf /etc/nginx/sites-available/otp`, which is how
# the live config both rots and leaks: the repo file carries __PLACEHOLDER__
# tokens, the live file carries substituted secrets, and copying either
# direction is a breakage.
#
#   ./install-house-nginx.sh              # render + show the diff, change nothing
#   ./install-house-nginx.sh --install    # ... and install it (needs sudo)
#
# UNLOCK_SECRET must be in the environment (deployment/.env has it; it is
# gitignored). Rotating it is backlog 2.10 and needs a human.
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1
[ $# -gt 1 ] && { echo "${RED}Usage: $0 [--install]${NC}"; exit 1; }

if [ -z "${UNLOCK_SECRET:-}" ] && [ -f "$SCRIPT_DIR/.env" ]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
fi
[ -n "${UNLOCK_SECRET:-}" ] || {
  echo "${RED}Error: UNLOCK_SECRET is not set and deployment/.env did not supply it.${NC}"
  echo "The renderer fails closed rather than installing a config with a live"
  echo "__UNLOCK_SECRET__ placeholder, which would lock every cookie holder out."
  exit 1
}

echo "${GREEN}=== Parity ===${NC}"
python3 "$SCRIPT_DIR/render-nginx.py" --check

echo
echo "${GREEN}=== Render (house) ===${NC}"
OUT="$SCRIPT_DIR/rendered/house"   # gitignored
rm -rf "$OUT"
UNLOCK_SECRET="$UNLOCK_SECRET" python3 "$SCRIPT_DIR/render-nginx.py" --env house --out "$OUT"

echo
echo "${GREEN}=== What would change ===${NC}"
CHANGED=0
DIFF_MAX_LINES=40

# Capture the diff, THEN print it. This used to be one long pipeline ending in
# `| head -40 | sed`, and under `set -euo pipefail` that killed the script:
# head exits after 40 lines, the grep upstream takes SIGPIPE (141), pipefail
# makes 141 the pipeline's status and set -e exits -- silently, with no error
# text, and only when the diff runs past 40 lines. That is precisely when the
# live file is far out of date, i.e. when you most need --install to work.
# Three consecutive --install runs died here on 2026-09-01, each printing one
# DIFFERS where two were due and never reaching `=== Install ===` (backlog
# 2.15). Reproduced at exit 141 before the fix, exit 0 after.
#
# diff's exit codes are inspected explicitly rather than swallowed with `||
# true`: 0 identical, 1 differs, >=2 the diff itself failed -- and "the diff
# failed" must not read as "unchanged", or a permissions problem looks like a
# clean box.
diff_one() {
  local rendered="$1" live="$2" out rc=0 n
  local -a reader=()
  # Pick the reader FIRST, and remember which one worked. The old code ran
  # `sudo -n diff ... || diff ...` and could not tell diff's exit 1 ("the files
  # differ") from sudo's exit 1 ("a password would be required"), so on a box
  # without passwordless sudo an unreadable file reported a phantom empty diff.
  if [ -r "$live" ]; then
    :
  elif sudo -n test -r "$live" 2>/dev/null; then
    reader=(sudo -n)
  else
    echo "${YELLOW}  $live: not present (or unreadable) — would be created${NC}"
    CHANGED=1
    return 0
  fi
  out="$(${reader[@]+"${reader[@]}"} diff -u "$live" "$rendered" 2>/dev/null)" || rc=$?
  if [ "$rc" -ge 2 ]; then
    echo "${RED}  $live: diff failed (exit $rc) — cannot say what would change${NC}"
    CHANGED=1
    return 0
  fi
  if [ "$rc" -eq 0 ]; then
    echo "  $live: unchanged"
    return 0
  fi
  echo "${YELLOW}  $live: DIFFERS${NC}"
  # awk, not head: awk reads its input to EOF, so there is no early close and
  # nothing upstream can be signalled.
  out="$(printf '%s\n' "$out" | grep -E '^[+-]' | grep -vE '^[+-][+-]' || true)"
  n="$(printf '%s\n' "$out" | grep -c '' || true)"
  printf '%s\n' "$out" | awk -v m="$DIFF_MAX_LINES" 'NR<=m' | sed 's/^/    /'
  if [ "${n:-0}" -gt "$DIFF_MAX_LINES" ]; then
    echo "    ... and $((n - DIFF_MAX_LINES)) more changed line(s) (full diff: diff -u $live $rendered)"
  fi
  CHANGED=1
  return 0
}
diff_one "$OUT/otp-common.conf" /etc/nginx/snippets/otp-common.conf
diff_one "$OUT/otp.conf"        /etc/nginx/sites-available/otp

if [ "$INSTALL" -ne 1 ]; then
  echo
  echo "${YELLOW}Dry run. Nothing was installed.${NC} Re-run with --install to apply."
  echo "The rendered files are in $OUT (mode 600 — they hold the real secret)."
  exit 0
fi

if [ "$CHANGED" -eq 0 ]; then
  echo
  echo "Nothing to do."
  exit 0
fi

echo
echo "${GREEN}=== Install ===${NC}"
# Markers around every write. What made 2.15 hard to read was a log that simply
# stopped: no error, no `=== Install ===`, nothing to say whether a file had
# been touched. Now the log names each destination before and after it is
# written, so an abort is locatable from the log alone.
install_one() {
  local src="$1" dest="$2"
  echo "  install  -> $dest"
  sudo install -D -m 644 -o root -g root "$src" "$dest"
  echo "  installed   $dest ($(stat -c %s "$dest" 2>/dev/null || echo '?') bytes)"
}
install_one "$OUT/otp-common.conf" /etc/nginx/snippets/otp-common.conf
install_one "$OUT/otp.conf"        /etc/nginx/sites-available/otp
echo "  link     -> /etc/nginx/sites-enabled/otp"
sudo ln -sfn /etc/nginx/sites-available/otp /etc/nginx/sites-enabled/otp
install_one "$REPO_ROOT/config/nginx/conf.d/00-map-hash.conf" /etc/nginx/conf.d/00-map-hash.conf
echo "  nginx -t ..."
sudo nginx -t
echo "  reloading nginx ..."
sudo systemctl reload nginx
echo "${GREEN}=== Install complete ===${NC} nginx reloaded."

echo
echo "${GREEN}=== Deploy manifest ===${NC}"
PROVENANCE="$(python3 "$REPO_ROOT/scripts/deploy-manifest.py" provenance \
  --repo "otp-minneapolis=$REPO_ROOT" \
  --repo "transitnav=$HOME/projects/transitnav" \
  --repo "otprr=$HOME/projects/otprr/otp-react-redux")"
python3 "$REPO_ROOT/scripts/deploy-manifest.py" record \
  --target house --steps nginx --provenance "$PROVENANCE" \
  --file /etc/nginx/snippets/otp-common.conf \
  --file /etc/nginx/sites-available/otp \
  --file /etc/nginx/conf.d/00-map-hash.conf
echo
echo "Check the ladder against what is now live:"
# `local`, not `localhost`: this box's sshd rejects a loopback connection, and
# `--target house` is the one flag that pins BOTH the probe's IP and the host
# the written line is read back from to this machine. Naming only one of them is
# backlog 2.16.
echo "  scripts/check-config-ladder.py --deployed --ssh local"
echo "  scripts/check-debug-log-payload.py --target house"
