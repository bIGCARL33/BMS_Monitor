"""Device-info decoding, checked against a real JK-BD4A8S4P frame.

This is the one test file backed by actual hardware output rather than
synthetic data, so it validates offsets and not merely the decoder.
"""

from __future__ import annotations

from pathlib import Path

from jkbms.crc import check_sum8
from jkbms.deviceinfo import DeviceInfo, decode_device_info
from jkbms.frames import Frame, FrameAssembler
from jkbms.transports.replay import load_capture

FIXTURE = Path(__file__).parent / "fixtures" / "device_info_bd4a8s4p.hex"


def real_frame() -> bytes:
    text = "".join(line for line in FIXTURE.read_text().splitlines(keepends=True)
                   if not line.startswith("#"))
    return bytes.fromhex("".join(text.split()))


def test_fixture_is_a_valid_300_byte_frame():
    raw = real_frame()
    assert len(raw) == 300
    assert raw[:4] == b"\x55\xAA\xEB\x90"
    assert raw[4] == 0x03
    assert check_sum8(raw)


def test_assembler_accepts_the_real_frame():
    """Framing written against docs must handle a frame from actual hardware."""
    frames = FrameAssembler().feed(real_frame())
    assert len(frames) == 1
    assert frames[0].type_name == "DEVICE_INFO"


def test_decodes_the_real_board_identity():
    info = decode_device_info(Frame(real_frame()))
    assert info.model == "JK_BD4A8S4P"
    assert info.hardware_version == "15H"
    assert info.software_version == "15.41"
    assert info.serial == "50913310848"
    assert info.device_name == "JK-BMS"


def test_hardware_version_without_an_A_flags_no_pc_uart():
    """The observed board has no 'A', which explains its silent UART."""
    info = decode_device_info(Frame(real_frame()))
    assert not info.supports_pc_uart
    report = info.report()
    assert "contains no 'A'" in report
    assert "not a wiring" in report


def test_hardware_version_with_an_A_reports_uart_supported():
    info = DeviceInfo("JK_BD4A8S4P", "15A", "15.41", "1", "JK-BMS")
    assert info.supports_pc_uart
    assert "should work" in info.report()


def test_serial_matches_the_advertised_ble_name():
    """The board advertises under its serial, which is why it looked unnamed."""
    info = decode_device_info(Frame(real_frame()))
    assert info.serial == "50913310848"


def test_load_capture_reads_the_hex_fixture_with_comments_stripped():
    """Captures kept as commented hex must still load."""
    raw = load_capture(FIXTURE)
    assert raw[:4] == b"\x55\xAA\xEB\x90"
    assert len(raw) == 300


def test_cli_deviceinfo_from_a_capture(capsys):
    from jkbms.cli import main
    assert main(["deviceinfo", "--replay", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "JK_BD4A8S4P" in out
    assert "15.41" in out
    assert "contains no 'A'" in out


def test_cli_deviceinfo_without_a_device_info_frame(tmp_path, capsys):
    from jkbms.cli import main
    from tests.test_pipeline import capture_bytes      # cell-info only
    cap = tmp_path / "cells.bin"
    cap.write_bytes(capture_bytes(1))
    assert main(["deviceinfo", "--replay", str(cap)]) == 1
    assert "no device-info frame" in capsys.readouterr().err
