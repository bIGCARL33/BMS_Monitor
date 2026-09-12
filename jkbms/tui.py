"""Full-screen terminal UI: live pack readout plus a command line.

One window, no browser, no second machine. The top of the screen is a live
readout that repaints several times a second; the bottom is the same command
prompt :mod:`jkbms.console` provides, so every write still goes through the
backup / confirm / verify path -- the TUI only changes how it is presented.

Layout is deliberately split from curses: :func:`render` is a pure function
from a state snapshot to a list of lines, so the layout can be tested without
a terminal, and curses does nothing but paint what it returns.

The cell display shows **deviation from the pack mean**, for the same reason
the web dashboard does: four groups within a millivolt of each other make four
identical full-length bars, which is worse than no chart. A centre line marks
the mean, bars grow left for below and right for above.
"""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .console import Console

__all__ = ["render", "ScreenState", "run_tui"]

#: Columns reserved for the deviation bars.
BAR_WIDTH = 34
#: Never scale tighter than this, or ADC noise on a balanced pack fills the bar.
MIN_SCALE_MV = 2.0


@dataclass
class ScreenState:
    """Everything the screen needs, gathered in one place for testability."""

    reading: object = None
    device: object = None
    switches: dict = field(default_factory=dict)
    log: Sequence = ()
    prompt: str = ""
    confirmed_layout: bool | None = None
    samples: int = 0
    status: str = ""
    now: str = ""


