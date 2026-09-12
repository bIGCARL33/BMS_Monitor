#!/usr/bin/env bash
#  One command: clear whatever is holding the BLE link, check the environment,
#  confirm the board is reachable, then open the console.
#
#      ./start.sh                       # uses the address below
#      ./start.sh AA:BB:CC:DD:EE:FF     # or one you pass
#      ./start.sh --dashboard           # web dashboard instead of the console
#
#  The BMS accepts exactly one BLE connection, and every "not found" failure so
#  far has been something else still holding it -- a dashboard left running, or
#  a bluetoothctl session. Rather than a checklist to work through by hand,
#  this clears those, verifies, and launches.
set -uo pipefail

ADDR="C8:47:80:46:6E:37"
MODE="console"
for arg in "$@"; do
  case "$arg" in
    --dashboard) MODE="dashboard" ;;
    --console)   MODE="console" ;;
    -*)          echo "unknown option $arg" >&2; exit 2 ;;
    *)           ADDR="$arg" ;;
  esac
done

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
say() { printf '\n=== %s\n' "$*"; }

# 1 ── test artifacts that were committed by mistake and pulled down with the
#      code. Real backups written by 'settings save' are gitignored and stay.
if [ -d settings-backups ] && git ls-files --error-unmatch \
     settings-backups >/dev/null 2>&1; then
  say "removing settings-backups tracked by git (test artifacts, not yours)"
  git rm -r --quiet --cached settings-backups 2>/dev/null
  rm -rf settings-backups
fi

# 2 ── anything of ours still holding the single BLE connection
#      Never pkill by pattern here: this script's own ancestors can match it,
#      and killing your own process tree is a memorable way to lose a session.
#      Collect candidates, subtract self and every ancestor, kill what is left.
say "checking for a process already holding the link"
ancestors() {
  local pid=$$
  while [ "$pid" -gt 1 ]; do
    echo "$pid"
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ') || break
    [ -n "$pid" ] || break
  done
}
SAFE=$(ancestors)
FOUND=0
for pid in $(pgrep -f "jkbms\.cli" 2>/dev/null); do
  if echo "$SAFE" | grep -qx "$pid"; then
    continue
  fi
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
  echo "  $pid  $cmd"
  kill "$pid" 2>/dev/null && FOUND=$((FOUND + 1))
done
if [ "$FOUND" -gt 0 ]; then
  echo "stopped $FOUND process(es)"
  sleep 2
else
  echo "none"
fi

# 3 ── BlueZ can hold a connection independently of us, and while the board is
#      connected it stops advertising -- which surfaces as "was not found"
#      rather than anything mentioning a conflict.
if command -v bluetoothctl >/dev/null; then
  say "checking BlueZ"
  if bluetoothctl info "$ADDR" 2>/dev/null | grep -q "Connected: yes"; then
    echo "BlueZ holds $ADDR -- disconnecting"
    bluetoothctl disconnect "$ADDR" >/dev/null 2>&1
    sleep 2
  else
    echo "not connected at the BlueZ level"
  fi
fi

# 4 ── environment
say "environment"
python3 -m jkbms.cli doctor
DOCTOR=$?

# 5 ── is the board actually advertising? A scan that finds nothing means the
#      pack is asleep, which no amount of retrying the console will fix.
say "looking for the board (up to 20s)"
if python3 -m jkbms.cli scan --timeout 20 2>&1 | tee /tmp/jkbms-scan.$$ \
     | grep -q "$ADDR"; then
  echo "found $ADDR"
  rm -f /tmp/jkbms-scan.$$
else
  rm -f /tmp/jkbms-scan.$$
  cat <<EOF

$ADDR is not advertising.

Either the pack has gone to sleep -- wake it with the button, or briefly apply
the charger -- or something outside this script still holds the connection
(the board stops advertising while connected).

  pgrep -af python3            what else is running
  bluetoothctl devices         what BlueZ knows about
EOF
  exit 1
fi

[ "$DOCTOR" -eq 0 ] || echo "(doctor reported problems above; continuing anyway)"

say "starting the $MODE"
if [ "$MODE" = "dashboard" ]; then
  exec python3 -m jkbms.cli dashboard --ble "$ADDR" --lan \
       -o "run-%Y%m%d-%H%M%S.csv"
fi
exec python3 -m jkbms.cli console --ble "$ADDR"
