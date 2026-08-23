"""Decode the device-info record (frame type 0x03).

Unlike the cell-info layout, these offsets are **self-verifying**: the fields
are NUL-terminated ASCII, so a wrong offset yields mojibake rather than a
plausible-looking wrong number. If the model string reads ``JK_BD4A8S4P`` the
offset is right. That makes this the one part of the protocol that can be
trusted without cross-field checks -- and it is confirmed against a real
JK-BD4A8S4P (see ``tests/fixtures/device_info_bd4a8s4p.hex``).

Why it matters beyond curiosity: the hardware-version string decides whether
the wired UART path can work at all. JK's guidance is that PC connectivity
requires an "A" in that string, so reading it turns "why is the serial link
silent?" from a wiring hunt into a settled question.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .frames import Frame

__all__ = ["DeviceInfo", "decode_device_info"]


def _text(raw: bytes, offset: int, length: int) -> str:
    """Read a NUL-padded ASCII field."""
    if offset + length > len(raw):
        return ""
    return raw[offset : offset + length].split(b"\x00")[0].decode("ascii", "replace")


def _u32(raw: bytes, offset: int) -> int | None:
    if offset + 4 > len(raw):
        return None
    return struct.unpack_from("<I", raw, offset)[0]


@dataclass
class DeviceInfo:
    """Identity and firmware of the board."""

    model: str
    hardware_version: str
    software_version: str
    serial: str
    device_name: str
    #: Seconds the board has been powered, as reported. Units unconfirmed.
    uptime_raw: int | None = None
    power_on_count: int | None = None
    manufacture_date: str = ""

    @property
    def supports_pc_uart(self) -> bool:
        """Whether JK's guidance says the wired PC link can work on this board.

        JK document PC connectivity as requiring an "A" in the hardware version
        string. This is their rule of thumb, not something derived from the
        protocol -- treat a False here as a strong hint explaining a silent
        UART, not as proof the port is dead.
        """
        return "A" in self.hardware_version.upper()

    def report(self) -> str:
        lines = [
            f"model             {self.model}",
            f"hardware version  {self.hardware_version}",
            f"software version  {self.software_version}",
            f"serial            {self.serial}",
            f"device name       {self.device_name}",
        ]
        if self.manufacture_date:
            lines.append(f"manufacture date  {self.manufacture_date}")
        if self.power_on_count is not None:
            lines.append(f"power-on count    {self.power_on_count}")
        if self.uptime_raw is not None:
            lines.append(f"uptime (raw)      {self.uptime_raw}")

        lines.append("")
        if self.supports_pc_uart:
            lines.append(
                "The hardware version contains an 'A', so JK's guidance says the\n"
                "wired UART path should work on this board.")
        else:
            lines.append(
                f"NOTE: hardware version {self.hardware_version!r} contains no 'A'.\n"
                f"JK's guidance is that PC connectivity over UART requires one, so a\n"
                f"silent serial link is expected on this board and is not a wiring\n"
                f"fault. Use BLE, which needs no board-side configuration.")
        return "\n".join(lines)


#: Offsets confirmed against a real JK-BD4A8S4P running firmware 15.41.
#: ASCII content makes these self-checking -- see the module docstring.
_MODEL = (6, 16)
_HARDWARE = (22, 8)
_SOFTWARE = (30, 8)
_SERIAL = (46, 16)
_MANUFACTURE_DATE = (78, 8)
_DEVICE_NAME = (102, 16)
_UPTIME = 38
_POWER_ON_COUNT = 42


def decode_device_info(frame: Frame | bytes) -> DeviceInfo:
    """Decode a device-info record."""
    raw = frame.raw if isinstance(frame, Frame) else frame
    return DeviceInfo(
        model=_text(raw, *_MODEL),
        hardware_version=_text(raw, *_HARDWARE),
        software_version=_text(raw, *_SOFTWARE),
        serial=_text(raw, *_SERIAL),
        device_name=_text(raw, *_DEVICE_NAME),
        manufacture_date=_text(raw, *_MANUFACTURE_DATE),
        uptime_raw=_u32(raw, _UPTIME),
        power_on_count=_u32(raw, _POWER_ON_COUNT),
    )