def _bar(dev_mv: float, scale_mv: float, width: int = BAR_WIDTH) -> str:
    """A centred bar: left of the midline is below the mean, right is above."""
    half = max(1, (width - 1) // 2)
    span = max(MIN_SCALE_MV, scale_mv)
    filled = min(half, int(round(abs(dev_mv) / span * half)))
    if dev_mv >= 0:
        return " " * half + "|" + "#" * filled + " " * (half - filled)
    return " " * (half - filled) + "#" * filled + "|" + " " * half


def _rule(width: int) -> str:
    return "-" * width


def render(state: ScreenState, width: int = 80, height: int = 24) -> list:
    """Build the whole screen as plain lines. Pure -- no curses, no I/O."""
    width = max(40, width)
    lines = []

    title = "JK BMS"
    if state.device is not None:
        title += f"  {getattr(state.device, 'model', '')}"
        version = getattr(state.device, "software_version", "")
        if version:
            title += f"  fw {version}"
    right = f"{state.status}  {state.now}".strip()
    pad = max(1, width - len(title) - len(right))
    lines.append(title + " " * pad + right)
    lines.append(_rule(width))

    r = state.reading
    if r is None:
        lines.append("  waiting for the first frame...")
    else:
        def num(value, fmt, dash="  --"):
            return dash if value is None else format(value, fmt)

        # Units are padded to two columns so the third field of both rows
        # starts at the same place; 'A' against 'mV' otherwise shifts it.
        lines.append(
            f"  PACK {num(r.pack_v, '9.3f')} {'V':<2}"
            f"  CURRENT {num(r.current_a, '+8.3f')} {'A':<2}"
            f"  POWER {num(r.power_w, '7.1f')} W")
        spread = None if r.cell_delta_v is None else r.cell_delta_v * 1000
        lines.append(
            f"  SOC  {num(r.soc_pct, '9.0f')} {'%':<2}"
            f"  SPREAD  {num(spread, '8.1f')} {'mV':<2}"
            f"  TEMP  {num(r.temp1_c, '.1f')}/{num(r.temp2_c, '.1f')} C"
            f"  MOS {num(r.temp_mos_c, '.1f')}")
        lines.append(_rule(width))

        mean = r.cell_avg_v
        if r.cells_v and mean is not None:
            devs = [(v - mean) * 1000 for v in r.cells_v]
            scale = max(MIN_SCALE_MV, max(abs(d) for d in devs))
            header = (f"  CELL BALANCE - deviation from mean {mean:.4f} V"
                      f"   (scale +/-{scale:.1f} mV)")
            lines.append(header[:width])
            for i, (volts, dev) in enumerate(zip(r.cells_v, devs), 1):
                lines.append(
                    f"  {i:<3}{volts:8.4f} {dev:+7.1f}  {_bar(dev, scale)}"[:width])
        else:
            lines.append("  no cell data")

    lines.append(_rule(width))
    if state.switches:
        parts = []
        for name in ("charge", "discharge", "balancer"):
            value = state.switches.get(name)
            shown = "?" if value is None else ("ON" if value else "off")
            parts.append(f"{name} {shown}")
        lines.append("  SWITCHES   " + "   ".join(parts))
    else:
        lines.append("  SWITCHES   unread   (type 'switches')")
    lines.append(_rule(width))

    # Whatever room is left goes to the command output, newest at the bottom.
    used = len(lines) + 2
    room = max(3, height - used)
    log = list(state.log)[-room:]
    for line in log:
        lines.append("  " + str(line)[: width - 2])
    for _ in range(room - len(log)):
        lines.append("")

    lines.append(_rule(width))
    lines.append("jkbms> " + state.prompt)

    # One clip at the end rather than at every append: a line longer than the
    # terminal wraps, which pushes everything below it down and turns the
    # readout into scrolling mush. Narrow terminals lose the right-hand
    # columns, which is the right thing to lose.
    return [line[:width] for line in lines]


class _Tui:
    def __init__(self, screen, console: Console) -> None:
        self.screen = screen
        self.console = console
        self.log: list = [
            "'help' for commands, 'quit' to leave.",
            "Writes take a backup, confirm, then verify by read-back.",
        ]
        self.prompt = ""
        self.switches: dict = {}
        self.device = None
        self._history: list = []
        self._history_pos = 0

    # -- output plumbing ---------------------------------------------------
    def emit(self, *parts) -> None:
        text = " ".join(str(p) for p in parts)
        for line in text.splitlines() or [""]:
            self.log.append(line)
        del self.log[:-400]
        self.paint()

    def ask(self, prompt: str) -> str:
        """Modal confirmation, drawn in place of the command line."""
        self.emit(prompt.strip())
        curses.echo()
        try:
            height, _ = self.screen.getmaxyx()
            self.screen.move(height - 1, 0)
            self.screen.clrtoeol()
            self.screen.addstr(height - 1, 0, "confirm> ")
            self.screen.refresh()
            self.screen.nodelay(False)
            answer = self.screen.getstr().decode("utf-8", "replace")
        finally:
            curses.noecho()
            self.screen.nodelay(True)
        self.emit(f"  (answered {answer!r})")
        return answer

    # -- drawing -----------------------------------------------------------
    def state(self) -> ScreenState:
        reading = self.console.latest
        result = getattr(self.console, "_verified", None)
        return ScreenState(
            reading=reading,
            device=self.device,
            switches=self.switches,
            log=self.log,
            prompt=self.prompt,
            confirmed_layout=result,
            status="live" if reading is not None else "waiting",
            now=datetime.now().strftime("%H:%M:%S"),
        )

    def paint(self) -> None:
        height, width = self.screen.getmaxyx()
        lines = render(self.state(), width - 1, height)
        self.screen.erase()
        for y, line in enumerate(lines[:height]):
            try:
                self.screen.addnstr(y, 0, line, width - 1)
            except curses.error:      # bottom-right cell always raises
                pass
        try:
            self.screen.move(height - 1, min(width - 1, 7 + len(self.prompt)))
        except curses.error:
            pass
        self.screen.refresh()

    # -- input -------------------------------------------------------------
    def submit(self) -> bool:
        line = self.prompt
        self.prompt = ""
        if line.strip():
            self._history.append(line)
            self._history_pos = len(self._history)
            self.emit(f"jkbms> {line}")
            verb = line.strip().split()[0].lower()
            if verb == "watch":
                self.emit("the readout above is already live; 'watch' is for the "
                          "plain console")
                return True
            try:
                keep_going = self.console.dispatch(line)
            except Exception as exc:
                self.emit(f"error: {type(exc).__name__}: {exc}")
                return True
            if verb == "switches":
                self.refresh_switches()
            return keep_going
        return True

    def refresh_switches(self) -> None:
        """Re-read the switch states into the header panel."""
        import struct

        from .control import SWITCH_REGISTERS
        raw = self.console.fetch_settings()
        if raw is None:
            return
        for reg in SWITCH_REGISTERS:
            off = reg.verify_offset
            if off is not None and off + 4 <= len(raw):
                (value,) = struct.unpack_from("<I", raw, off)
                self.switches[reg.name] = value

    def key(self, ch: int) -> bool:
        if ch in (curses.KEY_ENTER, 10, 13):
            return self.submit()
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            self.prompt = self.prompt[:-1]
        elif ch == curses.KEY_UP and self._history:
            self._history_pos = max(0, self._history_pos - 1)
            self.prompt = self._history[self._history_pos]
        elif ch == curses.KEY_DOWN and self._history:
            self._history_pos = min(len(self._history), self._history_pos + 1)
            self.prompt = ("" if self._history_pos >= len(self._history)
                           else self._history[self._history_pos])
        elif ch == curses.KEY_RESIZE:
            pass
        elif ch == 21:                      # Ctrl-U clears the line
            self.prompt = ""
        elif 32 <= ch < 127:
            self.prompt += chr(ch)
        return True


def run_tui(transport, profile, *, backup_dir=None, device=None) -> int:
    """Run the full-screen UI until the user quits."""

    def main(screen) -> int:
        curses.curs_set(1)
        screen.nodelay(True)
        screen.keypad(True)

        console = Console(transport, profile, backup_dir=backup_dir)
        ui = _Tui(screen, console)
        console.out = ui.emit
        console.confirm = ui.ask
        ui.device = device
        console.start_reader()

        last_paint = 0.0
        try:
            while True:
                ch = screen.getch()
                if ch != -1:
                    if not ui.key(ch):
                        break
                    ui.paint()
                now = time.monotonic()
                if now - last_paint > 0.25:
                    ui.paint()
                    last_paint = now
                if ch == -1:
                    time.sleep(0.03)
        except KeyboardInterrupt:
            pass
        finally:
            console.stop()
        return 0

    return curses.wrapper(main)
