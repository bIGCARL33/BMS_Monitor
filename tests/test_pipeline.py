"""End-to-end: bytes in, CSV out, via the replay transport and the CLI."""

from __future__ import annotations

import csv

from jkbms.cli import main
from jkbms.csvlog import CsvLogger
from jkbms.decode import decode
from jkbms.frames import Frame
from jkbms.profiles import JK02_32S
from jkbms.transports.replay import ReplayTransport, load_capture

from .synth import build_cell_info_frame

CELLS = [3.912, 3.908, 3.915, 3.910]


def capture_bytes(n: int = 3) -> bytes:
    return b"".join(
        build_cell_info_frame(JK02_32S, CELLS, counter=i, current_a=-2.5 - i)
        for i in range(n))


# ---------------------------------------------------------------- transport
def test_replay_transport_yields_every_frame():
    frames = list(ReplayTransport(capture_bytes(3)).frames())
    assert len(frames) == 3


def test_replay_survives_awkward_chunk_boundaries():
    frames = list(ReplayTransport(capture_bytes(3), chunk_size=7).frames())
    assert len(frames) == 3


def test_load_capture_accepts_hex_text(tmp_path):
    raw = capture_bytes(1)
    path = tmp_path / "cap.hex"
    path.write_text(" ".join(f"{b:02X}" for b in raw))
    assert load_capture(path) == raw


def test_load_capture_accepts_binary(tmp_path):
    raw = capture_bytes(1)
    path = tmp_path / "cap.bin"
    path.write_bytes(raw)
    assert load_capture(path) == raw


def test_cell_info_limit_counts_only_cell_info_frames():
    frames = list(ReplayTransport(capture_bytes(3)).cell_info_frames(limit=2))
    assert len(frames) == 2


# ---------------------------------------------------------------- csv
def test_csv_header_and_row(tmp_path):
    out = tmp_path / "log.csv"
    reading = decode(Frame(build_cell_info_frame(JK02_32S, CELLS)), JK02_32S)
    with CsvLogger(out) as logger:
        logger.write(reading)
        logger.write(reading)

    rows = list(csv.reader(out.open()))
    header, first = rows[0], rows[1]
    assert header[0] == "timestamp_iso"
    assert "cell01_v" in header and "cell04_v" in header
    assert "cell05_v" not in header
    assert len(rows) == 3
    assert len(first) == len(header)
    assert first[header.index("pack_v")].startswith("15.6")
    assert first[header.index("cell01_v")].startswith("3.912")
    assert first[header.index("profile")] == "jk02_32s"


def test_csv_absent_values_are_blank_not_zero(tmp_path):
    """Blanks are skipped by a spreadsheet; zeros would corrupt an average."""
    out = tmp_path / "log.csv"
    reading = decode(Frame(build_cell_info_frame(JK02_32S, CELLS)), JK02_32S)
    reading.soc_pct = None
    with CsvLogger(out) as logger:
        logger.write(reading)
    rows = list(csv.reader(out.open()))
    assert rows[1][rows[0].index("soc_pct")] == ""


def test_csv_row_width_is_stable_when_a_cell_drops_out(tmp_path):
    """The header is fixed at open time; a short reading must still line up."""
    out = tmp_path / "log.csv"
    full = decode(Frame(build_cell_info_frame(JK02_32S, CELLS)), JK02_32S)
    short = decode(Frame(build_cell_info_frame(JK02_32S, CELLS)), JK02_32S)
    short.cells_v = short.cells_v[:2]
    with CsvLogger(out) as logger:
        logger.write(full)
        logger.write(short)
    rows = list(csv.reader(out.open()))
    assert len({len(r) for r in rows}) == 1


def test_csv_resistance_columns_are_opt_in(tmp_path):
    out = tmp_path / "log.csv"
    reading = decode(Frame(build_cell_info_frame(JK02_32S, CELLS)), JK02_32S)
    with CsvLogger(out, include_resistance=True) as logger:
        logger.write(reading)
    header = next(csv.reader(out.open()))
    assert "cell01_ohm" in header


# ---------------------------------------------------------------- cli
def test_cli_probe_confirms_the_layout(tmp_path, capsys):
    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(2))
    assert main(["probe", "--replay", str(cap)]) == 0
    assert "CONFIRMED: jk02_32s" in capsys.readouterr().out


