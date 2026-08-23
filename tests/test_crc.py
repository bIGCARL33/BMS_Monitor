"""CRC tests, anchored on bytes actually observed on the wire."""

from __future__ import annotations

from jkbms.crc import (append_modbus_crc, check_modbus_crc, check_sum8,
                       crc16_modbus, sum8)
from jkbms.transports.serial_link import POLL_FRAME_REFERENCE, build_poll_frame


def test_observed_poll_frame_has_valid_modbus_crc():
    """The frame captured from JK's Monitor is well-formed Modbus RTU.

    This is the one piece of protocol ground truth available without hardware,
    so it is worth pinning: it is what justifies generating poll frames rather
    than replaying a magic constant.
    """
    assert check_modbus_crc(POLL_FRAME_REFERENCE)


def test_build_poll_frame_reproduces_the_observed_bytes():
    assert build_poll_frame(device_address=1) == POLL_FRAME_REFERENCE


def test_poll_frame_decodes_as_write_multiple_registers():
    frame = build_poll_frame(1)
    assert frame[0] == 0x01              # slave address
    assert frame[1] == 0x10              # function: write multiple registers
    assert frame[2:4] == b"\x16\x20"     # register 0x1620
    assert frame[4:6] == b"\x00\x01"     # one register
    assert frame[6] == 0x02              # two data bytes


def test_poll_frame_honours_device_address():
    frame = build_poll_frame(device_address=2)
    assert frame[0] == 0x02
    assert check_modbus_crc(frame)
    assert frame != POLL_FRAME_REFERENCE


def test_crc16_known_vector():
    # Classic Modbus test vector.
    assert crc16_modbus(b"\x01\x04\x02\xFF\xFF") == 0x80B8


def test_append_and_check_roundtrip():
    payload = b"\x11\x03\x00\x6B\x00\x03"
    assert check_modbus_crc(append_modbus_crc(payload))


def test_check_modbus_crc_rejects_corruption():
    frame = bytearray(build_poll_frame(1))
    frame[3] ^= 0xFF
    assert not check_modbus_crc(bytes(frame))


def test_sum8_wraps_at_one_byte():
    assert sum8(b"\xFF\x02") == 0x01
    assert check_sum8(b"\xFF\x02\x01")
    assert not check_sum8(b"\xFF\x02\x02")


def test_check_sum8_on_empty_input():
    assert not check_sum8(b"")


def test_legacy_frame_is_available_as_a_diagnostic():
    """Kept so 'this board ignores 4E 57' can be tested, not just believed."""
    from jkbms.transports.serial_link import LEGACY_POLL_FRAME, SerialTransport
    assert LEGACY_POLL_FRAME[:2] == b"\x4E\x57"
    # It is not Modbus, so it must not be confused with the working poll frame.
    assert not check_modbus_crc(LEGACY_POLL_FRAME)
    assert SerialTransport("/dev/null", legacy=True).poll_frame == LEGACY_POLL_FRAME
    assert SerialTransport("/dev/null").poll_frame == POLL_FRAME_REFERENCE
