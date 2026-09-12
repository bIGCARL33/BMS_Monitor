"""Interactive console: monitor the pack and change its settings.

The session keeps one BLE link open -- the JK allows only one, and
reconnecting per command would be slow and fragile -- and reads frames in a
background thread so ``status`` is always current.

Writes go through :mod:`jkbms.control`, which will not send anything it cannot
verify afterwards. The console adds the operator-facing half of that contract:

* the settings frame is captured and **saved to disk before the first write**,
  because JK's software has no config export and there is otherwise no undo
* every write is shown in full -- register, value, literal bytes -- and
  confirmed before it goes out
* after each write the settings are re-read and diffed, and anything other
  than the intended single change is reported loudly

Type ``help`` at the prompt.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

#: Firmware 15.41 pushes its settings frame exactly once, unprompted, right
#: after a BLE connect -- an explicit re-request on an already-open link gets
#: no reply (confirmed: 4 requests over 32s, 0 replies). The only way to get a
#: fresh settings frame later in a session is to reconnect. This is the pause
#: given the link to settle before reopening it -- shorter than start.sh's own
#: post-disconnect delay only because there is no BlueZ-level cleanup to wait
#: for here, just the peripheral itself.
RECONNECT_SETTLE_S = 2.0

from .control import (SWITCH_REGISTERS, THRESHOLD_REGISTERS, WriteError,
                      WritePlan, get_register)
from .decode import decode
from .settings import (SETTINGS_FRAME_TYPE, describe_words, diff_frames,
                       render_diff)

__all__ = ["Console", "run_console"]

BANNER = """\
JK BMS console.  'help' for commands, 'quit' to leave.
Writes are confirmed, verified by read-back, and preceded by a settings backup.
"""

HELP = """\
  status              latest reading (cells, pack, current, temps)
  cells               per-cell voltages and deviation from the mean
  settings            decode the settings frame word by word
  settings save FILE  write the raw settings frame to a file (your only undo)
  settings diff FILE  compare current settings against a saved file
  switches            state of the charge / discharge / balancer switches

  set NAME VALUE      change a setting; confirmed and verified
                      switches:   charge, discharge, balancer   (on|off)
                      thresholds: cell_ovp, cell_uvp            (volts)

  watch [N]           print a reading a second for N seconds (default 30)
  raw                 hexdump the latest cell-info frame
  help / quit
