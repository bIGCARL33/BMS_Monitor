"""Checksums used by JK BMS links.

Two different, unrelated schemes are in play:

* Modbus RTU CRC-16 wraps the *request* frames on the RS485/UART link when the
  board is set to protocol #001 ("JK BMS RS485 Modbus V1.0").
* A plain 8-bit additive checksum terminates the JK *response* frames
  (``55 AA EB 90 ...``) and the 20-byte BLE command frames.

The Modbus implementation here is verified against the poll frame captured from
JK's own Windows Monitor application -- see ``tests/test_crc.py``.
"""

from __future__ import annotations

__all__ = ["crc16_modbus", "append_modbus_crc", "check_modbus_crc", "sum8", "check_sum8"]


def crc16_modbus(data: bytes) -> int:
    """Standard Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_modbus_crc(payload: bytes) -> bytes:
    """Return ``payload`` with its CRC-16 appended low byte first (wire order)."""
    crc = crc16_modbus(payload)
    return payload + bytes((crc & 0xFF, crc >> 8))


def check_modbus_crc(frame: bytes) -> bool:
    """True when ``frame`` ends with a correct little-endian Modbus CRC."""
    if len(frame) < 3:
        return False
    return append_modbus_crc(frame[:-2]) == frame


def sum8(data: bytes) -> int:
    """8-bit additive checksum: the low byte of the sum of every byte."""
    return sum(data) & 0xFF


def check_sum8(frame: bytes) -> bool:
    """True when the final byte of ``frame`` is the sum8 of everything before it."""
    if not frame:
        return False
    return sum8(frame[:-1]) == frame[-1]
