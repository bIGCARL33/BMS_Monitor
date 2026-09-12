"""Console behaviour, driven through a fake transport."""

from __future__ import annotations

import struct
import tempfile

from jkbms.console import Console
from jkbms.frames import Frame
from jkbms.profiles import JK02_32S

from .synth import build_cell_info_frame
from .test_control import settings_frame

CELLS = [3.442, 3.442, 3.472, 3.473]


class FakeTransport:
    """Enough of a transport to drive the console without hardware."""

    def __init__(self, settings=None, accept_writes=True, applies=True):
        self.settings = settings or settings_frame(**{"122": 0, "126": 1, "130": 0})
        self.sent = []
        self.accept_writes = accept_writes
        self.applies = applies
        self.requests = []

    def frames(self, **kw):
        yield Frame(build_cell_info_frame(JK02_32S, CELLS))
        yield Frame(self.settings)

    def request(self, command, value=0):
        self.requests.append(command)

    def send_raw(self, frame):
        if not self.accept_writes:
            raise RuntimeError("link dropped")
        self.sent.append(frame)
        if self.applies:
            raw = bytearray(self.settings)
            struct.pack_into("<I", raw, 122, 1)      # charge switch -> on
            raw[-1] = sum(raw[:-1]) & 0xFF
            self.settings = bytes(raw)


def make(transport, answers=("yes",), out=None, backup_dir=None):
    # Default to a throwaway directory. Three tests originally forgot to
    # override this and wrote real backup files into the repository, which
    # then got committed. Defaulting here means forgetting is harmless.
    lines = out if out is not None else []
    replies = iter(answers)
    console = Console(transport, JK02_32S,
                      backup_dir=backup_dir or tempfile.mkdtemp(prefix="jkbms-test-"),
                      out=lines.append,
                      confirm=lambda _p: next(replies, "no"))
    # Feed frames synchronously instead of starting the reader thread.
    for frame in transport.frames():
        if frame.type_byte == 0x01:
            console._settings_raw = frame.raw
            console._settings_seen.set()
        elif frame.is_cell_info:
            from jkbms.decode import decode
            console._latest = decode(frame, JK02_32S)
            console._latest_frame = frame
    console.fetch_settings = lambda timeout_s=8.0: transport.settings
    return console, lines


def test_status_reports_the_latest_reading():
    console, lines = make(FakeTransport())
    console.dispatch("status")
    assert "13.8" in "".join(lines) or "3.4" in "".join(lines)


def test_cells_shows_deviation_from_the_mean():
    console, lines = make(FakeTransport())
    console.dispatch("cells")
    text = "\n".join(lines)
    assert "dev mV" in text
    assert "spread" in text
    # Groups 3 and 4 sit above the mean, 1 and 2 below.
    assert "+" in text and "-" in text


def test_switches_lists_state_and_says_it_is_unconfirmed():
    console, lines = make(FakeTransport())
    console.dispatch("switches")
    text = "\n".join(lines)
    assert "charge" in text and "discharge" in text and "balancer" in text
    assert "unconfirmed" in text


def test_write_is_confirmed_verified_and_reported():
    transport = FakeTransport()
    console, lines = make(transport, answers=("yes",))
    console.dispatch("set charge on")
    text = "\n".join(lines)
    assert transport.sent, "nothing was sent"
    assert transport.sent[0][4] == 0x1D
    assert "About to write" in text
    assert "OK: verified" in text


def test_declining_the_prompt_sends_nothing():
    transport = FakeTransport()
    console, lines = make(transport, answers=("no",))
    console.dispatch("set charge on")
    assert transport.sent == []
    assert "cancelled" in "\n".join(lines)


def test_only_the_literal_word_yes_proceeds():
    transport = FakeTransport()
    console, _ = make(transport, answers=("y",))
    console.dispatch("set charge on")
    assert transport.sent == [], "'y' should not be enough to write to a BMS"