def test_cli_probe_discover_mode(tmp_path, capsys):
    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(1))
    main(["probe", "--replay", str(cap), "--discover"])
    assert "layout discovery" in capsys.readouterr().out


def test_cli_log_writes_rows(tmp_path):
    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(3))
    out = tmp_path / "log.csv"
    assert main(["log", "--replay", str(cap), "-o", str(out), "--quiet"]) == 0
    assert len(list(csv.reader(out.open()))) == 4      # header + 3


def test_cli_monitor_prints_readings(tmp_path, capsys):
    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(2))
    assert main(["monitor", "--replay", str(cap)]) == 0
    assert "SOC" in capsys.readouterr().out


def test_cli_errors_when_no_link_is_selected(capsys):
    assert main(["monitor"]) == 2
    assert "no link selected" in capsys.readouterr().err


def test_cli_auto_profile_picks_the_right_one(tmp_path, capsys):
    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(1))
    main(["monitor", "--replay", str(cap), "--profile", "auto"])
    assert "using profile: jk02_32s" in capsys.readouterr().err


def test_cli_warns_when_layout_is_unconfirmed(tmp_path, capsys):
    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(1))
    main(["monitor", "--replay", str(cap), "--profile", "jk02_24s"])
    assert "NOT confirmed" in capsys.readouterr().err


# ------------------------------------------------- error reporting
def test_missing_capture_reports_cleanly_not_a_traceback(tmp_path, capsys):
    """A missing capture is a predictable mistake; it must not raise."""
    assert main(["probe", "--replay", str(tmp_path / "nope.bin")]) == 2
    err = capsys.readouterr().err
    assert "no capture at" in err
    assert "Traceback" not in err


def test_empty_capture_is_explained(tmp_path, capsys):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert main(["probe", "--replay", str(empty)]) == 2
    assert "empty" in capsys.readouterr().err


def test_capture_directory_is_rejected(tmp_path, capsys):
    assert main(["probe", "--replay", str(tmp_path)]) == 2
    assert "is a directory" in capsys.readouterr().err


def test_permission_error_names_the_dialout_fix():
    """EACCES on the port looks exactly like a dead link -- say the real cause."""
    from jkbms.transports.serial_link import SerialTransport, _is_permission_error

    assert _is_permission_error(PermissionError(13, "Permission denied"))
    assert _is_permission_error(
        Exception("could not open port /dev/ttyUSB0: [Errno 13] Permission denied"))
    assert not _is_permission_error(FileNotFoundError("no such device"))

    msg = SerialTransport("/dev/ttyUSB0")._open_error(
        Exception("[Errno 13] Permission denied: '/dev/ttyUSB0'"))
    assert "usermod -aG dialout" in msg
    assert "not a wiring one" in msg


def test_non_permission_open_error_stays_terse():
    from jkbms.transports.serial_link import SerialTransport
    msg = SerialTransport("/dev/ttyUSB9")._open_error(FileNotFoundError("nope"))
    assert "dialout" not in msg
    assert "/dev/ttyUSB9" in msg


def test_bluetooth_off_is_explained_not_a_traceback():
    """A powered-off radio is the commonest BLE failure; name the fix."""
    from jkbms.transports.ble_link import _adapter_error

    hint = _adapter_error(Exception(
        "('No powered Bluetooth adapters found. Turn on Bluetooth and try "
        "again.', <BleakBluetoothNotAvailableReason.POWERED_OFF: 3>)"))
    assert hint is not None
    assert "rfkill unblock bluetooth" in hint
    assert "bluetoothctl power on" in hint


def test_dbus_failure_points_at_the_service():
    from jkbms.transports.ble_link import _adapter_error
    hint = _adapter_error(Exception("failed to connect to org.bluez over dbus"))
    assert hint and "bluetooth.service" in hint


def test_unrelated_ble_error_is_not_misdiagnosed():
    """Don't claim the radio is off when the real error is something else."""
    from jkbms.transports.ble_link import _adapter_error
    assert _adapter_error(Exception("device disconnected unexpectedly")) is None
