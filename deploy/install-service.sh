#!/usr/bin/env bash
#  Install the dashboard+logger as a systemd service.
#
#  Usage:  sudo ./deploy/install-service.sh <BLE-ADDRESS> [LOGDIR]
#  e.g.    sudo ./deploy/install-service.sh C8:47:80:46:6E:37
#
#  Prints what it will do and asks before touching anything under /etc.
set -euo pipefail

UNIT_NAME=jkbms-dashboard.service
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$HERE")"
TEMPLATE="$HERE/$UNIT_NAME"
TARGET="/etc/systemd/system/$UNIT_NAME"

BLE_ADDR="${1:-}"
LOGDIR="${2:-/var/lib/jkbms}"

die() { echo "error: $*" >&2; exit 1; }

[ -n "$BLE_ADDR" ] || die "no BLE address given.
  Find it with:  python3 -m jkbms.cli scan
  Then:          sudo $0 <BLE-ADDRESS> [LOGDIR]"

# Accept the usual AA:BB:CC:DD:EE:FF form; a wrong address here becomes a
# restart loop that only shows up in the journal.
[[ "$BLE_ADDR" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] \
  || die "'$BLE_ADDR' does not look like a BLE address (AA:BB:CC:DD:EE:FF)"

[ "$(id -u)" -eq 0 ] || die "run with sudo -- this writes to /etc/systemd/system"
[ -f "$TEMPLATE" ] || die "template not found at $TEMPLATE"
command -v systemctl >/dev/null || die "systemd not found on this machine"

#  Run as the invoking user, not root: BlueZ access comes from the user's
#  session and the logs should not end up root-owned.
RUN_USER="${SUDO_USER:-root}"
RUN_GROUP="$(id -gn "$RUN_USER")"

#  Prefer a project venv if one exists, else the system interpreter.
if [ -x "$WORKDIR/.venv/bin/python" ]; then
  PYTHON="$WORKDIR/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi
[ -n "$PYTHON" ] || die "no python3 found"

"$PYTHON" -c "import jkbms" 2>/dev/null || die \
  "$PYTHON cannot import jkbms. Run this from the project directory, or install the package."

cat <<SUMMARY

About to install $UNIT_NAME:

  user         $RUN_USER:$RUN_GROUP
  workdir      $WORKDIR
  python       $PYTHON
  BLE address  $BLE_ADDR
  logs         $LOGDIR/pack-<date>.csv
  dashboard    http://<this-machine>:8765/   (--lan: no authentication)

SUMMARY

read -r -p "Proceed? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }

install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0755 "$LOGDIR"

sed -e "s|__USER__|$RUN_USER|g" \
    -e "s|__GROUP__|$RUN_GROUP|g" \
    -e "s|__WORKDIR__|$WORKDIR|g" \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__BLE_ADDR__|$BLE_ADDR|g" \
    -e "s|__LOGDIR__|$LOGDIR|g" \
    "$TEMPLATE" > "$TARGET"
chmod 0644 "$TARGET"

systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

sleep 2
echo
systemctl --no-pager --lines=15 status "$UNIT_NAME" || true
cat <<'NEXT'

Useful from here:
  journalctl -u jkbms-dashboard -f      follow the log
  systemctl restart jkbms-dashboard     restart (starts a new dated CSV)
  systemctl disable --now jkbms-dashboard   stop and remove from boot

If it restart-loops, the usual causes are the BLE address being wrong, the
radio being blocked, or something else already holding the one connection the
BMS allows. Run 'python3 -m jkbms.cli doctor' to check the first two.
NEXT