def test_write_that_does_not_take_effect_is_reported_as_a_problem():
    transport = FakeTransport(applies=False)
    console, lines = make(transport, answers=("yes",))
    console.dispatch("set charge on")
    text = "\n".join(lines)
    assert "PROBLEM" in text
    assert "NOT APPLIED" in text


def test_backup_is_taken_before_the_first_write(tmp_path):
    transport = FakeTransport()
    console, _ = make(transport, answers=("yes",))
    console.backup_dir = tmp_path
    console.dispatch("set charge on")
    saved = list(tmp_path.glob("settings-*.hex"))
    assert saved, "no settings backup was written before the write"
    assert "JK BMS settings frame" in saved[0].read_text()


def test_write_refused_when_settings_cannot_be_read(tmp_path):
    """No backup and no verification possible means no write."""
    transport = FakeTransport()
    console, lines = make(transport, answers=("yes",))
    console.backup_dir = tmp_path
    console.fetch_settings = lambda timeout_s=8.0: None
    console.dispatch("set charge on")
    assert transport.sent == []
    assert "REFUSING TO WRITE" in "\n".join(lines)


def test_bad_switch_value_is_rejected_before_any_prompt():
    transport = FakeTransport()
    console, lines = make(transport, answers=("yes",))
    console.dispatch("set charge maybe")
    assert transport.sent == []
    assert "must be on/off" in "\n".join(lines)


def test_unknown_setting_is_rejected():
    transport = FakeTransport()
    console, lines = make(transport, answers=("yes",))
    console.dispatch("set fusion_reactor on")
    assert transport.sent == []
    assert "unknown setting" in "\n".join(lines)


def test_help_and_quit():
    console, lines = make(FakeTransport())
    assert console.dispatch("help") is True
    assert "settings save" in "\n".join(lines)
    assert console.dispatch("quit") is False


def test_unknown_command_does_not_crash():
    console, lines = make(FakeTransport())
    assert console.dispatch("frobnicate") is True
    assert "unknown command" in "\n".join(lines)


def test_settings_save_writes_a_restorable_file(tmp_path):
    transport = FakeTransport()
    console, _ = make(transport)
    target = tmp_path / "cfg.hex"
    console.dispatch(f"settings save {target}")
    assert target.exists()
    from jkbms.transports.replay import load_capture
    assert load_capture(target) == transport.settings


def test_tests_never_write_backups_into_the_project(tmp_path, monkeypatch):
    """A test that forgets to redirect backup_dir must not touch the repo."""
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(tmp_path)          # even with a hostile cwd
    transport = FakeTransport()
    console, _ = make(transport)          # deliberately no backup_dir given
    console.dispatch("set charge on")
    assert transport.sent, "the write should still have happened"
    assert not (repo / "settings-backups").exists(), \
        "a test wrote backups into the project directory"


# ------------------------------------------------- non-interactive verdict
def test_last_write_ok_records_a_verified_write():
    """A script needs the verdict as an exit code, not as text to parse."""
    transport = FakeTransport()
    console, _ = make(transport, answers=("yes",))
    assert console.last_write_ok is None
    console.dispatch("set charge on")
    assert console.last_write_ok is True


def test_last_write_ok_records_a_failed_write():
    transport = FakeTransport(applies=False)
    console, _ = make(transport, answers=("yes",))
    console.dispatch("set charge on")
    assert console.last_write_ok is False


def test_last_write_ok_records_a_cancelled_write():
    transport = FakeTransport()
    console, _ = make(transport, answers=("no",))
    console.dispatch("set charge on")
    assert console.last_write_ok is False, "a cancelled write must not exit 0"


def test_last_write_ok_records_a_refused_write(tmp_path):
    transport = FakeTransport()
    console, _ = make(transport, answers=("yes",), backup_dir=tmp_path)
    console.fetch_settings = lambda timeout_s=8.0: None
    console.dispatch("set charge on")
    assert console.last_write_ok is False