"""


class Console:
    """Command interpreter over an open transport."""

    def __init__(self, transport, profile, *, backup_dir: Path | None = None,
                 out=print, confirm=input) -> None:
        self.transport = transport
        self.profile = profile
        self.out = out
        self.confirm = confirm
        self.backup_dir = Path(backup_dir or "settings-backups")

        self._lock = threading.Lock()
        self._latest = None
        self._latest_frame = None
        self._settings_raw = None
        self._settings_seen = threading.Event()
        self._stop = threading.Event()
        #: Set while fetch_settings() is reconnecting to force a fresh
        #: settings push. Tells the background reader that the exception
        #: this causes is expected, not a dead link.
        self._reconnecting = threading.Event()
        self._backed_up_this_session = False
        #: Verdict of the most recent write: True verified, False problem,
        #: None nothing attempted. Lets a non-interactive caller exit non-zero
        #: on an unverified write instead of parsing the text.
        self.last_write_ok = None

    # -- background reader -------------------------------------------------
    def start_reader(self) -> None:
        def pump():
            while not self._stop.is_set():
                try:
                    for frame in self.transport.frames():
                        if self._stop.is_set():
                            return
                        if frame.type_byte == SETTINGS_FRAME_TYPE:
                            with self._lock:
                                self._settings_raw = frame.raw
                            self._settings_seen.set()
                        elif frame.is_cell_info:
                            with self._lock:
                                self._latest = decode(frame, self.profile)
                                self._latest_frame = frame
                except Exception as exc:  # pragma: no cover - hardware dependent
                    if self._stop.is_set():
                        return
                    if not self._reconnecting.is_set():
                        self.out(f"\n[reader stopped: {type(exc).__name__}: {exc}]")
                        return
                    # fetch_settings() is mid-reconnect and tore down the
                    # transport out from under us -- that's the expected
                    # cause of this exception. Poll rather than a single
                    # wait(): the flag is already set for the duration of
                    # the reconnect, so a wait() on it would return
                    # immediately instead of pausing for it to clear.
                    waited = 0.0
                    while self._reconnecting.is_set() and waited < 60.0:
                        time.sleep(0.25)
                        waited += 0.25
                    # loop back to the top and call frames() again, fresh
        threading.Thread(target=pump, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def latest(self):
        with self._lock:
            return self._latest

    # -- settings ----------------------------------------------------------
    def fetch_settings(self, timeout_s: float = 30.0) -> bytes | None:
        """Ask for a fresh settings frame and wait for it.

        On firmware that actually answers 0x95 on an open link, the live
        request below is all this needs. Firmware 15.41 does not: confirmed
        by sending 0x95 four times over 32s on one connection with zero
        replies, while a fresh connect gets exactly one settings frame,
        unprompted, every time. So when the live request times out, this
        falls back to forcing that connect-time push by reconnecting --
        see CMD_SETTINGS in transports/ble_link.py for how that was found.
        """
        from .transports.ble_link import CMD_SETTINGS

        self._settings_seen.clear()
        request = getattr(self.transport, "request", None)
        if request is None:
            self.out("this transport cannot request settings")
            return None
        try:
            request(CMD_SETTINGS)
        except Exception as exc:
            self.out(f"could not request settings: {exc}")
            return None

        live_timeout = min(8.0, timeout_s)
        if self._settings_seen.wait(live_timeout):
            with self._lock:
                return self._settings_raw

        self.out(f"no reply to 0x95 within {live_timeout:.0f}s -- "
                 "reconnecting to force this firmware's connect-time "
                 "settings push instead.")
        return self._fetch_settings_via_reconnect(timeout_s)

    def _fetch_settings_via_reconnect(self, timeout_s: float) -> bytes | None:
        """Force a fresh settings push by disconnecting and reconnecting.

        Only entered after a live 0x95 request already timed out. Marks
        ``_reconnecting`` so the background reader (mid-iteration on the
        transport we are about to tear down) treats the resulting exception
        as expected and waits, instead of reporting a dead link.
        """
        self._reconnecting.set()
        try:
            try:
                self.transport.close()
            except Exception:
                pass
            time.sleep(RECONNECT_SETTLE_S)
            try:
                self.transport.open()
            except Exception as exc:
                self.out(f"reconnect failed: {exc}")
                return None

            self._settings_seen.clear()
            try:
                for frame in self.transport.frames(timeout_s=timeout_s):
                    if frame.type_byte == SETTINGS_FRAME_TYPE:
                        with self._lock:
                            self._settings_raw = frame.raw
                        self._settings_seen.set()
                        break
                    elif frame.is_cell_info:
                        with self._lock:
                            self._latest = decode(frame, self.profile)
                            self._latest_frame = frame
            except Exception as exc:
                self.out(f"reconnected but reading it back failed: {exc}")
                return None

            if not self._settings_seen.is_set():
                self.out(f"reconnected but no settings frame arrived within "
                         f"{timeout_s:.0f}s either -- this firmware may not "
                         "push one on every connect, or something else is "
                         "wrong.")
                return None
            self.out("reconnected; got a fresh settings frame.")
            with self._lock:
                return self._settings_raw
        finally:
            self._reconnecting.clear()

    def save_settings(self, path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (f"# JK BMS settings frame\n"
                  f"# saved {datetime.now().isoformat(timespec='seconds')}\n"
                  f"# This is the config export JK's software does not provide.\n")
        hexed = "\n".join(" ".join(f"{b:02X}" for b in raw[i:i + 16])
                          for i in range(0, len(raw), 16))
        path.write_text(header + hexed + "\n")
        self.out(f"saved {len(raw)} bytes to {path}")

    def ensure_backup(self) -> tuple[bool, bytes | None]:
        """Take a settings backup before the first write of the session.

        Returns ``(ok, raw)``. ``raw`` carries the settings frame this call
        just fetched, so a caller that also needs an immediate "before"
        snapshot for a read-back diff can reuse it instead of triggering a
        second reconnect for data that has not had time to change; it is
        ``None`` when the backup already happened earlier in the session.
        """
        if self._backed_up_this_session:
            return True, None
        raw = self.fetch_settings()
        if raw is None:
            self.out("REFUSING TO WRITE: could not read the settings frame, so "
                     "no backup exists and no write could be verified.")
            return False, None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.save_settings(self.backup_dir / f"settings-{stamp}.hex", raw)
        self._backed_up_this_session = True
        return True, raw

    # -- commands ----------------------------------------------------------
    def cmd_status(self, _args) -> None:
        reading = self.latest
        if reading is None:
            self.out("no reading yet")
            return
        self.out(str(reading))

    def cmd_cells(self, _args) -> None:
        reading = self.latest
        if reading is None or not reading.cells_v:
            self.out("no reading yet")
            return
        mean = reading.cell_avg_v
        self.out(f"{'group':<8}{'volts':>9}{'dev mV':>9}")
        for i, v in enumerate(reading.cells_v, 1):
            self.out(f"{i:<8}{v:>9.4f}{(v - mean) * 1000:>+9.1f}")
        self.out(f"mean {mean:.4f} V   spread {reading.cell_delta_v * 1000:.1f} mV"
                 f"   sum {reading.cell_sum_v:.3f} V")

    def cmd_raw(self, _args) -> None:
        with self._lock:
            frame = self._latest_frame
        self.out(frame.hexdump() if frame else "no frame yet")

    def cmd_settings(self, args) -> None:
        if args and args[0] == "save":
            raw = self.fetch_settings()
            if raw is None:
                return
            target = Path(args[1]) if len(args) > 1 else (
                self.backup_dir / f"settings-{datetime.now():%Y%m%d-%H%M%S}.hex")
            self.save_settings(target, raw)
            return

        if args and args[0] == "diff":
            if len(args) < 2:
                self.out("usage: settings diff FILE")
                return
            from .transports.replay import load_capture
            saved = load_capture(Path(args[1]))
            raw = self.fetch_settings()
            if raw is None:
                return
            self.out(render_diff(diff_frames(saved, raw)))
            return

        raw = self.fetch_settings()
        if raw is not None:
            self.out(describe_words(raw, only_interesting=True))

    def cmd_switches(self, _args) -> None:
        raw = self.fetch_settings()
        if raw is None:
            return
        import struct
        for reg in SWITCH_REGISTERS:
            off = reg.verify_offset
            if off is None or off + 4 > len(raw):
                self.out(f"  {reg.name:<10} unknown")
                continue
            (value,) = struct.unpack_from("<I", raw, off)
            state = "ON" if value == 1 else ("off" if value == 0 else f"?({value})")
            self.out(f"  {reg.name:<10} {state:<6} (offset {off}, raw {value})")
        self.out("\nOffsets are unconfirmed on this firmware. Flip one with "
                 "'set', and\nthe read-back diff will tell you whether the "
                 "mapping is right.")

    def cmd_set(self, args) -> None:
        if len(args) < 2:
            self.out("usage: set NAME VALUE   (see 'help')")
            return
        name, raw_value = args[0], args[1]

        try:
            register = get_register(name)
        except WriteError as exc:
            self.out(str(exc))
            return

        if register in SWITCH_REGISTERS:
            lowered = raw_value.lower()
            if lowered in ("on", "1", "true", "yes"):
                value = 1
            elif lowered in ("off", "0", "false", "no"):
                value = 0
            else:
                self.out(f"switch value must be on/off, got {raw_value!r}")
                return
        else:
            try:
                value = float(raw_value)
            except ValueError:
                self.out(f"expected a number, got {raw_value!r}")
                return

        try:
            plan = WritePlan(register, value,
                             i_understand_this_changes_protection=(
                                 register in THRESHOLD_REGISTERS))
        except WriteError as exc:
            self.out(str(exc))
            return

        self.last_write_ok = None
        backup_ok, fresh_backup = self.ensure_backup()
        if not backup_ok:
            self.last_write_ok = False
            return

        self.out("\nAbout to write:")
        self.out(plan.describe())
        if register.is_protection:
            self.out("\n  This changes when the pack disconnects itself. Getting "
                     "it wrong\n  is a safety problem, not an inconvenience.")
        answer = self.confirm("\nProceed? type 'yes' to send: ")
        if answer.strip().lower() != "yes":
            self.out("cancelled, nothing sent")
            self.last_write_ok = False
            return

        # Reuse the backup's read if it was just taken this call -- nothing
        # has changed since (we're still between backup and the write below),
        # and each fetch on this firmware costs a reconnect. Only fetch again
        # if the backup happened earlier in the session.
        before = fresh_backup if fresh_backup is not None else self.fetch_settings()
        if before is None:
            self.out("could not read settings before the write; aborting")
            self.last_write_ok = False
            return

        send = getattr(self.transport, "send_raw", None)
        if send is None:
            self.out("this transport cannot send commands")
            self.last_write_ok = False
            return
        try:
            send(plan.command())
        except Exception as exc:
            self.out(f"send failed: {exc}")
            self.last_write_ok = False
            return
        self.out("sent; re-reading settings to verify...")
        time.sleep(1.0)

        after = self.fetch_settings()
        if after is None:
            self.out("WROTE BUT COULD NOT VERIFY -- re-read failed. Check "
                     "'switches' manually before trusting the state.")
            self.last_write_ok = False
            return
        ok, message = plan.check_result(before, after)
        self.last_write_ok = ok
        self.out(("OK: " if ok else "PROBLEM: ") + message)

    def cmd_watch(self, args) -> None:
        seconds = 30.0
        if args:
            try:
                seconds = float(args[0])
            except ValueError:
                pass
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                reading = self.latest
                if reading is not None:
                    self.out(f"{datetime.now():%H:%M:%S}  {reading}")
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.out("(stopped)")

    COMMANDS = {
        "status": cmd_status, "cells": cmd_cells, "raw": cmd_raw,
        "settings": cmd_settings, "switches": cmd_switches,
        "set": cmd_set, "watch": cmd_watch,
    }

    def dispatch(self, line: str) -> bool:
        """Run one command line. Returns False to exit."""
        parts = line.strip().split()
        if not parts:
            return True
        verb, args = parts[0].lower(), parts[1:]
        if verb in ("quit", "exit", "q"):
            return False
        if verb in ("help", "?"):
            self.out(HELP)
            return True
        handler = self.COMMANDS.get(verb)
        if handler is None:
            self.out(f"unknown command {verb!r}; 'help' for the list")
            return True
        handler(self, args)
        return True


def run_console(transport, profile, *, backup_dir=None) -> int:
    console = Console(transport, profile, backup_dir=backup_dir)
    console.start_reader()
    print(BANNER)
    # Give the reader a moment so the first 'status' is not empty.
    for _ in range(40):
        if console.latest is not None:
            break
        time.sleep(0.25)
    if console.latest is None:
        print("(no cell data yet -- the link may still be settling)")
    try:
        while True:
            try:
                line = input("jkbms> ")
            except EOFError:
                break
            if not console.dispatch(line):
                break
    except KeyboardInterrupt:
        print()
    finally:
        console.stop()
    return 0
