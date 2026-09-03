#!/usr/bin/env bash
#
# test-otp-image-check.sh — grade check-otp-image.py against a stubbed docker.
#
# The live boxes can only ever demonstrate the passing case: prod and the house
# both run the image their tag names, and the way to make them disagree is to
# break production. The failure this check exists to catch (backlog 2.27/2.28)
# therefore has to be manufactured, so this puts a fake `docker` first on PATH
# and drives it through the six shapes that matter:
#
#   match        the ordinary green night
#   restart      ids equal, container restarted hours after it was created --
#                cron-gtfs-refresh.sh's scheduled `docker restart`, which is what
#                made a stale container look fresh. Must PASS, and must SAY so.
#   digests      ids differ in spelling, container's id is one of the tag's
#                RepoDigests -- the containerd image store. Must PASS.
#   layers       ids differ, RepoDigests do not help, RootFS.Layers identical.
#                Must PASS.
#   mismatch     genuinely different content. Must FAIL (exit 1).
#   nocontainer  the tag exists, OTP does not run. Must FAIL.
#   stopped      the container exists, its ids match, and it is exited. Must
#                FAIL: docker inspect answers for a stopped container and its
#                .Image still equals the tag's, so the id comparison alone would
#                print OK over a dead OTP.
#   unreachable  the daemon does not answer. Must SKIP (75), not FAIL -- an off
#                Linode is not a red night.
#
# Run it from anywhere; it needs no docker, no ssh and no network.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK="$HERE/check-otp-image.py"
STUBDIR="$(mktemp -d)"
trap 'rm -rf "$STUBDIR"' EXIT

TAG_ID=sha256:1111111111111111111111111111111111111111111111111111111111111111
ALT_ID=sha256:2222222222222222222222222222222222222222222222222222222222222222

cat > "$STUBDIR/docker" <<'STUB'
#!/usr/bin/env bash
# Fake docker. STUB_MODE picks the scenario; see test-otp-image-check.sh.
TAG_ID=sha256:1111111111111111111111111111111111111111111111111111111111111111
ALT_ID=sha256:2222222222222222222222222222222222222222222222222222222222222222

case "${STUB_MODE:-match}" in
  unreachable)
    echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock." >&2
    exit 1 ;;
esac

case "$1" in
  version) echo "29.7.2"; exit 0 ;;
esac

# `docker image inspect NAME --format FMT`
if [ "$1" = image ] && [ "$2" = inspect ]; then
  name="$3"; fmt="$5"
  if [ "$name" = docker-otp ]; then
    case "$fmt" in
      *'.Id'*)          echo "$TAG_ID" ;;
      *'.Created'*)     echo "2026-09-02T22:27:21.226258123Z" ;;
      *RepoDigests*)
        case "${STUB_MODE:-match}" in
          digests) echo "[\"docker-otp@${TAG_ID}\",\"docker-otp@${ALT_ID}\"]" ;;
          *)       echo "[\"docker-otp@${TAG_ID}\"]" ;;
        esac ;;
      *RootFS*)         echo '["sha256:aaa","sha256:bbb"]' ;;
    esac
    exit 0
  fi
  # inspecting the container's own image id (the layers fallback)
  case "${STUB_MODE:-match}" in
    layers)   echo '["sha256:aaa","sha256:bbb"]' ;;
    mismatch) echo '["sha256:aaa","sha256:zzz"]' ;;
    *)        echo '["sha256:aaa","sha256:bbb"]' ;;
  esac
  exit 0
fi

# `docker inspect --format FMT NAME`
if [ "$1" = inspect ]; then
  fmt="$3"; name="$4"
  if [ "${STUB_MODE:-match}" = nocontainer ]; then
    echo "Error: No such object: $name" >&2; exit 1
  fi
  case "$fmt" in
    *'.Image'*)
      case "${STUB_MODE:-match}" in
        match|restart) echo "$TAG_ID" ;;
        *)             echo "$ALT_ID" ;;
      esac ;;
    *'.State.StartedAt'*)
      case "${STUB_MODE:-match}" in
        restart) echo "2026-09-03T04:00:11.000000000Z" ;;
        *)       echo "2026-09-02T22:27:55.591711539Z" ;;
      esac ;;
    *'.Created'*)       echo "2026-09-02T22:27:45.206595782Z" ;;
    *'.State.Status'*)
      case "${STUB_MODE:-match}" in
        stopped) echo "exited" ;;
        *)       echo "running" ;;
      esac ;;
    *'.State.Health'*)  echo "healthy" ;;
    *compose.image*)    echo "sha256:2e4eb1846111151dac05418d869ee8cc676158257fa9c4eaeb781a252bdaa0e4" ;;
  esac
  exit 0
fi
exit 0
STUB
chmod +x "$STUBDIR/docker"

pass=0; fail=0
expect() { # expect <mode> <wanted-rc> <description> [grep-pattern]
  local mode="$1" want="$2" desc="$3" pat="${4:-}"
  local out rc
  out=$(PATH="$STUBDIR:$PATH" STUB_MODE="$mode" python3 "$CHECK" --ssh local 2>&1)
  rc=$?
  if [ "$rc" -ne "$want" ]; then
    echo "FAIL  $desc — wanted exit $want, got $rc"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1)); return
  fi
  if [ -n "$pat" ] && ! printf '%s' "$out" | grep -q -- "$pat"; then
    echo "FAIL  $desc — exit $rc was right but the output never said \"$pat\""
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1)); return
  fi
  echo "ok    $desc (exit $rc)"
  pass=$((pass + 1))
}

expect match       0  "matching ids pass"                      "OK: otp-minneapolis"
expect restart     0  "a restart without a rebuild still passes, and is named" "restarted "
expect digests     0  "a RepoDigests spelling difference passes" "RepoDigests"
expect layers      0  "identical RootFS.Layers pass"            "identical RootFS.Layers"
expect mismatch    1  "genuinely different content fails"       "different layer stack"
expect nocontainer 1  "no OTP container fails"                  "OTP is"
expect stopped     1  "matching ids over a STOPPED container fail" "not running"
expect unreachable 75 "an unreachable daemon skips"             "did not answer"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
